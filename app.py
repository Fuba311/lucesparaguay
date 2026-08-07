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
- servicios/servicios.csv.gz
- industrias/industrias.csv.gz
- industrias/zonas_industriales_web.geojson.gz
- SQL: lotes guardados, precios y evaluaciones

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
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone
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
from shapely.affinity import rotate as shapely_rotate
from shapely.geometry import Point, Polygon, box, mapping, shape
from shapely.ops import transform as shapely_transform
from sqlalchemy import Boolean, Column, DateTime, Float, Integer, MetaData, String, Table, Text, create_engine, delete, func, insert, select, update
from sqlalchemy.engine import Engine

try:
    from pyogrio import list_layers as pyogrio_list_layers
except Exception:  # pragma: no cover
    pyogrio_list_layers = None


APP_TITLE = "Explorador de luces nocturnas de Paraguay"
APP_BUILD = "2026-08-07-R10-MENU-COLAPSABLE-LATEST"
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
    "service_access_score": "Acceso a servicios (0–100)",
    "service_gap_score": "Déficit de servicios (0–100)",
    "demand_score": "Demanda poblacional (0–100)",
    "investment_score_experimental": "Puntaje inversión experimental",
    "population": "Población estimada de localidad",
    "population_est": "Población estimada del distrito",
    "hospital_nearest_km": "Hospital más cercano (km)",
    "primary_health_nearest_km": "Atención primaria más cercana (km)",
    "supermarket_nearest_km": "Supermercado más cercano (km)",
    "education_nearest_km": "Centro educativo más cercano (km)",
    "pharmacy_nearest_km": "Farmacia más cercana (km)",
    "hospital_per_10k": "Hospitales por 10.000 hab.",
    "primary_health_per_10k": "Atención primaria por 10.000 hab.",
    "education_per_10k": "Centros educativos por 10.000 hab.",
    "supermarket_per_10k": "Supermercados por 10.000 hab.",
    "pharmacy_per_10k": "Farmacias por 10.000 hab.",
    "industrial_opportunity_score": "Oportunidad industrial experimental (0–100)",
    "industrial_residential_balance_score": "Balance industria–residencia experimental (0–100)",
    "industrial_employment_access_score": "Acceso a empleo industrial (0–100)",
    "industrial_concentration_score": "Concentración industrial (0–100)",
    "industrial_exposure_score": "Exposición industrial potencial (0–100)",
    "factory_nearest_km": "Fábrica más cercana (km)",
    "productive_site_nearest_km": "Sitio productivo más cercano (km)",
    "industrial_zone_nearest_km": "Zona industrial más cercana (km)",
    "factory_count_10km": "Fábricas dentro de 10 km",
    "productive_site_count_10km": "Sitios productivos dentro de 10 km",
    "industrial_zone_area_ha_10km": "Área industrial cercana (ha)",
    "factory_count": "Fábricas en el distrito",
    "warehouse_count": "Depósitos/logística en el distrito",
    "productive_site_count": "Sitios productivos en el distrito",
    "factory_per_10k": "Fábricas por 10.000 hab.",
    "productive_site_per_10k": "Sitios productivos por 10.000 hab.",
    "industrial_zone_area_km2": "Área industrial del distrito (km²)",
    "industrial_zone_pct": "Superficie industrial del distrito (%)",
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
            find_first(root, "ranking_crecimiento_servicios_industrias.csv.gz")
            or find_first(root, "ranking_crecimiento_servicios_industrias.csv")
            or find_first(root, "ranking_crecimiento_y_servicios.csv.gz")
            or find_first(root, "ranking_crecimiento_y_servicios.csv")
            or find_first(root, "ranking_crecimiento.csv.gz")
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


SERVICE_GROUPS: dict[str, tuple[str, set[str] | None]] = {
    "hospital": ("health", {"hospital", "hospital_major"}),
    "primary_health": ("health", {"clinic", "health_centre", "health_post", "usf"}),
    "health_facility": ("health", {"hospital", "hospital_major", "clinic", "health_centre", "health_post", "usf", "health_other"}),
    "education": ("education", None),
    "supermarket": ("supermarket", None),
    "pharmacy": ("pharmacy", None),
    "bank": ("bank", None),
    "fuel": ("fuel", None),
    "police": ("police", None),
    "fire_station": ("fire_station", None),
    "market": ("market", None),
    "doctor": ("health", {"doctors"}),
    "dentist": ("health", {"dentist"}),
    "factory": ("industry", {"factory"}),
    "warehouse": ("industry", {"warehouse"}),
    "industrial_building": ("industry", {"industrial_building"}),
    "power_plant": ("industry", {"power_plant"}),
    "utility_waste": ("industry", {"utility_waste"}),
    "quarry": ("industry", {"quarry"}),
    "industrial_zone": ("industry", {"industrial_zone"}),
}
SERVICE_LABELS = {
    "hospital": "Hospitales",
    "primary_health": "Clínicas, centros, puestos y USF",
    "education": "Escuelas y centros educativos",
    "supermarket": "Supermercados",
    "pharmacy": "Farmacias",
    "bank": "Bancos y cajeros",
    "fuel": "Estaciones de servicio",
    "police": "Policía",
    "fire_station": "Bomberos",
    "market": "Mercados",
    "factory": "Fábricas y plantas",
    "warehouse": "Depósitos y logística",
    "industrial_building": "Edificios industriales (menor certeza)",
    "power_plant": "Plantas de energía",
    "utility_waste": "Servicios industriales y residuos",
    "quarry": "Canteras y extracción",
    "industrial_zone": "Centros de zonas industriales",
}
DEFAULT_SERVICE_GROUPS = ("hospital", "primary_health", "education", "supermarket", "pharmacy")


def haversine_km(lat: float, lon: float, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    radius = 6371.0088
    lat1 = np.radians(float(lat))
    lon1 = np.radians(float(lon))
    lat2 = np.radians(lats.astype("float64"))
    lon2 = np.radians(lons.astype("float64"))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2.0) ** 2
    return 2.0 * radius * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


class ServiceStore:
    """Carga servicios e industrias en una sola tabla ligera para la web."""

    def __init__(self, root: Path):
        service_path = find_first(root, "servicios.csv.gz") or find_first(root, "servicios.csv")
        industry_path = find_first(root, "industrias.csv.gz") or find_first(root, "industrias.csv")
        self.paths = [p for p in (service_path, industry_path) if p is not None]
        self.available = bool(self.paths)
        self.frame = pd.DataFrame()
        if not self.available:
            return

        required = [
            "service_id", "category", "subcategory", "name", "source", "department", "district",
            "lat", "lon", "sector", "risk_class", "confidence", "area_ha", "product", "raw_type",
        ]
        pieces: list[pd.DataFrame] = []
        for path in self.paths:
            frame = pd.read_csv(
                path,
                compression="infer",
                low_memory=False,
                usecols=lambda c: c in required,
            )
            if "category" not in frame.columns and "industrias" in path.name.lower():
                frame["category"] = "industry"
            pieces.append(frame)
        self.frame = pd.concat(pieces, ignore_index=True, sort=False)
        for col in ("lat", "lon", "area_ha"):
            if col not in self.frame.columns:
                self.frame[col] = np.nan
            self.frame[col] = pd.to_numeric(self.frame[col], errors="coerce", downcast="float")
        self.frame = self.frame.dropna(subset=["lat", "lon"]).reset_index(drop=True)
        for col in (
            "service_id", "category", "subcategory", "name", "source", "department", "district",
            "sector", "risk_class", "confidence", "product", "raw_type",
        ):
            if col not in self.frame.columns:
                self.frame[col] = ""
            self.frame[col] = self.frame[col].fillna("").astype(str)

    def group_mask(self, group: str) -> pd.Series:
        raw, allowed = SERVICE_GROUPS.get(group, (group, None))
        mask = self.frame["category"].eq(raw)
        if allowed is not None:
            mask &= self.frame["subcategory"].isin(allowed)
        return mask

    def public_config(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "count": int(len(self.frame)),
            "service_count": int(self.frame["category"].ne("industry").sum()) if self.available else 0,
            "industry_count": int(self.frame["category"].eq("industry").sum()) if self.available else 0,
            "groups": [
                {"key": key, "label": SERVICE_LABELS[key]}
                for key in SERVICE_LABELS
                if self.available and bool(self.group_mask(key).any())
            ],
            "default_groups": list(DEFAULT_SERVICE_GROUPS),
        }

    def bbox(self, west: float, south: float, east: float, north: float, groups: list[str], limit: int) -> dict[str, Any]:
        if not self.available:
            return {"type": "FeatureCollection", "features": [], "meta": {"available": False}}
        mask = self.frame["lon"].between(west, east) & self.frame["lat"].between(south, north)
        if groups:
            selected_groups = pd.Series(False, index=self.frame.index)
            for group in groups:
                selected_groups |= self.group_mask(group)
            mask &= selected_groups
        selected = self.frame.loc[mask].copy()
        total = len(selected)
        if total > limit:
            # Named/high-confidence records first; generic industrial buildings last.
            selected["_named"] = selected["name"].ne("").astype(int)
            selected["_confidence"] = selected["confidence"].map({"high": 3, "medium": 2, "low": 1}).fillna(0)
            selected = selected.sort_values(["_confidence", "_named"], ascending=False).head(limit)
        features = []
        property_cols = [
            "service_id", "category", "subcategory", "name", "source", "department", "district",
            "sector", "risk_class", "confidence", "area_ha", "product", "raw_type",
        ]
        for _, row in selected.iterrows():
            properties = {key: row.get(key) for key in property_cols}
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [float(row["lon"]), float(row["lat"])]},
                "properties": clean_record(properties),
            })
        return {
            "type": "FeatureCollection",
            "features": features,
            "meta": {"available": True, "count": len(features), "total_in_bbox": total, "truncated": total > limit},
        }

    def nearest_air(self, lat: float, lon: float, groups: list[str], candidates_per_group: int = 5) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for group in groups:
            subset = self.frame.loc[self.group_mask(group)].copy()
            if subset.empty:
                continue
            distances = haversine_km(lat, lon, subset["lat"].to_numpy(), subset["lon"].to_numpy())
            order = np.argsort(distances)[:max(1, candidates_per_group)]
            for pos in order:
                row = subset.iloc[int(pos)]
                rows.append(clean_record({
                    "query_category": group,
                    "service_id": row["service_id"],
                    "category": row["category"],
                    "subcategory": row["subcategory"],
                    "name": row["name"],
                    "lat": row["lat"],
                    "lon": row["lon"],
                    "air_distance_km": float(distances[int(pos)]),
                    "sector": row.get("sector"),
                    "risk_class": row.get("risk_class"),
                    "confidence": row.get("confidence"),
                    "area_ha": row.get("area_ha"),
                    "product": row.get("product"),
                    "raw_type": row.get("raw_type"),
                }))
        return {"origin": {"lat": lat, "lon": lon}, "method": "air", "candidates": rows}


