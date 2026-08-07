#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Luces Paraguay — Google Maps frontend
=====================================

This file deliberately REUSES the existing app.py backend. Upload it next to the
current app.py and start Gunicorn with:

    gunicorn app_google_maps:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 180

Required environment variable:
    GOOGLE_MAPS_BROWSER_KEY   Browser-restricted key with Maps JavaScript API.

Optional environment variable:
    GOOGLE_PLACES_SERVER_KEY  Server-restricted key with Places API (New).
                              If absent, lot evaluation keeps the existing local
                              OSM service dataset while Google POIs still appear
                              natively on the map.

The existing data files, SQL lots, VIIRS raster endpoints, search, rankings,
industries and evaluation logic are preserved from app.py.
"""

from __future__ import annotations

import json
import math
import os
import urllib.request
from functools import lru_cache
from typing import Any

import numpy as np
from flask import jsonify, render_template_string

import app as legacy


# Reuse the full existing Flask app/back end instead of duplicating it.
app = legacy.app
legacy.APP_BUILD = "2026-08-07-GMAPS-R1"
APP_BUILD = legacy.APP_BUILD
APP_TITLE = legacy.APP_TITLE

GOOGLE_MAPS_BROWSER_KEY = os.environ.get("GOOGLE_MAPS_BROWSER_KEY", "").strip()
GOOGLE_PLACES_SERVER_KEY = os.environ.get("GOOGLE_PLACES_SERVER_KEY", "").strip()

# Google Places types used only for OPTIONAL server-side lot evaluation.
GOOGLE_PLACE_TYPES: dict[str, list[str]] = {
    "hospital": ["hospital", "general_hospital", "medical_center"],
    "primary_health": ["medical_clinic", "doctor"],
    "supermarket": ["supermarket", "grocery_store", "hypermarket"],
    "education": ["school", "primary_school", "secondary_school", "educational_institution"],
    "pharmacy": ["pharmacy", "drugstore"],
    "bank": ["bank", "atm"],
    "fuel": ["gas_station"],
    "police": ["police"],
    "fire_station": ["fire_station"],
    "market": ["market", "farmers_market"],
}


def _haversine_one(lat: float, lon: float, other_lat: float, other_lon: float) -> float:
    return float(
        legacy.haversine_km(
            lat,
            lon,
            np.asarray([other_lat], dtype="float64"),
            np.asarray([other_lon], dtype="float64"),
        )[0]
    )


@lru_cache(maxsize=1024)
def _google_nearby_cached(
    lat_round: float,
    lon_round: float,
    group: str,
    radius_m: int,
    max_results: int,
) -> tuple[dict[str, Any], ...]:
    """Nearby Search (New). Cache is process-local and intentionally short-lived with deploys."""
    if not GOOGLE_PLACES_SERVER_KEY or group not in GOOGLE_PLACE_TYPES:
        return tuple()

    url = "https://places.googleapis.com/v1/places:searchNearby"
    body = {
        "includedTypes": GOOGLE_PLACE_TYPES[group],
        "maxResultCount": max(1, min(int(max_results), 20)),
        "rankPreference": "DISTANCE",
        "locationRestriction": {
            "circle": {
                "center": {"latitude": float(lat_round), "longitude": float(lon_round)},
                "radius": float(max(100, min(int(radius_m), 50000))),
            }
        },
        "languageCode": "es",
        "regionCode": "PY",
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": GOOGLE_PLACES_SERVER_KEY,
            "X-Goog-FieldMask": "places.id,places.displayName,places.location,places.primaryType,places.googleMapsUri",
            "User-Agent": "LucesParaguay/GooglePlaces-R1",
        },
    )
    timeout = float(os.environ.get("GOOGLE_PLACES_TIMEOUT", "12"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        legacy.app.logger.warning("Google Places Nearby failed for %s: %s", group, exc)
        return tuple()

    rows: list[dict[str, Any]] = []
    for place in payload.get("places", []) or []:
        loc = place.get("location") or {}
        plat = legacy.safe_float(loc.get("latitude"))
        plon = legacy.safe_float(loc.get("longitude"))
        if plat is None or plon is None:
            continue
        display = place.get("displayName") or {}
        rows.append(
            {
                "query_category": group,
                "service_id": place.get("id"),
                "category": group,
                "subcategory": place.get("primaryType") or group,
                "name": display.get("text") or place.get("primaryType") or group,
                "lat": plat,
                "lon": plon,
                "air_distance_km": _haversine_one(lat_round, lon_round, plat, plon),
                "distance_km": _haversine_one(lat_round, lon_round, plat, plon),
                "google_maps_uri": place.get("googleMapsUri"),
                "source": "Google Places",
                "confidence": "google",
            }
        )
    rows.sort(key=lambda row: row.get("air_distance_km", 1e9))
    return tuple(rows)


def google_nearby(lat: float, lon: float, group: str, radius_m: int = 50000, max_results: int = 4) -> list[dict[str, Any]]:
    # ~11 m cache cell. This prevents tiny coordinate differences from repeating billed queries.
    lat_round = round(float(lat), 4)
    lon_round = round(float(lon), 4)
    return [dict(row) for row in _google_nearby_cached(lat_round, lon_round, group, radius_m, max_results)]


# Preserve the old functions so the fallback remains exactly as in app.py.
_original_nearest_service_summary = legacy.nearest_service_summary
_original_evaluate_lot = legacy.evaluate_lot


def google_aware_nearest_service_summary(lat: float, lon: float, include_driving: bool) -> dict[str, Any]:
    """Google for consumer/public services; existing local data for industry and fallback."""
    if not GOOGLE_PLACES_SERVER_KEY:
        return _original_nearest_service_summary(lat, lon, include_driving)

    public_groups = ["hospital", "primary_health", "supermarket", "education", "pharmacy"]
    google_rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for group in public_groups:
        candidates = google_nearby(lat, lon, group, radius_m=50000, max_results=4 if include_driving else 1)
        if candidates:
            # The first candidate is closest by Google's DISTANCE rank. Keep one for scoring.
            google_rows.append(candidates[0])
        else:
            missing.append(group)

    # Heavy-industry context still comes from the existing curated OSM extraction.
    industrial_groups = [g for g in ("factory", "utility_waste", "quarry") if g in legacy.SERVICE_GROUPS]
    local_groups = missing + industrial_groups
    local_payload = (
        legacy.nearest_driving(lat, lon, local_groups)
        if include_driving and local_groups
        else legacy.SERVICES.nearest_air(lat, lon, local_groups, candidates_per_group=1)
    )
    local_rows = local_payload.get("services") or local_payload.get("candidates") or []

    return {
        "origin": {"lat": lat, "lon": lon},
        "method": "Google Places + local industry" if google_rows else local_payload.get("method", "local"),
        "services": google_rows + local_rows,
        "google_places_enabled": True,
        "google_groups": [row["query_category"] for row in google_rows],
        "local_fallback_groups": local_groups,
    }


def google_aware_evaluate_lot(*args, **kwargs):
    result = _original_evaluate_lot(*args, **kwargs)
    if GOOGLE_PLACES_SERVER_KEY:
        caveats = list(result.get("caveats") or [])
        caveats = [c for c in caveats if "cobertura de servicios" not in c.lower()]
        caveats.insert(
            max(0, len(caveats) - 1),
            "Los servicios públicos cercanos se consultan con Google Places cuando está disponible; industrias y conteos históricos conservan la base local existente.",
        )
        result["caveats"] = caveats
        result["service_source"] = "google_places_with_local_fallback"
    else:
        result["service_source"] = "local_osm_fallback"
    return result


legacy.nearest_service_summary = google_aware_nearest_service_summary
legacy.evaluate_lot = google_aware_evaluate_lot


@app.get("/api/google-status")
def google_status():
    return jsonify(
        {
            "maps_browser_key_configured": bool(GOOGLE_MAPS_BROWSER_KEY),
            "places_server_key_configured": bool(GOOGLE_PLACES_SERVER_KEY),
            "mode": "google_places" if GOOGLE_PLACES_SERVER_KEY else "google_map_pois_with_local_evaluation",
        }
    )


GOOGLE_INDEX_HTML = r"""
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }}</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
  <style>
    :root{--bg:#f4f6f8;--panel:#fff;--ink:#17202a;--muted:#64748b;--line:#dce3e9;--accent:#1e5da8;--accent-soft:#eaf2fb;--danger:#a83232;--shadow:0 8px 24px rgba(15,23,42,.12)}
    *{box-sizing:border-box} html,body{height:100%;margin:0;font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;color:var(--ink);background:var(--bg)}
    #app{height:100dvh;display:grid;grid-template-columns:380px minmax(0,1fr);transition:grid-template-columns .22s ease} #sidebar{height:100dvh;overflow-y:auto;overflow-x:hidden;background:#fff;border-right:1px solid var(--line);padding:18px 18px 46px;z-index:10;scrollbar-gutter:stable} #map{height:100dvh;width:100%;min-width:0;background:#dbe4ec}
    body.sidebar-collapsed #app{grid-template-columns:0 minmax(0,1fr)} body.sidebar-collapsed #sidebar{opacity:0;pointer-events:none;padding-left:0;padding-right:0;border:0}
    #menu-toggle{position:fixed;top:16px;left:14px;z-index:1000;width:42px;height:42px;padding:0;display:none;align-items:center;justify-content:center;border-radius:10px;background:var(--accent);color:#fff;box-shadow:var(--shadow);font-size:20px} body.sidebar-collapsed #menu-toggle{display:flex}
    .sidebar-heading{display:flex;align-items:flex-start;gap:10px}.sidebar-heading h1{flex:1;min-width:0}.sidebar-heading button{flex:0 0 auto;width:34px;height:34px;padding:0;font-size:20px}
    h1{font-size:22px;line-height:1.15;margin:0 0 6px;letter-spacing:-.02em} h2{font-size:14px;margin:12px 0 7px}.subtitle{color:var(--muted);font-size:12px;line-height:1.45;margin-bottom:14px}.badge{display:inline-block;margin:5px 0 10px;padding:4px 8px;border-radius:999px;background:#e8f4ff;color:#075985;font-size:10px;font-weight:800}
    .card{border:1px solid var(--line);border-radius:12px;margin-bottom:12px;background:#fff;box-shadow:0 2px 8px rgba(15,23,42,.035);overflow:visible}.card>summary{list-style:none;cursor:pointer;user-select:none;display:flex;align-items:center;padding:12px 13px;font-size:14px;font-weight:800}.card>summary::-webkit-details-marker{display:none}.card[open]>summary{border-bottom:1px solid #eef2f5}.chev{margin-left:auto;color:var(--muted);transition:transform .18s}.card[open] .chev{transform:rotate(180deg)}.content{padding:2px 13px 13px}
    label{display:block;font-size:12px;font-weight:700;margin:9px 0 5px}select,input[type=text],input[type=number],input[type=password],textarea{width:100%;border:1px solid #cfd8e1;border-radius:8px;padding:8px 9px;background:#fff;color:var(--ink)}input[type=range]{width:100%}button{border:0;border-radius:8px;padding:8px 10px;cursor:pointer;font-weight:700;background:var(--accent);color:#fff}button.secondary{background:var(--accent-soft);color:var(--accent);border:1px solid #c6d9ee}button.danger{background:#fff4f4;color:#a83232;border:1px solid #efcaca}.row{display:grid;grid-template-columns:1fr 1fr;gap:8px}.check{display:flex;align-items:center;gap:8px;margin-top:8px;font-size:12px}.check input{width:auto}.small{font-size:10.5px;color:var(--muted);line-height:1.4}.status{font-size:11px;color:var(--muted);min-height:16px;margin-top:7px}.warning{color:#8a3d00;background:#fff7ed;border:1px solid #fed7aa;padding:8px;border-radius:8px;margin-top:8px;font-size:11px;line-height:1.35}
    .legend{height:12px;border-radius:999px;background:linear-gradient(90deg,#194696,#4a91c4,#f4f4f4,#ecaa82,#941423);margin-top:8px}.legend.rad{background:linear-gradient(90deg,#000,#2d0a46,#69126e,#b42d50,#eb6923,#fabe2d,#ffffdc)}.legend-labels{display:flex;justify-content:space-between;color:var(--muted);font-size:10px;margin-top:3px}
    .search-wrap{position:relative}#search-results{display:none;position:absolute;top:calc(100% + 5px);left:0;right:0;max-height:300px;overflow-y:auto;background:#fff;border:1px solid #cfd8e1;border-radius:9px;box-shadow:var(--shadow);z-index:3000}.search-result{width:100%;display:block;text-align:left;border-radius:0;border-bottom:1px solid #eef2f5;background:#fff;color:var(--ink);padding:9px 10px;font-weight:500}.search-result strong{display:block;font-size:12px}.search-result span{display:block;color:var(--muted);font-size:10.5px;margin-top:2px}
    .metrics{display:grid;grid-template-columns:1fr 1fr;gap:7px}.metric{background:#f8fafc;border-radius:8px;padding:8px;border:1px solid #edf1f5}.metric span{display:block;font-size:9.5px;text-transform:uppercase;color:var(--muted)}.metric strong{display:block;margin-top:3px;font-size:13px}.info-title{font-weight:800;font-size:15px;margin-bottom:4px}.info-sub{color:var(--muted);font-size:11px;margin-bottom:9px}#chart-wrap{height:200px;margin-top:9px}
    #top-list{margin:0;padding:0;list-style:none;max-height:260px;overflow-y:auto}#top-list li{display:grid;grid-template-columns:24px 1fr auto;gap:7px;padding:7px 4px;border-bottom:1px solid #eef2f5;font-size:11.5px;cursor:pointer}.rank{width:22px;height:22px;border-radius:50%;background:var(--accent-soft);color:var(--accent);display:flex;align-items:center;justify-content:center;font-weight:800}.value{font-variant-numeric:tabular-nums;font-weight:800}
    .lots-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px 8px}.full{grid-column:1/-1}.lot-actions{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:8px}.lot-list{margin-top:9px;border-top:1px solid #eef2f5}.lot-item{padding:9px 0;border-bottom:1px solid #eef2f5}.lot-item-title{display:flex;gap:6px;align-items:center}.lot-item-title strong{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.lot-badge{margin-left:auto;padding:2px 6px;border-radius:999px;background:var(--accent-soft);color:var(--accent);font-size:9px;font-weight:800}.lot-item-actions{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}.lot-item-actions button{padding:5px 7px;font-size:9.5px}.lot-source{margin:8px 0 10px;padding:9px;border:1px solid #dbe5ef;border-radius:9px;background:#f8fafc;font-size:11px;overflow-wrap:anywhere}.lot-source a{color:var(--accent);font-weight:800}.lot-notes{white-space:pre-wrap;margin-top:7px}.lot-score{font-size:24px;font-weight:800;color:var(--accent)}.lot-table{width:100%;border-collapse:collapse;font-size:10.5px}.lot-table td{border-bottom:1px solid #eef2f5;padding:5px 2px;vertical-align:top}.lot-table td:last-child{text-align:right}
    .service-options{display:grid;grid-template-columns:1fr 1fr;gap:5px 8px;margin-top:8px}.service-option{display:flex;align-items:center;gap:6px;font-size:11px}.service-option input{width:auto}.service-result{border-top:1px solid #eef2f5;padding:8px 0}.service-result button{padding:5px 7px;font-size:9.5px;margin-top:5px}
    .gm-style .gm-style-iw-c{max-width:340px!important}
    @media(max-width:900px){#app{display:block}#map{position:fixed;inset:0;height:100dvh}#sidebar{position:fixed;inset:0 auto 0 0;width:min(88vw,380px);height:100dvh;box-shadow:var(--shadow)}body.sidebar-collapsed #sidebar{transform:translateX(-100%)}}
  </style>
</head>
<body>
<button id="menu-toggle" type="button" aria-label="Abrir menú">⚙</button>
<div id="app">
<aside id="sidebar">
  <div class="sidebar-heading"><h1>{{ title }}</h1><button id="menu-close" class="secondary" type="button">×</button></div>
  <div class="badge">GOOGLE MAPS · {{ build }}</div>
  <div class="subtitle">Google Maps como mapa base, con POIs nativos y vistas satélite/híbrida. Las capas VIIRS, lotes y análisis territorial siguen siendo tuyas.</div>
  {% if not google_key %}<div class="warning"><strong>Falta GOOGLE_MAPS_BROWSER_KEY.</strong> El backend puede iniciar, pero el mapa no cargará hasta configurar la clave.</div>{% endif %}

  <details class="card"><summary>Buscar cualquier zona <span class="chev">⌄</span></summary><div class="content">
    <div class="search-wrap"><input id="search-input" type="text" placeholder="Ej.: Benjamin Aceval, Villa Hayes…" autocomplete="off"><div id="search-results"></div></div>
    <button id="clear-search" class="secondary" style="width:100%;margin-top:8px;display:none">Quitar selección</button>
    <div class="small" style="margin-top:7px">Busca tus departamentos, distritos, ciudades y localidades del análisis.</div>
  </div></details>

  <details class="card"><summary>Visualización <span class="chev">⌄</span></summary><div class="content">
    <label for="base-map">Mapa Google</label><select id="base-map"><option value="roadmap">Mapa</option><option value="hybrid">Híbrido</option><option value="satellite">Satélite</option><option value="terrain">Terreno</option></select>
    <div class="check"><input id="show-google-pois" type="checkbox" checked><span>Mostrar POIs de Google (hospitales, comercios, escuelas, etc.)</span></div>
    <label for="raster-layer">Capa VIIRS</label><select id="raster-layer"></select>
    <label for="opacity">Opacidad VIIRS: <span id="opacity-value">55%</span></label><input id="opacity" type="range" min="0" max="100" value="55">
    <div id="raster-legend" class="legend"></div><div class="legend-labels"><span id="legend-min">Bajo</span><span id="legend-mid">0</span><span id="legend-max">Alto</span></div>
    <label for="admin-layer">Capa administrativa</label><select id="admin-layer"></select>
    <label for="metric">Métrica</label><select id="metric"></select>
    <label for="department">Departamento</label><select id="department"><option value="">Todo Paraguay</option></select>
    <div class="check"><input id="show-admin-labels" type="checkbox"><span>Mostrar nombres propios de nuestra capa</span></div>
    <div class="check"><input id="show-hotspots" type="checkbox"><span>Mostrar hotspots de crecimiento</span></div>
    <label>Hotspots: percentil <span id="hotspot-label">98.5</span></label><input id="hotspot-percentile" type="range" min="90" max="99.8" step="0.1" value="98.5">
    <div class="row" style="margin-top:10px"><button id="reset-view" class="secondary">Vista nacional</button><button id="reload">Actualizar</button></div><div id="status" class="status"></div>
  </div></details>

  <details class="card" id="lots-card"><summary>Lotes guardados y evaluación <span class="chev">⌄</span></summary><div class="content">
    <div class="check"><input id="show-lots" type="checkbox" checked><span>Mostrar lotes</span></div>
    <div class="check"><input id="lots-only-mode" type="checkbox"><span>Modo solo lotes</span></div>
    <label>Token de administración</label><input id="lots-token" type="password" placeholder="Solo si LOTS_ADMIN_TOKEN está configurado">
    <div class="lots-grid">
      <div class="full"><label>Nombre</label><input id="lot-name" type="text" placeholder="Ej.: Benjamin Aceval – lote 12"></div>
      <div><label>Estado</label><select id="lot-status"><option value="candidate">Candidato</option><option value="interesting">Interesante</option><option value="visited">Visitado</option><option value="negotiating">Negociando</option><option value="bought">Comprado</option><option value="discarded">Descartado</option></select></div>
      <div><label>Uso</label><select id="lot-use"><option value="residential">Residencial</option><option value="commercial">Comercial</option><option value="industrial">Industrial</option><option value="mixed">Mixto</option></select></div>
      <div><label>Latitud</label><input id="lot-lat" type="number" step="0.000001"></div><div><label>Longitud</label><input id="lot-lon" type="number" step="0.000001"></div>
      <div><label>Superficie m²</label><input id="lot-area" type="number" min="1" step="1" placeholder="360"></div><div><label>Frente m</label><input id="lot-frontage" type="number" min="0" step="0.1"></div>
      <div><label>Precio total</label><input id="lot-price" type="number" min="0" step="0.01"></div><div><label>Moneda</label><select id="lot-currency"><option value="PYG">PYG</option><option value="USD">USD</option></select></div>
      <div><label>Anillo 1 km</label><input id="lot-ring1" type="number" value="1" min="0.2" max="25" step="0.1"></div><div><label>Anillo 2 km</label><input id="lot-ring2" type="number" value="5" min="0.5" max="80" step="0.5"></div>
      <div class="full"><label>Enlace del aviso</label><input id="lot-url" type="text" placeholder="https://..."></div><div class="full"><label>Notas</label><textarea id="lot-notes" rows="3"></textarea></div>
    </div>
    <div class="lot-actions"><button id="pick-lot-point" class="secondary">Elegir punto</button><button id="draw-lot" class="secondary">Dibujar polígono</button><button id="finish-lot" class="secondary">Cerrar polígono</button><button id="clear-draft" class="secondary">Limpiar dibujo</button><button id="save-lot" class="full">Guardar lote</button><button id="cancel-edit" class="secondary full" style="display:none">Cancelar edición</button></div>
    <div id="lot-drawing-status" class="warning" style="display:none"></div><div id="lots-status" class="status"></div><div id="lots-list" class="lot-list small"></div><div id="lot-evaluation" class="small" style="margin-top:10px">Selecciona “Evaluar” en un lote para comparar radiancia, servicios, ciudades, industrias y precio.</div>
  </div></details>

  <details class="card" id="services-card"><summary>Google POIs, servicios e industrias <span class="chev">⌄</span></summary><div class="content">
    <div class="small">Los POIs visibles del mapa son de Google. Esta sección opcional mantiene tus puntos locales/industriales y el cálculo de rutas.</div>
    <div id="google-mode" class="warning"></div>
    <div class="check"><input id="show-local-services" type="checkbox"><span>Superponer nuestra base local de servicios/industrias</span></div><div id="service-options" class="service-options"></div>
    <div class="check"><input id="show-industrial-zones" type="checkbox"><span>Polígonos de zonas industriales</span></div><div class="check"><input id="driving-times" type="checkbox" checked><span>Calcular tiempos reales en auto</span></div>
    <div class="row" style="margin-top:9px"><button id="reload-services" class="secondary">Actualizar</button><button id="clear-route" class="secondary">Quitar ruta</button></div><div id="service-status" class="status"></div><div id="service-results" class="small">Haz clic en un punto vacío del mapa para analizar esa coordenada.</div>
  </div></details>

  <details class="card"><summary>Zona seleccionada <span class="chev">⌄</span></summary><div class="content"><div id="feature-info" class="small">Pasa el cursor sobre una zona propia o haz clic para ver su serie.</div><div id="chart-wrap"><canvas id="series-chart"></canvas></div></div></details>
  <details class="card"><summary>Píxel seleccionado <span class="chev">⌄</span></summary><div class="content"><div id="pixel-info" class="small">Haz clic en el mapa para consultar VIIRS.</div></div></details>
  <details class="card"><summary>Ranking de zonas <span class="chev">⌄</span></summary><div class="content"><ol id="top-list"></ol></div></details>
  <div class="small">Google aporta el mapa base y POIs visibles. Tus métricas VIIRS, polígonos, lotes e industrias siguen siendo capas independientes.</div>
</aside>
<main id="map"></main>
</div>
<script>
const GOOGLE_PLACES_SERVER_ENABLED={{ places_enabled|tojson }};
const state={config:null,map:null,raster:null,admin:null,search:null,hotspots:[],adminLabels:[],industries:null,localServices:null,lotPolygons:new Map(),lotsRows:[],chart:null,info:null,route:null,routeMarkers:[],serviceOrigin:null,searchTimer:null,searchRows:[],editingId:null,editingGeometry:null,pointMode:false,drawMode:false,draftVertices:[],draftShape:null,currentGeoJSON:null};
const fmt=(v,d=2)=>(v===null||v===undefined||v===''||!Number.isFinite(Number(v)))?'—':Number(v).toLocaleString('es-PY',{maximumFractionDigits:d});
const esc=v=>String(v??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));
async function fetchJson(url,opt={}){const r=await fetch(url,{cache:'default',...opt});let p={};try{p=await r.json()}catch(_){ }if(!r.ok)throw new Error(p.error||`${r.status} ${r.statusText}`);return p}
const selectedLayer=()=>document.getElementById('admin-layer').value, selectedMetric=()=>document.getElementById('metric').value, selectedDep=()=>document.getElementById('department').value;
function metricLabel(k){return state.config.metrics.find(x=>x.key===k)?.label||k} function setStatus(t){document.getElementById('status').textContent=t||''}
function setSidebar(c){document.body.classList.toggle('sidebar-collapsed',!!c);setTimeout(()=>google.maps.event.trigger(state.map,'resize'),230)}
function quantile(a,q){if(!a.length)return 0;const p=(a.length-1)*q,b=Math.floor(p),r=p-b;return a[b+1]!==undefined?a[b]+r*(a[b+1]-a[b]):a[b]}
function domain(gj,m){const a=(gj.features||[]).map(f=>Number(f.properties?.[m])).filter(Number.isFinite).sort((a,b)=>a-b);if(!a.length)return{min:0,max:1,div:false};let min=quantile(a,.05),max=quantile(a,.95);if(min===max){min=Math.min(0,min);max=Math.max(1,max)}return{min,max,div:min<0&&max>0}}
function hexRgb(h){const n=parseInt(h.slice(1),16);return[(n>>16)&255,(n>>8)&255,n&255]} function mix(a,b,t){const A=hexRgb(a),B=hexRgb(b);return'#'+A.map((v,i)=>Math.round(v+(B[i]-v)*t).toString(16).padStart(2,'0')).join('')}
function color(v,d){v=Number(v);if(!Number.isFinite(v))return'#cbd5e1';if(d.div){const z=Math.max(Math.abs(d.min),Math.abs(d.max),1e-9),t=Math.max(-1,Math.min(1,v/z));return t<0?mix('#245a9a','#f7f7f7',t+1):mix('#f7f7f7','#b62d2d',t)}const t=Math.max(0,Math.min(1,(v-d.min)/(d.max-d.min||1)));return mix('#edf5fb','#145b9d',Math.sqrt(t))}
function boundsFromConfig(){const b=state.config.bounds;return{south:b[0][0],west:b[0][1],north:b[1][0],east:b[1][1]}}
function fitConfig(){state.map.fitBounds(boundsFromConfig(),55)}
function fillSelects(){
  const r=document.getElementById('raster-layer'); for(const x of state.config.raster_layers){const o=new Option(x.label,x.key,x.key===state.config.default_raster,x.key===state.config.default_raster);r.add(o)}
  const a=document.getElementById('admin-layer');for(const k of ['departamentos','distritos','localidades','localidades_ine']){const x=state.config.vector_layers.find(z=>z.key===k);if(x)a.add(new Option(x.label,x.key))}
  const m=document.getElementById('metric');for(const x of state.config.metrics){const o=new Option(x.label,x.key,x.key===state.config.default_metric,x.key===state.config.default_metric);m.add(o)}
  const d=document.getElementById('department');for(const x of state.config.departments)d.add(new Option(x,x));
}
function fillServiceOptions(){const w=document.getElementById('service-options');w.innerHTML='';const cfg=state.config.services||{};for(const g of cfg.groups||[]){const l=document.createElement('label');l.className='service-option';l.innerHTML=`<input class="service-category" type="checkbox" value="${esc(g.key)}" ${(cfg.default_groups||[]).includes(g.key)?'checked':''}><span>${esc(g.label)}</span>`;w.appendChild(l)}document.querySelectorAll('.service-category').forEach(x=>x.addEventListener('change',loadLocalServices))}
function selectedServiceGroups(){return [...document.querySelectorAll('.service-category:checked')].map(x=>x.value)}
function mapStyles(){return document.getElementById('show-google-pois').checked?[]:[{featureType:'poi',stylers:[{visibility:'off'}]}]}
function updateMapType(){state.map.setMapTypeId(document.getElementById('base-map').value);state.map.setOptions({styles:mapStyles()})}
function updateRasterLegend(){const k=document.getElementById('raster-layer').value,x=state.config.raster_layers.find(z=>z.key===k),l=document.getElementById('raster-legend');l.className=x?.kind==='radiance'?'legend rad':'legend';if(x?.style){document.getElementById('legend-min').textContent=fmt(x.style.vmin,1);document.getElementById('legend-mid').textContent=x.kind==='radiance'?'Radiancia':'0';document.getElementById('legend-max').textContent=fmt(x.style.vmax,1)}}
function loadRaster(){if(state.raster){state.map.overlayMapTypes.removeAt(0);state.raster=null}const k=document.getElementById('raster-layer').value;if(!k)return;updateRasterLegend();state.raster=new google.maps.ImageMapType({getTileUrl:(coord,zoom)=>`/tiles/${encodeURIComponent(k)}/${zoom}/${coord.x}/${coord.y}.png`,tileSize:new google.maps.Size(256,256),maxZoom:19,minZoom:3,name:'VIIRS',opacity:Number(document.getElementById('opacity').value)/100});state.map.overlayMapTypes.insertAt(0,state.raster)}
function clearData(layer){layer.forEach(f=>layer.remove(f))}
function pointIcon(v,d){return{path:google.maps.SymbolPath.CIRCLE,scale:5.5,fillColor:color(v,d),fillOpacity:.9,strokeColor:'#fff',strokeWeight:1}}
async function loadAdmin(){const layer=selectedLayer();if(!layer)return;setStatus('Cargando capa…');const p=new URLSearchParams();if(selectedDep())p.set('department',selectedDep());const gj=await fetchJson(`/api/geojson/${encodeURIComponent(layer)}?${p}`);clearData(state.admin);const d=domain(gj,selectedMetric());state.admin.addGeoJson(gj);state.admin.setStyle(f=>{const v=f.getProperty(selectedMetric()),geom=f.getGeometry()?.getType();if(geom==='Point')return{icon:pointIcon(v,d),zIndex:3};return{strokeColor:'#52616f',strokeWeight:layer==='departamentos'?1.4:.9,fillColor:color(v,d),fillOpacity:.52,zIndex:2}});state.currentGeoJSON=gj;refreshAdminLabels();setStatus(`${gj.meta?.count||gj.features?.length||0} elementos cargados.`)}
function adminFeatureProps(f){const o={};f.forEachProperty((v,k)=>o[k]=v);return o}
function featureHtml(p){return `<div class="info-title">${esc(p.nombre||p.id_zona||'Zona')}</div><div class="info-sub">${esc(p.nivel||'')}${p.departamento?' · '+esc(p.departamento):''}</div><div class="metrics"><div class="metric"><span>Radiancia inicial</span><strong>${fmt(p.radiancia_inicial)}</strong></div><div class="metric"><span>Radiancia final</span><strong>${fmt(p.radiancia_final)}</strong></div><div class="metric"><span>Cambio absoluto</span><strong>${fmt(p.cambio_absoluto)}</strong></div><div class="metric"><span>Cambio %</span><strong>${fmt(p.cambio_pct)}%</strong></div><div class="metric"><span>CAGR anual</span><strong>${fmt(p.cagr_pct_anual)}%</strong></div><div class="metric"><span>Puntaje</span><strong>${fmt(p.puntaje_crecimiento)}</strong></div></div>`}
function showFeature(p,open=false,latLng=null){document.getElementById('feature-info').innerHTML=featureHtml(p);if(open&&latLng){state.info.setContent(`<div style="min-width:220px">${featureHtml(p)}</div>`);state.info.setPosition(latLng);state.info.open({map:state.map})}}
async function loadSeries(id,name){if(!id)return;const p=await fetchJson(`/api/series/${encodeURIComponent(id)}`),rows=p.series||[];if(state.chart)state.chart.destroy();if(!rows.length)return;state.chart=new Chart(document.getElementById('series-chart'),{type:'line',data:{labels:rows.map(r=>`${r.anio}${r.anio_parcial?'*':''}`),datasets:[{label:'Radiancia media',data:rows.map(r=>r.radiancia_media),borderWidth:2,pointRadius:2.5,tension:.18}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},title:{display:true,text:name||id}},scales:{x:{ticks:{font:{size:9}}},y:{ticks:{font:{size:9}}}}}})}
function clearAdminLabels(){state.adminLabels.forEach(m=>m.setMap(null));state.adminLabels=[]}
function featureCenterFromGeometry(g){const b=new google.maps.LatLngBounds();g.forEachLatLng(ll=>b.extend(ll));return b.isEmpty()?null:b.getCenter()}
function refreshAdminLabels(){clearAdminLabels();if(!document.getElementById('show-admin-labels').checked||!state.currentGeoJSON)return;const z=state.map.getZoom(),layer=selectedLayer();let max=layer==='departamentos'?25:(layer==='distritos'&&z>=8?100:(layer.includes('localidades')&&z>=10?120:0));if(!max)return;const rows=[];state.admin.forEach(f=>{const n=f.getProperty('nombre');if(!n)return;const c=featureCenterFromGeometry(f.getGeometry());if(c)rows.push({name:n,c,priority:Number(f.getProperty('population')||0)})});rows.sort((a,b)=>b.priority-a.priority);for(const r of rows.slice(0,max)){const m=new google.maps.Marker({map:state.map,position:r.c,icon:{path:google.maps.SymbolPath.CIRCLE,scale:0},label:{text:String(r.name),color:'#17202a',fontSize:'11px',fontWeight:'700'},clickable:false,zIndex:10});state.adminLabels.push(m)}}
async function loadHotspots(){state.hotspots.forEach(c=>c.setMap(null));state.hotspots=[];if(!document.getElementById('show-hotspots').checked)return;const q=document.getElementById('hotspot-percentile').value,p=await fetchJson(`/api/hotspots?percentile=${q}&max_points=1200`);for(const f of p.features||[]){const [lon,lat]=f.geometry.coordinates,w=Number(f.properties?.weight||.4);state.hotspots.push(new google.maps.Circle({map:state.map,center:{lat,lng:lon},radius:180+350*w,strokeOpacity:0,fillColor:'#ef4444',fillOpacity:.08+.22*w,clickable:false,zIndex:1}))}setStatus(`${state.hotspots.length} hotspots visibles.`)}
function dataBounds(layer){const b=new google.maps.LatLngBounds();layer.forEach(f=>f.getGeometry()?.forEachLatLng(ll=>b.extend(ll)));return b}
async function zoomDepartment(name){if(!name)return;const gj=await fetchJson(`/api/geojson/departamentos?department=${encodeURIComponent(name)}`),tmp=new google.maps.Data();tmp.addGeoJson(gj);const b=dataBounds(tmp);if(!b.isEmpty())state.map.fitBounds(b,60)}
async function updateTop(){const p=new URLSearchParams({layer:selectedLayer(),metric:selectedMetric(),limit:'15'});if(selectedDep())p.set('department',selectedDep());const x=await fetchJson(`/api/top?${p}`),list=document.getElementById('top-list');list.innerHTML='';(x.rows||[]).forEach((r,i)=>{const li=document.createElement('li');li.innerHTML=`<span class="rank">${i+1}</span><span>${esc(r.nombre||r.id_zona)}<br><small>${esc(r.nivel||'')}</small></span><span class="value">${fmt(r.value)}</span>`;li.onclick=()=>{state.map.panTo({lat:Number(r.lat),lng:Number(r.lon)});state.map.setZoom(selectedLayer().includes('localidades')?12:9);loadSeries(r.id_zona,r.nombre)};list.appendChild(li)})}
function lotHeaders(){const t=document.getElementById('lots-token').value.trim();return t?{'X-Lotes-Token':t}:{}}
async function lotFetch(url,opt={}){const r=await fetch(url,{cache:'no-store',...opt,headers:{'Content-Type':'application/json',...lotHeaders(),...(opt.headers||{})}});let p={};try{p=await r.json()}catch(_){ }if(!r.ok)throw new Error(p.error||`${r.status} ${r.statusText}`);return p}
function money(v,c){if(!Number.isFinite(Number(v)))return'—';return `${Number(v).toLocaleString('es-PY',{maximumFractionDigits:String(c).toUpperCase()==='PYG'?0:2})} ${esc(c||'')}`}
function statusLabel(s){return({candidate:'Candidato',interesting:'Interesante',visited:'Visitado',negotiating:'Negociando',bought:'Comprado',discarded:'Descartado'})[s]||s||'Candidato'}
function sourceHtml(l){const u=String(l?.source_url||'').trim(),n=String(l?.notes||'').trim();return(!u&&!n)?'':`<div class="lot-source">${u?`<div><a href="${esc(u)}" target="_blank" rel="noopener">Abrir aviso original ↗</a></div>`:''}${n?`<div class="lot-notes"><strong>Notas:</strong><br>${esc(n)}</div>`:''}</div>`}
function lotPaths(g){if(!g)return[];if(g.type==='Polygon')return(g.coordinates?.[0]||[]).map(c=>({lat:Number(c[1]),lng:Number(c[0])}));if(g.type==='MultiPolygon')return(g.coordinates?.[0]?.[0]||[]).map(c=>({lat:Number(c[1]),lng:Number(c[0])}));return[]}
function lotColor(s){return({candidate:'#d97706',interesting:'#2563eb',visited:'#7c3aed',negotiating:'#ea580c',bought:'#15803d',discarded:'#64748b'})[s]||'#d97706'}
function clearLots(){for(const p of state.lotPolygons.values())p.setMap(null);state.lotPolygons.clear()}
async function loadLots(){clearLots();if(!document.getElementById('show-lots').checked){renderLots([]);return}try{const p=await lotFetch('/api/lots',{method:'GET',headers:{}});state.lotsRows=p.rows||[];for(const l of state.lotsRows){const path=lotPaths(l.geometry);if(path.length<3)continue;const poly=new google.maps.Polygon({map:state.map,paths:path,strokeColor:lotColor(l.status),strokeWeight:2.5,strokeOpacity:1,fillColor:lotColor(l.status),fillOpacity:.16,clickable:true,zIndex:20});poly.addListener('click',e=>{showLotEvaluation(l.id);state.info.setContent(`<strong>${esc(l.name)}</strong><br>${fmt(l.area_m2,0)} m² · ${l.price_total?money(l.price_total,l.currency):'sin precio'}${sourceHtml(l)}`);state.info.setPosition(e.latLng);state.info.open({map:state.map})});state.lotPolygons.set(Number(l.id),poly)}renderLots(state.lotsRows);document.getElementById('lots-status').textContent=`${state.lotsRows.length} lotes guardados · ${p.meta?.database||''}`}catch(e){document.getElementById('lots-status').textContent=`Error: ${e.message}`}}
function renderLots(rows){const c=document.getElementById('lots-list');c.innerHTML='';if(!rows.length){c.innerHTML='<div style="padding:9px 0">Todavía no hay lotes guardados.</div>';return}for(const l of rows){const x=document.createElement('div');x.className='lot-item';x.innerHTML=`<div class="lot-item-title"><strong>${esc(l.name)}</strong><span class="lot-badge">${esc(statusLabel(l.status))}</span></div><div class="small">${fmt(l.area_m2,0)} m² · ${l.price_total?money(l.price_total,l.currency):'sin precio'}${Number.isFinite(Number(l.price_per_m2))?' · '+money(l.price_per_m2,l.currency)+'/m²':''}</div>`;x.onclick=()=>showLotEvaluation(l.id);const a=document.createElement('div');a.className='lot-item-actions';[['Ubicar',()=>locateLot(l)],['Evaluar',()=>evaluateLot(l.id)],['Editar',()=>editLot(l)],['Eliminar',()=>deleteLot(l.id)]].forEach(([t,fn])=>{const b=document.createElement('button');b.textContent=t;b.className=t==='Eliminar'?'danger':'secondary';b.onclick=e=>{e.stopPropagation();fn()};a.appendChild(b)});x.appendChild(a);c.appendChild(x)}}
function locateLot(l){const p=state.lotPolygons.get(Number(l.id));if(p){const b=new google.maps.LatLngBounds();p.getPath().forEach(ll=>b.extend(ll));state.map.fitBounds(b,80)}else{state.map.setCenter({lat:Number(l.center_lat),lng:Number(l.center_lon)});state.map.setZoom(17)}}
function clearDraft(){state.pointMode=false;state.drawMode=false;state.draftVertices=[];if(state.draftShape)state.draftShape.setMap(null);state.draftShape=null;document.getElementById('lot-drawing-status').style.display='none'}
function redrawDraft(closed=false){if(state.draftShape)state.draftShape.setMap(null);if(!state.draftVertices.length)return;const opt={map:state.map,path:state.draftVertices,strokeColor:'#f59e0b',strokeWeight:3,strokeOpacity:1,clickable:false,zIndex:40};state.draftShape=closed?new google.maps.Polygon({...opt,paths:state.draftVertices,fillColor:'#f59e0b',fillOpacity:.15}):new google.maps.Polyline(opt)}
function startPoint(){clearDraft();state.pointMode=true;const s=document.getElementById('lot-drawing-status');s.style.display='block';s.textContent='Haz clic en el centro aproximado del lote.'}
function startDraw(){clearDraft();state.drawMode=true;const s=document.getElementById('lot-drawing-status');s.style.display='block';s.textContent='Haz clic en cada vértice. Luego presiona “Cerrar polígono”.'}
function addDraft(ll){state.draftVertices.push({lat:ll.lat(),lng:ll.lng()});redrawDraft(false);document.getElementById('lot-drawing-status').textContent=`${state.draftVertices.length} vértices.`}
function finishDraw(){if(state.draftVertices.length<3){document.getElementById('lots-status').textContent='Se necesitan al menos 3 vértices.';return}state.drawMode=false;redrawDraft(true);const area=google.maps.geometry.spherical.computeArea(state.draftVertices);document.getElementById('lot-area').value=Math.round(area);const lat=state.draftVertices.reduce((a,v)=>a+v.lat,0)/state.draftVertices.length,lng=state.draftVertices.reduce((a,v)=>a+v.lng,0)/state.draftVertices.length;document.getElementById('lot-lat').value=lat.toFixed(6);document.getElementById('lot-lon').value=lng.toFixed(6);document.getElementById('lot-drawing-status').textContent=`Polígono listo · ~${fmt(area,0)} m².`}
function lotPayload(){let geometry=state.editingGeometry;if(state.draftVertices.length>=3){const coords=state.draftVertices.map(v=>[v.lng,v.lat]);coords.push(coords[0]);geometry={type:'Polygon',coordinates:[coords]}}return{name:document.getElementById('lot-name').value.trim()||'Lote sin nombre',status:document.getElementById('lot-status').value,intended_use:document.getElementById('lot-use').value,lat:Number(document.getElementById('lot-lat').value),lon:Number(document.getElementById('lot-lon').value),area_m2:Number(document.getElementById('lot-area').value),frontage_m:Number(document.getElementById('lot-frontage').value)||null,price_total:Number(document.getElementById('lot-price').value)||null,currency:document.getElementById('lot-currency').value,source_url:document.getElementById('lot-url').value.trim(),notes:document.getElementById('lot-notes').value.trim(),geometry}}
async function saveLot(){sessionStorage.setItem('lucesparaguay_lots_token',document.getElementById('lots-token').value.trim());try{const url=state.editingId?`/api/lots/${state.editingId}`:'/api/lots',method=state.editingId?'PUT':'POST';await lotFetch(url,{method,body:JSON.stringify(lotPayload())});resetForm();await loadLots();document.getElementById('lots-status').textContent='Lote guardado correctamente.'}catch(e){document.getElementById('lots-status').textContent=`No se pudo guardar: ${e.message}`}}
function editLot(l){clearDraft();state.editingId=l.id;state.editingGeometry=l.geometry;for(const [id,v] of Object.entries({'lot-name':l.name,'lot-lat':l.center_lat,'lot-lon':l.center_lon,'lot-area':l.area_m2,'lot-frontage':l.frontage_m||'','lot-price':l.price_total||'','lot-url':l.source_url||'','lot-notes':l.notes||''}))document.getElementById(id).value=v;document.getElementById('lot-status').value=l.status||'candidate';document.getElementById('lot-use').value=l.intended_use||'residential';document.getElementById('lot-currency').value=l.currency||'PYG';document.getElementById('save-lot').textContent='Guardar cambios';document.getElementById('cancel-edit').style.display='block';locateLot(l)}
function resetForm(){state.editingId=null;state.editingGeometry=null;clearDraft();['lot-name','lot-lat','lot-lon','lot-area','lot-frontage','lot-price','lot-url','lot-notes'].forEach(id=>document.getElementById(id).value='');document.getElementById('lot-status').value='candidate';document.getElementById('lot-use').value='residential';document.getElementById('lot-currency').value='PYG';document.getElementById('save-lot').textContent='Guardar lote';document.getElementById('cancel-edit').style.display='none'}
async function deleteLot(id){if(!confirm('¿Eliminar este lote?'))return;try{await lotFetch(`/api/lots/${id}`,{method:'DELETE'});await loadLots()}catch(e){document.getElementById('lots-status').textContent=e.message}}
async function evaluateLot(id){document.getElementById('lot-evaluation').textContent='Evaluando…';try{const l=await lotFetch(`/api/lots/${id}/evaluate`,{method:'POST',body:JSON.stringify({ring1_km:Number(document.getElementById('lot-ring1').value)||1,ring2_km:Number(document.getElementById('lot-ring2').value)||5,include_driving:document.getElementById('driving-times').checked})});await loadLots();renderEvaluation(l)}catch(e){document.getElementById('lot-evaluation').textContent=`No se pudo evaluar: ${e.message}`}}
function showLotEvaluation(id){const l=state.lotsRows.find(x=>Number(x.id)===Number(id));if(!l)return;document.getElementById('lots-card').open=true;if(l.evaluation)renderEvaluation(l);else document.getElementById('lot-evaluation').innerHTML=`<div class="info-title">${esc(l.name)}</div><div class="info-sub">${fmt(l.area_m2,0)} m² · ${l.price_total?money(l.price_total,l.currency):'sin precio'}</div>${sourceHtml(l)}<div>Aún no evaluado.</div>`}
function renderEvaluation(l){const e=l.evaluation||{},score=e.scores?.screening_score,r=e.rings||[],s=e.nearest_services?.services||[],c=e.nearby_localities||[],p=e.price_comparison;document.getElementById('lot-evaluation').innerHTML=`<div class="info-title">${esc(l.name)}</div><div class="lot-score">${Number.isFinite(Number(score))?fmt(score,1)+'/100':'Sin puntaje'}</div><div class="info-sub">Fuente servicios: ${esc(e.service_source||e.nearest_services?.method||'local')}</div>${sourceHtml(l)}<strong>Radiancia</strong><table class="lot-table">${r.map(x=>`<tr><td>${fmt(x.inner_km,1)}–${fmt(x.outer_km,1)} km</td><td>${fmt(x.end?.mean,3)} · ${fmt(x.change_pct,1)}%</td></tr>`).join('')}</table><strong style="display:block;margin-top:8px">Servicios / industria</strong><table class="lot-table">${s.slice(0,10).map(x=>`<tr><td>${esc(x.name||x.query_category)}</td><td>${x.duration_minutes!==undefined?fmt(x.duration_minutes,1)+' min · ':''}${fmt(x.distance_km??x.air_distance_km,2)} km</td></tr>`).join('')}</table><strong style="display:block;margin-top:8px">Localidades</strong><table class="lot-table">${c.slice(0,5).map(x=>`<tr><td>${esc(x.name)}</td><td>${fmt(x.distance_km,1)} km</td></tr>`).join('')}</table>${p?`<div style="margin-top:8px"><strong>Precio:</strong> ${money(p.price_per_m2,p.currency)}/m²${p.median_nearby!==null?' · mediana '+money(p.median_nearby,p.currency)+'/m²':''}</div>`:''}<div class="warning">${esc((e.caveats||[]).slice(-1)[0]||'Preevaluación; no sustituye tasación ni revisión legal.')}</div>`}
function toggleLotsOnly(){const only=document.getElementById('lots-only-mode').checked;state.admin.setMap(only?null:state.map);state.search.setMap(only?null:state.map);state.industries.setMap(only?null:state.map);state.localServices.setMap(only?null:state.map);state.hotspots.forEach(x=>x.setVisible(!only));if(state.raster)state.raster.setOpacity(only?0:Number(document.getElementById('opacity').value)/100);if(!only){state.admin.setMap(state.map);state.search.setMap(state.map);state.industries.setMap(state.map);state.localServices.setMap(state.map)} }
function localServiceGroup(p){if(p.category==='health'){if(['hospital','hospital_major'].includes(p.subcategory))return'hospital';if(['clinic','health_centre','health_post','usf'].includes(p.subcategory))return'primary_health'}return p.category==='industry'?(p.subcategory||'industrial_building'):(p.category||'service')}
function serviceIcon(group){const c=({hospital:'#b91c1c',primary_health:'#dc2626',education:'#7c3aed',supermarket:'#15803d',pharmacy:'#db2777',bank:'#1d4ed8',fuel:'#a16207',police:'#334155',fire_station:'#ea580c',market:'#0f766e',industry:'#c2410c',factory:'#c2410c',warehouse:'#92400e',quarry:'#475569',utility_waste:'#57534e'})[group]||'#475569';return{path:google.maps.SymbolPath.CIRCLE,scale:6,fillColor:c,fillOpacity:.95,strokeColor:'#fff',strokeWeight:1.2}}
async function loadLocalServices(){clearData(state.localServices);if(!document.getElementById('show-local-services').checked||state.map.getZoom()<8)return;const g=selectedServiceGroups();if(!g.length)return;const b=state.map.getBounds();if(!b)return;const ne=b.getNorthEast(),sw=b.getSouthWest(),bbox=[sw.lng(),sw.lat(),ne.lng(),ne.lat()].join(',');try{const p=await fetchJson(`/api/services?bbox=${encodeURIComponent(bbox)}&groups=${encodeURIComponent(g.join(','))}&limit=1200`);state.localServices.addGeoJson(p);state.localServices.setStyle(f=>({icon:serviceIcon(localServiceGroup(adminFeatureProps(f))),zIndex:25}));document.getElementById('service-status').textContent=`${p.meta?.count||0} puntos locales visibles.`}catch(e){document.getElementById('service-status').textContent=e.message}}
async function loadIndustrialZones(){clearData(state.industries);if(!document.getElementById('show-industrial-zones').checked||state.map.getZoom()<7)return;const b=state.map.getBounds();if(!b)return;const ne=b.getNorthEast(),sw=b.getSouthWest(),bbox=[sw.lng(),sw.lat(),ne.lng(),ne.lat()].join(',');try{const p=await fetchJson(`/api/industrial-zones?bbox=${encodeURIComponent(bbox)}&limit=1000`);state.industries.addGeoJson(p);state.industries.setStyle(f=>{const t=f.getProperty('zone_type')||'industrial';return{strokeColor:t==='industrial'?'#c2410c':'#57534e',strokeWeight:1.2,fillColor:t==='industrial'?'#f97316':'#78716c',fillOpacity:.12,clickable:false,zIndex:8}})}catch(e){console.warn(e)}}
async function queryPixel(ll){const k=document.getElementById('raster-layer').value,p=await fetchJson(`/api/pixel?lat=${ll.lat()}&lon=${ll.lng()}&layer=${encodeURIComponent(k)}`);document.getElementById('pixel-info').innerHTML=`<div class="info-sub">${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}</div><div class="metrics">${(p.values||[]).map(x=>`<div class="metric"><span>${esc(x.label)}</span><strong>${fmt(x.value,3)}</strong></div>`).join('')}</div>`}
async function queryNearest(ll){const g=selectedServiceGroups();if(!g.length)return;state.serviceOrigin={lat:ll.lat(),lon:ll.lng()};document.getElementById('service-results').textContent='Buscando servicios…';const ep=document.getElementById('driving-times').checked?'nearest-driving':'nearest';try{const p=await fetchJson(`/api/services/${ep}?lat=${ll.lat()}&lon=${ll.lng()}&groups=${encodeURIComponent(g.join(','))}`);renderNearest(p)}catch(e){document.getElementById('service-results').textContent=e.message}}
function renderNearest(p){const c=document.getElementById('service-results'),rows=p.services||[];c.innerHTML=`<div class="info-sub">Método: ${esc(p.method||'')}</div>`;if(!rows.length){c.innerHTML+='No se encontraron resultados.';return}for(const r of rows){const x=document.createElement('div');x.className='service-result';x.innerHTML=`<strong>${esc(r.name||r.query_category)}</strong><br>${r.duration_minutes!==undefined?fmt(r.duration_minutes,1)+' min · ':''}${fmt(r.distance_km??r.air_distance_km,2)} km <span class="small">(${esc(r.query_category||'')})</span>`;const b=document.createElement('button');b.textContent='Dibujar ruta';b.className='secondary';b.onclick=()=>drawRoute(r);x.appendChild(document.createElement('br'));x.appendChild(b);c.appendChild(x)}}
function clearRoute(){if(state.route){state.route.setMap(null);state.route=null}state.routeMarkers.forEach(m=>m.setMap(null));state.routeMarkers=[]}
async function drawRoute(s){if(!state.serviceOrigin)return;const q=new URLSearchParams({from_lat:state.serviceOrigin.lat,from_lon:state.serviceOrigin.lon,to_lat:s.lat,to_lon:s.lon});try{const r=await fetchJson(`/api/route?${q}`);clearRoute();const path=(r.geometry?.coordinates||[]).map(c=>({lat:c[1],lng:c[0]}));state.route=new google.maps.Polyline({map:state.map,path,strokeColor:'#2563eb',strokeOpacity:.9,strokeWeight:5,zIndex:50});state.routeMarkers=[new google.maps.Marker({map:state.map,position:state.serviceOrigin,label:'A'}),new google.maps.Marker({map:state.map,position:{lat:Number(s.lat),lng:Number(s.lon)},label:'B'})];const b=new google.maps.LatLngBounds();path.forEach(x=>b.extend(x));state.map.fitBounds(b,70);document.getElementById('service-status').textContent=`Ruta: ${fmt(r.distance_km,2)} km · ${fmt(r.duration_minutes,1)} min.`}catch(e){document.getElementById('service-status').textContent=`Ruta: ${e.message}`}}
async function performSearch(q){const box=document.getElementById('search-results'),v=q.trim();if(v.length<2){box.style.display='none';return}box.style.display='block';box.innerHTML='<div class="small" style="padding:10px">Buscando…</div>';try{const p=await fetchJson(`/api/search?q=${encodeURIComponent(v)}&limit=12`);state.searchRows=p.rows||[];box.innerHTML='';if(!state.searchRows.length){box.innerHTML='<div class="small" style="padding:10px">Sin coincidencias.</div>';return}for(const r of state.searchRows){const b=document.createElement('button');b.className='search-result';b.innerHTML=`<strong>${esc(r.nombre)}</strong><span>${esc([r.nivel||r.layer_label,r.departamento].filter(Boolean).join(' · '))}</span>`;b.onclick=()=>chooseSearch(r);box.appendChild(b)}}catch(e){box.innerHTML=`<div class="small" style="padding:10px">${esc(e.message)}</div>`}}
function clearSearch(){clearData(state.search);document.getElementById('search-input').value='';document.getElementById('search-results').style.display='none';document.getElementById('clear-search').style.display='none'}
async function chooseSearch(r){document.getElementById('search-results').style.display='none';document.getElementById('search-input').value=r.nombre||'';document.getElementById('clear-search').style.display='block';clearData(state.search);const gj=await fetchJson(`/api/feature/${encodeURIComponent(r.layer)}/${encodeURIComponent(r.id_zona)}`);state.search.addGeoJson(gj);state.search.setStyle(f=>{const type=f.getGeometry()?.getType();return type==='Point'?{icon:{path:google.maps.SymbolPath.CIRCLE,scale:10,fillColor:'#f59e0b',fillOpacity:1,strokeColor:'#fff',strokeWeight:2},zIndex:100}:{strokeColor:'#f59e0b',strokeWeight:4,fillColor:'#fbbf24',fillOpacity:.18,zIndex:100}});const b=dataBounds(state.search);if(!b.isEmpty())state.map.fitBounds(b,70);showFeature(r,false);loadSeries(r.id_zona,r.nombre)}
function bindEvents(){
  document.getElementById('menu-close').onclick=()=>setSidebar(true);document.getElementById('menu-toggle').onclick=()=>setSidebar(false);
  document.getElementById('base-map').onchange=updateMapType;document.getElementById('show-google-pois').onchange=updateMapType;
  document.getElementById('raster-layer').onchange=loadRaster;document.getElementById('opacity').oninput=e=>{document.getElementById('opacity-value').textContent=`${e.target.value}%`;if(state.raster)state.raster.setOpacity(Number(e.target.value)/100)};
  document.getElementById('admin-layer').onchange=async()=>{await loadAdmin();await updateTop()};document.getElementById('metric').onchange=async()=>{await loadAdmin();await updateTop()};document.getElementById('department').onchange=async e=>{await loadAdmin();await updateTop();e.target.value?await zoomDepartment(e.target.value):fitConfig()};document.getElementById('show-admin-labels').onchange=refreshAdminLabels;
  document.getElementById('show-hotspots').onchange=loadHotspots;document.getElementById('hotspot-percentile').oninput=e=>document.getElementById('hotspot-label').textContent=e.target.value;document.getElementById('hotspot-percentile').onchange=loadHotspots;
  document.getElementById('reset-view').onclick=async()=>{document.getElementById('department').value='';clearSearch();fitConfig();await loadAdmin();await updateTop()};document.getElementById('reload').onclick=async()=>{loadRaster();await Promise.all([loadAdmin(),loadHotspots(),loadLocalServices(),loadIndustrialZones(),updateTop(),loadLots()])};
  document.getElementById('show-lots').onchange=loadLots;document.getElementById('lots-only-mode').onchange=toggleLotsOnly;document.getElementById('pick-lot-point').onclick=startPoint;document.getElementById('draw-lot').onclick=startDraw;document.getElementById('finish-lot').onclick=finishDraw;document.getElementById('clear-draft').onclick=clearDraft;document.getElementById('save-lot').onclick=saveLot;document.getElementById('cancel-edit').onclick=resetForm;
  document.getElementById('show-local-services').onchange=loadLocalServices;document.getElementById('show-industrial-zones').onchange=loadIndustrialZones;document.getElementById('reload-services').onclick=async()=>{await loadLocalServices();await loadIndustrialZones()};document.getElementById('clear-route').onclick=clearRoute;
  document.getElementById('clear-search').onclick=clearSearch;const inp=document.getElementById('search-input');inp.oninput=()=>{clearTimeout(state.searchTimer);state.searchTimer=setTimeout(()=>performSearch(inp.value),220)};inp.onkeydown=e=>{if(e.key==='Enter'&&state.searchRows.length)chooseSearch(state.searchRows[0]);if(e.key==='Escape')clearSearch()};
  state.admin.addListener('mouseover',e=>showFeature(adminFeatureProps(e.feature),false));state.admin.addListener('click',e=>{const p=adminFeatureProps(e.feature);showFeature(p,true,e.latLng);loadSeries(p.id_zona,p.nombre)});
  state.localServices.addListener('click',e=>{const p=adminFeatureProps(e.feature);state.info.setContent(`<strong>${esc(p.name||'Servicio')}</strong><br><span class="small">${esc([p.subcategory,p.district,p.department].filter(Boolean).join(' · '))}</span>`);state.info.setPosition(e.latLng);state.info.open({map:state.map})});
  state.map.addListener('zoom_changed',()=>{refreshAdminLabels();setTimeout(()=>{loadLocalServices();loadIndustrialZones()},100)});state.map.addListener('idle',()=>{loadLocalServices();loadIndustrialZones()});
  state.map.addListener('click',e=>{if(e.placeId)return;if(state.pointMode){document.getElementById('lot-lat').value=e.latLng.lat().toFixed(6);document.getElementById('lot-lon').value=e.latLng.lng().toFixed(6);state.pointMode=false;document.getElementById('lot-drawing-status').style.display='none';return}if(state.drawMode){addDraft(e.latLng);return}queryPixel(e.latLng).catch(console.warn);queryNearest(e.latLng).catch(console.warn)});
}
async function initMap(){
  try{
    document.querySelectorAll('details.card').forEach(x=>x.open=false);setSidebar(false);
    state.config=await fetchJson('/api/config');
    state.map=new google.maps.Map(document.getElementById('map'),{center:{lat:-23.4,lng:-58.4},zoom:6,mapTypeId:'roadmap',mapTypeControl:false,streetViewControl:true,fullscreenControl:true,zoomControl:true,gestureHandling:'greedy',styles:mapStyles(),clickableIcons:true});
    state.info=new google.maps.InfoWindow();
    state.admin=new google.maps.Data({map:state.map});state.search=new google.maps.Data({map:state.map});state.industries=new google.maps.Data({map:state.map});state.localServices=new google.maps.Data({map:state.map});
    fillSelects();fillServiceOptions();document.getElementById('lots-token').value=sessionStorage.getItem('lucesparaguay_lots_token')||'';
    document.getElementById('google-mode').innerHTML=GOOGLE_PLACES_SERVER_ENABLED?'<strong>Evaluación Google activada.</strong> Los servicios públicos de los lotes se consultan con Places API; industrias siguen usando la base local.':'<strong>Modo sin Places API.</strong> Los POIs del mapa siguen siendo de Google, pero el puntaje numérico de servicios usa la base local existente.';
    bindEvents();fitConfig();loadRaster();await Promise.all([loadAdmin(),updateTop(),loadLots()]);
  }catch(e){console.error(e);document.getElementById('status').textContent=`Error iniciando mapa: ${e.message}`}
}
window.initMap=initMap;
</script>
{% if google_key %}
<script async src="https://maps.googleapis.com/maps/api/js?key={{ google_key }}&loading=async&libraries=geometry&callback=initMap&v=weekly"></script>
{% endif %}
</body></html>
"""


def google_index() -> str:
    return render_template_string(
        GOOGLE_INDEX_HTML,
        title=APP_TITLE,
        build=APP_BUILD,
        google_key=GOOGLE_MAPS_BROWSER_KEY,
        places_enabled=bool(GOOGLE_PLACES_SERVER_KEY),
    )


# Replace the existing '/' endpoint from app.py without touching any other route.
app.view_functions["index"] = google_index


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8050"))
    print(f"Iniciando {APP_TITLE} | {APP_BUILD} | Google Maps")
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1", threaded=True)
