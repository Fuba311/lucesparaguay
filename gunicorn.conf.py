"""Production runtime patches for the Google Maps Luces Paraguay frontend.

The main application remains in app_google_maps.py. These exact string patches
keep the deployment reversible while fixing two regressions from the Google
Maps migration: heavy administrative-layer rendering and missing visible hover
feedback.
"""

CSS_OLD = """    .gm-style .gm-style-iw-c{max-width:340px!important}\n    @media(max-width:900px){#app{display:block}#map{position:fixed;inset:0;height:100dvh}#sidebar{position:fixed;inset:0 auto 0 0;width:min(88vw,380px);height:100dvh;box-shadow:var(--shadow)}body.sidebar-collapsed #sidebar{transform:translateX(-100%)}}\n"""
CSS_NEW = """    .gm-style .gm-style-iw-c{max-width:340px!important}\n    #hover-card{position:fixed;top:18px;right:18px;z-index:900;display:none;pointer-events:none;max-width:310px;padding:10px 12px;border-radius:10px;background:rgba(255,255,255,.96);border:1px solid #dbe3ea;box-shadow:var(--shadow);font-size:11px;line-height:1.35}.hover-name{font-size:13px;font-weight:800;margin-bottom:3px}.hover-meta{color:var(--muted);margin-bottom:5px}.hover-metrics{display:flex;gap:9px;flex-wrap:wrap}.hover-metrics strong{font-variant-numeric:tabular-nums}\n    @media(max-width:900px){ #app{display:block}#map{position:fixed;inset:0;height:100dvh}#sidebar{position:fixed;inset:0 auto 0 0;width:min(88vw,380px);height:100dvh;box-shadow:var(--shadow)}body.sidebar-collapsed #sidebar{transform:translateX(-100%)}}\n"""

BODY_OLD = '<button id="menu-toggle" type="button" aria-label="Abrir menú">⚙</button>\n<div id="app">'
BODY_NEW = '<button id="menu-toggle" type="button" aria-label="Abrir menú">⚙</button>\n<div id="hover-card"></div>\n<div id="app">'

STATE_OLD = "const state={config:null,map:null,raster:null,admin:null,search:null,hotspots:[],adminLabels:[],industries:null,localServices:null,lotPolygons:new Map(),lotsRows:[],chart:null,info:null,route:null,routeMarkers:[],serviceOrigin:null,searchTimer:null,searchRows:[],editingId:null,editingGeometry:null,pointMode:false,drawMode:false,draftVertices:[],draftShape:null,currentGeoJSON:null};"
STATE_NEW = "const state={config:null,map:null,raster:null,admin:null,search:null,hotspots:[],adminLabels:[],industries:null,localServices:null,lotPolygons:new Map(),lotsRows:[],chart:null,info:null,route:null,routeMarkers:[],serviceOrigin:null,searchTimer:null,searchRows:[],editingId:null,editingGeometry:null,pointMode:false,drawMode:false,draftVertices:[],draftShape:null,currentGeoJSON:null,adminDomain:null,adminCache:new Map(),adminViewportMode:false,adminRenderTimer:null};"