class IndustrialZoneStore:
    """Mantiene polígonos industriales simplificados y los filtra por bbox."""

    def __init__(self, root: Path):
        self.path = find_first(root, "zonas_industriales_web.geojson.gz") or find_first(root, "zonas_industriales_web.geojson")
        self.available = self.path is not None
        self.features: list[dict[str, Any]] = []
        self.bounds: list[tuple[float, float, float, float]] = []
        if not self.available:
            return
        opener = gzip.open if self.path.suffix.lower() == ".gz" else open
        with opener(self.path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
        for feature in payload.get("features", []):
            try:
                geom = shape(feature.get("geometry"))
                if geom.is_empty:
                    continue
                self.features.append(feature)
                self.bounds.append(tuple(float(x) for x in geom.bounds))
            except Exception:
                continue

    def public_config(self) -> dict[str, Any]:
        return {"available": self.available, "count": len(self.features)}

    def bbox(self, west: float, south: float, east: float, north: float, limit: int = 1000) -> dict[str, Any]:
        if not self.available:
            return {"type": "FeatureCollection", "features": [], "meta": {"available": False}}
        selected: list[dict[str, Any]] = []
        total = 0
        for feature, bounds in zip(self.features, self.bounds):
            minx, miny, maxx, maxy = bounds
            if maxx < west or minx > east or maxy < south or miny > north:
                continue
            total += 1
            if len(selected) < limit:
                selected.append(feature)
        return {
            "type": "FeatureCollection",
            "features": selected,
            "meta": {"available": True, "count": len(selected), "total_in_bbox": total, "truncated": total > limit},
        }




# ---------------------------------------------------------------------------
# Lotes guardados en SQL y evaluación territorial
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_utm_crs(lon: float, lat: float) -> str:
    """Devuelve una zona UTM apropiada para medir lotes pequeños."""
    zone = max(1, min(60, int((float(lon) + 180.0) // 6.0) + 1))
    epsg = 32600 + zone if float(lat) >= 0 else 32700 + zone
    return f"EPSG:{epsg}"


def geometry_area_m2(geom_wgs84) -> float:
    if geom_wgs84 is None or geom_wgs84.is_empty:
        return 0.0
    center = geom_wgs84.centroid
    transformer = Transformer.from_crs(WGS84, local_utm_crs(center.x, center.y), always_xy=True)
    projected = shapely_transform(transformer.transform, geom_wgs84)
    return float(abs(projected.area))


def approximate_lot_polygon(
    lat: float,
    lon: float,
    area_m2: float,
    frontage_m: float | None = None,
    bearing_deg: float = 0.0,
):
    """Crea un rectángulo aproximado centrado en el punto indicado."""
    area = max(float(area_m2), 1.0)
    frontage = safe_float(frontage_m)
    if frontage is None or frontage <= 0:
        frontage = math.sqrt(area)
    depth = area / frontage
    to_local = Transformer.from_crs(WGS84, local_utm_crs(lon, lat), always_xy=True)
    to_wgs = Transformer.from_crs(local_utm_crs(lon, lat), WGS84, always_xy=True)
    x, y = to_local.transform(lon, lat)
    rectangle = box(x - frontage / 2.0, y - depth / 2.0, x + frontage / 2.0, y + depth / 2.0)
    if bearing_deg:
        rectangle = shapely_rotate(rectangle, float(bearing_deg), origin=(x, y), use_radians=False)
    return shapely_transform(to_wgs.transform, rectangle)


def parse_lot_geometry(payload: dict[str, Any]):
    geometry_payload = payload.get("geometry")
    geometry_source = "drawn"
    if geometry_payload:
        geom = shape(geometry_payload)
        if geom.geom_type not in {"Polygon", "MultiPolygon"}:
            raise ValueError("La geometría del lote debe ser un polígono")
        if not geom.is_valid:
            geom = geom.buffer(0)
        if geom.is_empty:
            raise ValueError("El polígono del lote está vacío")
    else:
        lat = safe_float(payload.get("lat"))
        lon = safe_float(payload.get("lon"))
        area_m2 = safe_float(payload.get("area_m2"))
        if lat is None or lon is None:
            raise ValueError("Debe indicar latitud y longitud o dibujar un polígono")
        if area_m2 is None or area_m2 <= 0:
            raise ValueError("Debe indicar una superficie mayor que cero")
        geom = approximate_lot_polygon(
            lat,
            lon,
            area_m2,
            safe_float(payload.get("frontage_m")),
            safe_float(payload.get("bearing_deg"), 0.0) or 0.0,
        )
        geometry_source = "approximate"
    return geom, geometry_source


def normalized_database_url(root: Path) -> str:
    configured = os.environ.get("DATABASE_URL", "").strip()
    if configured:
        if configured.startswith("postgres://"):
            configured = "postgresql+psycopg2://" + configured[len("postgres://"):]
        elif configured.startswith("postgresql://"):
            configured = "postgresql+psycopg2://" + configured[len("postgresql://"):]
        return configured
    db_path = Path(os.environ.get("LOTS_DB_PATH", str(root / "lotes.sqlite3"))).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return "sqlite:///" + db_path.as_posix()


LOTS_METADATA = MetaData()
LOTS_TABLE = Table(
    "lots",
    LOTS_METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(180), nullable=False),
    Column("status", String(40), nullable=False, default="candidate"),
    Column("intended_use", String(40), nullable=False, default="residential"),
    Column("center_lat", Float, nullable=False),
    Column("center_lon", Float, nullable=False),
    Column("area_m2", Float, nullable=False),
    Column("polygon_area_m2", Float, nullable=True),
    Column("frontage_m", Float, nullable=True),
    Column("bearing_deg", Float, nullable=True),
    Column("price_total", Float, nullable=True),
    Column("currency", String(12), nullable=True),
    Column("price_usd", Float, nullable=True),
    Column("source_url", Text, nullable=True),
    Column("notes", Text, nullable=True),
    Column("geometry_json", Text, nullable=False),
    Column("geometry_source", String(24), nullable=False, default="approximate"),
    Column("evaluation_json", Text, nullable=True),
    Column("evaluated_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


class LotStore:
    def __init__(self, root: Path):
        self.url = normalized_database_url(root)
        connect_args = {"check_same_thread": False} if self.url.startswith("sqlite") else {}
        self.engine: Engine = create_engine(self.url, future=True, pool_pre_ping=True, connect_args=connect_args)
        LOTS_METADATA.create_all(self.engine)
        self.write_token = os.environ.get("LOTS_ADMIN_TOKEN", "").strip()

    def public_config(self) -> dict[str, Any]:
        return {
            "available": True,
            "write_protected": bool(self.write_token),
            "database": "postgresql" if self.url.startswith("postgresql") else "sqlite",
            "count": self.count(),
        }

    def count(self) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(LOTS_TABLE)).scalar_one())

    def authorized(self, supplied: str | None) -> bool:
        return not self.write_token or bool(supplied) and str(supplied) == self.write_token

    def _record(self, row) -> dict[str, Any]:
        data = dict(row._mapping if hasattr(row, "_mapping") else row)
        geometry = json.loads(data.pop("geometry_json"))
        evaluation_raw = data.pop("evaluation_json", None)
        evaluation = json.loads(evaluation_raw) if evaluation_raw else None
        for key in ("created_at", "updated_at", "evaluated_at"):
            value = data.get(key)
            if value is not None:
                data[key] = value.isoformat() if hasattr(value, "isoformat") else str(value)
        data["geometry"] = geometry
        data["evaluation"] = evaluation
        area = safe_float(data.get("area_m2")) or safe_float(data.get("polygon_area_m2"))
        price = safe_float(data.get("price_total"))
        data["price_per_m2"] = price / area if price is not None and area and area > 0 else None
        return clean_record(data)

    def list(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(LOTS_TABLE).order_by(LOTS_TABLE.c.updated_at.desc())).all()
        return [self._record(row) for row in rows]

    def get(self, lot_id: int) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(LOTS_TABLE).where(LOTS_TABLE.c.id == int(lot_id))).first()
        return self._record(row) if row else None

    def _values_from_payload(self, payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
        merged = dict(existing or {})
        merged.update(payload or {})
        geom, geometry_source = parse_lot_geometry(merged)
        centroid = geom.centroid
        polygon_area = geometry_area_m2(geom)
        declared_area = safe_float(merged.get("area_m2"))
        if declared_area is None or declared_area <= 0:
            declared_area = polygon_area
        price_total = safe_float(merged.get("price_total"))
        currency = str(merged.get("currency") or "PYG").upper().strip()[:12]
        fx = safe_float(os.environ.get("PYG_PER_USD"))
        price_usd = None
        if price_total is not None:
            if currency == "USD":
                price_usd = price_total
            elif currency == "PYG" and fx and fx > 0:
                price_usd = price_total / fx
        return {
            "name": str(merged.get("name") or "Lote sin nombre").strip()[:180],
            "status": str(merged.get("status") or "candidate").strip()[:40],
            "intended_use": str(merged.get("intended_use") or "residential").strip()[:40],
            "center_lat": float(centroid.y),
            "center_lon": float(centroid.x),
            "area_m2": float(declared_area),
            "polygon_area_m2": float(polygon_area),
            "frontage_m": safe_float(merged.get("frontage_m")),
            "bearing_deg": safe_float(merged.get("bearing_deg"), 0.0),
            "price_total": price_total,
            "currency": currency,
            "price_usd": price_usd,
            "source_url": str(merged.get("source_url") or "").strip() or None,
            "notes": str(merged.get("notes") or "").strip() or None,
            "geometry_json": json.dumps(mapping(geom), ensure_ascii=False, separators=(",", ":")),
            "geometry_source": geometry_source if payload.get("geometry") or not existing else str(existing.get("geometry_source") or geometry_source),
            "updated_at": utc_now(),
        }

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        values = self._values_from_payload(payload)
        values["created_at"] = utc_now()
        with self.engine.begin() as conn:
            result = conn.execute(insert(LOTS_TABLE).values(**values))
            lot_id = int(result.inserted_primary_key[0])
        return self.get(lot_id)

    def update(self, lot_id: int, payload: dict[str, Any]) -> dict[str, Any] | None:
        existing = self.get(lot_id)
        if not existing:
            return None
        values = self._values_from_payload(payload, existing)
        # Se invalida la evaluación cuando cambia la ubicación, superficie o precio.
        values["evaluation_json"] = None
        values["evaluated_at"] = None
        with self.engine.begin() as conn:
            conn.execute(update(LOTS_TABLE).where(LOTS_TABLE.c.id == int(lot_id)).values(**values))
        return self.get(lot_id)

    def delete(self, lot_id: int) -> bool:
        with self.engine.begin() as conn:
            result = conn.execute(delete(LOTS_TABLE).where(LOTS_TABLE.c.id == int(lot_id)))
        return bool(result.rowcount)

    def save_evaluation(self, lot_id: int, evaluation: dict[str, Any]) -> dict[str, Any] | None:
        with self.engine.begin() as conn:
            result = conn.execute(
                update(LOTS_TABLE)
                .where(LOTS_TABLE.c.id == int(lot_id))
                .values(
                    evaluation_json=json.dumps(evaluation, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
                    evaluated_at=utc_now(),
                    updated_at=utc_now(),
                )
            )
        return self.get(lot_id) if result.rowcount else None


def require_lot_write_access() -> Response | None:
    supplied = request.headers.get("X-Lotes-Token") or request.args.get("token")
    if LOTS.authorized(supplied):
        return None
    return jsonify({"error": "Token de administración inválido"}), 401


def raster_stats_for_geometry(path: Path, geom_wgs84) -> dict[str, Any]:
    try:
        with rasterio.open(path) as src:
            transformer = Transformer.from_crs(WGS84, src.crs, always_xy=True)
            geom_src = shapely_transform(transformer.transform, geom_wgs84)
            from rasterio.mask import mask as raster_mask
            arr, _ = raster_mask(src, [mapping(geom_src)], crop=True, filled=False, indexes=1)
            values = arr.compressed().astype("float64") if np.ma.isMaskedArray(arr) else arr[np.isfinite(arr)].astype("float64")
            values = values[np.isfinite(values)]
            if values.size == 0:
                return {"mean": None, "median": None, "p90": None, "valid_pixels": 0}
            return {
                "mean": float(np.mean(values)),
                "median": float(np.median(values)),
                "p90": float(np.percentile(values, 90)),
                "valid_pixels": int(values.size),
            }
    except Exception as exc:
        return {"mean": None, "median": None, "p90": None, "valid_pixels": 0, "error": str(exc)}


def ring_radiance_stats(lat: float, lon: float, inner_km: float, outer_km: float) -> dict[str, Any]:
    utm = local_utm_crs(lon, lat)
    to_local = Transformer.from_crs(WGS84, utm, always_xy=True)
    to_wgs = Transformer.from_crs(utm, WGS84, always_xy=True)
    x, y = to_local.transform(lon, lat)
    outer = Point(x, y).buffer(float(outer_km) * 1000.0, resolution=48)
    if inner_km > 0:
        inner = Point(x, y).buffer(float(inner_km) * 1000.0, resolution=48)
        ring = outer.difference(inner)
    else:
        ring = outer
    ring_wgs = shapely_transform(to_wgs.transform, ring)
    radiance_layers = sorted(
        (m for m in STORE.rasters.values() if m.get("kind") == "radiance"),
        key=lambda m: m.get("year", 0),
    )
    if not radiance_layers:
        return {"inner_km": inner_km, "outer_km": outer_km}
    first = radiance_layers[0]
    last = radiance_layers[-1]
    start = raster_stats_for_geometry(first["path"], ring_wgs)
    end = raster_stats_for_geometry(last["path"], ring_wgs)
    start_mean = safe_float(start.get("mean"))
    end_mean = safe_float(end.get("mean"))
    change_abs = end_mean - start_mean if start_mean is not None and end_mean is not None else None
    change_pct = None
    if change_abs is not None and abs(start_mean) >= 0.05:
        change_pct = 100.0 * change_abs / abs(start_mean)
    return clean_record({
        "inner_km": inner_km,
        "outer_km": outer_km,
        "start_year": first.get("year"),
        "end_year": last.get("year"),
        "start": start,
        "end": end,
        "change_abs": change_abs,
        "change_pct": change_pct,
    })


def nearby_localities(lat: float, lon: float, limit: int = 6, max_km: float = 80.0) -> list[dict[str, Any]]:
    gdf = STORE.get_vector("localidades")
    if gdf.empty:
        return []
    points = gdf.geometry
    distances = haversine_km(lat, lon, points.y.to_numpy(), points.x.to_numpy())
    order = np.argsort(distances)
    rows = []
    for pos in order:
        distance = float(distances[int(pos)])
        if distance > max_km:
            break
        row = gdf.iloc[int(pos)]
        rows.append(clean_record({
            "id_zona": row.get("id_zona"),
            "name": row.get("nombre"),
            "department": row.get("departamento"),
            "distance_km": distance,
            "population": row.get("population"),
            "cagr_pct_anual": row.get("cagr_pct_anual"),
            "cambio_absoluto": row.get("cambio_absoluto"),
            "lat": points.iloc[int(pos)].y,
            "lon": points.iloc[int(pos)].x,
        }))
        if len(rows) >= limit:
            break
    return rows


def district_at_point(lat: float, lon: float) -> dict[str, Any] | None:
    districts = STORE.get_vector("distritos")
    if districts.empty:
        return None
    point = Point(lon, lat)
    try:
        matches = districts[districts.geometry.covers(point)]
    except Exception:
        matches = districts[districts.geometry.intersects(point)]
    if matches.empty:
        return None
    row = matches.iloc[0]
    fields = [
        "id_zona", "nombre", "departamento", "cagr_pct_anual", "cambio_absoluto",
        "radiancia_inicial", "radiancia_final", "service_access_score", "service_undersupply_score",
        "industrial_opportunity_score", "industrial_risk_score", "population_est",
    ]
    return clean_record({key: row.get(key) for key in fields if key in row.index})


def nearest_service_summary(lat: float, lon: float, include_driving: bool) -> dict[str, Any]:
    groups = [g for g in ("hospital", "primary_health", "supermarket", "education", "pharmacy", "factory", "utility_waste", "quarry") if g in SERVICE_GROUPS]
    if include_driving:
        return nearest_driving(lat, lon, groups)
    air = SERVICES.nearest_air(lat, lon, groups, candidates_per_group=1)
    return {"origin": air["origin"], "method": "air", "services": air.get("candidates", [])}


def service_counts(lat: float, lon: float, radii_km: tuple[float, ...] = (5.0, 10.0, 20.0)) -> dict[str, Any]:
    if not SERVICES.available or SERVICES.frame.empty:
        return {}
    distances = haversine_km(lat, lon, SERVICES.frame["lat"].to_numpy(), SERVICES.frame["lon"].to_numpy())
    result: dict[str, Any] = {}
    for group in ("hospital", "primary_health", "supermarket", "education", "pharmacy", "factory", "warehouse", "utility_waste", "quarry"):
        mask = SERVICES.group_mask(group).to_numpy()
        result[group] = {f"within_{int(radius)}km": int(np.sum(mask & (distances <= radius))) for radius in radii_km}
    return result


def nearby_price_comparison(lot: dict[str, Any], radius_km: float = 30.0) -> dict[str, Any] | None:
    price = safe_float(lot.get("price_total"))
    area = safe_float(lot.get("area_m2"))
    currency = str(lot.get("currency") or "").upper()
    if price is None or not area or area <= 0 or not currency:
        return None
    unit_price = price / area
    comparables = []
    for other in LOTS.list():
        if other.get("id") == lot.get("id") or str(other.get("currency") or "").upper() != currency:
            continue
        other_price = safe_float(other.get("price_total")); other_area = safe_float(other.get("area_m2"))
        if other_price is None or not other_area or other_area <= 0:
            continue
        distance = float(haversine_km(
            lot["center_lat"], lot["center_lon"],
            np.asarray([other["center_lat"]]), np.asarray([other["center_lon"]]),
        )[0])
        if distance <= radius_km:
            comparables.append(other_price / other_area)
    if len(comparables) < 3:
        return {"currency": currency, "price_per_m2": unit_price, "comparable_count": len(comparables), "median_nearby": None, "discount_vs_median_pct": None}
    median = float(np.median(comparables))
    discount = 100.0 * (median - unit_price) / median if median > 0 else None
    return clean_record({
        "currency": currency,
        "price_per_m2": unit_price,
        "comparable_count": len(comparables),
        "median_nearby": median,
        "discount_vs_median_pct": discount,
        "radius_km": radius_km,
    })


def clamp_score(value: float | None) -> float | None:
    if value is None or not math.isfinite(float(value)):
        return None
    return float(min(max(float(value), 0.0), 100.0))


def calculate_lot_scores(lot: dict[str, Any], rings: list[dict[str, Any]], services: dict[str, Any], cities: list[dict[str, Any]], price_cmp: dict[str, Any] | None) -> dict[str, Any]:
    outer = rings[-1] if rings else {}
    latest = safe_float((outer.get("end") or {}).get("mean"))
    change_pct = safe_float(outer.get("change_pct"))
    activity = clamp_score(100.0 * (1.0 - math.exp(-max(latest or 0.0, 0.0) / 2.0)))
    growth = clamp_score(50.0 + 50.0 * math.tanh((change_pct or 0.0) / 120.0))

    service_scales = {"hospital": 20.0, "primary_health": 12.0, "supermarket": 10.0, "education": 8.0, "pharmacy": 10.0}
    service_by_group = {str(row.get("query_category")): row for row in services.get("services", [])}
    access_components = []
    for group, scale in service_scales.items():
        row = service_by_group.get(group)
        distance = safe_float((row or {}).get("distance_km"), safe_float((row or {}).get("air_distance_km")))
        if distance is not None:
            access_components.append(100.0 * math.exp(-distance / scale))
    access = clamp_score(float(np.mean(access_components))) if access_components else None

    nearest_city_km = safe_float(cities[0].get("distance_km")) if cities else None
    city_score = clamp_score(100.0 * math.exp(-(nearest_city_km or 80.0) / 18.0)) if nearest_city_km is not None else None

    discount = safe_float((price_cmp or {}).get("discount_vs_median_pct"))
    price_score = clamp_score(50.0 + (discount or 0.0) * 1.5) if discount is not None else None

    industrial_distances = []
    for key in ("factory", "utility_waste", "quarry"):
        row = service_by_group.get(key)
        distance = safe_float((row or {}).get("distance_km"), safe_float((row or {}).get("air_distance_km")))
        if distance is not None:
            industrial_distances.append(distance)
    nearest_industry = min(industrial_distances) if industrial_distances else None
    intended_use = str(lot.get("intended_use") or "residential")
    if nearest_industry is None:
        industry_component = 50.0
    elif intended_use == "industrial":
        industry_component = 100.0 * math.exp(-nearest_industry / 12.0)
    elif intended_use == "commercial":
        industry_component = 50.0 + 35.0 * math.exp(-nearest_industry / 10.0)
    else:
        # Residencial: muy cerca de industria pesada reduce el componente.
        industry_component = 100.0 * (1.0 - math.exp(-nearest_industry / 5.0))
    industry_component = clamp_score(industry_component)

    components = {
        "growth": growth,
        "current_activity": activity,
        "service_access": access,
        "nearby_city": city_score,
        "relative_price": price_score,
        "industry_context": industry_component,
    }
    if intended_use == "industrial":
        weights = {"growth": .20, "current_activity": .10, "service_access": .10, "nearby_city": .10, "relative_price": .10, "industry_context": .40}
    elif intended_use == "commercial":
        weights = {"growth": .25, "current_activity": .20, "service_access": .25, "nearby_city": .10, "relative_price": .10, "industry_context": .10}
    else:
        weights = {"growth": .25, "current_activity": .15, "service_access": .25, "nearby_city": .10, "relative_price": .15, "industry_context": .10}
    available = [(key, val) for key, val in components.items() if val is not None]
    denominator = sum(weights[key] for key, _ in available)
    total = sum(weights[key] * float(val) for key, val in available) / denominator if denominator else None
    return clean_record({
        "screening_score": clamp_score(total),
        "components": components,
        "weights_used": {key: weights[key] for key, _ in available},
        "nearest_industry_km": nearest_industry,
        "intended_use": intended_use,
        "label": "Puntaje preliminar de comparación, no tasación",
    })


def evaluate_lot(lot: dict[str, Any], ring1_km: float = 1.0, ring2_km: float = 5.0, include_driving: bool = True) -> dict[str, Any]:
    ring1 = min(max(float(ring1_km), 0.2), 25.0)
    ring2 = min(max(float(ring2_km), ring1 + 0.2), 80.0)
    lat = float(lot["center_lat"]); lon = float(lot["center_lon"])
    rings = [
        ring_radiance_stats(lat, lon, 0.0, ring1),
        ring_radiance_stats(lat, lon, ring1, ring2),
    ]
    nearest = nearest_service_summary(lat, lon, include_driving)
    cities = nearby_localities(lat, lon)
    district = district_at_point(lat, lon)
    counts = service_counts(lat, lon)
    price_cmp = nearby_price_comparison(lot)
    scores = calculate_lot_scores(lot, rings, nearest, cities, price_cmp)
    return clean_record({
        "lot_id": lot["id"],
        "evaluated_at": utc_now().isoformat(),
        "rings": rings,
        "nearest_services": nearest,
        "service_counts": counts,
        "nearby_localities": cities,
        "district": district,
        "price_comparison": price_cmp,
        "scores": scores,
        "caveats": [
            "La radiancia es un indicador indirecto de actividad y urbanización.",
            "El polígono creado desde un punto y una superficie es aproximado salvo que se dibujen los vértices reales.",
            "La cobertura de servicios e industrias depende de OpenStreetMap y otras fuentes disponibles.",
            "El puntaje sirve para comparar lotes guardados; no sustituye tasación, título, cota, drenaje ni visita en terreno.",
        ],
    })


@lru_cache(maxsize=512)
def osrm_json(path: str, query_string: str) -> dict[str, Any]:
    base = os.environ.get("OSRM_URL", "https://router.project-osrm.org").rstrip("/")
    url = f"{base}{path}?{query_string}"
    req = urllib.request.Request(url, headers={"User-Agent": "LucesParaguayMap/1.0"})
    timeout = float(os.environ.get("OSRM_TIMEOUT", "25"))
    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("code") != "Ok":
        raise RuntimeError(payload.get("message") or payload.get("code") or "OSRM error")
    return payload


def nearest_driving(lat: float, lon: float, groups: list[str]) -> dict[str, Any]:
    air = SERVICES.nearest_air(lat, lon, groups, candidates_per_group=4)
    candidates = air["candidates"]
    if not candidates:
        return {"origin": {"lat": lat, "lon": lon}, "method": "air", "services": []}
    coords = [f"{lon:.7f},{lat:.7f}"] + [f"{float(r['lon']):.7f},{float(r['lat']):.7f}" for r in candidates]
    query = urllib.parse.urlencode({
        "sources": "0",
        "destinations": ";".join(str(i) for i in range(1, len(coords))),
        "annotations": "duration,distance",
    })
    try:
        payload = osrm_json("/table/v1/driving/" + ";".join(coords), query)
        durations = payload.get("durations", [[]])[0]
        distances = payload.get("distances", [[]])[0]
        best: dict[str, dict[str, Any]] = {}
        for row, duration, distance in zip(candidates, durations, distances):
            if duration is None or distance is None:
                continue
            enriched = dict(row)
            enriched["duration_minutes"] = float(duration) / 60.0
            enriched["distance_km"] = float(distance) / 1000.0
            group = str(row["query_category"])
            if group not in best or enriched["duration_minutes"] < best[group]["duration_minutes"]:
                best[group] = enriched
        return {"origin": {"lat": lat, "lon": lon}, "method": "OSRM", "services": [best[g] for g in groups if g in best]}
    except Exception as exc:
        best_air: dict[str, dict[str, Any]] = {}
        for row in candidates:
            group = str(row["query_category"])
            if group not in best_air or float(row["air_distance_km"]) < float(best_air[group]["air_distance_km"]):
                best_air[group] = row
        return {
            "origin": {"lat": lat, "lon": lon},
            "method": "air_fallback",
            "warning": str(exc),
            "services": [best_air[g] for g in groups if g in best_air],
        }


def route_between(from_lat: float, from_lon: float, to_lat: float, to_lon: float) -> dict[str, Any]:
    coords = f"{from_lon:.7f},{from_lat:.7f};{to_lon:.7f},{to_lat:.7f}"
    query = urllib.parse.urlencode({"overview": "full", "geometries": "geojson", "steps": "false", "alternatives": "false"})
    payload = osrm_json("/route/v1/driving/" + coords, query)
    route = payload["routes"][0]
    return {
        "duration_minutes": float(route["duration"]) / 60.0,
        "distance_km": float(route["distance"]) / 1000.0,
        "geometry": route.get("geometry"),
        "snapped": [w.get("location") for w in payload.get("waypoints", [])],
        "method": "OSRM",
    }


DATA_DIR = locate_data_dir()
STORE = NightLightsStore(DATA_DIR)
SERVICES = ServiceStore(DATA_DIR)
INDUSTRIAL_ZONES = IndustrialZoneStore(DATA_DIR)
LOTS = LotStore(DATA_DIR)
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
    return jsonify({
        "status": "ok", "build": APP_BUILD, "data_dir": str(DATA_DIR),
        "rasters": len(STORE.rasters),
        "services": int(SERVICES.frame["category"].ne("industry").sum()) if SERVICES.available else 0,
        "services_and_industries": len(SERVICES.frame) if SERVICES.available else 0,
        "industries": int(SERVICES.frame["category"].eq("industry").sum()) if SERVICES.available else 0,
        "industrial_zones": len(INDUSTRIAL_ZONES.features) if INDUSTRIAL_ZONES.available else 0,
        "lots": LOTS.count(),
        "lots_database": LOTS.public_config().get("database"),
    })


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
            "services": SERVICES.public_config(),
            "industrial_zones": INDUSTRIAL_ZONES.public_config(),
            "lots": LOTS.public_config(),
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


@app.get("/api/services")
def api_services() -> Response:
    if not SERVICES.available:
        return jsonify({"type": "FeatureCollection", "features": [], "meta": {"available": False}})
    try:
        west, south, east, north = [float(x) for x in request.args.get("bbox", "").split(",")]
    except Exception:
        return jsonify({"error": "bbox debe ser west,south,east,north"}), 400
    groups = [g for g in request.args.get("groups", "").split(",") if g in SERVICE_GROUPS]
    limit = min(max(int(request.args.get("limit", "1500")), 1), 3000)
    return jsonify(SERVICES.bbox(west, south, east, north, groups, limit))


@app.get("/api/industrial-zones")
def api_industrial_zones() -> Response:
    if not INDUSTRIAL_ZONES.available:
        return jsonify({"type": "FeatureCollection", "features": [], "meta": {"available": False}})
    try:
        west, south, east, north = [float(x) for x in request.args.get("bbox", "").split(",")]
    except Exception:
        return jsonify({"error": "bbox debe ser west,south,east,north"}), 400
    limit = min(max(int(request.args.get("limit", "1000")), 1), 2000)
    return jsonify(INDUSTRIAL_ZONES.bbox(west, south, east, north, limit))


@app.get("/api/services/nearest")
def api_services_nearest() -> Response:
    try:
        lat = float(request.args["lat"]); lon = float(request.args["lon"])
    except (KeyError, ValueError):
        return jsonify({"error": "lat y lon son obligatorios"}), 400
    groups = [g for g in request.args.get("groups", ",".join(DEFAULT_SERVICE_GROUPS)).split(",") if g in SERVICE_GROUPS]
    payload = SERVICES.nearest_air(lat, lon, groups, candidates_per_group=1)
    payload["services"] = payload.pop("candidates", [])
    return jsonify(payload)


@app.get("/api/services/nearest-driving")
def api_services_nearest_driving() -> Response:
    try:
        lat = float(request.args["lat"]); lon = float(request.args["lon"])
    except (KeyError, ValueError):
        return jsonify({"error": "lat y lon son obligatorios"}), 400
    groups = [g for g in request.args.get("groups", ",".join(DEFAULT_SERVICE_GROUPS)).split(",") if g in SERVICE_GROUPS]
    return jsonify(nearest_driving(lat, lon, groups))


@app.get("/api/route")
def api_route() -> Response:
    try:
        values = {key: float(request.args[key]) for key in ("from_lat", "from_lon", "to_lat", "to_lon")}
    except (KeyError, ValueError):
        return jsonify({"error": "Coordenadas de origen y destino obligatorias"}), 400
    try:
        return jsonify(route_between(**values))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502




@app.get("/api/lots")
def api_lots_list() -> Response:
    return jsonify({"rows": LOTS.list(), "meta": LOTS.public_config()})


@app.post("/api/lots")
def api_lots_create() -> Response:
    denied = require_lot_write_access()
    if denied:
        return denied
    try:
        payload = request.get_json(force=True, silent=False) or {}
        return jsonify(LOTS.create(payload)), 201
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("No se pudo crear lote")
        return jsonify({"error": str(exc)}), 500


@app.put("/api/lots/<int:lot_id>")
def api_lots_update(lot_id: int) -> Response:
    denied = require_lot_write_access()
    if denied:
        return denied
    try:
        payload = request.get_json(force=True, silent=False) or {}
        row = LOTS.update(lot_id, payload)
        if not row:
            abort(404)
        return jsonify(row)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("No se pudo actualizar lote")
        return jsonify({"error": str(exc)}), 500


@app.delete("/api/lots/<int:lot_id>")
def api_lots_delete(lot_id: int) -> Response:
    denied = require_lot_write_access()
    if denied:
        return denied
    if not LOTS.delete(lot_id):
        abort(404)
    return jsonify({"deleted": True, "id": lot_id})


@app.post("/api/lots/<int:lot_id>/evaluate")
def api_lots_evaluate(lot_id: int) -> Response:
    denied = require_lot_write_access()
    if denied:
        return denied
    lot = LOTS.get(lot_id)
    if not lot:
        abort(404)
    payload = request.get_json(silent=True) or {}
    try:
        ring1 = safe_float(payload.get("ring1_km"), 1.0) or 1.0
        ring2 = safe_float(payload.get("ring2_km"), 5.0) or 5.0
        include_driving = bool(payload.get("include_driving", True))
        evaluation = evaluate_lot(lot, ring1, ring2, include_driving)
        saved = LOTS.save_evaluation(lot_id, evaluation)
        return jsonify(saved)
    except Exception as exc:
        app.logger.exception("No se pudo evaluar lote %s", lot_id)
        return jsonify({"error": str(exc)}), 500


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
    #app { height: 100%; position: relative; overflow: hidden; }
    #map { position: absolute; inset: 0; height: 100%; width: 100%; background: #dbe4ec; }
    #menu-toggle {
      position: fixed; top: 12px; left: 12px; z-index: 2300;
      width: 46px; height: 46px; padding: 0; border-radius: 10px;
      display: flex; align-items: center; justify-content: center;
      background: var(--accent); color: #fff; border: 1px solid rgba(255,255,255,.28);
      box-shadow: 0 8px 22px rgba(15,23,42,.22); font-size: 23px; line-height: 1;
    }
    #menu-toggle:hover { filter: brightness(.96); }
    #menu-toggle:focus-visible { outline: 3px solid rgba(255,255,255,.9); outline-offset: 2px; }
    #sidebar {
      position: fixed; top: 68px; left: 12px; z-index: 2200;
      width: min(390px, calc(100vw - 24px)); max-height: calc(100vh - 80px);
      overflow-y: auto; overflow-x: hidden; background: var(--panel);
      border: 1px solid var(--line); border-radius: 14px; padding: 12px;
      box-shadow: 0 14px 34px rgba(15,23,42,.20);
      opacity: 1; visibility: visible; transform: translateY(0);
      transition: opacity .16s ease, transform .16s ease, visibility .16s ease;
    }
    #sidebar.menu-closed { opacity: 0; visibility: hidden; pointer-events: none; transform: translateY(-8px); }
    .sidebar-head { padding: 2px 3px 9px; border-bottom: 1px solid #eef2f5; margin-bottom: 6px; }
    .sidebar-head h1 { font-size: 18px; margin-bottom: 3px; }
    .sidebar-head .subtitle { margin: 5px 0 0; font-size: 11px; }
    .menu-section { border: 0; border-bottom: 1px solid #edf1f5; margin: 0; background: #fff; }
    .menu-section:last-of-type { border-bottom: 0; }
    .menu-section > summary {
      position: relative; list-style: none; cursor: pointer; user-select: none;
      padding: 12px 34px 12px 4px; margin: 0;
      font-size: 13px; font-weight: 800; color: var(--ink);
    }
    .menu-section > summary::-webkit-details-marker { display: none; }
    .menu-section > summary::after {
      content: '⌄'; position: absolute; right: 7px; top: 50%;
      transform: translateY(-54%); color: var(--muted); font-size: 18px; font-weight: 700;
      transition: transform .14s ease;
    }
    .menu-section[open] > summary::after { transform: translateY(-45%) rotate(180deg); }
    .menu-section > summary:hover { color: var(--accent); }
    .menu-section-body { padding: 0 4px 13px; }
    .menu-footnote { padding: 10px 4px 2px; }
    h1 { font-size: 22px; line-height: 1.15; margin: 0 0 6px; letter-spacing: -0.02em; }
    h2 { font-size: 15px; margin: 0 0 10px; }
    .subtitle { color: var(--muted); font-size: 12.5px; line-height: 1.45; margin-bottom: 14px; }
    .card { border: 1px solid var(--line); border-radius: 12px; padding: 13px; margin-bottom: 12px; background: #fff; }
    label { display: block; font-size: 12px; font-weight: 700; margin: 9px 0 5px; }
    select, input[type="text"], input[type="number"], input[type="password"], textarea { width: 100%; border: 1px solid #cfd8e1; border-radius: 8px; padding: 8px 9px; background: #fff; color: var(--ink); }
    input[type="text"]:focus, input[type="number"]:focus, input[type="password"]:focus, textarea:focus, select:focus { outline: 2px solid rgba(30,93,168,.16); border-color: #75a2d1; }
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
    .service-options { display:grid; grid-template-columns:1fr 1fr; gap:5px 8px; margin-top:8px; }
    .service-section-title { grid-column:1 / -1; margin-top:7px; padding-top:7px; border-top:1px solid #eef2f5; color:var(--muted); font-size:10px; font-weight:800; text-transform:uppercase; letter-spacing:.05em; }
    .service-option { display:flex; align-items:center; gap:6px; font-size:11px; }
    .service-option input { width:auto; }
    .service-result { border-top:1px solid #eef2f5; padding:9px 0; }
    .service-result:first-child { border-top:0; }
    .service-result-head { display:flex; align-items:center; min-width:0; }
    .service-result-name { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .service-result-row { display:flex; align-items:center; gap:10px; margin-top:5px; }
    .service-result-meta { flex:1 1 auto; min-width:0; }
    .service-result-route { flex:0 0 auto; margin:0 0 0 auto; padding:5px 9px; font-size:10px; white-space:nowrap; }
    .service-dot { display:inline-block; flex:0 0 auto; width:9px; height:9px; border-radius:50%; margin-right:5px; background:var(--accent); }
    .leaflet-tooltip { font-size: 11px; border-radius: 7px; box-shadow: var(--shadow); }
    .service-tooltip { max-width:260px; line-height:1.35; }
    .leaflet-raster-pane, .leaflet-hotspot-pane, .leaflet-route-pane { pointer-events:none; }
    .leaflet-services-pane { pointer-events:none; }
    .leaflet-services-pane svg { pointer-events:none; }
    .leaflet-services-pane .leaflet-interactive { pointer-events:auto; cursor:pointer; }

    .lots-grid { display:grid; grid-template-columns:1fr 1fr; gap:7px 8px; }
    .lots-grid .full { grid-column:1 / -1; }
    .lot-actions { display:grid; grid-template-columns:1fr 1fr; gap:7px; margin-top:8px; }
    .lot-actions .full { grid-column:1 / -1; }
    .lot-drawing { background:#fff7ed; border:1px solid #fed7aa; color:#9a4d00; border-radius:8px; padding:7px; margin-top:8px; font-size:11px; }
    .lot-list { margin-top:9px; max-height:300px; overflow:auto; border-top:1px solid #eef2f5; }
    .lot-item { padding:9px 0; border-bottom:1px solid #eef2f5; }
    .lot-item-title { display:flex; align-items:center; gap:6px; }
    .lot-item-title strong { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .lot-badge { margin-left:auto; flex:0 0 auto; padding:2px 6px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:9px; font-weight:800; }
    .lot-item-actions { display:flex; flex-wrap:wrap; gap:5px; margin-top:6px; }
    .lot-item-actions button { padding:5px 7px; font-size:9.5px; }
    .lot-evaluation { margin-top:9px; padding-top:9px; border-top:1px solid #eef2f5; }
    .lot-score { font-size:24px; font-weight:800; color:var(--accent); }
    .lot-table { width:100%; border-collapse:collapse; font-size:10.5px; }
    .lot-table td { border-bottom:1px solid #eef2f5; padding:5px 2px; vertical-align:top; }
    .lot-table td:last-child { text-align:right; font-variant-numeric:tabular-nums; }
    .leaflet-lots-pane { pointer-events:none; }
    .leaflet-lots-pane .leaflet-interactive { pointer-events:auto; cursor:pointer; }

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
      #menu-toggle { top: 10px; left: 10px; width: 44px; height: 44px; }
      #sidebar { top: 64px; left: 10px; width: calc(100vw - 20px); max-height: calc(100vh - 74px); padding: 10px; }
      .service-options { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
<div id="app">
  <button id="menu-toggle" type="button" aria-label="Abrir o cerrar menú" aria-expanded="true" title="Abrir/cerrar menú">⚙</button>
  <aside id="sidebar">
    <div class="sidebar-head">
      <h1>{{ title }}</h1>
      <div style="display:inline-block;margin:3px 0 2px;padding:3px 7px;border-radius:999px;background:#e8f4ff;color:#075985;font-size:9px;font-weight:800;letter-spacing:.03em">VERSIÓN {{ build }}</div>
      <div class="subtitle">Luces nocturnas, servicios, industrias y evaluación territorial.</div>
    </div>

    <details class="menu-section" open>
      <summary>Buscar cualquier zona</summary>
      <div class="menu-section-body">
        <div class="search-wrap">
          <input id="search-input" type="text" placeholder="Ej.: San Bernardino, Yby Yaú, Caaguazú…" autocomplete="off">
          <div id="search-results"></div>
        </div>
        <div id="search-selection-actions" class="search-selection-actions">
          <button id="clear-search-selection" type="button" class="secondary">Quitar selección buscada</button>
        </div>
        <div class="small" style="margin-top:7px">Busca departamentos, distritos, ciudades, pueblos y localidades. Usa “Quitar selección” o Esc para volver al hover normal.</div>
      </div>
    </details>

    <details class="menu-section" open>
      <summary>Visualización</summary>
      <div class="menu-section-body">
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
    </details>

    <details class="menu-section" id="lots-card">
      <summary>Lotes guardados y evaluación</summary>
      <div class="menu-section-body">
        <div class="check-row"><input id="show-lots" type="checkbox" checked><span>Mostrar lotes en el mapa</span></div>
        <div class="check-row"><input id="lots-only-mode" type="checkbox"><span>Modo solo lotes</span></div>
        <label for="lots-token">Token de administración</label>
        <input id="lots-token" type="password" placeholder="Solo necesario si LOTS_ADMIN_TOKEN está configurado" autocomplete="off">
        <div class="lots-grid">
          <div class="full"><label for="lot-name">Nombre del lote</label><input id="lot-name" type="text" placeholder="Ej.: Chaco'i – lote 12"></div>
          <div><label for="lot-status">Estado</label><select id="lot-status"><option value="candidate">Candidato</option><option value="interesting">Interesante</option><option value="visited">Visitado</option><option value="negotiating">Negociando</option><option value="bought">Comprado</option><option value="discarded">Descartado</option></select></div>
          <div><label for="lot-use">Uso evaluado</label><select id="lot-use"><option value="residential">Residencial</option><option value="commercial">Comercial</option><option value="industrial">Industrial</option><option value="mixed">Mixto</option></select></div>
          <div><label for="lot-lat">Latitud</label><input id="lot-lat" type="number" step="0.000001" placeholder="-25.2865"></div>
          <div><label for="lot-lon">Longitud</label><input id="lot-lon" type="number" step="0.000001" placeholder="-57.6470"></div>
          <div><label for="lot-area">Superficie m²</label><input id="lot-area" type="number" min="1" step="1" placeholder="360"></div>
          <div><label for="lot-frontage">Frente m (opcional)</label><input id="lot-frontage" type="number" min="0" step="0.1" placeholder="12"></div>
          <div><label for="lot-price">Precio total</label><input id="lot-price" type="number" min="0" step="0.01" placeholder="45000000"></div>
          <div><label for="lot-currency">Moneda</label><select id="lot-currency"><option value="PYG">PYG / guaraníes</option><option value="USD">USD</option></select></div>
          <div><label for="lot-ring1">Anillo 1 km</label><input id="lot-ring1" type="number" min="0.2" max="25" step="0.1" value="1"></div>
          <div><label for="lot-ring2">Anillo 2 km</label><input id="lot-ring2" type="number" min="0.5" max="80" step="0.5" value="5"></div>
          <div class="full"><label for="lot-url">Enlace del aviso</label><input id="lot-url" type="text" placeholder="https://..."></div>
          <div class="full"><label for="lot-notes">Notas</label><textarea id="lot-notes" rows="2" style="width:100%;border:1px solid #cfd8e1;border-radius:8px;padding:8px" placeholder="Título, padrón, cota, acceso, vendedor..."></textarea></div>
        </div>
        <div class="lot-actions">
          <button id="pick-lot-point" type="button" class="secondary">Elegir punto</button>
          <button id="draw-lot-polygon" type="button" class="secondary">Dibujar polígono</button>
          <button id="finish-lot-polygon" type="button" class="secondary">Cerrar polígono</button>
          <button id="clear-lot-draft" type="button" class="secondary">Limpiar dibujo</button>
          <button id="save-lot" type="button" class="full">Guardar lote</button>
          <button id="cancel-lot-edit" type="button" class="secondary full" style="display:none">Cancelar edición</button>
        </div>
        <div id="lot-drawing-status" class="lot-drawing" style="display:none"></div>
        <div id="lots-status" class="status"></div>
        <div id="lots-list" class="lot-list small"></div>
        <div id="lot-evaluation" class="lot-evaluation small">Selecciona “Evaluar” en un lote guardado para comparar radiancia, servicios, ciudades, industrias y precio.</div>
      </div>
    </details>

    <details class="menu-section" id="services-card">
      <summary>Servicios, industrias y tiempos de viaje</summary>
      <div class="menu-section-body">
        <div class="check-row"><input id="show-services" type="checkbox"><span>Mostrar servicios e industrias en el mapa</span></div>
        <div id="service-options" class="service-options"></div>
        <div class="check-row"><input id="show-industrial-zones" type="checkbox"><span>Mostrar polígonos de zonas industriales</span></div>
        <div class="check-row"><input id="driving-times" type="checkbox" checked><span>Calcular tiempos reales en auto al hacer clic</span></div>
        <div class="row" style="margin-top:9px">
          <button id="reload-services" class="secondary" type="button">Actualizar servicios</button>
          <button id="clear-route" class="secondary" type="button">Quitar ruta</button>
        </div>
        <div id="service-status" class="status"></div>
        <div id="service-results" class="small">Haz clic en el mapa para buscar los servicios o industrias seleccionados más cercanos.</div>
      </div>
    </details>

    <details class="menu-section" id="selected-zone-section">
      <summary>Zona seleccionada</summary>
      <div class="menu-section-body">
        <div id="feature-info" class="small">Pasa el cursor sobre una zona para ver sus métricas. Haz clic para cargar su serie anual.</div>
        <div id="chart-wrap"><canvas id="series-chart"></canvas></div>
      </div>
    </details>

    <details class="menu-section" id="pixel-section">
      <summary>Píxel seleccionado</summary>
      <div class="menu-section-body">
        <div id="pixel-info" class="small">Haz clic en el mapa para consultar los valores del píxel VIIRS en esa coordenada.</div>
      </div>
    </details>

    <details class="menu-section" id="ranking-section">
      <summary>Ranking de zonas</summary>
      <div class="menu-section-body"><ol id="top-list"></ol></div>
    </details>

    <div class="small menu-footnote">Los años parciales pueden aparecer en las series, pero el script principal los excluye de los rankings. Las capas de industrias dependen de la cobertura de OpenStreetMap y no constituyen un censo oficial.</div>
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
  adminRenderer: null,
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
  servicesLayer: null,
  servicesRenderer: null,
  industrialZonesLayer: null,
  industrialZoneRequestToken: 0,
  serviceRequestToken: 0,
  serviceMoveTimer: null,
  serviceOrigin: null,
  routeLayer: null,
  routeMarkers: null,
  lotsLayer: null,
  lotsRenderer: null,
  lotsRows: [],
  lotsRequestToken: 0,
  lotPointMode: false,
  lotDrawMode: false,
  lotDraftVertices: [],
  lotDraftLayer: null,
  lotDraftMarkers: null,
  lotEditingId: null,
  lotEditingGeometry: null,
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
    industrialZonesPane: 450,
    servicesPane: 500,
    routePane: 560,
    labelTilePane: 600,
    nameLabelPane: 650,
    lotsPane: 690,
    searchPane: 740,
  };
  for (const [name, zIndex] of Object.entries(panes)) {
    state.map.createPane(name);
    state.map.getPane(name).style.zIndex = String(zIndex);
  }
  // Las capas visuales de pantalla completa nunca deben bloquear el mouse.
  state.map.getPane('rasterPane').style.pointerEvents = 'none';
  state.map.getPane('hotspotPane').style.pointerEvents = 'none';
  state.map.getPane('routePane').style.pointerEvents = 'none';
  state.map.getPane('industrialZonesPane').style.pointerEvents = 'none';
  state.map.getPane('labelTilePane').style.pointerEvents = 'none';
  state.map.getPane('nameLabelPane').style.pointerEvents = 'none';
  // El pane de servicios deja pasar el mouse salvo sobre cada marcador SVG.
  state.map.getPane('servicesPane').style.pointerEvents = 'none';
  state.map.getPane('lotsPane').style.pointerEvents = 'none';
  // El resaltado de una búsqueda debe ser solo visual: no puede bloquear
  // mouseover/click de la capa administrativa que está debajo.
  state.map.getPane('searchPane').style.pointerEvents = 'none';
}

async function init() {
  state.config = await fetchJson('/api/config');
  state.map = L.map('map', {zoomControl:false, preferCanvas:true});
  L.control.zoom({position:'topright'}).addTo(state.map);
  createMapPanes();
  // Un solo renderer administrativo evita canvases superpuestos después de
  // cambiar repetidamente de nivel, departamento o métrica.
  state.adminRenderer = L.canvas({pane:'adminPane', padding:.35, tolerance:8});
  // Los servicios usan SVG: solo los círculos visibles capturan el mouse.
  // Un canvas ocuparía todo el mapa y podría bloquear el hover administrativo.
  state.servicesRenderer = L.svg({pane:'servicesPane', padding:.35});
  state.lotsRenderer = L.svg({pane:'lotsPane', padding:.35});
  state.baseLayers = {
    osm: L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {pane:'basePane', maxZoom:19, attribution:'© OpenStreetMap contributors'}),
    carto: L.tileLayer('https://{s}.basemaps.cartocdn.com/light_nolabels/{z}/{x}/{y}{r}.png', {pane:'basePane', maxZoom:20, attribution:'© OpenStreetMap © CARTO'}),
    dark: L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png', {pane:'basePane', maxZoom:20, attribution:'© OpenStreetMap © CARTO'})
  };
  state.currentBase = state.baseLayers.osm.addTo(state.map);
  state.nameLabelLayer = L.layerGroup().addTo(state.map);
  state.map.fitBounds(state.config.bounds);

  fillSelects();
  fillServiceOptions();
  restoreLotsToken();
  bindEvents();
  updateLabelTiles();
  await Promise.all([loadRaster(), loadAdminLayer(), updateTopList(), loadLots()]);

  state.map.on('click', handleMapClick);
  state.map.on('zoomend', scheduleFeatureLabels);
  state.map.on('moveend', scheduleServicesLoad);
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

function fillServiceOptions() {
  const wrapper = document.getElementById('service-options');
  wrapper.innerHTML = '';
  const cfg = state.config.services || {available:false, groups:[], default_groups:[]};
  if (!cfg.available) {
    document.getElementById('services-card').style.display = 'none';
    return;
  }
  const industryKeys = new Set(['factory','warehouse','industrial_building','power_plant','utility_waste','quarry','industrial_zone']);
  let insertedIndustryTitle = false;
  for (const group of cfg.groups || []) {
    if (industryKeys.has(group.key) && !insertedIndustryTitle) {
      const title = document.createElement('div');
      title.className = 'service-section-title';
      title.textContent = 'Industrias';
      wrapper.appendChild(title);
      insertedIndustryTitle = true;
    }
    const label = document.createElement('label');
    label.className = 'service-option';
    const input = document.createElement('input');
    input.type = 'checkbox'; input.value = group.key; input.className = 'service-category';
    input.checked = (cfg.default_groups || []).includes(group.key);
    const span = document.createElement('span'); span.textContent = group.label;
    label.append(input, span); wrapper.appendChild(label);
  }
  const zoneToggle = document.getElementById('show-industrial-zones');
  if (zoneToggle) zoneToggle.closest('.check-row').style.display = state.config.industrial_zones?.available ? '' : 'none';
}

function selectedServiceGroups() {
  return [...document.querySelectorAll('.service-category:checked')].map(el => el.value);
}

function bindEvents() {
  const menuToggle = document.getElementById('menu-toggle');
  const sidebar = document.getElementById('sidebar');
  const setMenuOpen = open => {
    sidebar.classList.toggle('menu-closed', !open);
    menuToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    menuToggle.title = open ? 'Cerrar menú' : 'Abrir menú';
  };
  menuToggle.addEventListener('click', () => setMenuOpen(sidebar.classList.contains('menu-closed')));

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
    await loadServices();
    await loadIndustrialZones();
    await updateTopList();
  });
  document.getElementById('show-services').addEventListener('change', async () => { await loadServices(); await loadIndustrialZones(); });
  document.getElementById('reload-services').addEventListener('click', async () => { await loadServices(); await loadIndustrialZones(); });
  document.getElementById('clear-route').addEventListener('click', clearRoute);
  document.querySelectorAll('.service-category').forEach(el => el.addEventListener('change', loadServices));
  document.getElementById('show-industrial-zones').addEventListener('change', loadIndustrialZones);
  document.getElementById('show-lots').addEventListener('change', loadLots);
  document.getElementById('lots-only-mode').addEventListener('change', toggleLotsOnlyMode);
  document.getElementById('lots-token').addEventListener('change', saveLotsToken);
  document.getElementById('pick-lot-point').addEventListener('click', startLotPointMode);
  document.getElementById('draw-lot-polygon').addEventListener('click', startLotDrawMode);
  document.getElementById('finish-lot-polygon').addEventListener('click', finishLotPolygon);
  document.getElementById('clear-lot-draft').addEventListener('click', clearLotDraft);
  document.getElementById('save-lot').addEventListener('click', saveLotFromForm);
  document.getElementById('cancel-lot-edit').addEventListener('click', resetLotForm);
  ['lot-lat','lot-lon','lot-area','lot-frontage'].forEach(id => document.getElementById(id).addEventListener('change', updateApproximateLotPreview));

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
  if (!Number.isFinite(value)) return 4;
  const span = Math.abs(domain.max - domain.min) || 1;
  return Math.max(4, Math.min(11, 4.5 + Math.abs(value - domain.min) / span * 5.5));
}

function buildAdminLayer(geojson, layerKey, metric, domain) {
  const canvasRenderer = state.adminRenderer;
  return L.geoJSON(geojson, {
    pane:'adminPane',
    renderer:canvasRenderer,
    interactive:true,
    bubblingMouseEvents:false,
    style: feature => ({
      pane:'adminPane', color:'#52616f', weight:layerKey === 'departamentos' ? 1.3 : .8,
      fillColor:featureColor(feature.properties[metric], domain), fillOpacity:.58,
    }),
    pointToLayer: (feature, latlng) => {
      const value = Number(feature.properties[metric]);
      return L.circleMarker(latlng, {
        pane:'adminPane', renderer:canvasRenderer,
        interactive:true, bubblingMouseEvents:false,
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

    // Limpia cualquier capa administrativa antigua que haya quedado adjunta
    // por cambios rápidos o respuestas asíncronas fuera de orden.
    for (const cachedLayer of state.adminLayerCache.values()) {
      if (cachedLayer !== nextLayer && state.map.hasLayer(cachedLayer)) state.map.removeLayer(cachedLayer);
    }
    if (previous && previous !== nextLayer && state.map.hasLayer(previous)) state.map.removeLayer(previous);
    if (!state.map.hasLayer(nextLayer)) nextLayer.addTo(state.map);
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
    ${(p.service_access_score !== null && p.service_access_score !== undefined) ? `<h2 style="margin-top:12px">Servicios</h2><div class="metrics-grid">
      <div class="metric-box"><span>Acceso</span><strong>${fmt(p.service_access_score)}</strong></div>
      <div class="metric-box"><span>Déficit</span><strong>${fmt(p.service_gap_score)}</strong></div>
      <div class="metric-box"><span>Hospital cercano</span><strong>${fmt(p.hospital_nearest_km)} km</strong></div>
      <div class="metric-box"><span>Supermercado cercano</span><strong>${fmt(p.supermarket_nearest_km)} km</strong></div>
      <div class="metric-box"><span>Población estimada</span><strong>${fmt(p.population ?? p.population_est,0)}</strong></div>
      <div class="metric-box"><span>Hospitales / 10k</span><strong>${fmt(p.hospital_per_10k)}</strong></div>
    </div>` : ''}
    ${(p.industrial_employment_access_score !== null && p.industrial_employment_access_score !== undefined) ? `<h2 style="margin-top:12px">Industrias</h2><div class="metrics-grid">
      <div class="metric-box"><span>Acceso a empleo industrial</span><strong>${fmt(p.industrial_employment_access_score)}</strong></div>
      <div class="metric-box"><span>Exposición potencial</span><strong>${fmt(p.industrial_exposure_score)}</strong></div>
      <div class="metric-box"><span>Fábrica cercana</span><strong>${fmt(p.factory_nearest_km)} km</strong></div>
      <div class="metric-box"><span>Zona industrial cercana</span><strong>${fmt(p.industrial_zone_nearest_km)} km</strong></div>
      <div class="metric-box"><span>Fábricas / sitios</span><strong>${fmt(p.factory_count ?? p.factory_count_10km,0)} / ${fmt(p.productive_site_count ?? p.productive_site_count_10km,0)}</strong></div>
      <div class="metric-box"><span>Área industrial</span><strong>${fmt(p.industrial_zone_area_km2 ?? p.industrial_zone_area_ha_10km,1)} ${p.industrial_zone_area_km2 !== null && p.industrial_zone_area_km2 !== undefined ? 'km²' : 'ha'}</strong></div>
    </div>` : ''}
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

function serviceGroupForFeature(p) {
  if (p.category === 'health') {
    if (['hospital','hospital_major'].includes(p.subcategory)) return 'hospital';
    if (['clinic','health_centre','health_post','usf'].includes(p.subcategory)) return 'primary_health';
  }
  if (p.category === 'industry') return p.subcategory || 'industrial_building';
  return p.category;
}

function serviceMarkerStyle(group) {
  const styles = {
    hospital:['#b91c1c',7], primary_health:['#dc2626',6], education:['#7c3aed',6],
    supermarket:['#15803d',6], pharmacy:['#db2777',6], bank:['#1d4ed8',6], fuel:['#a16207',6],
    police:['#334155',6], fire_station:['#ea580c',6], market:['#0f766e',6],
    factory:['#c2410c',7], warehouse:['#92400e',6], industrial_building:['#b45309',5],
    power_plant:['#ca8a04',7], utility_waste:['#57534e',6], quarry:['#475569',6], industrial_zone:['#ea580c',6]
  };
  return styles[group] || ['#475569',5];
}

function scheduleServicesLoad() {
  clearTimeout(state.serviceMoveTimer);
  state.serviceMoveTimer = setTimeout(() => {
    loadServices();
    loadIndustrialZones();
  }, 180);
}

async function loadServices() {
  // Invalida SIEMPRE las solicitudes anteriores, incluso al apagar la capa,
  // cambiar de zoom o dejar todas las categorías sin marcar.
  const token = ++state.serviceRequestToken;
  if (state.servicesLayer) {
    state.map.removeLayer(state.servicesLayer);
    state.servicesLayer = null;
  }
  if (!state.config.services?.available || !document.getElementById('show-services').checked) {
    document.getElementById('service-status').textContent = '';
    return;
  }
  if (state.map.getZoom() < 8) {
    document.getElementById('service-status').textContent = 'Acércate a zoom 8 o superior para cargar los puntos.';
    return;
  }
  const groups = selectedServiceGroups();
  if (!groups.length) {
    document.getElementById('service-status').textContent = 'Selecciona al menos una categoría.';
    return;
  }
  const b = state.map.getBounds();
  const bbox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].join(',');
  document.getElementById('service-status').textContent = 'Cargando servicios visibles…';
  try {
    const payload = await fetchJson(`/api/services?bbox=${encodeURIComponent(bbox)}&groups=${encodeURIComponent(groups.join(','))}&limit=1500`);
    if (token !== state.serviceRequestToken) return;
    state.servicesLayer = L.geoJSON(payload, {
      pane:'servicesPane',
      renderer:state.servicesRenderer,
      interactive:true,
      bubblingMouseEvents:false,
      pointToLayer:(feature,latlng) => {
        const group = serviceGroupForFeature(feature.properties || {});
        const [color,radius] = serviceMarkerStyle(group);
        return L.circleMarker(latlng, {
          pane:'servicesPane', renderer:state.servicesRenderer,
          interactive:true, bubblingMouseEvents:false,
          radius, color:'#fff', weight:1.2,
          fillColor:color, fillOpacity:.94
        });
      },
      onEachFeature:(feature,layer) => {
        const p = feature.properties || {};
        const group = serviceGroupForFeature(p);
        const groupLabel = (state.config.services?.groups || []).find(x => x.key === group)?.label || group;
        const detail = [groupLabel, p.product || p.sector, p.district, p.department].filter(Boolean).join(' · ');
        const source = p.source ? `<br><span class="small">Fuente: ${escapeHtml(p.source)}${p.confidence ? ` · certeza ${escapeHtml(p.confidence)}` : ''}${Number(p.area_ha) > 0 ? ` · ${fmt(p.area_ha,1)} ha` : ''}</span>` : '';
        const tooltip = `<strong>${escapeHtml(p.name || 'Servicio')}</strong><br><span class="small">${escapeHtml(detail)}</span>`;
        const popup = `<strong>${escapeHtml(p.name || 'Servicio')}</strong><br><span class="small">${escapeHtml(detail)}</span>${source}`;
        layer.bindTooltip(tooltip, {
          sticky:true, direction:'top', opacity:.97,
          pane:'nameLabelPane', className:'service-tooltip'
        });
        layer.bindPopup(popup, {autoPan:true, closeButton:true});

        const baseRadius = typeof layer.getRadius === 'function' ? layer.getRadius() : null;
        layer.on('mouseover', () => {
          if (typeof layer.setStyle === 'function') layer.setStyle({weight:2.2, fillOpacity:1});
          if (baseRadius !== null && typeof layer.setRadius === 'function') layer.setRadius(baseRadius + 1.5);
          if (typeof layer.bringToFront === 'function') layer.bringToFront();
        });
        layer.on('mouseout', () => {
          if (typeof layer.setStyle === 'function') layer.setStyle({weight:1.2, fillOpacity:.94});
          if (baseRadius !== null && typeof layer.setRadius === 'function') layer.setRadius(baseRadius);
        });
        layer.on('click', ev => {
          if (ev?.originalEvent) L.DomEvent.stopPropagation(ev.originalEvent);
          layer.openPopup();
        });
      }
    }).addTo(state.map);
    const meta = payload.meta || {};
    document.getElementById('service-status').textContent = `${meta.count || 0} servicios visibles${meta.truncated ? ' (límite alcanzado; acerca el mapa)' : ''}.`;
  } catch (error) {
    if (token !== state.serviceRequestToken) return;
    document.getElementById('service-status').textContent = `Error cargando servicios: ${error.message}`;
  }
}

function industrialZoneStyle(feature) {
  const type = feature?.properties?.zone_type || 'industrial';
  if (type === 'quarry') return {color:'#475569',weight:1.2,fillColor:'#64748b',fillOpacity:.16,interactive:false};
  if (type === 'landfill') return {color:'#57534e',weight:1.2,fillColor:'#78716c',fillOpacity:.16,interactive:false};
  return {color:'#c2410c',weight:1.3,fillColor:'#f97316',fillOpacity:.12,interactive:false};
}

async function loadIndustrialZones() {
  const token = ++state.industrialZoneRequestToken;
  if (state.industrialZonesLayer) {
    state.map.removeLayer(state.industrialZonesLayer);
    state.industrialZonesLayer = null;
  }
  const enabled = state.config.industrial_zones?.available
    && document.getElementById('show-services').checked
    && document.getElementById('show-industrial-zones').checked;
  if (!enabled || state.map.getZoom() < 7) return;
  const b = state.map.getBounds();
  const bbox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].join(',');
  try {
    const payload = await fetchJson(`/api/industrial-zones?bbox=${encodeURIComponent(bbox)}&limit=1200`);
    if (token !== state.industrialZoneRequestToken) return;
    state.industrialZonesLayer = L.geoJSON(payload, {
      pane:'industrialZonesPane',
      interactive:false,
      style:industrialZoneStyle,
    }).addTo(state.map);
  } catch (error) {
    if (token !== state.industrialZoneRequestToken) return;
    console.warn('No se pudieron cargar zonas industriales', error);
  }
}

async function handleMapClick(ev) {
  if (state.lotPointMode) {
    setLotPoint(ev.latlng);
    return;
  }
  if (state.lotDrawMode) {
    addLotDraftVertex(ev.latlng);
    return;
  }
  queryPixel(ev).catch(console.error);
  if (state.config.services?.available) queryNearestServices(ev.latlng).catch(console.error);
}



function lotAdminHeaders() {
  const token = document.getElementById('lots-token').value.trim();
  return token ? {'X-Lotes-Token':token} : {};
}

function restoreLotsToken() {
  const stored = sessionStorage.getItem('lucesparaguay_lots_token') || '';
  document.getElementById('lots-token').value = stored;
}

function saveLotsToken() {
  sessionStorage.setItem('lucesparaguay_lots_token', document.getElementById('lots-token').value.trim());
}

async function lotFetch(url, options={}) {
  const headers = {'Content-Type':'application/json', ...lotAdminHeaders(), ...(options.headers || {})};
  const response = await fetch(url, {cache:'no-store', ...options, headers});
  let payload = {};
  try { payload = await response.json(); } catch (_) {}
  if (!response.ok) throw new Error(payload.error || `${response.status} ${response.statusText}`);
  return payload;
}

function moneyFmt(value, currency) {
  if (value === null || value === undefined || value === '' || !Number.isFinite(Number(value))) return '—';
  const digits = String(currency).toUpperCase() === 'PYG' ? 0 : 2;
  return `${Number(value).toLocaleString('es-PY',{maximumFractionDigits:digits})} ${escapeHtml(currency || '')}`;
}

function lotStatusLabel(value) {
  return ({candidate:'Candidato',interesting:'Interesante',visited:'Visitado',negotiating:'Negociando',bought:'Comprado',discarded:'Descartado'})[value] || value || 'Candidato';
}

function lotStyle(feature) {
  const p=feature?.properties || {};
  const status=p.status || 'candidate';
  const colors={candidate:'#d97706',interesting:'#2563eb',visited:'#7c3aed',negotiating:'#ea580c',bought:'#15803d',discarded:'#64748b'};
  return {pane:'lotsPane',renderer:state.lotsRenderer,color:colors[status] || '#d97706',weight:2.5,fillColor:colors[status] || '#d97706',fillOpacity:.16,dashArray:p.geometry_source==='approximate'?'7 5':null,interactive:true,bubblingMouseEvents:false};
}

async function loadLots() {
  const token=++state.lotsRequestToken;
  if (state.lotsLayer) { state.map.removeLayer(state.lotsLayer); state.lotsLayer=null; }
  if (!document.getElementById('show-lots').checked) { renderLotsList([]); return; }
  try {
    const payload=await lotFetch('/api/lots',{method:'GET',headers:{}});
    if (token!==state.lotsRequestToken) return;
    state.lotsRows=payload.rows || [];
    const geo={type:'FeatureCollection',features:state.lotsRows.map(row=>({type:'Feature',geometry:row.geometry,properties:{...row,geometry:undefined,evaluation:undefined,screening_score:row.evaluation?.scores?.screening_score}}))};
    state.lotsLayer=L.geoJSON(geo,{
      pane:'lotsPane',renderer:state.lotsRenderer,style:lotStyle,
      onEachFeature:(feature,layer)=>{
        const p=feature.properties || {};
        const score=Number(p.screening_score);
        const price=p.price_total?moneyFmt(p.price_total,p.currency):'Sin precio';
        const tooltip=`<strong>${escapeHtml(p.name || 'Lote')}</strong><br>${fmt(p.area_m2,0)} m² · ${price}${Number.isFinite(score)?`<br>Puntaje: ${fmt(score,1)}/100`:''}`;
        layer.bindTooltip(tooltip,{sticky:true,direction:'top',pane:'lotsPane',className:'service-tooltip'});
        layer.on('mouseover',()=>{layer.setStyle({weight:4,fillOpacity:.24}); if(layer.bringToFront) layer.bringToFront();});
        layer.on('mouseout',()=>layer.setStyle(lotStyle(feature)));
        layer.on('click',ev=>{if(ev?.originalEvent)L.DomEvent.stopPropagation(ev.originalEvent); showLotEvaluation(p.id);});
      }
    }).addTo(state.map);
    renderLotsList(state.lotsRows);
    document.getElementById('lots-status').textContent=`${state.lotsRows.length} lotes guardados · base ${payload.meta?.database || ''}`;
  } catch(error) {
    document.getElementById('lots-status').textContent=`Error cargando lotes: ${error.message}`;
  }
}

function renderLotsList(rows) {
  const container=document.getElementById('lots-list');
  container.innerHTML='';
  if (!rows.length) { container.innerHTML='<div style="padding:9px 0">Todavía no hay lotes guardados.</div>'; return; }
  rows.forEach(lot=>{
    const item=document.createElement('div'); item.className='lot-item';
    const unit=Number(lot.price_per_m2);
    item.innerHTML=`<div class="lot-item-title"><strong>${escapeHtml(lot.name)}</strong><span class="lot-badge">${escapeHtml(lotStatusLabel(lot.status))}</span></div><div class="small">${fmt(lot.area_m2,0)} m² · ${lot.price_total?moneyFmt(lot.price_total,lot.currency):'sin precio'}${Number.isFinite(unit)?` · ${moneyFmt(unit,lot.currency)}/m²`:''}</div>`;
    const actions=document.createElement('div'); actions.className='lot-item-actions';
    const buttons=[['Ubicar',()=>locateLot(lot)],['Evaluar',()=>evaluateLot(lot.id)],['Editar',()=>editLot(lot)],['Eliminar',()=>deleteLot(lot.id)]];
    buttons.forEach(([label,fn])=>{const b=document.createElement('button');b.type='button';b.className=label==='Eliminar'?'secondary':'';b.textContent=label;b.addEventListener('click',fn);actions.appendChild(b);});
    item.appendChild(actions); container.appendChild(item);
  });
}

function locateLot(lot) {
  const layer=[...(state.lotsLayer?Object.values(state.lotsLayer._layers):[])].find(l=>Number(l.feature?.properties?.id)===Number(lot.id));
  if (layer && layer.getBounds) state.map.fitBounds(layer.getBounds(),{padding:[35,35],maxZoom:18});
  else state.map.setView([lot.center_lat,lot.center_lon],17);
}

function setLotPoint(latlng) {
  document.getElementById('lot-lat').value=Number(latlng.lat).toFixed(6);
  document.getElementById('lot-lon').value=Number(latlng.lng).toFixed(6);
  state.lotPointMode=false;
  document.getElementById('lot-drawing-status').style.display='none';
  updateApproximateLotPreview();
}

function startLotPointMode() {
  state.lotPointMode=true; state.lotDrawMode=false;
  document.getElementById('lot-drawing-status').style.display='block';
  document.getElementById('lot-drawing-status').textContent='Haz clic en el centro aproximado del lote.';
}

function startLotDrawMode() {
  clearLotDraft();
  state.lotDrawMode=true; state.lotPointMode=false;
  document.getElementById('lot-drawing-status').style.display='block';
  document.getElementById('lot-drawing-status').textContent='Haz clic en cada vértice del lote y luego presiona “Cerrar polígono”.';
}

function addLotDraftVertex(latlng) {
  state.lotDraftVertices.push([latlng.lng,latlng.lat]);
  redrawLotDraft();
  document.getElementById('lot-drawing-status').textContent=`${state.lotDraftVertices.length} vértices. Agrega más o cierra el polígono.`;
}

function redrawLotDraft(closed=false) {
  if (state.lotDraftLayer) state.map.removeLayer(state.lotDraftLayer);
  if (!state.lotDraftVertices.length) { state.lotDraftLayer=null; return; }
  const latlngs=state.lotDraftVertices.map(([lon,lat])=>[lat,lon]);
  state.lotDraftLayer=closed && latlngs.length>=3
    ? L.polygon(latlngs,{pane:'lotsPane',color:'#f59e0b',weight:3,fillOpacity:.18,dashArray:'6 4',interactive:false}).addTo(state.map)
    : L.polyline(latlngs,{pane:'lotsPane',color:'#f59e0b',weight:3,dashArray:'6 4',interactive:false}).addTo(state.map);
}

function finishLotPolygon() {
  if (state.lotDraftVertices.length<3) { document.getElementById('lots-status').textContent='Se necesitan al menos 3 vértices.'; return; }
  state.lotDrawMode=false;
  redrawLotDraft(true);
  const lat=state.lotDraftVertices.reduce((a,v)=>a+v[1],0)/state.lotDraftVertices.length;
  const lon=state.lotDraftVertices.reduce((a,v)=>a+v[0],0)/state.lotDraftVertices.length;
  document.getElementById('lot-lat').value=lat.toFixed(6); document.getElementById('lot-lon').value=lon.toFixed(6);
  document.getElementById('lot-drawing-status').textContent='Polígono listo. La superficie real se calculará al guardar.';
}

function clearLotDraft() {
  state.lotPointMode=false; state.lotDrawMode=false; state.lotDraftVertices=[];
  if (state.lotDraftLayer) state.map.removeLayer(state.lotDraftLayer);
  state.lotDraftLayer=null;
  document.getElementById('lot-drawing-status').style.display='none';
}

function updateApproximateLotPreview() {
  clearLotDraft();
  const lat=Number(document.getElementById('lot-lat').value),lon=Number(document.getElementById('lot-lon').value),area=Number(document.getElementById('lot-area').value);
  if (!Number.isFinite(lat)||!Number.isFinite(lon)||!Number.isFinite(area)||area<=0) return;
  const frontage=Number(document.getElementById('lot-frontage').value)>0?Number(document.getElementById('lot-frontage').value):Math.sqrt(area);
  const depth=area/frontage, latDelta=(depth/2)/111320, lonDelta=(frontage/2)/(111320*Math.cos(lat*Math.PI/180));
  state.lotDraftLayer=L.rectangle([[lat-latDelta,lon-lonDelta],[lat+latDelta,lon+lonDelta]],{pane:'lotsPane',color:'#f59e0b',weight:3,fillOpacity:.12,dashArray:'6 4',interactive:false}).addTo(state.map);
}

function lotPayloadFromForm() {
  const geometry=state.lotDraftVertices.length>=3?{type:'Polygon',coordinates:[[...state.lotDraftVertices,state.lotDraftVertices[0]]]}:state.lotEditingGeometry;
  return {
    name:document.getElementById('lot-name').value.trim() || 'Lote sin nombre',
    status:document.getElementById('lot-status').value,
    intended_use:document.getElementById('lot-use').value,
    lat:Number(document.getElementById('lot-lat').value),lon:Number(document.getElementById('lot-lon').value),
    area_m2:Number(document.getElementById('lot-area').value),frontage_m:Number(document.getElementById('lot-frontage').value)||null,
    price_total:Number(document.getElementById('lot-price').value)||null,currency:document.getElementById('lot-currency').value,
    source_url:document.getElementById('lot-url').value.trim(),notes:document.getElementById('lot-notes').value.trim(),geometry
  };
}

async function saveLotFromForm() {
  saveLotsToken();
  const payload=lotPayloadFromForm();
  document.getElementById('lots-status').textContent='Guardando lote…';
  try {
    const url=state.lotEditingId?`/api/lots/${state.lotEditingId}`:'/api/lots';
    const method=state.lotEditingId?'PUT':'POST';
    await lotFetch(url,{method,body:JSON.stringify(payload)});
    resetLotForm(); await loadLots();
    document.getElementById('lots-status').textContent='Lote guardado correctamente.';
  } catch(error) { document.getElementById('lots-status').textContent=`No se pudo guardar: ${error.message}`; }
}

function editLot(lot) {
  state.lotEditingId=lot.id; state.lotEditingGeometry=lot.geometry;
  document.getElementById('lot-name').value=lot.name||'';document.getElementById('lot-status').value=lot.status||'candidate';document.getElementById('lot-use').value=lot.intended_use||'residential';
  document.getElementById('lot-lat').value=lot.center_lat;document.getElementById('lot-lon').value=lot.center_lon;document.getElementById('lot-area').value=lot.area_m2||'';document.getElementById('lot-frontage').value=lot.frontage_m||'';
  document.getElementById('lot-price').value=lot.price_total||'';document.getElementById('lot-currency').value=lot.currency||'PYG';document.getElementById('lot-url').value=lot.source_url||'';document.getElementById('lot-notes').value=lot.notes||'';
  document.getElementById('save-lot').textContent='Guardar cambios';document.getElementById('cancel-lot-edit').style.display='block';locateLot(lot);
}

function resetLotForm() {
  state.lotEditingId=null; state.lotEditingGeometry=null;clearLotDraft();
  ['lot-name','lot-lat','lot-lon','lot-area','lot-frontage','lot-price','lot-url','lot-notes'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('lot-status').value='candidate';document.getElementById('lot-use').value='residential';document.getElementById('lot-currency').value='PYG';
  document.getElementById('save-lot').textContent='Guardar lote';document.getElementById('cancel-lot-edit').style.display='none';
}

async function deleteLot(id) {
  if (!confirm('¿Eliminar este lote guardado?')) return;
  try { await lotFetch(`/api/lots/${id}`,{method:'DELETE'}); await loadLots(); document.getElementById('lot-evaluation').textContent='Lote eliminado.'; }
  catch(error){ document.getElementById('lots-status').textContent=`No se pudo eliminar: ${error.message}`; }
}

async function evaluateLot(id) {
  saveLotsToken(); document.getElementById('lot-evaluation').innerHTML='Evaluando radiancia, servicios, ciudades, industrias y precio…';
  const payload={ring1_km:Number(document.getElementById('lot-ring1').value)||1,ring2_km:Number(document.getElementById('lot-ring2').value)||5,include_driving:document.getElementById('driving-times').checked};
  try { const lot=await lotFetch(`/api/lots/${id}/evaluate`,{method:'POST',body:JSON.stringify(payload)}); await loadLots(); renderLotEvaluation(lot); }
  catch(error){ document.getElementById('lot-evaluation').textContent=`No se pudo evaluar: ${error.message}`; }
}

function showLotEvaluation(id) {
  const lot=state.lotsRows.find(x=>Number(x.id)===Number(id));
  if (!lot) return;
  if (lot.evaluation) renderLotEvaluation(lot); else document.getElementById('lot-evaluation').innerHTML=`<strong>${escapeHtml(lot.name)}</strong><br>Aún no evaluado. Usa el botón “Evaluar”.`;
}

function renderLotEvaluation(lot) {
  const e=lot.evaluation || {}, score=e.scores?.screening_score, rings=e.rings || [], nearest=e.nearest_services?.services || [], cities=e.nearby_localities || [], price=e.price_comparison;
  const ringRows=rings.map(r=>`<tr><td>${fmt(r.inner_km,1)}–${fmt(r.outer_km,1)} km</td><td>${fmt(r.end?.mean,3)}</td><td>${fmt(r.change_pct,1)}%</td></tr>`).join('');
  const serviceRows=nearest.slice(0,8).map(r=>`<tr><td>${escapeHtml(r.query_category||'')}</td><td>${r.duration_minutes!==undefined?`${fmt(r.duration_minutes,1)} min · `:''}${fmt(r.distance_km??r.air_distance_km,2)} km</td></tr>`).join('');
  const cityRows=cities.slice(0,5).map(r=>`<tr><td>${escapeHtml(r.name||'')}</td><td>${fmt(r.distance_km,1)} km</td></tr>`).join('');
  document.getElementById('lot-evaluation').innerHTML=`
    <div class="info-title">${escapeHtml(lot.name)}</div>
    <div class="lot-score">${Number.isFinite(Number(score))?`${fmt(score,1)}/100`:'Sin puntaje'}</div>
    <div class="info-sub">Preevaluación comparativa · ${fmt(lot.area_m2,0)} m² · ${lot.price_total?moneyFmt(lot.price_total,lot.currency):'sin precio'}</div>
    <strong>Radiancia por anillo</strong><table class="lot-table"><tr><td>Anillo</td><td>Actual / cambio</td></tr>${ringRows}</table>
    <strong style="display:block;margin-top:8px">Servicios e industrias más cercanos</strong><table class="lot-table">${serviceRows}</table>
    <strong style="display:block;margin-top:8px">Localidades cercanas</strong><table class="lot-table">${cityRows}</table>
    ${price?`<div style="margin-top:8px"><strong>Precio:</strong> ${moneyFmt(price.price_per_m2,price.currency)}/m² · ${price.median_nearby!==null?`mediana cercana ${moneyFmt(price.median_nearby,price.currency)}/m² · descuento ${fmt(price.discount_vs_median_pct,1)}%`:`${price.comparable_count||0} comparables; aún insuficientes`}</div>`:''}
    <div class="warning">${escapeHtml((e.caveats||[]).slice(-1)[0]||'Este análisis no sustituye una tasación ni revisión legal.')}</div>`;
}

function toggleLotsOnlyMode() {
  const only=document.getElementById('lots-only-mode').checked;
  ['rasterPane','hotspotPane','adminPane','industrialZonesPane','servicesPane','routePane','labelTilePane','nameLabelPane','searchPane'].forEach(name=>{const pane=state.map.getPane(name);if(pane)pane.style.display=only?'none':'';});
  if (only && state.lotsRows.length) {
    const bounds=state.lotsLayer?.getBounds(); if(bounds?.isValid()) state.map.fitBounds(bounds,{padding:[35,35]});
  }
}


async function queryNearestServices(latlng) {
  const groups=selectedServiceGroups();
  if (!groups.length) return;
  state.serviceOrigin={lat:latlng.lat,lon:latlng.lng};
  document.getElementById('service-results').innerHTML='Buscando servicios cercanos…';
  const endpoint=document.getElementById('driving-times').checked ? 'nearest-driving' : 'nearest';
  try {
    const payload=await fetchJson(`/api/services/${endpoint}?lat=${latlng.lat}&lon=${latlng.lng}&groups=${encodeURIComponent(groups.join(','))}`);
    renderNearestServices(payload);
  } catch(error) {
    document.getElementById('service-results').innerHTML=`No se pudo consultar: ${escapeHtml(error.message)}`;
  }
}

function renderNearestServices(payload) {
  const container = document.getElementById('service-results');
  const rows = payload.services || [];
  if (!rows.length) {
    container.textContent = 'No se encontraron servicios para las categorías seleccionadas.';
    return;
  }
  container.innerHTML = `<div class="info-sub">Origen: ${fmt(payload.origin.lat,5)}, ${fmt(payload.origin.lon,5)} · ${escapeHtml(payload.method || '')}</div>`;
  rows.forEach(row => {
    const item = document.createElement('div');
    item.className = 'service-result';
    const distance = row.distance_km ?? row.air_distance_km;
    const travel = row.duration_minutes !== undefined ? `${fmt(row.duration_minutes,1)} min · ` : '';

    const head = document.createElement('div');
    head.className = 'service-result-head';
    head.innerHTML = `<span class="service-dot"></span><strong class="service-result-name">${escapeHtml(row.name || row.query_category)}</strong>`;

    const resultRow = document.createElement('div');
    resultRow.className = 'service-result-row';
    const meta = document.createElement('span');
    meta.className = 'service-result-meta';
    const extra = [row.product || row.sector, row.risk_class ? `riesgo ${row.risk_class}` : ''].filter(Boolean).join(' · ');
    meta.innerHTML = `${travel}${fmt(distance,2)} km <span class="small">(${escapeHtml(row.query_category || '')})${extra ? ` · ${escapeHtml(extra)}` : ''}</span>`;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'service-result-route';
    button.textContent = 'Dibujar ruta';
    button.addEventListener('click', () => drawRouteTo(row));
    resultRow.append(meta, button);
    item.append(head, resultRow);
    container.appendChild(item);
  });
  if (payload.warning) {
    const warning = document.createElement('div');
    warning.className = 'warning';
    warning.textContent = 'OSRM no respondió; se muestran distancias en línea recta.';
    container.appendChild(warning);
  }
}

async function drawRouteTo(service) {
  if (!state.serviceOrigin) return;
  document.getElementById('service-status').textContent='Calculando ruta…';
  const p=new URLSearchParams({from_lat:state.serviceOrigin.lat,from_lon:state.serviceOrigin.lon,to_lat:service.lat,to_lon:service.lon});
  try {
    const route=await fetchJson(`/api/route?${p}`);
    clearRoute();
    const latlngs=(route.geometry?.coordinates||[]).map(c=>[c[1],c[0]]);
    state.routeLayer=L.polyline(latlngs,{pane:'routePane',weight:5,opacity:.9,interactive:false}).addTo(state.map);
    state.routeMarkers=L.layerGroup([
      L.circleMarker([state.serviceOrigin.lat,state.serviceOrigin.lon],{pane:'routePane',interactive:false,radius:7,color:'#fff',weight:2,fillColor:'#2563eb',fillOpacity:1}),
      L.circleMarker([service.lat,service.lon],{pane:'routePane',interactive:false,radius:7,color:'#fff',weight:2,fillColor:'#dc2626',fillOpacity:1})
    ]).addTo(state.map);
    if (state.routeLayer.getBounds().isValid()) state.map.fitBounds(state.routeLayer.getBounds(),{padding:[30,30]});
    document.getElementById('service-status').textContent=`Ruta: ${fmt(route.distance_km,2)} km · ${fmt(route.duration_minutes,1)} min.`;
  } catch(error) {
    document.getElementById('service-status').textContent=`No se pudo calcular la ruta: ${error.message}`;
  }
}

function clearRoute() {
  if (state.routeLayer) { state.map.removeLayer(state.routeLayer); state.routeLayer=null; }
  if (state.routeMarkers) { state.map.removeLayer(state.routeMarkers); state.routeMarkers=null; }
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
