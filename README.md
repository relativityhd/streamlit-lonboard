# streamlit-lonboard

A Streamlit custom component for [lonboard](https://github.com/developmentseed/lonboard) — fast, GPU-accelerated geospatial visualization in Streamlit, powered by [deck.gl](https://deck.gl) and [GeoArrow](https://geoarrow.org).

> **Status: early development.** Scatterplot/Path/Polygon/SolidPolygon layers, multi-layer maps, click/hover picking, and view-state persistence across reruns all work. Heatmap is wired but untested; Bitmap/Raster layers aren't supported yet. See [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md) for the roadmap and progress.
>
> **pyarrow 25.0.0 is excluded** (`pyarrow>=14,!=25.0.0` in `pyproject.toml`): its bundled mimalloc 3.3.1 segfaults when libarrow is first loaded on a non-main thread that then exits — which is exactly how Streamlit runs every script. Known upstream as [apache/arrow#50471](https://github.com/apache/arrow/issues/50471) / [microsoft/mimalloc#1287](https://github.com/microsoft/mimalloc/issues/1287); no fixed release yet. If another dependency forces 25.0.0 on you, set `ARROW_DEFAULT_MEMORY_POOL=system` as a workaround. All Python versions ≥3.10 (including 3.14) are supported.

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

## Performance

Streamlit reruns your whole script on every interaction, so building this
`ScatterplotLayer` from scratch happens again on every rerun unless you cache
it. **Wrap layer construction in `@st.cache_resource`**:

```python
@st.cache_resource
def build_layer():
    gdf = gpd.read_parquet("internet-speeds.parquet")
    return ScatterplotLayer.from_geopandas(gdf, get_fill_color=[255, 0, 0])

layer = build_layer()
result = st_lonboard(layers=[layer], height=600, key="map")
```

This matters more than it might look like: `st_lonboard()` memoizes its own
Arrow serialization keyed on the layer *object*, so a cached layer skips
re-serialization entirely on reruns that don't touch it (invalidated
automatically if you mutate a layer's properties). Without
`@st.cache_resource`, a fresh layer object is built every rerun and the cache
never hits. See `examples/app.py` for a full example and
[`IMPLEMENTATION_PLAN.md`](./IMPLEMENTATION_PLAN.md) Phase 4 for the full
performance investigation, including a genuinely surprising find: Streamlit's
component runtime already skips re-parsing and re-rendering on the frontend
entirely when a rerun's output is byte-for-byte unchanged (see
[`benchmarks/RESULTS.md`](./benchmarks/RESULTS.md) for measured numbers at
10k/100k/1M points) — so the main thing left to optimize is Python-side
re-serialization, which is exactly what the cache above avoids.

## Development

Managed with [uv](https://docs.astral.sh/uv/). A [Hatchling build
hook](hatch_build.py) runs `npm install && npm run build` automatically
whenever the package is built or synced, so `uv sync`/`uv build` produce a
wheel with the frontend already bundled into
`src/streamlit_lonboard/frontend_dist/` (gitignored source-tree-side; only
Node is required to build it, not to install the published wheel):

```bash
uv sync --extra dev
uv run streamlit run examples/app.py
```

If you edit the frontend, run `cd frontend && npm run dev` (watch build) or
`npm run build` (one-off) yourself and refresh the browser tab — the build
hook only runs when the package itself is (re)built (`uv sync`/`uv build`),
not on every `uv run`.

```bash
uv build          # sdist + wheel into dist/
uv run pytest     # tests/test_serialize.py
uv run ruff check # lint
```

## License

MIT

## Acknowledgements

- [lonboard](https://github.com/developmentseed/lonboard) by Development Seed
- [@geoarrow/deck.gl-layers](https://github.com/geoarrow/deck.gl-layers)
- Prior discussion: [developmentseed/lonboard#342](https://github.com/developmentseed/lonboard/discussions/342)
