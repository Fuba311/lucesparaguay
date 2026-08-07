"""Runtime overlay: use Google Places for nearest public-service POIs.

Loaded by Gunicorn through GUNICORN_CMD_ARGS. It chains the existing
``gunicorn.conf.py`` performance/hover patch, then replaces only the service
lookup UI so public POIs come from live Google Places. The existing /api/route
(OSRM) endpoint still draws the road route to the Google POI coordinates.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


# Chain the current production performance/hover config instead of replacing it.
_base_path = Path(__file__).with_name("gunicorn.conf.py")
_spec = importlib.util.spec_from_file_location("luces_base_gunicorn", _base_path)
_base = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(_base)

DESC_OLD = '    <div class="small">Los POIs visibles del mapa son de Google. Esta sección opcional mantiene tus puntos locales/industriales y el cálculo de rutas.</div>'
DESC_NEW = '    <div class="small">Hospitales, salud, escuelas, supermercados y otros servicios se buscan en vivo con Google Places. La base local queda solo para industrias u otras categorías sin equivalente Google.</div>'

QUERY_OLD = """async function queryNearest(ll){const g=selectedServiceGroups();if(!g.length)return;state.serviceOrigin={lat:ll.lat(),lon:ll.lng()};document.getElementById('service-results').textContent='Buscando servicios…';const ep=document.getElementById('driving-times').checked?'nearest-driving':'nearest';try{const p=await fetchJson(`/api/services/${ep}?lat=${ll.lat()}&lon=${ll.lng()}&groups=${encodeURIComponent(g.join(','))}`);renderNearest(p)}catch(e){document.getElementById('service-results').textContent=e.message}}
function renderNearest(p){const c=document.getElementById('service-results'),rows=p.services||[];c.innerHTML=`<div class="info-sub">Método: ${esc(p.method||'')}</div>`;if(!rows.length){c.innerHTML+='No se encontraron resultados.';return}for(const r of rows){const x=document.createElement('div');x.className='service-result';x.innerHTML=`<strong>${esc(r.name||r.query_category)}</strong><br>${r.duration_minutes!==undefined?fmt(r.duration_minutes,1)+' min · ':''}${fmt(r.distance_km??r.air_distance_km,2)} km <span class="small">(${esc(r.query_category||'')})</span>`;const b=document.createElement('button');b.textContent='Dibujar ruta';b.className='secondary';b.onclick=()=>drawRoute(r);x.appendChild(document.createElement('br'));x.appendChild(b);c.appendChild(x)}}
"""

QUERY_NEW = """const GOOGLE_SERVICE_TYPES={hospital:['hospital','general_hospital','medical_center'],primary_health:['medical_clinic','doctor','medical_center'],education:['school','primary_school','secondary_school','educational_institution'],supermarket:['supermarket','grocery_store','hypermarket'],pharmacy:['pharmacy','drugstore'],bank:['bank','atm'],fuel:['gas_station'],police:['police'],fire_station:['fire_station'],market:['market','farmers_market']};
async function googleNearestRows(ll,groups){const {Place,SearchNearbyRankPreference}=await google.maps.importLibrary('places'),rows=[],errors=[];for(const group of groups){const types=GOOGLE_SERVICE_TYPES[group];if(!types?.length)continue;try{const res=await Place.searchNearby({fields:['id','displayName','location','primaryType','googleMapsURI'],locationRestriction:{center:{lat:ll.lat(),lng:ll.lng()},radius:50000},includedTypes:types,maxResultCount:1,rankPreference:SearchNearbyRankPreference.DISTANCE,language:'es',region:'py'}),place=res.places?.[0];if(!place?.location)continue;const km=google.maps.geometry.spherical.computeDistanceBetween(ll,place.location)/1000;rows.push({id:place.id,place_id:place.id,name:place.displayName||group,lat:place.location.lat(),lon:place.location.lng(),query_category:group,primary_type:place.primaryType||'',distance_km:km,air_distance_km:km,google_maps_uri:place.googleMapsURI||'',source:'google_places'})}catch(e){console.warn('Google Places',group,e);errors.push(`${group}: ${e?.message||e}`)}}return{rows,errors}}
async function queryNearest(ll){const groups=selectedServiceGroups();if(!groups.length)return;state.serviceOrigin={lat:ll.lat(),lon:ll.lng()};document.getElementById('service-results').textContent='Buscando POIs en Google…';const googleGroups=groups.filter(g=>GOOGLE_SERVICE_TYPES[g]),localGroups=groups.filter(g=>!GOOGLE_SERVICE_TYPES[g]);try{const gp=await googleNearestRows(ll,googleGroups);let localRows=[],localMethod='';if(localGroups.length){const ep=document.getElementById('driving-times').checked?'nearest-driving':'nearest',lp=await fetchJson(`/api/services/${ep}?lat=${ll.lat()}&lon=${ll.lng()}&groups=${encodeURIComponent(localGroups.join(','))}`);localRows=lp.services||[];localMethod=lp.method||'base local'}if(googleGroups.length&&!gp.rows.length&&gp.errors.length)throw new Error('Google Places no pudo consultar los POIs. Activa Places API (New) para la misma clave del mapa. '+gp.errors[0]);renderNearest({method:[gp.rows.length?'Google Places (en vivo)':'',localRows.length?localMethod:''].filter(Boolean).join(' + '),services:[...gp.rows,...localRows],google_errors:gp.errors})}catch(e){document.getElementById('service-results').textContent=e.message}}
function renderNearest(p){const c=document.getElementById('service-results'),rows=p.services||[];c.innerHTML=`<div class="info-sub">Método: ${esc(p.method||'')}</div>`;if(!rows.length){c.innerHTML+='No se encontraron resultados.';return}for(const r of rows){const x=document.createElement('div');x.className='service-result';const src=r.source==='google_places'?'Google':'local';x.innerHTML=`<strong>${esc(r.name||r.query_category)}</strong><br>${r.duration_minutes!==undefined?fmt(r.duration_minutes,1)+' min · ':''}${fmt(r.distance_km??r.air_distance_km,2)} km <span class="small">(${esc(r.query_category||'')} · ${src})</span>${r.google_maps_uri?`<br><a href="${esc(r.google_maps_uri)}" target="_blank" rel="noopener">Ver POI en Google Maps ↗</a>`:''}`;const b=document.createElement('button');b.textContent='Dibujar ruta';b.className='secondary';b.onclick=()=>drawRoute(r);x.appendChild(document.createElement('br'));x.appendChild(b);c.appendChild(x)}}
"""

MODE_OLD = "    document.getElementById('google-mode').innerHTML=GOOGLE_PLACES_SERVER_ENABLED?'<strong>Evaluación Google activada.</strong> Los servicios públicos de los lotes se consultan con Places API; industrias siguen usando la base local.':'<strong>Modo sin Places API.</strong> Los POIs del mapa siguen siendo de Google, pero el puntaje numérico de servicios usa la base local existente.';"
MODE_NEW = "    document.getElementById('google-mode').innerHTML='<strong>POIs Google en vivo.</strong> Al analizar un punto, hospitales, salud, educación, supermercados, farmacias, bancos, combustible, policía, bomberos y mercados se consultan directamente con Google Places. Industrias conservan la base local.';"

LOADER_OLD = '<script async src="https://maps.googleapis.com/maps/api/js?key={{ google_key }}&loading=async&libraries=geometry&callback=initMap&v=weekly"></script>'
LOADER_NEW = '<script async src="https://maps.googleapis.com/maps/api/js?key={{ google_key }}&loading=async&libraries=geometry,places&callback=initMap&v=weekly"></script>'


def _patch(html: str, worker) -> str:
    patches = [
        ("service_copy", DESC_OLD, DESC_NEW),
        ("google_nearest", QUERY_OLD, QUERY_NEW),
        ("google_mode", MODE_OLD, MODE_NEW),
        ("places_library", LOADER_OLD, LOADER_NEW),
    ]
    for name, old, new in patches:
        if old in html:
            html = html.replace(old, new, 1)
            worker.log.info("Applied live Google POI patch: %s", name)
        elif new in html:
            worker.log.info("Live Google POI patch already present: %s", name)
        else:
            worker.log.warning("Live Google POI patch target missing: %s", name)
    return html


def post_fork(server, worker):
    # First retain the existing fast-layer + hover fixes.
    _base.post_fork(server, worker)

    import app_google_maps as google_app

    google_app.GOOGLE_INDEX_HTML = _patch(google_app.GOOGLE_INDEX_HTML, worker)
    google_app.legacy.APP_BUILD = "2026-08-07-GMAPS-R3-LIVE-GOOGLE-POIS"
    google_app.APP_BUILD = google_app.legacy.APP_BUILD