ADMIN_OLD = """function clearData(layer){layer.forEach(f=>layer.remove(f))}
function pointIcon(v,d){return{path:google.maps.SymbolPath.CIRCLE,scale:5.5,fillColor:color(v,d),fillOpacity:.9,strokeColor:'#fff',strokeWeight:1}}
async function loadAdmin(){const layer=selectedLayer();if(!layer)return;setStatus('Cargando capa…');const p=new URLSearchParams();if(selectedDep())p.set('department',selectedDep());const gj=await fetchJson(`/api/geojson/${encodeURIComponent(layer)}?${p}`);clearData(state.admin);const d=domain(gj,selectedMetric());state.admin.addGeoJson(gj);state.admin.setStyle(f=>{const v=f.getProperty(selectedMetric()),geom=f.getGeometry()?.getType();if(geom==='Point')return{icon:pointIcon(v,d),zIndex:3};return{strokeColor:'#52616f',strokeWeight:layer==='departamentos'?1.4:.9,fillColor:color(v,d),fillOpacity:.52,zIndex:2}});state.currentGeoJSON=gj;refreshAdminLabels();setStatus(`${gj.meta?.count||gj.features?.length||0} elementos cargados.`)}
"""
ADMIN_NEW = """function clearData(layer){if(!layer)return;layer.forEach(f=>layer.remove(f))}
function pointIcon(v,d){return{path:google.maps.SymbolPath.CIRCLE,scale:5.5,fillColor:color(v,d),fillOpacity:.9,strokeColor:'#fff',strokeWeight:1}}
function hoverHtml(p){return `<div class="hover-name">${esc(p.nombre||p.id_zona||'Zona')}</div><div class="hover-meta">${esc([p.nivel,p.departamento].filter(Boolean).join(' · '))}</div><div class="hover-metrics"><span>${esc(metricLabel(selectedMetric()))}: <strong>${fmt(p[selectedMetric()])}</strong></span><span>CAGR: <strong>${fmt(p.cagr_pct_anual)}%</strong></span></div>`}
function showHover(p){const h=document.getElementById('hover-card');h.innerHTML=hoverHtml(p);h.style.display='block'}
function hideHover(){document.getElementById('hover-card').style.display='none'}
function bindAdminDataEvents(){
  state.admin.addListener('mouseover',e=>{const p=adminFeatureProps(e.feature);showFeature(p,false);showHover(p);state.admin.overrideStyle(e.feature,{strokeColor:'#0f4c81',strokeWeight:3,fillOpacity:.72,zIndex:30})});
  state.admin.addListener('mouseout',e=>{state.admin.revertStyle(e.feature);hideHover()});
  state.admin.addListener('click',e=>{const p=adminFeatureProps(e.feature);showFeature(p,true,e.latLng);loadSeries(p.id_zona,p.nombre)});
}
function resetAdminData(){if(state.admin)state.admin.setMap(null);state.admin=new google.maps.Data({map:state.map});bindAdminDataEvents()}
function sampleRing(ring,maxPts){if(!Array.isArray(ring)||ring.length<=maxPts)return ring;const last=ring.length-1,step=Math.max(1,Math.ceil(last/(maxPts-1))),out=[];for(let i=0;i<last;i+=step)out.push(ring[i]);out.push(ring[last]);return out}
function simplifyGeometry(g,layer){if(!g)return g;const maxPts=layer==='distritos'?180:(layer==='departamentos'?420:110);if(g.type==='Polygon')return{...g,coordinates:g.coordinates.map(r=>sampleRing(r,maxPts))};if(g.type==='MultiPolygon')return{...g,coordinates:g.coordinates.map(poly=>poly.map(r=>sampleRing(r,maxPts)))};return g}
function localityPointLayer(gj){const t=gj?.features?.find(f=>f?.geometry)?.geometry?.type;return selectedLayer().includes('localidades')&&t==='Point'}
function visiblePointFeatures(gj){const feats=gj.features||[],b=state.map.getBounds();if(!b)return feats.slice(0,700);const sw=b.getSouthWest(),ne=b.getNorthEast();let rows=feats.filter(f=>{const c=f.geometry?.coordinates;if(!c||c.length<2)return false;return c[1]>=sw.lat()&&c[1]<=ne.lat()&&c[0]>=sw.lng()&&c[0]<=ne.lng()});const z=state.map.getZoom()||6,cap=z<7?450:z<9?1000:z<11?1800:3000;if(rows.length>cap){const m=selectedMetric();rows=rows.sort((a,b)=>Math.abs(Number(b.properties?.[m])||0)-Math.abs(Number(a.properties?.[m])||0)).slice(0,cap)}return rows}
function adminDisplayGeoJSON(gj){const layer=selectedLayer();if(localityPointLayer(gj))return{...gj,features:visiblePointFeatures(gj)};return{...gj,features:(gj.features||[]).map(f=>({...f,geometry:simplifyGeometry(f.geometry,layer)}))}}
function applyAdminStyle(){const layer=selectedLayer(),d=state.adminDomain||domain(state.currentGeoJSON||{features:[]},selectedMetric());state.admin.setStyle(f=>{const v=f.getProperty(selectedMetric()),geom=f.getGeometry()?.getType();if(geom==='Point')return{icon:pointIcon(v,d),zIndex:3};return{strokeColor:'#52616f',strokeWeight:layer==='departamentos'?1.4:.8,fillColor:color(v,d),fillOpacity:.50,zIndex:2}})}
function renderAdminSubset(){if(!state.currentGeoJSON)return;resetAdminData();const view=adminDisplayGeoJSON(state.currentGeoJSON);state.admin.addGeoJson(view);applyAdminStyle();refreshAdminLabels();const shown=view.features?.length||0,total=state.currentGeoJSON.features?.length||0;setStatus(state.adminViewportMode&&shown<total?`${shown} de ${total} elementos visibles. Mueve o acerca el mapa para ver más.`:`${total} elementos cargados.`)}
function scheduleAdminViewportRender(){if(!state.adminViewportMode)return;clearTimeout(state.adminRenderTimer);state.adminRenderTimer=setTimeout(renderAdminSubset,120)}
async function getAdminGeoJSON(layer,dep){const key=`${layer}|${dep||''}`;if(state.adminCache.has(key))return state.adminCache.get(key);const p=new URLSearchParams();if(dep)p.set('department',dep);const gj=await fetchJson(`/api/geojson/${encodeURIComponent(layer)}?${p}`);state.adminCache.set(key,gj);while(state.adminCache.size>4)state.adminCache.delete(state.adminCache.keys().next().value);return gj}
async function loadAdmin(){const layer=selectedLayer();if(!layer)return;setStatus('Cargando capa…');const gj=await getAdminGeoJSON(layer,selectedDep());state.currentGeoJSON=gj;state.adminDomain=domain(gj,selectedMetric());state.adminViewportMode=localityPointLayer(gj);renderAdminSubset()}
function restyleAdmin(){if(!state.currentGeoJSON||!state.admin)return;state.adminDomain=domain(state.currentGeoJSON,selectedMetric());applyAdminStyle();if(state.adminViewportMode)renderAdminSubset();else refreshAdminLabels()}
"""

