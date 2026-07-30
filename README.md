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

