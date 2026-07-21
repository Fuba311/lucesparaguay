# Mapa interactivo de luces nocturnas de Paraguay

Esta aplicación usa **los resultados ya generados** por `luces_nocturnas_paraguay.py`. No vuelve a consultar Google Earth Engine.

## Funciones incluidas

- OpenStreetMap, Carto claro y Carto oscuro como mapas base.
- Píxeles VIIRS por año, cambio absoluto y cambio porcentual como teselas raster.
- Raster estable con panel propio: no desaparece detrás del mapa base al cambiar de selección.
- Se conserva la capa raster anterior hasta que la nueva termina de cargar.
- Consulta de valores del píxel al hacer clic.
- Departamentos, distritos, ciudades, pueblos y localidades.
- Buscador global por nombre de departamento, distrito, ciudad, pueblo o localidad.
- Nombres geográficos visibles por encima del raster y etiquetas permanentes ajustadas al zoom.
- Tooltips con radiancia inicial/final, cambio absoluto, cambio porcentual, CAGR y ranking.
- Gráfico anual al seleccionar una zona.
- Mapa de calor de hotspots basado en el raster de cambio absoluto.
- Filtro por departamento y ranking interactivo de las zonas con mayor crecimiento.

Las capas y controles de pines o áreas especiales de Fernando no se muestran en esta versión.

## Estructura esperada

Coloca la carpeta generada por el análisis junto a `app.py`:

```text
mapa_interactivo_paraguay/
├─ app.py
├─ requirements.txt
├─ render.yaml
└─ Resultados_luces_nocturnas_Paraguay/
   ├─ vectores/areas_estudio_paraguay.gpkg
   ├─ tablas/metricas_anuales_todas_las_zonas.csv
   ├─ tablas/ranking_crecimiento.csv
   └─ cache/rasters/paraguay/
      ├─ viirs_2014.tif
      ├─ viirs_....tif
      ├─ cambio_abs_2014_ULTIMO.tif
      └─ cambio_pct_2014_ULTIMO.tif
```

También puedes mantener los datos en otra ruta y definir `DATA_DIR`.

## Ejecución local en Windows

Abre PowerShell en la carpeta del proyecto:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:DATA_DIR="C:\ruta\a\Resultados_luces_nocturnas_Paraguay"
python app.py
```

Abre `http://localhost:8050`.

Si ya instalaste las dependencias, basta con:

```powershell
$env:DATA_DIR="C:\ruta\a\Resultados_luces_nocturnas_Paraguay"
python app.py
```

## Publicación en Render

1. Sube esta carpeta y la carpeta de resultados a un repositorio.
2. Crea un servicio desde `render.yaml`, o configura manualmente:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120`
3. Asegúrate de que `DATA_DIR` apunte a la carpeta de resultados.

Los GeoTIFF nacionales pueden ser grandes. Si no deseas guardarlos en Git, colócalos en un volumen o almacenamiento persistente accesible por el servicio y cambia `DATA_DIR`.

## Nota sobre las etiquetas

Los nombres superiores usan teselas de etiquetas de Carto/OpenStreetMap y no requieren clave. Las etiquetas de la capa seleccionada se reducen automáticamente al alejar el mapa para evitar que se superpongan demasiado.
