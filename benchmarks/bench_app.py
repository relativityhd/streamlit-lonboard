"""Phase 4.0/4d baseline benchmark app: N-point scatterplot, perf spans on.

Usage: BENCH_N=1000000 ST_LONBOARD_PERF=1 streamlit run benchmarks/bench_app.py
Add BENCH_COMPRESSION=gzip|none to force a compression mode (default: "auto",
st_lonboard()'s own default). Add BENCH_CLUSTERED=1 for clustered (not
uniform-random) points - see the note below on why that changes gzip ratios.

Reports serialize/pack/mount timings via the streamlit_lonboard.perf logger
(stderr) and parse/build timings via the browser console (see _perf.py /
frontend/src/index.ts). Two buttons:
- "Rerun (unchanged data)": re-executes the script with byte-identical
  output. Found (Phase 4.0) that Streamlit's CCv2 skips invoking the
  frontend's mount() entirely in this case - not just reusing cached
  bytes, the JS callback never fires - so this path costs ~0 frontend CPU
  already, gated only on our serialization being deterministic.
- "Rerun (new data)": reseeds the RNG so the payload genuinely differs,
  forcing a real mount() invocation - this measures the "cold" parse +
  deck.gl layer-build cost at N points.
"""

import os

import geopandas as gpd
import numpy as np
import streamlit as st
from lonboard import ScatterplotLayer
from shapely.geometry import Point

from streamlit_lonboard import st_lonboard

N = int(os.environ.get("BENCH_N", "10000"))
_compression_env = os.environ.get("BENCH_COMPRESSION", "auto")
COMPRESSION = None if _compression_env == "none" else _compression_env
CLUSTERED = os.environ.get("BENCH_CLUSTERED") == "1"

st.title(f"Phase 4.0 baseline: {N:,} points (compression={COMPRESSION!r})")
col1, col2 = st.columns(2)
col1.button("Rerun (unchanged data)")
if col2.button("Rerun (new data)"):
    st.session_state["seed"] = st.session_state.get("seed", 0) + 1

rng = np.random.default_rng(st.session_state.get("seed", 0))
if CLUSTERED:
    # Uniform-random coordinates are close to gzip's worst case (near-maximal
    # entropy); real geographic data clusters (cities, sensor networks, GPS
    # tracks), which compresses far better. This mode approximates that for a
    # fairer compression-ratio measurement - see benchmarks/RESULTS.md.
    n_clusters = 20
    cluster_centers = rng.uniform([-122.6, 37.6], [-122.3, 37.9], size=(n_clusters, 2))
    assignments = rng.integers(0, n_clusters, N)
    jitter = rng.normal(0, 0.01, size=(N, 2))
    coords = cluster_centers[assignments] + jitter
    lon, lat = coords[:, 0], coords[:, 1]
else:
    lon = rng.uniform(-122.6, -122.3, N)
    lat = rng.uniform(37.6, 37.9, N)
colors = rng.integers(0, 255, size=(N, 3)).astype("uint8")
radii = rng.uniform(20, 200, N).astype("float32")

gdf = gpd.GeoDataFrame(geometry=[Point(x, y) for x, y in zip(lon, lat)], crs="EPSG:4326")
layer = ScatterplotLayer.from_geopandas(
    gdf, get_fill_color=colors, get_radius=radii, radius_units="meters", pickable=True
)

result = st_lonboard(
    layers=[layer], height=500, return_view_state=True, compression=COMPRESSION, key="map"
)
st.write({"view_state": result.view_state, "seed": st.session_state.get("seed", 0)})