METRIC_OLD = "document.getElementById('admin-layer').onchange=async()=>{await loadAdmin();await updateTop()};document.getElementById('metric').onchange=async()=>{await loadAdmin();await updateTop()};document.getElementById('department').onchange=async e=>{await loadAdmin();await updateTop();e.target.value?await zoomDepartment(e.target.value):fitConfig()};document.getElementById('show-admin-labels').onchange=refreshAdminLabels;"
METRIC_NEW = "document.getElementById('admin-layer').onchange=async()=>{await loadAdmin();await updateTop()};document.getElementById('metric').onchange=async()=>{restyleAdmin();await updateTop()};document.getElementById('department').onchange=async e=>{await loadAdmin();await updateTop();e.target.value?await zoomDepartment(e.target.value):fitConfig()};document.getElementById('show-admin-labels').onchange=refreshAdminLabels;"

ADMIN_LISTENERS_OLD = "  state.admin.addListener('mouseover',e=>showFeature(adminFeatureProps(e.feature),false));state.admin.addListener('click',e=>{const p=adminFeatureProps(e.feature);showFeature(p,true,e.latLng);loadSeries(p.id_zona,p.nombre)});\n"

MAP_EVENTS_OLD = "  state.map.addListener('zoom_changed',()=>{refreshAdminLabels();setTimeout(()=>{loadLocalServices();loadIndustrialZones()},100)});state.map.addListener('idle',()=>{loadLocalServices();loadIndustrialZones()});"
MAP_EVENTS_NEW = "  state.map.addListener('zoom_changed',()=>{refreshAdminLabels();scheduleAdminViewportRender();setTimeout(()=>{loadLocalServices();loadIndustrialZones()},100)});state.map.addListener('idle',()=>{scheduleAdminViewportRender();loadLocalServices();loadIndustrialZones()});"

INIT_OLD = "    state.admin=new google.maps.Data({map:state.map});state.search=new google.maps.Data({map:state.map});state.industries=new google.maps.Data({map:state.map});state.localServices=new google.maps.Data({map:state.map});"
INIT_NEW = "    state.search=new google.maps.Data({map:state.map});state.industries=new google.maps.Data({map:state.map});state.localServices=new google.maps.Data({map:state.map});resetAdminData();"

PATCHES = [
    ("css_hover", CSS_OLD, CSS_NEW),
    ("hover_card", BODY_OLD, BODY_NEW),
    ("state", STATE_OLD, STATE_NEW),
    ("admin_engine", ADMIN_OLD, ADMIN_NEW),
    ("metric_restyle", METRIC_OLD, METRIC_NEW),
    ("old_admin_listeners", ADMIN_LISTENERS_OLD, ""),
    ("viewport_events", MAP_EVENTS_OLD, MAP_EVENTS_NEW),
    ("admin_init", INIT_OLD, INIT_NEW),
]


def apply_frontend_patches(html, log=None):
    for name, old, new in PATCHES:
        if old in html:
            html = html.replace(old, new, 1)
            if log:
                log.info("Applied Google Maps frontend patch: %s", name)
        elif new in html:
            if log:
                log.info("Google Maps frontend patch already present: %s", name)
        elif log:
            log.warning("Google Maps frontend patch target missing: %s", name)
    return html


def post_fork(server, worker):
    # Import in the worker so heavy geospatial/SQL state is not preloaded into
    # the Gunicorn master process.
    import app_google_maps as google_app

    google_app.GOOGLE_INDEX_HTML = apply_frontend_patches(
        google_app.GOOGLE_INDEX_HTML, worker.log
    )
    google_app.legacy.APP_BUILD = "2026-08-07-GMAPS-R2-FAST-HOVER"
    google_app.APP_BUILD = google_app.legacy.APP_BUILD
