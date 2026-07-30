#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mapa interactivo de luces nocturnas de Paraguay
================================================

Aplicación Flask + Leaflet preparada para usar directamente los productos creados
por ``luces_nocturnas_paraguay.py``:

- vectores/areas_estudio_paraguay.gpkg
- tablas/metricas_anuales_todas_las_zonas.csv
- tablas/ranking_crecimiento.csv
- cache/rasters/paraguay/viirs_YYYY.tif
- cache/rasters/paraguay/cambio_abs_YYYY_YYYY.tif
- cache/rasters/paraguay/cambio_pct_YYYY_YYYY.tif

No consulta Google Earth Engine. La aplicación solo visualiza los resultados ya
calculados, por lo que es apropiada para subir a Render, Railway, Fly.io o un VPS.

Ejecución local:

    pip install -r requirements.txt
    export DATA_DIR=/ruta/Resultados_luces_nocturnas_Paraguay
    python app.py

En Windows PowerShell:

    $env:DATA_DIR="C:\\ruta\\Resultados_luces_nocturnas_Paraguay"
    python app.py

Producción:

    gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 180
"""

from __future__ import annotations

import gzip
import io
import json
import math
import os
import re
import threading
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import geopandas as gpd
import mercantile
import numpy as np
import pandas as pd
import rasterio
from affine import Affine
from flask import Flask, Response, abort, jsonify, render_template_string, request
from PIL import Image
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.transform import from_bounds, rowcol, xy
from rasterio.warp import reproject
from shapely.geometry import box, mapping
from shapely.ops import transform as shapely_transform

try:
    from pyogrio import list_layers as pyogrio_list_layers
except Exception:  # pragma: no cover
    pyogrio_list_layers = None


APP_TITLE = "Explorador de luces nocturnas de Paraguay"
APP_BUILD = "2026-07-30-R5-DATOS-COMPRIMIDOS"
DEFAULT_DATA_DIR = "Resultados_luces_nocturnas_Paraguay"
WEB_MERCATOR = "EPSG:3857"
WGS84 = "EPSG:4326"
TILE_SIZE = 256
LOCALITY_LIMIT = int(os.environ.get("LOCALITY_LIMIT", "8000"))
MAX_HOTSPOT_POINTS = int(os.environ.get("MAX_HOTSPOT_POINTS", "3000"))

# Campos que se exponen en tooltips, tablas y rankings.
METRIC_LABELS: dict[str, str] = {
    "cagr_pct_anual": "Crecimiento anual promedio (CAGR, %)",
    "cambio_absoluto": "Cambio absoluto de radiancia",
    "cambio_pct": "Cambio porcentual (%)",
    "radiancia_inicial": "Radiancia inicial",
    "radiancia_final": "Radiancia final",
    "puntaje_crecimiento": "Puntaje de crecimiento",
    "pendiente_theil_sen_anual": "Tendencia anual robusta",
    "area_nueva_1_0_km2_final": "Nueva área iluminada >1 nW (km²)",
    "ranking_nivel": "Ranking dentro del nivel",
}

TOOLTIP_FIELDS = [
    "radiancia_inicial",
    "radiancia_final",
    "cambio_absoluto",
    "cambio_pct",
    "cagr_pct_anual",
    "puntaje_crecimiento",
    "ranking_nivel",
    "cobertura_min_pct",
    "inicio_crecimiento_descriptivo",
    "crecimiento_persistente_2_3_anios",
    "advertencia",
]

VECTOR_LAYER_DEFS: dict[str, dict[str, Any]] = {
    "departamentos": {
        "gpkg": "departamentos",
        "label": "Departamentos",
        "kind": "polygon",
        "simplify": 0.0020,
    },
    "distritos": {
        "gpkg": "distritos_municipios",
        "label": "Distritos / municipios",
        "kind": "polygon",
        "simplify": 0.0008,
    },
    "localidades": {
        "gpkg": "localidades_geonames",
        "label": "Ciudades, pueblos y localidades",
        "kind": "point",
        "simplify": 0.0,
    },
    "localidades_ine": {
        "gpkg": "localidades_ine",
        "label": "Localidades INE",
        "kind": "point",
        "simplify": 0.0,
    },
    "hex_nacional": {
        "gpkg": "hex_nacional",
        "label": "Malla nacional",
        "kind": "polygon",
        "simplify": 0.0005,
    },
}


# ---------------------------------------------------------------------------
# Utilidades generales
# ---------------------------------------------------------------------------


def texto_normalizado(valor: Any) -> str:
    texto = "" if valor is None else str(valor)
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "_", texto.lower()).strip("_")


def clean_scalar(value: Any) -> Any:
    """Convierte valores numpy/pandas a JSON estricto y elimina NaN/Inf."""
    if value is None:
        return None
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        value = float(value)
        return value if math.isfinite(value) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    try:
        missing = pd.isna(value)
        if isinstance(missing, (bool, np.bool_)) and bool(missing):
            return None
    except Exception:
        pass
    return str(value) if not isinstance(value, (str, dict, list)) else value


def clean_record(record: dict[str, Any]) -> dict[str, Any]:
    return {str(k): clean_scalar(v) for k, v in record.items()}


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def find_first(root: Path, filename: str) -> Path | None:
    direct = root / filename
    if direct.exists():
        return direct
    matches = sorted(root.rglob(filename), key=lambda p: (len(p.parts), str(p)))
    return matches[0] if matches else None


def locate_data_dir() -> Path:
    """Localiza la carpeta generada por el script principal."""
    script_dir = Path(__file__).resolve().parent
    candidates: list[Path] = []
    env_path = os.environ.get("DATA_DIR")
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.extend(
        [
            script_dir / DEFAULT_DATA_DIR,
            script_dir.parent / DEFAULT_DATA_DIR,
            Path.cwd() / DEFAULT_DATA_DIR,
            Path.cwd(),
        ]
    )
    checked: set[str] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if str(candidate) in checked or not candidate.exists():
            continue
        checked.add(str(candidate))
        if find_first(candidate, "areas_estudio_paraguay.gpkg"):
            return candidate
    searched = "\n".join(f"  - {p}" for p in candidates)
    raise RuntimeError(
        "No se encontró 'areas_estudio_paraguay.gpkg'. Defina DATA_DIR con la "
        "carpeta de resultados del análisis. Rutas revisadas:\n" + searched
    )


def robust_percentile(values: np.ndarray, percentile: float, fallback: float) -> float:
    values = np.asarray(values, dtype="float64")
    values = values[np.isfinite(values)]
    if values.size == 0:
        return fallback
    result = float(np.nanpercentile(values, percentile))
    return result if math.isfinite(result) else fallback


def geometry_to_wgs84(geometry, source_crs):
    if source_crs is None or str(source_crs).upper() in {WGS84, "OGC:CRS84"}:
        return geometry
    transformer = Transformer.from_crs(source_crs, WGS84, always_xy=True)
    return shapely_transform(transformer.transform, geometry)


# ---------------------------------------------------------------------------
# Almacén de datos
# ---------------------------------------------------------------------------


class NightLightsStore:
    def __init__(self, root: Path):
        self.root = root
        self.gpkg = find_first(root, "areas_estudio_paraguay.gpkg")
        self.ranking_path = (
            find_first(root, "ranking_crecimiento.csv.gz")
            or find_first(root, "ranking_crecimiento.csv")
        )
        self.metrics_path = (
            find_first(root, "metricas_anuales_todas_las_zonas.csv.gz")
            or find_first(root, "metricas_anuales_todas_las_zonas.csv")
        )
        if self.gpkg is None or self.ranking_path is None:
            raise RuntimeError(
                "La carpeta DATA_DIR debe contener el GeoPackage y la tabla "
                "ranking_crecimiento.csv generados por el análisis principal."
            )

        ranking_columns = {
            "id_zona", "nivel", "nombre", "departamento",
            *TOOLTIP_FIELDS, *METRIC_LABELS.keys(),
        }
        metrics_columns = {
            "id_zona", "anio", "anio_parcial", "meses_disponibles",
            "radiancia_media", "radiancia_mediana", "radiancia_p90",
            "radiancia_p95", "radiancia_integrada_km2",
            "area_iluminada_1_0_km2", "area_nueva_1_0_km2",
            "cobertura_valida_pct",
        }
        self.ranking = pd.read_csv(
            self.ranking_path,
            encoding="utf-8-sig",
            low_memory=False,
            compression="infer",
            usecols=lambda column: column in ranking_columns,
        )
        self.metrics = (
            pd.read_csv(
                self.metrics_path,
                encoding="utf-8-sig",
                low_memory=False,
                compression="infer",
                usecols=lambda column: column in metrics_columns,
            )
            if self.metrics_path
            else pd.DataFrame()
        )
        self.ranking["id_zona"] = self.ranking["id_zona"].astype(str)
        for column in self.ranking.select_dtypes(include=["float64"]).columns:
            self.ranking[column] = pd.to_numeric(self.ranking[column], downcast="float")
        if not self.metrics.empty:
            self.metrics["id_zona"] = self.metrics["id_zona"].astype("category")
            for column in self.metrics.select_dtypes(include=["float64"]).columns:
                self.metrics[column] = pd.to_numeric(self.metrics[column], downcast="float")
            for column in self.metrics.select_dtypes(include=["int64"]).columns:
                self.metrics[column] = pd.to_numeric(self.metrics[column], downcast="integer")

        self.available_gpkg_layers = self._list_gpkg_layers()
        self._vector_cache: dict[str, gpd.GeoDataFrame] = {}
        self._geojson_bytes_cache: dict[tuple[str, str, bool], bytes] = {}
        self._department_geometry_cache: dict[str, Any] = {}
        self._search_index: pd.DataFrame | None = None
        self._lock = threading.RLock()

        self.rasters = self._discover_rasters()
        self.raster_stats = {key: self._compute_raster_style(meta) for key, meta in self.rasters.items()}
        self.bounds = self._derive_bounds()
        self.default_raster = self._choose_default_raster()
        self.default_metric = self._choose_default_metric()

    def _list_gpkg_layers(self) -> set[str]:
        if pyogrio_list_layers is not None:
            try:
                rows = pyogrio_list_layers(self.gpkg)
                return {str(row[0]) for row in rows}
            except Exception:
                pass
        # Respaldo: intenta las capas conocidas.
        available = set()
        for definition in VECTOR_LAYER_DEFS.values():
            name = definition["gpkg"]
            try:
                gpd.read_file(self.gpkg, layer=name, rows=1, engine="pyogrio")
                available.add(name)
            except Exception:
                continue
        return available

    def _discover_rasters(self) -> dict[str, dict[str, Any]]:
        candidates: list[Path] = []
        expected = self.root / "cache" / "rasters" / "paraguay"
        if expected.exists():
            candidates = sorted(expected.glob("*.tif"))
        if not candidates:
            # Se prefieren los rasters nacionales ubicados en una carpeta llamada paraguay.
            national_dirs = [
                p for p in self.root.rglob("paraguay")
                if p.is_dir() and p.parent.name == "rasters"
            ]
            for folder in national_dirs:
                candidates.extend(folder.glob("*.tif"))
        if not candidates:
            # Último respaldo: toma el TIFF más grande para cada nombre.
            grouped: dict[str, list[Path]] = {}
            for path in self.root.rglob("*.tif"):
                grouped.setdefault(path.name, []).append(path)
            candidates = [max(paths, key=lambda p: p.stat().st_size) for paths in grouped.values()]

        result: dict[str, dict[str, Any]] = {}
        for path in sorted(set(candidates)):
            name = path.stem.lower()
            match_rad = re.fullmatch(r"viirs_(\d{4})", name)
            match_abs = re.fullmatch(r"cambio_abs_(\d{4})_(\d{4})", name)
            match_pct = re.fullmatch(r"cambio_pct_(\d{4})_(\d{4})", name)
            if match_rad:
                year = int(match_rad.group(1))
                key = f"rad_{year}"
                result[key] = {
                    "key": key,
                    "path": path,
                    "kind": "radiance",
                    "label": f"Radiancia VIIRS {year}",
                    "year": year,
                }
            elif match_abs:
                y0, y1 = map(int, match_abs.groups())
                key = f"abs_{y0}_{y1}"
                result[key] = {
                    "key": key,
                    "path": path,
                    "kind": "absolute",
                    "label": f"Cambio absoluto {y0}–{y1}",
                    "start_year": y0,
                    "end_year": y1,
                }
            elif match_pct:
                y0, y1 = map(int, match_pct.groups())
                key = f"pct_{y0}_{y1}"
                result[key] = {
                    "key": key,
                    "path": path,
                    "kind": "percent",
                    "label": f"Cambio porcentual {y0}–{y1}",
                    "start_year": y0,
                    "end_year": y1,
                }
        return result

    def _compute_raster_style(self, meta: dict[str, Any]) -> dict[str, float]:
        path = meta["path"]
        with rasterio.open(path) as src:
            factor = max(1.0, math.sqrt((src.width * src.height) / 600_000.0))
            out_w = max(1, int(src.width / factor))
            out_h = max(1, int(src.height / factor))
            arr = src.read(
                1,
                out_shape=(out_h, out_w),
                masked=True,
                resampling=Resampling.average,
            ).astype("float64")
            values = arr.compressed() if np.ma.isMaskedArray(arr) else arr[np.isfinite(arr)]
        values = np.asarray(values, dtype="float64")
        values = values[np.isfinite(values)]
        kind = meta["kind"]
        if kind == "radiance":
            positive = values[values >= 0]
            vmax = max(robust_percentile(positive, 99.4, 1.0), 0.5)
            return {"vmin": 0.0, "vmax": vmax, "center": 0.0}
        absolute = np.abs(values)
        vmax = max(robust_percentile(absolute, 99.0, 1.0), 0.01)
        if kind == "percent":
            vmax = min(max(vmax, 25.0), 500.0)
        return {"vmin": -vmax, "vmax": vmax, "center": 0.0}

    def _derive_bounds(self) -> list[list[float]]:
        try:
            country = self.get_vector("departamentos")
            if not country.empty:
                minx, miny, maxx, maxy = country.total_bounds
                return [[float(miny), float(minx)], [float(maxy), float(maxx)]]
        except Exception:
            pass
        for meta in self.rasters.values():
            with rasterio.open(meta["path"]) as src:
                geom = geometry_to_wgs84(
                    box(*src.bounds), src.crs
                )
                minx, miny, maxx, maxy = geom.bounds
                return [[miny, minx], [maxy, maxx]]
        return [[-27.7, -62.7], [-19.2, -54.1]]

    def _choose_default_raster(self) -> str | None:
        abs_layers = sorted(
            (meta for meta in self.rasters.values() if meta["kind"] == "absolute"),
            key=lambda m: m.get("end_year", 0),
        )
        if abs_layers:
            return abs_layers[-1]["key"]
        radiance = sorted(
            (meta for meta in self.rasters.values() if meta["kind"] == "radiance"),
            key=lambda m: m.get("year", 0),
        )
        return radiance[-1]["key"] if radiance else None

    def _choose_default_metric(self) -> str:
        for metric in ("cagr_pct_anual", "cambio_absoluto", "puntaje_crecimiento"):
            if metric in self.ranking.columns:
                return metric
        numeric = self.ranking.select_dtypes(include=[np.number]).columns.tolist()
        return numeric[0] if numeric else "cambio_absoluto"

    def get_vector(self, key: str) -> gpd.GeoDataFrame:
        if key not in VECTOR_LAYER_DEFS:
            raise KeyError(key)
        with self._lock:
            if key in self._vector_cache:
                return self._vector_cache[key]

        definition = VECTOR_LAYER_DEFS[key]
        layer_name = definition["gpkg"]
        if layer_name not in self.available_gpkg_layers:
            return gpd.GeoDataFrame(geometry=[], crs=WGS84)

        gdf = gpd.read_file(self.gpkg, layer=layer_name, engine="pyogrio")
        if gdf.crs is None:
            gdf = gdf.set_crs(WGS84)
        else:
            gdf = gdf.to_crs(WGS84)
        if "id_zona" in gdf.columns:
            gdf["id_zona"] = gdf["id_zona"].astype(str)
            # Conserva la geometría original y añade las métricas de resumen.
            summary = self.ranking.drop_duplicates("id_zona").copy()
            keep = list(dict.fromkeys([
                c for c in [
                    "id_zona",
                    "nivel",
                    "nombre",
                    "departamento",
                    *TOOLTIP_FIELDS,
                    *METRIC_LABELS.keys(),
                ]
                if c in summary.columns
            ]))
            summary = summary[keep].copy()
            duplicate_names = [c for c in summary.columns if c != "id_zona" and c in gdf.columns]
            summary = summary.rename(columns={c: f"{c}_resumen" for c in duplicate_names})
            gdf = gdf.merge(summary, on="id_zona", how="left")
            # Rellena nombres y niveles desde el resumen solo cuando faltan.
            for base in ("nombre", "nivel", "departamento"):
                alt = f"{base}_resumen"
                if alt in gdf.columns:
                    if base not in gdf.columns:
                        gdf[base] = gdf[alt]
                    else:
                        gdf[base] = gdf[base].where(gdf[base].notna(), gdf[alt])

        if definition["kind"] == "point":
            # GeoNames e INE se guardan como buffers analíticos; para una web son
            # mucho más ligeros y legibles como puntos centroides.
            projected = gdf.to_crs("EPSG:32721")
            projected["geometry"] = projected.geometry.centroid
            gdf = projected.to_crs(WGS84)
            if key == "localidades":
                sort_cols = [c for c in ["population", "puntaje_crecimiento"] if c in gdf.columns]
                if sort_cols:
                    gdf = gdf.sort_values(sort_cols, ascending=False, na_position="last")
                gdf = gdf.head(LOCALITY_LIMIT)
        else:
            tolerance = float(definition.get("simplify", 0.0))
            if tolerance > 0:
                gdf["geometry"] = gdf.geometry.simplify(tolerance, preserve_topology=True)

        gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].reset_index(drop=True)
        with self._lock:
            self._vector_cache[key] = gdf
        return gdf

    def department_names(self) -> list[str]:
        deps = self.get_vector("departamentos")
        if deps.empty or "nombre" not in deps.columns:
            return []
        return sorted({str(x) for x in deps["nombre"].dropna() if str(x).strip()})

    def department_geometry(self, name: str):
        if not name:
            return None
        with self._lock:
            if name in self._department_geometry_cache:
                return self._department_geometry_cache[name]
        deps = self.get_vector("departamentos")
        match = deps[deps["nombre"].astype(str) == str(name)]
        geom = match.geometry.unary_union if not match.empty else None
        with self._lock:
            self._department_geometry_cache[name] = geom
        return geom

    def _filter_vector_by_department(
        self,
        gdf: gpd.GeoDataFrame,
        key: str,
        department: str,
    ) -> gpd.GeoDataFrame:
        """Filtra primero por atributo y usa intersección espacial solo como respaldo."""
        if not department or gdf.empty:
            return gdf
        if key == "departamentos":
            if "nombre" not in gdf.columns:
                return gdf.iloc[0:0].copy()
            wanted = texto_normalizado(department)
            mask = gdf["nombre"].map(texto_normalizado) == wanted
            return gdf[mask].copy()

        if "departamento" in gdf.columns:
            wanted = texto_normalizado(department)
            mask = gdf["departamento"].map(texto_normalizado) == wanted
            if bool(mask.any()):
                return gdf[mask].copy()

        dep_geom = self.department_geometry(department)
        if dep_geom is None:
            return gdf.iloc[0:0].copy()
        try:
            # GeoPandas usa el índice espacial cuando está disponible.
            return gdf[gdf.intersects(dep_geom)].copy()
        except Exception:
            return gdf

    def _build_geojson_payload(self, key: str, department: str = "") -> dict[str, Any]:
        gdf = self._filter_vector_by_department(
            self.get_vector(key).copy(), key, department
        )

        # Limita propiedades para que los GeoJSON no sean innecesariamente grandes.
        property_fields = list(dict.fromkeys([
            c for c in [
                "id_zona",
                "nombre",
                "nivel",
                "departamento",
                "zona",
                "tipo_area",
                "rol",
                "population",
                "radio_km",
                "direccion",
                "distancia_desde_km",
                "distancia_hasta_km",
                *TOOLTIP_FIELDS,
                *METRIC_LABELS.keys(),
            ]
            if c in gdf.columns
        ]))
        slim = gdf[property_fields + ["geometry"]].copy()
        for col in property_fields:
            slim[col] = slim[col].map(clean_scalar)
        payload = json.loads(slim.to_json(drop_id=True, na="null"))
        payload["meta"] = {
            "layer": key,
            "label": VECTOR_LAYER_DEFS[key]["label"],
            "count": len(slim),
        }
        return payload

    def geojson_bytes(
        self,
        key: str,
        department: str = "",
        compressed: bool = True,
    ) -> bytes:
        """Serializa una sola vez cada capa/filtro y conserva la versión comprimida."""
        cache_key = (key, department or "", compressed)
        with self._lock:
            cached = self._geojson_bytes_cache.get(cache_key)
        if cached is not None:
            return cached

        payload = self._build_geojson_payload(key, department)
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        data = gzip.compress(raw, compresslevel=5) if compressed else raw
        with self._lock:
            self._geojson_bytes_cache[cache_key] = data
        return data

    def series(self, zone_id: str) -> list[dict[str, Any]]:
        if self.metrics.empty:
            return []
        rows = self.metrics[self.metrics["id_zona"] == str(zone_id)].copy()
        if rows.empty:
            return []
        rows["anio"] = pd.to_numeric(rows["anio"], errors="coerce")
        rows = rows.sort_values("anio")
        fields = [
            c for c in [
                "anio",
                "anio_parcial",
                "meses_disponibles",
                "radiancia_media",
                "radiancia_mediana",
                "radiancia_p90",
                "radiancia_p95",
                "radiancia_integrada_km2",
                "area_iluminada_1_0_km2",
                "area_nueva_1_0_km2",
                "cobertura_valida_pct",
            ]
            if c in rows.columns
        ]
        return [clean_record(record) for record in rows[fields].to_dict("records")]

    def top_zones(
        self,
        layer_key: str,
        metric: str,
        department: str = "",
        limit: int = 15,
    ) -> list[dict[str, Any]]:
        if metric not in self.ranking.columns or layer_key not in VECTOR_LAYER_DEFS:
            return []
        gdf = self._filter_vector_by_department(
            self.get_vector(layer_key).copy(), layer_key, department
        )
        if gdf.empty or metric not in gdf.columns:
            return []
        gdf[metric] = pd.to_numeric(gdf[metric], errors="coerce")
        gdf = gdf.dropna(subset=[metric]).sort_values(metric, ascending=False).head(limit)
        centroids = gdf.to_crs("EPSG:32721").geometry.centroid.to_crs(WGS84)
        rows = []
        for (_, row), centroid in zip(gdf.iterrows(), centroids):
            rows.append(
                clean_record(
                    {
                        "id_zona": row.get("id_zona"),
                        "nombre": row.get("nombre"),
                        "nivel": row.get("nivel"),
                        "value": row.get(metric),
                        "lat": centroid.y,
                        "lon": centroid.x,
                    }
                )
            )
        return rows

    def _build_search_index(self) -> pd.DataFrame:
        """Crea un índice liviano para buscar zonas por nombre."""
        with self._lock:
            if self._search_index is not None:
                return self._search_index

        searchable_layers = ("departamentos", "distritos", "localidades", "localidades_ine")
        records: list[dict[str, Any]] = []
        exposed_fields = list(dict.fromkeys([
            "id_zona",
            "nombre",
            "nivel",
            "departamento",
            *TOOLTIP_FIELDS,
            *METRIC_LABELS.keys(),
        ]))

        for layer_key in searchable_layers:
            if layer_key not in VECTOR_LAYER_DEFS:
                continue
            definition = VECTOR_LAYER_DEFS[layer_key]
            if definition["gpkg"] not in self.available_gpkg_layers:
                continue
            gdf = self.get_vector(layer_key)
            if gdf.empty or "nombre" not in gdf.columns:
                continue

            projected = gdf.to_crs("EPSG:32721")
            centers = projected.geometry.centroid.to_crs(WGS84)
            for (_, row), center in zip(gdf.iterrows(), centers):
                name = str(row.get("nombre") or "").strip()
                if not name:
                    continue
                minx, miny, maxx, maxy = row.geometry.bounds
                item: dict[str, Any] = {
                    "layer": layer_key,
                    "layer_label": definition["label"],
                    "kind": definition["kind"],
                    "id_zona": str(row.get("id_zona") or ""),
                    "nombre": name,
                    "nombre_norm": texto_normalizado(name),
                    "departamento": clean_scalar(row.get("departamento")),
                    "nivel": clean_scalar(row.get("nivel")) or definition["label"],
                    "lat": float(center.y),
                    "lon": float(center.x),
                    "bounds": [[float(miny), float(minx)], [float(maxy), float(maxx)]],
                }
                for field in exposed_fields:
                    if field in row.index and field not in item:
                        item[field] = clean_scalar(row.get(field))
                records.append(item)

        index = pd.DataFrame(records)
        if not index.empty:
            departments = (
                index["departamento"].fillna("").astype(str)
                if "departamento" in index.columns
                else pd.Series("", index=index.index)
            )
            levels = (
                index["nivel"].fillna("").astype(str)
                if "nivel" in index.columns
                else pd.Series("", index=index.index)
            )
            index["search_text"] = (
                index["nombre"].fillna("").astype(str) + " " + departments + " " + levels
            ).map(texto_normalizado)
            index = index.drop_duplicates(subset=["layer", "id_zona", "nombre"]).reset_index(drop=True)

        with self._lock:
            self._search_index = index
        return index

    def search_zones(self, query: str, limit: int = 12) -> list[dict[str, Any]]:
        query_norm = texto_normalizado(query)
        if len(query_norm) < 2:
            return []
        index = self._build_search_index()
        if index.empty:
            return []

        names = index["nombre_norm"].fillna("").astype(str)
        text = index["search_text"].fillna("").astype(str)
        contains = text.str.contains(re.escape(query_norm), regex=True, na=False)
        candidates = index[contains].copy()

        if candidates.empty:
            tokens = [token for token in query_norm.split("_") if len(token) >= 2]
            if tokens:
                mask = pd.Series(True, index=index.index)
                for token in tokens:
                    mask &= text.str.contains(re.escape(token), regex=True, na=False)
                candidates = index[mask].copy()
        if candidates.empty:
            return []

        candidate_names = names.loc[candidates.index]
        candidates["score"] = 40.0
        candidates.loc[candidate_names == query_norm, "score"] = 120.0
        candidates.loc[candidate_names.str.startswith(query_norm), "score"] = 100.0
        word_match = candidate_names.str.contains(
            rf"(?:^|_){re.escape(query_norm)}", regex=True, na=False
        )
        candidates.loc[word_match, "score"] = candidates.loc[word_match, "score"].clip(lower=82.0)
        layer_bonus = {"departamentos": 4.0, "distritos": 3.0, "localidades": 2.0, "localidades_ine": 1.0}
        candidates["score"] += candidates["layer"].map(layer_bonus).fillna(0)
        candidates["name_length"] = candidates["nombre"].astype(str).str.len()
        candidates = candidates.sort_values(
            ["score", "name_length", "nombre"], ascending=[False, True, True]
        ).head(min(max(int(limit), 1), 30))

        drop_fields = {"nombre_norm", "search_text", "score", "name_length"}
        return [
            clean_record({k: v for k, v in row.items() if k not in drop_fields})
            for row in candidates.to_dict("records")
        ]

    def feature_geojson(self, layer_key: str, zone_id: str) -> dict[str, Any]:
        if layer_key not in VECTOR_LAYER_DEFS:
            return {"type": "FeatureCollection", "features": []}
        gdf = self.get_vector(layer_key)
        if gdf.empty or "id_zona" not in gdf.columns:
            return {"type": "FeatureCollection", "features": []}
        selected = gdf[gdf["id_zona"].astype(str) == str(zone_id)].copy()
        if selected.empty:
            return {"type": "FeatureCollection", "features": []}
        fields = list(dict.fromkeys([
            c for c in [
                "id_zona",
                "nombre",
                "nivel",
                "departamento",
                *TOOLTIP_FIELDS,
                *METRIC_LABELS.keys(),
            ]
            if c in selected.columns
        ]))
        slim = selected[fields + ["geometry"]].copy()
        for col in fields:
            slim[col] = slim[col].map(clean_scalar)
        return json.loads(slim.to_json(drop_id=True, na="null"))

    def raster_catalog(self) -> list[dict[str, Any]]:
        rows = []
        for key, meta in self.rasters.items():
            row = {k: v for k, v in meta.items() if k != "path"}
            row["style"] = self.raster_stats[key]
            rows.append(clean_record(row))
        order = {"absolute": 0, "percent": 1, "radiance": 2}
        return sorted(rows, key=lambda x: (order.get(x.get("kind"), 9), x.get("year", x.get("end_year", 0))))

    def pixel_values(self, lat: float, lon: float, selected: str | None = None) -> dict[str, Any]:
        keys: list[str] = []
        if selected and selected in self.rasters:
            keys.append(selected)
        radiance = sorted(
            (m for m in self.rasters.values() if m["kind"] == "radiance"),
            key=lambda m: m.get("year", 0),
        )
        if radiance:
            keys.extend([radiance[0]["key"], radiance[-1]["key"]])
        for kind in ("absolute", "percent"):
            matches = sorted(
                (m for m in self.rasters.values() if m["kind"] == kind),
                key=lambda m: m.get("end_year", 0),
            )
            if matches:
                keys.append(matches[-1]["key"])
        keys = list(dict.fromkeys(keys))

        values: list[dict[str, Any]] = []
        for key in keys:
            meta = self.rasters[key]
            try:
                with rasterio.open(meta["path"]) as src:
                    transformer = Transformer.from_crs(WGS84, src.crs, always_xy=True)
                    x, y = transformer.transform(lon, lat)
                    sample = next(src.sample([(x, y)], indexes=1, masked=True))
                    value = sample[0]
                    if np.ma.is_masked(value) or not np.isfinite(float(value)):
                        value = None
                    else:
                        value = float(value)
            except Exception:
                value = None
            values.append({"key": key, "label": meta["label"], "value": value, "kind": meta["kind"]})
        return {"lat": lat, "lon": lon, "values": values}

    def hotspot_points(self, percentile: float = 98.5, max_points: int = MAX_HOTSPOT_POINTS) -> dict[str, Any]:
        change_layers = sorted(
            (m for m in self.rasters.values() if m["kind"] == "absolute"),
            key=lambda m: m.get("end_year", 0),
        )
        if not change_layers:
            return {"type": "FeatureCollection", "features": [], "meta": {"reason": "No change raster"}}
        meta = change_layers[-1]
        path = meta["path"]
        percentile = min(max(float(percentile), 50.0), 99.99)
        max_points = min(max(int(max_points), 100), 10_000)

        with rasterio.open(path) as src:
            factor = max(1.0, math.sqrt((src.width * src.height) / 350_000.0))
            out_w = max(1, int(src.width / factor))
            out_h = max(1, int(src.height / factor))
            arr = src.read(
                1,
                out_shape=(out_h, out_w),
                masked=True,
                resampling=Resampling.average,
            ).astype("float64")
            out_transform = src.transform * Affine.scale(src.width / out_w, src.height / out_h)
            crs = src.crs

        data = arr.filled(np.nan) if np.ma.isMaskedArray(arr) else arr
        valid = data[np.isfinite(data) & (data > 0)]
        if valid.size == 0:
            return {"type": "FeatureCollection", "features": [], "meta": {"reason": "No positive change"}}
        threshold = float(np.nanpercentile(valid, percentile))
        rows, cols = np.where(np.isfinite(data) & (data >= threshold))
        values = data[rows, cols]
        if values.size > max_points:
            idx = np.argpartition(values, -max_points)[-max_points:]
            rows, cols, values = rows[idx], cols[idx], values[idx]
        order = np.argsort(values)[::-1]
        rows, cols, values = rows[order], cols[order], values[order]
        xs, ys = xy(out_transform, rows, cols, offset="center")
        if crs and str(crs).upper() != WGS84:
            transformer = Transformer.from_crs(crs, WGS84, always_xy=True)
            xs, ys = transformer.transform(xs, ys)
        vmax = float(np.nanmax(values)) if values.size else threshold
        denom = max(vmax - threshold, 1e-12)
        features = []
        for lon, lat, value in zip(xs, ys, values):
            weight = 0.25 + 0.75 * ((float(value) - threshold) / denom)
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
                    "properties": {
                        "value": float(value),
                        "weight": float(min(max(weight, 0.05), 1.0)),
                    },
                }
            )
        return {
            "type": "FeatureCollection",
            "features": features,
            "meta": {
                "layer": meta["key"],
                "label": meta["label"],
                "percentile": percentile,
                "threshold": threshold,
                "count": len(features),
            },
        }


DATA_DIR = locate_data_dir()
STORE = NightLightsStore(DATA_DIR)
app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False


# ---------------------------------------------------------------------------
# Renderizado de teselas raster
# ---------------------------------------------------------------------------


RADIANCE_RAMP = np.array(
    [
        [0.00, 0, 0, 0],
        [0.12, 45, 10, 70],
        [0.28, 105, 18, 110],
        [0.48, 180, 45, 80],
        [0.68, 235, 105, 35],
        [0.84, 250, 190, 45],
        [1.00, 255, 255, 220],
    ],
    dtype="float64",
)
DIVERGING_RAMP = np.array(
    [
        [0.00, 25, 70, 150],
        [0.20, 65, 120, 190],
        [0.42, 170, 210, 235],
        [0.50, 245, 245, 245],
        [0.58, 250, 190, 150],
        [0.80, 225, 85, 70],
        [1.00, 150, 20, 35],
    ],
    dtype="float64",
)


def interpolate_ramp(normalized: np.ndarray, ramp: np.ndarray) -> np.ndarray:
    result = np.zeros(normalized.shape + (3,), dtype="uint8")
    positions = ramp[:, 0]
    for channel in range(3):
        result[..., channel] = np.interp(normalized, positions, ramp[:, channel + 1]).astype("uint8")
    return result


def colorize_tile(data: np.ndarray, kind: str, style: dict[str, float]) -> np.ndarray:
    valid = np.isfinite(data)
    rgba = np.zeros(data.shape + (4,), dtype="uint8")
    if not valid.any():
        return rgba
    vmin, vmax = float(style["vmin"]), float(style["vmax"])
    if kind == "radiance":
        clipped = np.clip(data, max(vmin, 0.0), vmax)
        # Log1p permite leer tanto áreas tenues como centros urbanos intensos.
        normalized = np.log1p(clipped) / max(math.log1p(vmax), 1e-9)
        rgb = interpolate_ramp(normalized, RADIANCE_RAMP)
        alpha = np.where(data > 0.02, 225, 0).astype("uint8")
    else:
        normalized = (np.clip(data, vmin, vmax) - vmin) / max(vmax - vmin, 1e-9)
        rgb = interpolate_ramp(normalized, DIVERGING_RAMP)
        alpha = np.where(np.abs(data) > max(0.005 * vmax, 0.001), 210, 0).astype("uint8")
    rgba[..., :3] = rgb
    rgba[..., 3] = np.where(valid, alpha, 0)
    return rgba


@lru_cache(maxsize=1600)
def render_raster_tile(layer_key: str, z: int, x: int, y: int) -> bytes:
    if layer_key not in STORE.rasters:
        raise KeyError(layer_key)
    meta = STORE.rasters[layer_key]
    bounds = mercantile.xy_bounds(x, y, z)
    dst_transform = from_bounds(bounds.left, bounds.bottom, bounds.right, bounds.top, TILE_SIZE, TILE_SIZE)
    destination = np.full((TILE_SIZE, TILE_SIZE), np.nan, dtype="float32")
    with rasterio.open(meta["path"]) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=destination,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=src.nodata,
            dst_transform=dst_transform,
            dst_crs=WEB_MERCATOR,
            dst_nodata=np.nan,
            resampling=Resampling.nearest,
            num_threads=2,
        )
    rgba = colorize_tile(destination, meta["kind"], STORE.raster_stats[layer_key])
    buffer = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> Response:
    return jsonify({"status": "ok", "build": APP_BUILD, "data_dir": str(DATA_DIR), "rasters": len(STORE.rasters)})


@app.get("/api/config")
def api_config() -> Response:
    vector_layers = [
        {
            "key": key,
            "label": definition["label"],
            "kind": definition["kind"],
        }
        for key, definition in VECTOR_LAYER_DEFS.items()
        if definition["gpkg"] in STORE.available_gpkg_layers
    ]
    metrics = [
        {"key": key, "label": label}
        for key, label in METRIC_LABELS.items()
        if key in STORE.ranking.columns
    ]
    return jsonify(
        {
            "title": APP_TITLE,
            "build": APP_BUILD,
            "bounds": STORE.bounds,
            "default_raster": STORE.default_raster,
            "default_metric": STORE.default_metric,
            "raster_layers": STORE.raster_catalog(),
            "vector_layers": vector_layers,
            "metrics": metrics,
            "departments": STORE.department_names(),
            "data_dir_name": DATA_DIR.name,
        }
    )


@app.get("/api/geojson/<layer_key>")
def api_geojson(layer_key: str) -> Response:
    if layer_key not in VECTOR_LAYER_DEFS:
        abort(404)
    department = request.args.get("department", "").strip()
    accepts_gzip = "gzip" in request.headers.get("Accept-Encoding", "").lower()
    body = STORE.geojson_bytes(layer_key, department, compressed=accepts_gzip)
    response = Response(body, mimetype="application/json")
    if accepts_gzip:
        response.headers["Content-Encoding"] = "gzip"
    response.headers["Vary"] = "Accept-Encoding"
    response.headers["Cache-Control"] = "public, max-age=86400"
    response.set_etag(f"{APP_BUILD}:{layer_key}:{department}:{int(accepts_gzip)}")
    return response.make_conditional(request)


@app.get("/api/series/<zone_id>")
def api_series(zone_id: str) -> Response:
    return jsonify({"id_zona": zone_id, "series": STORE.series(zone_id)})


@app.get("/api/top")
def api_top() -> Response:
    layer = request.args.get("layer", "departamentos")
    metric = request.args.get("metric", STORE.default_metric)
    department = request.args.get("department", "").strip()
    limit = int(request.args.get("limit", "15"))
    limit = min(max(limit, 1), 50)
    return jsonify(
        {
            "layer": layer,
            "metric": metric,
            "rows": STORE.top_zones(layer, metric, department, limit),
        }
    )


@app.get("/api/search")
def api_search() -> Response:
    query = request.args.get("q", "").strip()
    limit = int(request.args.get("limit", "12"))
    return jsonify({"query": query, "rows": STORE.search_zones(query, limit)})


@app.get("/api/feature/<layer_key>/<zone_id>")
def api_feature(layer_key: str, zone_id: str) -> Response:
    if layer_key not in VECTOR_LAYER_DEFS:
        abort(404)
    return jsonify(STORE.feature_geojson(layer_key, zone_id))


@app.get("/api/pixel")
def api_pixel() -> Response:
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
    except (KeyError, ValueError):
        return jsonify({"error": "lat y lon son obligatorios"}), 400
    selected = request.args.get("layer")
    return jsonify(STORE.pixel_values(lat, lon, selected))


@app.get("/api/hotspots")
def api_hotspots() -> Response:
    percentile = float(request.args.get("percentile", "98.5"))
    max_points = int(request.args.get("max_points", str(MAX_HOTSPOT_POINTS)))
    return jsonify(STORE.hotspot_points(percentile, max_points))


@app.get("/tiles/<layer_key>/<int:z>/<int:x>/<int:y>.png")
def raster_tile(layer_key: str, z: int, x: int, y: int) -> Response:
    if layer_key not in STORE.rasters or z < 0 or z > 19:
        abort(404)
    try:
        png = render_raster_tile(layer_key, z, x, y)
    except Exception as exc:
        app.logger.exception("Error creando tesela %s/%s/%s/%s: %s", layer_key, z, x, y, exc)
        abort(500)
    response = Response(png, mimetype="image/png")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


# ---------------------------------------------------------------------------
# Interfaz Leaflet
# ---------------------------------------------------------------------------


INDEX_HTML = r"""
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }}</title>
  <link rel="preconnect" href="https://unpkg.com">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --bg: #f4f6f8;
      --panel: #ffffff;
      --ink: #17202a;
      --muted: #64748b;
      --line: #dce3e9;
      --accent: #1e5da8;
      --accent-soft: #eaf2fb;
      --danger: #a83232;
      --shadow: 0 8px 24px rgba(15, 23, 42, 0.10);
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; color: var(--ink); background: var(--bg); }
    #app { height: 100%; display: grid; grid-template-columns: 380px 1fr; }
    #sidebar { height: 100%; overflow-y: auto; background: var(--panel); border-right: 1px solid var(--line); padding: 18px; z-index: 1000; }
    #map { height: 100%; width: 100%; background: #dbe4ec; }
    h1 { font-size: 22px; line-height: 1.15; margin: 0 0 6px; letter-spacing: -0.02em; }
    h2 { font-size: 15px; margin: 0 0 10px; }
    .subtitle { color: var(--muted); font-size: 12.5px; line-height: 1.45; margin-bottom: 14px; }
    .card { border: 1px solid var(--line); border-radius: 12px; padding: 13px; margin-bottom: 12px; background: #fff; box-shadow: 0 2px 8px rgba(15,23,42,.035); }
    label { display: block; font-size: 12px; font-weight: 700; margin: 9px 0 5px; }
    select, input[type="text"], input[type="number"] { width: 100%; border: 1px solid #cfd8e1; border-radius: 8px; padding: 8px 9px; background: #fff; color: var(--ink); }
    input[type="text"]:focus, select:focus { outline: 2px solid rgba(30,93,168,.16); border-color: #75a2d1; }
    input[type="range"] { width: 100%; }
    button { border: 0; border-radius: 8px; padding: 8px 10px; cursor: pointer; font-weight: 700; background: var(--accent); color: #fff; }
    button.secondary { background: var(--accent-soft); color: var(--accent); border: 1px solid #c6d9ee; }
    button:hover { filter: brightness(.97); }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .check-row { display: flex; align-items: center; gap: 8px; margin-top: 8px; font-size: 12px; }
    .check-row input { width: auto; }
    .legend-bar { height: 12px; border-radius: 999px; background: linear-gradient(90deg,#194696,#4a91c4,#f4f4f4,#ecaa82,#941423); margin-top: 8px; }
    .legend-rad { background: linear-gradient(90deg,#000,#2d0a46,#69126e,#b42d50,#eb6923,#fabe2d,#ffffdc); }
    .legend-labels { display: flex; justify-content: space-between; color: var(--muted); font-size: 10px; margin-top: 3px; }
    .info-title { font-weight: 800; font-size: 15px; margin-bottom: 4px; }
    .info-sub { color: var(--muted); font-size: 11px; margin-bottom: 9px; }
    .metrics-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 7px; }
    .metric-box { background: #f8fafc; border-radius: 8px; padding: 8px; border: 1px solid #edf1f5; }
    .metric-box span { display: block; font-size: 9.5px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
    .metric-box strong { display: block; margin-top: 3px; font-size: 13px; }
    #chart-wrap { height: 210px; margin-top: 9px; }
    #top-list { margin: 0; padding: 0; list-style: none; max-height: 260px; overflow-y: auto; }
    #top-list li { display: grid; grid-template-columns: 24px 1fr auto; gap: 7px; padding: 7px 4px; border-bottom: 1px solid #eef2f5; font-size: 11.5px; cursor: pointer; }
    #top-list li:hover { background: #f6f9fc; }
    .rank { width: 22px; height: 22px; border-radius: 50%; background: var(--accent-soft); color: var(--accent); display:flex; align-items:center; justify-content:center; font-weight:800; }
    .value { font-variant-numeric: tabular-nums; font-weight: 800; }
    .warning { color: var(--danger); background: #fff4f4; border: 1px solid #f1caca; padding: 8px; border-radius: 8px; margin-top: 8px; font-size: 11px; line-height: 1.35; }
    .small { font-size: 10.5px; color: var(--muted); line-height: 1.4; }
    .status { font-size: 11px; color: var(--muted); min-height: 16px; margin-top: 7px; }
    .search-wrap { position: relative; }
    #search-results { display: none; position: absolute; top: calc(100% + 5px); left: 0; right: 0; max-height: 310px; overflow-y: auto; background: #fff; border: 1px solid #cfd8e1; border-radius: 9px; box-shadow: var(--shadow); z-index: 2000; }
    .search-result { width: 100%; display: block; text-align: left; border-radius: 0; border-bottom: 1px solid #eef2f5; background: #fff; color: var(--ink); padding: 9px 10px; font-weight: 500; }
    .search-result:hover, .search-result.active { background: #edf5fc; filter: none; }
    .search-result strong { display: block; font-size: 12px; }
    .search-result span { display: block; color: var(--muted); font-size: 10.5px; margin-top: 2px; }
    .search-empty { padding: 10px; color: var(--muted); font-size: 11px; }
    .search-selection-actions { display: none; margin-top: 8px; }
    .search-selection-actions button { width: 100%; background: #fff7ed; color: #9a4d00; border-color: #fed7aa; }
    .search-selection-actions button:hover { background: #ffedd5; }
    .leaflet-tooltip { font-size: 11px; border-radius: 7px; box-shadow: var(--shadow); }
    .map-name-label {
      background: rgba(255,255,255,.92);
      border: 1px solid rgba(71,85,105,.34);
      border-radius: 5px;
      box-shadow: 0 1px 4px rgba(15,23,42,.24);
      color: #142033;
      font-size: 11px;
      font-weight: 800;
      line-height: 1.1;
      padding: 2px 5px;
      white-space: nowrap;
      text-shadow: 0 1px 0 #fff;
      pointer-events: none;
    }
    .map-name-label::before { display: none; }
    .leaflet-control-attribution { font-size: 9px; }
    @media (max-width: 900px) {
      #app { grid-template-columns: 1fr; grid-template-rows: 48% 52%; }
      #sidebar { border-right: 0; border-bottom: 1px solid var(--line); padding: 12px; }
    }
  </style>
</head>
<body>
<div id="app">
  <aside id="sidebar">
    <h1>{{ title }}</h1>
    <div style="display:inline-block;margin:5px 0 10px;padding:4px 8px;border-radius:999px;background:#e8f4ff;color:#075985;font-size:10px;font-weight:800;letter-spacing:.03em">VERSIÓN {{ build }}</div>
    <div class="subtitle">Radiancia VIIRS anual ponderada (nW/cm²/sr). La luminosidad es un indicador indirecto de actividad y urbanización, no una prueba de valorización inmobiliaria.</div>

    <div class="card">
      <h2>Buscar cualquier zona</h2>
      <div class="search-wrap">
        <input id="search-input" type="text" placeholder="Ej.: San Bernardino, Yby Yaú, Caaguazú…" autocomplete="off">
        <div id="search-results"></div>
      </div>
      <div id="search-selection-actions" class="search-selection-actions">
        <button id="clear-search-selection" type="button" class="secondary">Quitar selección buscada</button>
      </div>
      <div class="small" style="margin-top:7px">Busca departamentos, distritos, ciudades, pueblos y localidades. Usa “Quitar selección” o la tecla Esc para volver al hover normal.</div>
    </div>

    <div class="card">
      <h2>Visualización</h2>
      <label for="base-map">Mapa base</label>
      <select id="base-map">
        <option value="osm">OpenStreetMap</option>
        <option value="carto">Carto claro</option>
        <option value="dark">Carto oscuro</option>
      </select>

      <label for="raster-layer">Capa de píxeles VIIRS</label>
      <select id="raster-layer"></select>
      <label for="opacity">Opacidad del raster: <span id="opacity-value">72%</span></label>
      <input id="opacity" type="range" min="0" max="100" value="72">
      <div id="raster-legend" class="legend-bar"></div>
      <div class="legend-labels"><span id="legend-min">Bajo</span><span id="legend-mid">0</span><span id="legend-max">Alto</span></div>

      <label for="admin-layer">Capa administrativa / localidades</label>
      <select id="admin-layer"></select>
      <label for="metric">Métrica para colorear y ranquear</label>
      <select id="metric"></select>
      <label for="department">Enfocar departamento</label>
      <select id="department"><option value="">Todo Paraguay</option></select>

      <div class="check-row"><input id="show-labels" type="checkbox" checked><span>Mostrar nombres claros sobre el raster</span></div>
      <div class="check-row"><input id="show-hotspots" type="checkbox"><span>Mapa de calor de hotspots del raster</span></div>
      <label for="hotspot-percentile">Hotspots: percentil <span id="hotspot-label">98.5</span></label>
      <input id="hotspot-percentile" type="range" min="90" max="99.8" step="0.1" value="98.5">
      <div class="row" style="margin-top:10px">
        <button id="reset-view" class="secondary">Vista nacional</button>
        <button id="reload">Actualizar capas</button>
      </div>
      <div id="status" class="status"></div>
    </div>

    <div class="card">
      <h2>Zona seleccionada</h2>
      <div id="feature-info" class="small">Pasa el cursor sobre una zona para ver sus métricas. Haz clic para cargar su serie anual.</div>
      <div id="chart-wrap"><canvas id="series-chart"></canvas></div>
    </div>

    <div class="card">
      <h2>Píxel seleccionado</h2>
      <div id="pixel-info" class="small">Haz clic en el mapa para consultar los valores del píxel VIIRS en esa coordenada.</div>
    </div>

    <div class="card">
      <h2>Zonas con mayor crecimiento</h2>
      <ol id="top-list"></ol>
    </div>

    <div class="small">Los años parciales pueden aparecer en las series, pero el script principal los excluye de los rankings frente a años completos.</div>
  </aside>
  <main id="map"></main>
</div>

<script>
const state = {
  config: null,
  map: null,
  baseLayers: {},
  currentBase: null,
  labelTileLayer: null,
  rasterLayer: null,
  pendingRasterLayer: null,
  rasterToken: 0,
  adminLayer: null,
  nameLabelLayer: null,
  hotspotLayer: null,
  searchHighlight: null,
  searchSelectedRow: null,
  searchRows: [],
  searchTimer: null,
  searchRequestToken: 0,
  chart: null,
  currentGeoJSON: null,
  currentAdminCacheKey: null,
  adminToken: 0,
  geojsonCache: new Map(),
  adminLayerCache: new Map(),
  labelRefreshTimer: null,
};

const fmt = (value, digits=2) => {
  if (value === null || value === undefined || value === '' || Number.isNaN(Number(value))) return '—';
  return Number(value).toLocaleString('es-PY', {maximumFractionDigits: digits});
};
const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
const metricLabel = key => {
  const item = state.config.metrics.find(x => x.key === key);
  return item ? item.label : key;
};
const setStatus = text => document.getElementById('status').textContent = text || '';
const selectedDepartment = () => document.getElementById('department').value;
const selectedLayer = () => document.getElementById('admin-layer').value;
const selectedMetric = () => document.getElementById('metric').value;

async function fetchJson(url, options={}) {
  const response = await fetch(url, {cache:'default', ...options});
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

function vectorCacheKey(layerKey=selectedLayer(), department=selectedDepartment()) {
  return `${layerKey}|${department || ''}`;
}

function setBoundedCache(map, key, value, maxEntries=10) {
  if (map.has(key)) map.delete(key);
  map.set(key, value);
  while (map.size > maxEntries) {
    const oldest = map.keys().next().value;
    if (oldest === state.currentAdminCacheKey && map.size > 1) {
      const current = map.get(oldest);
      map.delete(oldest);
      map.set(oldest, current);
      continue;
    }
    map.delete(oldest);
  }
}

async function getCachedGeoJSON(layerKey, department) {
  const key = vectorCacheKey(layerKey, department);
  if (state.geojsonCache.has(key)) return state.geojsonCache.get(key);
  const params = new URLSearchParams();
  if (department) params.set('department', department);
  const promise = fetchJson(`/api/geojson/${layerKey}?${params}`);
  setBoundedCache(state.geojsonCache, key, promise, 12);
  try {
    const geojson = await promise;
    setBoundedCache(state.geojsonCache, key, geojson, 12);
    return geojson;
  } catch (error) {
    state.geojsonCache.delete(key);
    throw error;
  }
}

function quantile(sorted, q) {
  if (!sorted.length) return 0;
  const pos = (sorted.length - 1) * q;
  const base = Math.floor(pos), rest = pos - base;
  return sorted[base + 1] !== undefined ? sorted[base] + rest * (sorted[base + 1] - sorted[base]) : sorted[base];
}
function metricDomain(geojson, metric) {
  const values = geojson.features.map(f => Number(f.properties[metric])).filter(Number.isFinite).sort((a,b)=>a-b);
  if (!values.length) return {min:0,max:1,diverging:false};
  let min = quantile(values, .05), max = quantile(values, .95);
  if (min === max) { min = Math.min(0, min); max = Math.max(1, max); }
  return {min, max, diverging: min < 0 && max > 0};
}
function hexToRgb(hex) {
  const n = parseInt(hex.replace('#',''), 16);
  return [(n>>16)&255, (n>>8)&255, n&255];
}
function rgbToHex(rgb) { return '#' + rgb.map(v => Math.max(0,Math.min(255,Math.round(v))).toString(16).padStart(2,'0')).join(''); }
function mix(a,b,t) { const A=hexToRgb(a),B=hexToRgb(b); return rgbToHex(A.map((v,i)=>v+(B[i]-v)*t)); }
function featureColor(value, domain) {
  value = Number(value);
  if (!Number.isFinite(value)) return '#cbd5e1';
  if (domain.diverging) {
    const maxAbs = Math.max(Math.abs(domain.min), Math.abs(domain.max), 1e-9);
    const t = Math.max(-1, Math.min(1, value/maxAbs));
    return t < 0 ? mix('#245a9a','#f7f7f7',t+1) : mix('#f7f7f7','#b62d2d',t);
  }
  const t = Math.max(0, Math.min(1, (value-domain.min)/(domain.max-domain.min || 1)));
  return mix('#edf5fb','#145b9d',Math.sqrt(t));
}

function createMapPanes() {
  const panes = {
    basePane: 200,
    rasterPane: 300,
    hotspotPane: 360,
    adminPane: 420,
    labelTilePane: 600,
    nameLabelPane: 650,
    searchPane: 700,
  };
  for (const [name, zIndex] of Object.entries(panes)) {
    state.map.createPane(name);
    state.map.getPane(name).style.zIndex = String(zIndex);
  }
  state.map.getPane('labelTilePane').style.pointerEvents = 'none';
  state.map.getPane('nameLabelPane').style.pointerEvents = 'none';
  // El resaltado de una búsqueda debe ser solo visual: no puede bloquear
  // mouseover/click de la capa administrativa que está debajo.
  state.map.getPane('searchPane').style.pointerEvents = 'none';
}

async function init() {
  state.config = await fetchJson('/api/config');
  state.map = L.map('map', {zoomControl:true, preferCanvas:true});
  createMapPanes();
  state.baseLayers = {
    osm: L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {pane:'basePane', maxZoom:19, attribution:'© OpenStreetMap contributors'}),
    carto: L.tileLayer('https://{s}.basemaps.cartocdn.com/light_nolabels/{z}/{x}/{y}{r}.png', {pane:'basePane', maxZoom:20, attribution:'© OpenStreetMap © CARTO'}),
    dark: L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png', {pane:'basePane', maxZoom:20, attribution:'© OpenStreetMap © CARTO'})
  };
  state.currentBase = state.baseLayers.osm.addTo(state.map);
  state.nameLabelLayer = L.layerGroup().addTo(state.map);
  state.map.fitBounds(state.config.bounds);

  fillSelects();
  bindEvents();
  updateLabelTiles();
  await Promise.all([loadRaster(), loadAdminLayer(), updateTopList()]);

  state.map.on('click', queryPixel);
  state.map.on('zoomend', scheduleFeatureLabels);
}

function fillSelects() {
  const raster = document.getElementById('raster-layer');
  for (const layer of state.config.raster_layers) {
    const option = document.createElement('option'); option.value = layer.key; option.textContent = layer.label;
    if (layer.key === state.config.default_raster) option.selected = true;
    raster.appendChild(option);
  }
  const admin = document.getElementById('admin-layer');
  const preferred = ['departamentos','distritos','localidades','localidades_ine','hex_nacional'];
  for (const key of preferred) {
    const layer = state.config.vector_layers.find(x => x.key === key);
    if (!layer) continue;
    const option = document.createElement('option'); option.value = layer.key; option.textContent = layer.label;
    admin.appendChild(option);
  }
  const metric = document.getElementById('metric');
  for (const item of state.config.metrics) {
    const option = document.createElement('option'); option.value = item.key; option.textContent = item.label;
    if (item.key === state.config.default_metric) option.selected = true;
    metric.appendChild(option);
  }
  const dep = document.getElementById('department');
  for (const name of state.config.departments) {
    const option = document.createElement('option'); option.value = name; option.textContent = name; dep.appendChild(option);
  }
}

function bindEvents() {
  document.getElementById('base-map').addEventListener('change', ev => {
    if (state.currentBase) state.map.removeLayer(state.currentBase);
    state.currentBase = state.baseLayers[ev.target.value].addTo(state.map);
    updateLabelTiles();
  });
  document.getElementById('raster-layer').addEventListener('change', loadRaster);
  document.getElementById('opacity').addEventListener('input', ev => {
    document.getElementById('opacity-value').textContent = `${ev.target.value}%`;
    const opacity = Number(ev.target.value)/100;
    if (state.rasterLayer) state.rasterLayer.setOpacity(opacity);
    if (state.pendingRasterLayer) state.pendingRasterLayer.setOpacity(0);
  });
  document.getElementById('admin-layer').addEventListener('change', async () => { clearSearchHighlight(); await loadAdminLayer(); await updateTopList(); });
  document.getElementById('metric').addEventListener('change', async () => { restyleCurrentAdminLayer(); scheduleFeatureLabels(); await updateTopList(); });
  document.getElementById('department').addEventListener('change', async ev => {
    clearSearchHighlight();
    await loadAdminLayer(); await updateTopList();
    if (!ev.target.value) state.map.fitBounds(state.config.bounds);
    else await zoomToDepartment(ev.target.value);
  });
  document.getElementById('show-labels').addEventListener('change', () => { updateLabelTiles(); scheduleFeatureLabels(); });
  document.getElementById('show-hotspots').addEventListener('change', loadHotspots);
  document.getElementById('hotspot-percentile').addEventListener('input', ev => document.getElementById('hotspot-label').textContent = ev.target.value);
  document.getElementById('hotspot-percentile').addEventListener('change', loadHotspots);
  document.getElementById('reset-view').addEventListener('click', async () => {
    document.getElementById('department').value='';
    clearSearchHighlight();
    state.map.fitBounds(state.config.bounds);
    await loadAdminLayer();
    await updateTopList();
  });
  document.getElementById('reload').addEventListener('click', async () => {
    await loadRaster();
    await loadAdminLayer();
    await loadHotspots();
    await updateTopList();
  });

  document.getElementById('clear-search-selection').addEventListener('click', () => clearSearchHighlight());

  const searchInput = document.getElementById('search-input');
  searchInput.addEventListener('input', () => {
    if (state.searchSelectedRow && searchInput.value.trim() !== String(state.searchSelectedRow.nombre || '').trim()) {
      clearSearchHighlight({clearInput:false, resetPanel:false, clearStatus:false});
    }
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(() => performSearch(searchInput.value), 220);
  });
  searchInput.addEventListener('keydown', ev => {
    if (ev.key === 'Enter' && state.searchRows.length) {
      ev.preventDefault();
      chooseSearchResult(state.searchRows[0]);
    } else if (ev.key === 'Escape') {
      ev.preventDefault();
      if (state.searchHighlight) clearSearchHighlight();
      else hideSearchResults();
    }
  });
  document.addEventListener('click', ev => {
    if (!ev.target.closest('.search-wrap')) hideSearchResults();
  });
  document.addEventListener('keydown', ev => {
    if (ev.key === 'Escape' && state.searchHighlight && ev.target !== searchInput) {
      clearSearchHighlight();
    }
  });
}

function updateLabelTiles() {
  if (state.labelTileLayer) {
    state.map.removeLayer(state.labelTileLayer);
    state.labelTileLayer = null;
  }
  if (!document.getElementById('show-labels').checked) return;
  const dark = document.getElementById('base-map').value === 'dark';
  const style = dark ? 'dark_only_labels' : 'light_only_labels';
  state.labelTileLayer = L.tileLayer(`https://{s}.basemaps.cartocdn.com/${style}/{z}/{x}/{y}{r}.png`, {
    pane:'labelTilePane', maxZoom:20, updateWhenIdle:false, keepBuffer:4,
    attribution:'© OpenStreetMap © CARTO'
  }).addTo(state.map);
}

function updateRasterLegend(key) {
  const meta = state.config.raster_layers.find(x => x.key === key);
  const legend = document.getElementById('raster-legend');
  legend.className = meta && meta.kind === 'radiance' ? 'legend-bar legend-rad' : 'legend-bar';
  if (meta && meta.style) {
    document.getElementById('legend-min').textContent = fmt(meta.style.vmin,1);
    document.getElementById('legend-mid').textContent = meta.kind === 'radiance' ? 'Radiancia' : '0';
    document.getElementById('legend-max').textContent = fmt(meta.style.vmax,1);
  }
}

async function loadRaster() {
  const key = document.getElementById('raster-layer').value;
  if (!key) return;
  const token = ++state.rasterToken;
  const opacity = Number(document.getElementById('opacity').value)/100;
  updateRasterLegend(key);

  if (state.pendingRasterLayer) {
    state.map.removeLayer(state.pendingRasterLayer);
    state.pendingRasterLayer = null;
  }

  let loadedTiles = 0;
  let tileErrors = 0;
  let finalized = false;
  const transparentTile = 'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=';
  const nextLayer = L.tileLayer(`/tiles/${encodeURIComponent(key)}/{z}/{x}/{y}.png`, {
    pane:'rasterPane',
    opacity:0,
    maxZoom:18,
    minZoom:3,
    tileSize:256,
    bounds:state.config.bounds,
    noWrap:true,
    updateWhenIdle:false,
    updateWhenZooming:true,
    keepBuffer:5,
    crossOrigin:true,
    errorTileUrl:transparentTile,
  });
  state.pendingRasterLayer = nextLayer;

  const finalize = (fromTimeout=false) => {
    if (finalized || token !== state.rasterToken) return;
    if (loadedTiles === 0 && tileErrors > 0) {
      finalized = true;
      state.map.removeLayer(nextLayer);
      if (state.pendingRasterLayer === nextLayer) state.pendingRasterLayer = null;
      setStatus('No se pudo cargar el nuevo raster; se conserva la capa anterior. Pulsa “Actualizar capas” para reintentar.');
      return;
    }
    if (loadedTiles === 0 && !fromTimeout) return;
    finalized = true;
    const previous = state.rasterLayer;
    state.rasterLayer = nextLayer;
    if (state.pendingRasterLayer === nextLayer) state.pendingRasterLayer = null;
    nextLayer.setOpacity(Number(document.getElementById('opacity').value)/100);
    if (previous && previous !== nextLayer) state.map.removeLayer(previous);
    setStatus(tileErrors ? `Raster cargado con ${tileErrors} tesela(s) omitida(s).` : 'Raster cargado correctamente.');
  };

  nextLayer.on('tileload', () => { loadedTiles += 1; });
  nextLayer.on('tileerror', () => { tileErrors += 1; });
  nextLayer.on('load', () => finalize(false));
  nextLayer.addTo(state.map);
  window.setTimeout(() => finalize(true), 4500);
}

function pointRadius(value, domain) {
  value = Number(value);
  if (!Number.isFinite(value)) return 3;
  const span = Math.abs(domain.max - domain.min) || 1;
  return Math.max(3, Math.min(10, 4 + Math.abs(value - domain.min) / span * 5));
}

function buildAdminLayer(geojson, layerKey, metric, domain) {
  const canvasRenderer = L.canvas({padding:.35});
  return L.geoJSON(geojson, {
    pane:'adminPane',
    renderer:canvasRenderer,
    style: feature => ({
      pane:'adminPane', color:'#52616f', weight:layerKey === 'departamentos' ? 1.3 : .8,
      fillColor:featureColor(feature.properties[metric], domain), fillOpacity:.58,
    }),
    pointToLayer: (feature, latlng) => {
      const value = Number(feature.properties[metric]);
      return L.circleMarker(latlng, {
        pane:'adminPane', renderer:canvasRenderer,
        radius:pointRadius(value, domain), color:'#fff', weight:.8,
        fillColor:featureColor(value,domain), fillOpacity:.88
      });
    },
    onEachFeature: (feature, layer) => {
      layer.bindTooltip(tooltipHtml(feature.properties, metric), {sticky:true, direction:'auto', pane:'nameLabelPane'});
      layer.on('mouseover', () => showFeatureInfo(feature.properties, false));
      layer.on('click', () => { showFeatureInfo(feature.properties, true); loadSeries(feature.properties.id_zona, feature.properties.nombre); });
    }
  });
}

function restyleAdminLayer(layer=state.adminLayer, geojson=state.currentGeoJSON, layerKey=selectedLayer()) {
  if (!layer || !geojson) return;
  const metric = selectedMetric();
  const domain = metricDomain(geojson, metric);
  layer.eachLayer(item => {
    if (!item.feature) return;
    const p = item.feature.properties || {};
    const value = Number(p[metric]);
    if (typeof item.setRadius === 'function') item.setRadius(pointRadius(value, domain));
    if (typeof item.setStyle === 'function') {
      item.setStyle({
        color: typeof item.getLatLng === 'function' ? '#fff' : '#52616f',
        weight: typeof item.getLatLng === 'function' ? .8 : (layerKey === 'departamentos' ? 1.3 : .8),
        fillColor:featureColor(value, domain),
        fillOpacity:typeof item.getLatLng === 'function' ? .88 : .58,
      });
    }
    if (typeof item.setTooltipContent === 'function') item.setTooltipContent(tooltipHtml(p, metric));
  });
}

async function loadAdminLayer() {
  const layerKey = selectedLayer();
  if (!layerKey) return;
  const department = selectedDepartment();
  const cacheKey = vectorCacheKey(layerKey, department);
  const token = ++state.adminToken;
  setStatus('Cargando capa…');

  const wasCached = state.geojsonCache.has(cacheKey) || state.adminLayerCache.has(cacheKey);
  try {
    const geojson = await getCachedGeoJSON(layerKey, department);
    if (token !== state.adminToken) return;
    const metric = selectedMetric();
    const domain = metricDomain(geojson, metric);
    let nextLayer = state.adminLayerCache.get(cacheKey);
    if (!nextLayer) {
      nextLayer = buildAdminLayer(geojson, layerKey, metric, domain);
      setBoundedCache(state.adminLayerCache, cacheKey, nextLayer, 8);
    }

    const previous = state.adminLayer;
    state.currentGeoJSON = geojson;
    state.currentAdminCacheKey = cacheKey;
    state.adminLayer = nextLayer;
    restyleAdminLayer(nextLayer, geojson, layerKey);

    if (!state.map.hasLayer(nextLayer)) nextLayer.addTo(state.map);
    if (previous && previous !== nextLayer && state.map.hasLayer(previous)) state.map.removeLayer(previous);
    scheduleFeatureLabels();
    setStatus(`${geojson.meta?.count || geojson.features.length} elementos cargados${wasCached ? ' · desde caché' : ''}.`);
  } catch (error) {
    if (token !== state.adminToken) return;
    setStatus(`No se pudo cargar la capa: ${error.message}`);
  }
}

function clearFeatureLabels() {
  if (state.nameLabelLayer) state.nameLabelLayer.clearLayers();
}

function scheduleFeatureLabels() {
  clearTimeout(state.labelRefreshTimer);
  state.labelRefreshTimer = setTimeout(refreshFeatureLabels, 90);
}

function refreshFeatureLabels() {
  clearFeatureLabels();
  if (!state.adminLayer || !document.getElementById('show-labels').checked) return;
  const layerKey = selectedLayer();
  const zoom = state.map.getZoom();
  let limit = 0;
  if (layerKey === 'departamentos') limit = 30;
  else if (layerKey === 'distritos') limit = zoom >= 7 ? 350 : (zoom >= 6 ? 120 : 0);
  else if (layerKey === 'localidades' || layerKey === 'localidades_ine') {
    limit = zoom >= 12 ? 650 : zoom >= 10 ? 260 : zoom >= 8 ? 80 : 0;
  }
  if (!limit) return;

  const metric = selectedMetric();
  const candidates = [];
  state.adminLayer.eachLayer(layer => {
    if (!layer.feature) return;
    const p = layer.feature.properties || {};
    const name = p.nombre || p.id_zona;
    if (!name) return;
    let latlng = null;
    if (typeof layer.getLatLng === 'function') latlng = layer.getLatLng();
    else if (typeof layer.getBounds === 'function' && layer.getBounds().isValid()) latlng = layer.getBounds().getCenter();
    if (!latlng) return;
    const population = Number(p.population);
    const metricValue = Math.abs(Number(p[metric]));
    const priority = Number.isFinite(population) ? population : (Number.isFinite(metricValue) ? metricValue : 0);
    candidates.push({name, latlng, priority});
  });
  candidates.sort((a,b) => b.priority - a.priority || String(a.name).localeCompare(String(b.name)));
  for (const item of candidates.slice(0, limit)) {
    const label = L.tooltip({
      permanent:true, direction:'center', className:'map-name-label', pane:'nameLabelPane', opacity:1, interactive:false
    }).setLatLng(item.latlng).setContent(escapeHtml(item.name));
    state.nameLabelLayer.addLayer(label);
  }
}

function tooltipHtml(p, metric) {
  return `<strong>${escapeHtml(p.nombre || p.id_zona || 'Zona')}</strong><br>${escapeHtml(metricLabel(metric))}: ${fmt(p[metric])}<br>Cambio abs.: ${fmt(p.cambio_absoluto)} · CAGR: ${fmt(p.cagr_pct_anual)}%`;
}

function showFeatureInfo(p, selected) {
  const html = `
    <div class="info-title">${escapeHtml(p.nombre || p.id_zona || 'Zona')}</div>
    <div class="info-sub">${escapeHtml(p.nivel || '')}${p.departamento ? ' · '+escapeHtml(p.departamento) : ''}${selected ? ' · seleccionada' : ''}</div>
    <div class="metrics-grid">
      <div class="metric-box"><span>Radiancia inicial</span><strong>${fmt(p.radiancia_inicial)}</strong></div>
      <div class="metric-box"><span>Radiancia final</span><strong>${fmt(p.radiancia_final)}</strong></div>
      <div class="metric-box"><span>Cambio absoluto</span><strong>${fmt(p.cambio_absoluto)}</strong></div>
      <div class="metric-box"><span>Cambio porcentual</span><strong>${fmt(p.cambio_pct)}%</strong></div>
      <div class="metric-box"><span>CAGR anual</span><strong>${fmt(p.cagr_pct_anual)}%</strong></div>
      <div class="metric-box"><span>Puntaje</span><strong>${fmt(p.puntaje_crecimiento)}</strong></div>
    </div>
    ${p.advertencia ? `<div class="warning">${escapeHtml(p.advertencia)}</div>` : ''}`;
  document.getElementById('feature-info').innerHTML = html;
}

async function loadSeries(id, name) {
  if (!id) return;
  const payload = await fetchJson(`/api/series/${encodeURIComponent(id)}`);
  const rows = payload.series || [];
  const ctx = document.getElementById('series-chart');
  if (state.chart) state.chart.destroy();
  if (!rows.length) return;
  state.chart = new Chart(ctx, {
    type:'line',
    data:{
      labels:rows.map(r => `${r.anio}${r.anio_parcial ? '*' : ''}`),
      datasets:[{label:'Radiancia media', data:rows.map(r=>r.radiancia_media), borderWidth:2, pointRadius:2.5, tension:.18}]
    },
    options:{
      responsive:true, maintainAspectRatio:false,
      plugins:{legend:{display:false}, title:{display:true,text:name || id,font:{size:11}}},
      scales:{x:{ticks:{font:{size:9}}},y:{ticks:{font:{size:9}},title:{display:true,text:'nW/cm²/sr',font:{size:9}}}}
    }
  });
}

async function loadHotspots() {
  if (state.hotspotLayer) { state.map.removeLayer(state.hotspotLayer); state.hotspotLayer = null; }
  if (!document.getElementById('show-hotspots').checked) return;
  setStatus('Calculando hotspots…');
  const percentile = document.getElementById('hotspot-percentile').value;
  const gj = await fetchJson(`/api/hotspots?percentile=${encodeURIComponent(percentile)}`);
  const points = (gj.features || []).map(f => [f.geometry.coordinates[1], f.geometry.coordinates[0], f.properties.weight]);
  state.hotspotLayer = L.heatLayer(points, {pane:'hotspotPane', radius:16, blur:18, maxZoom:13, minOpacity:.25}).addTo(state.map);
  setStatus(`${points.length} celdas hotspot; umbral ${fmt(gj.meta?.threshold)}.`);
}

async function queryPixel(ev) {
  const raster = document.getElementById('raster-layer').value;
  const url = `/api/pixel?lat=${ev.latlng.lat}&lon=${ev.latlng.lng}&layer=${encodeURIComponent(raster)}`;
  document.getElementById('pixel-info').innerHTML = 'Consultando píxel…';
  const p = await fetchJson(url);
  const rows = (p.values || []).map(x => `<div class="metric-box"><span>${escapeHtml(x.label)}</span><strong>${fmt(x.value,3)}</strong></div>`).join('');
  document.getElementById('pixel-info').innerHTML = `<div class="info-sub">${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}</div><div class="metrics-grid">${rows}</div>`;
}

async function updateTopList() {
  const params = new URLSearchParams({layer:selectedLayer(), metric:selectedMetric(), limit:'15'});
  if (selectedDepartment()) params.set('department', selectedDepartment());
  const payload = await fetchJson(`/api/top?${params}`);
  const list = document.getElementById('top-list'); list.innerHTML = '';
  (payload.rows || []).forEach((row, index) => {
    const li = document.createElement('li');
    li.innerHTML = `<span class="rank">${index+1}</span><span>${escapeHtml(row.nombre || row.id_zona)}<br><small>${escapeHtml(row.nivel || '')}</small></span><span class="value">${fmt(row.value)}</span>`;
    li.addEventListener('click', () => { state.map.setView([row.lat,row.lon], Math.max(state.map.getZoom(), selectedLayer()==='localidades'?11:8)); loadSeries(row.id_zona,row.nombre); });
    list.appendChild(li);
  });
}

async function zoomToDepartment(name) {
  const gj = await getCachedGeoJSON('departamentos', name);
  if (gj.features && gj.features.length) {
    const temp = L.geoJSON(gj); state.map.fitBounds(temp.getBounds(), {padding:[20,20]});
  }
}

async function performSearch(query) {
  const results = document.getElementById('search-results');
  const clean = query.trim();
  if (clean.length < 2) {
    state.searchRows = [];
    hideSearchResults();
    return;
  }
  const requestToken = ++state.searchRequestToken;
  results.style.display = 'block';
  results.innerHTML = '<div class="search-empty">Buscando…</div>';
  try {
    const payload = await fetchJson(`/api/search?q=${encodeURIComponent(clean)}&limit=12`);
    if (requestToken !== state.searchRequestToken) return;
    state.searchRows = payload.rows || [];
    renderSearchResults();
  } catch (error) {
    if (requestToken !== state.searchRequestToken) return;
    results.innerHTML = `<div class="search-empty">Error al buscar: ${escapeHtml(error.message)}</div>`;
  }
}

function renderSearchResults() {
  const results = document.getElementById('search-results');
  results.innerHTML = '';
  if (!state.searchRows.length) {
    results.innerHTML = '<div class="search-empty">No se encontraron coincidencias.</div>';
    results.style.display = 'block';
    return;
  }
  for (const row of state.searchRows) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'search-result';
    const detail = [row.nivel || row.layer_label, row.departamento].filter(Boolean).join(' · ');
    button.innerHTML = `<strong>${escapeHtml(row.nombre)}</strong><span>${escapeHtml(detail)}</span>`;
    button.addEventListener('click', () => chooseSearchResult(row));
    results.appendChild(button);
  }
  results.style.display = 'block';
}

function hideSearchResults() {
  document.getElementById('search-results').style.display = 'none';
}

function clearSearchHighlight(options={}) {
  const {clearInput=true, resetPanel=true, clearStatus=true} = options;
  if (state.searchHighlight) {
    state.map.removeLayer(state.searchHighlight);
    state.searchHighlight = null;
  }
  state.searchSelectedRow = null;

  const actions = document.getElementById('search-selection-actions');
  if (actions) actions.style.display = 'none';
  const clearButton = document.getElementById('clear-search-selection');
  if (clearButton) clearButton.textContent = 'Quitar selección buscada';

  if (clearInput) {
    const input = document.getElementById('search-input');
    if (input) input.value = '';
    state.searchRows = [];
    hideSearchResults();
  }

  if (resetPanel) {
    document.getElementById('feature-info').innerHTML = 'Pasa el cursor sobre una zona para ver sus métricas. Haz clic para cargar su serie anual.';
    if (state.chart) {
      state.chart.destroy();
      state.chart = null;
    }
  }
  if (clearStatus) setStatus('Selección buscada eliminada. El hover normal está activo.');
}

async function chooseSearchResult(row) {
  document.getElementById('search-input').value = row.nombre || '';
  hideSearchResults();
  clearSearchHighlight({clearInput:false, resetPanel:false, clearStatus:false});
  state.searchSelectedRow = row;
  const selectionActions = document.getElementById('search-selection-actions');
  selectionActions.style.display = 'block';
  document.getElementById('clear-search-selection').textContent = `Quitar selección: ${row.nombre || 'zona'}`;
  document.getElementById('department').value = '';

  const adminSelect = document.getElementById('admin-layer');
  if ([...adminSelect.options].some(option => option.value === row.layer)) {
    adminSelect.value = row.layer;
    await loadAdminLayer();
    await updateTopList();
  }

  const gj = await fetchJson(`/api/feature/${encodeURIComponent(row.layer)}/${encodeURIComponent(row.id_zona)}`);
  state.searchHighlight = L.geoJSON(gj, {
    pane:'searchPane',
    interactive:false,
    style:{pane:'searchPane', interactive:false, color:'#f59e0b', weight:4, fillColor:'#fbbf24', fillOpacity:.18},
    pointToLayer:(feature, latlng) => L.circleMarker(latlng, {pane:'searchPane', interactive:false, radius:10, color:'#fff', weight:2.5, fillColor:'#f59e0b', fillOpacity:1}),
    onEachFeature:(feature, layer) => layer.bindTooltip(`<strong>${escapeHtml(feature.properties.nombre || row.nombre)}</strong>`, {permanent:true, direction:'top', pane:'nameLabelPane'})
  }).addTo(state.map);

  const bounds = state.searchHighlight.getBounds();
  if (bounds.isValid() && bounds.getNorthEast().distanceTo(bounds.getSouthWest()) > 25) {
    state.map.fitBounds(bounds, {padding:[45,45], maxZoom:13});
  } else {
    state.map.setView([Number(row.lat), Number(row.lon)], row.layer === 'departamentos' ? 7 : row.layer === 'distritos' ? 10 : 13);
  }
  showFeatureInfo(row, true);
  await loadSeries(row.id_zona, row.nombre);
  setStatus(`Zona encontrada: ${row.nombre}.`);
}

init().catch(err => {
  console.error(err);
  document.getElementById('status').textContent = `Error: ${err.message}`;
});
</script>
</body>
</html>
"""

@app.get("/")
def index() -> str:
    return render_template_string(INDEX_HTML, title=APP_TITLE, build=APP_BUILD)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8050"))
    print(f"Iniciando {APP_TITLE} | versión {APP_BUILD}")
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1", threaded=True)
