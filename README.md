# streamlit-lonboard

A Streamlit custom component for [lonboard](https://github.com/developmentseed/lonboard) — fast, GPU-accelerated geospatial visualization in Streamlit, powered by [deck.gl](https://deck.gl) and [GeoArrow](https://geoarrow.org).

> **Status: early development.** Scatterplot/Path/Polygon/SolidPolygon layers, multi-layer maps, click/hover picking, and view-state persistence across reruns all work. Heatmap is wired but untested; Bitmap/Raster layers aren't supported yet. See [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md) for the roadmap and progress.

## Why?

Streamlit's built-in DeckGL support (`st.pydeck_chart`) goes through [pydeck](https://pydeck.gl/), which serializes data as **GeoJSON/JSON** — slow to encode, slow to transfer, slow to parse, and impractical beyond ~100k features.

Lonboard instead moves data as **Apache Arrow** (GeoArrow) binary buffers that deck.gl can consume with zero parsing. But lonboard is built on [anywidget](https://anywidget.dev/) / Jupyter widgets, which Streamlit does not support. The lonboard maintainers consider a Streamlit connector [out of scope for lonboard itself, but support a third-party one](https://github.com/developmentseed/lonboard/discussions/342) — this project is that connector.

The previously suggested workarounds don't cut it:

- `Map.to_html()` + `st.components.v1.html`: static snapshot, no bidirectionality, full re-render on every rerun, huge inlined HTML.
- [`streamlit-deckgl`](https://pypi.org/project/streamlit-deckgl/): pydeck/JSON only — exactly the bottleneck we want to avoid.

## How it works

```
lonboard Map/Layers (Python)          frontend (TypeScript)
  pyarrow.Table (GeoArrow)              apache-arrow: parse IPC
    → Arrow IPC bytes          ──────►    → @geoarrow/deck.gl-layers
  layer props → JSON                       → deck.gl + MapLibre basemap
        ▲                                       │
        └── picking / view state (bidi) ◄───────┘
```

Data crosses the Python↔browser boundary as raw Arrow IPC bytes via Streamlit's [custom components v2](https://docs.streamlit.io/develop/concepts/custom-components/components-v2) — no GeoJSON anywhere in the pipeline.

## API

```python
import geopandas as gpd
import streamlit as st
from lonboard import ScatterplotLayer
from streamlit_lonboard import st_lonboard

gdf = gpd.read_parquet("internet-speeds.parquet")
layer = ScatterplotLayer.from_geopandas(gdf, get_fill_color=[255, 0, 0])

result = st_lonboard(layers=[layer], height=600, key="map")
st.write("Clicked feature index:", result.clicked)
```

## Development

The frontend must be built before the Python package will render anything
(its assets live in `src/streamlit_lonboard/frontend_dist/`, which is
gitignored):

```bash
# Frontend (build first - the Python package serves this output)
cd frontend && npm install && npm run build

# Python
cd ..
pip install -e ".[dev]"
streamlit run examples/app.py
```

Re-run `npm run build` (or `npm run dev` for a watch build) after any
frontend change and refresh the browser tab.

## License

MIT

## Acknowledgements

- [lonboard](https://github.com/developmentseed/lonboard) by Development Seed
- [@geoarrow/deck.gl-layers](https://github.com/geoarrow/deck.gl-layers)
- Prior discussion: [developmentseed/lonboard#342](https://github.com/developmentseed/lonboard/discussions/342)
