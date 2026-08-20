"""Visual end-to-end check for every wire option in one page.

Usage: uv run streamlit run benchmarks/wire_verify_app.py

Renders the same scatterplot once per `compression=` mode ("auto", None,
"gzip", "zstd", "parquet"). Every map must look identical and the browser
console must stay error-free. N is chosen above
`serialize.AUTO_COMPRESSION_THRESHOLD` so the "auto" map resolves to Parquet
- i.e. this page covers the default path, not just the explicit ones.
"""

import os

os.environ.setdefault("ST_LONBOARD_PERF", "1")  # noqa: E402 - must precede streamlit_lonboard import

import numpy as np
import streamlit as st

import geopandas as gpd
from lonboard import ScatterplotLayer
from shapely.geometry import Point

from streamlit_lonboard import st_lonboard

# Above AUTO_COMPRESSION_THRESHOLD (~2.3MB of table at this N), so the
# "auto" map below actually exercises the Parquet branch of the default.
N = 100_000
MODES = ["auto", None, "gzip", "zstd", "parquet"]

st.set_page_config(layout="wide")
st.title(f"Wire-format verification: {N:,} points x {len(MODES)} encodings")


@st.cache_resource
def build_layer() -> ScatterplotLayer:
    rng = np.random.default_rng(42)
    lon = rng.uniform(-122.6, -122.3, N)
    lat = rng.uniform(37.6, 37.9, N)
    colors = rng.integers(0, 255, size=(N, 3)).astype("uint8")
    radii = rng.uniform(20, 200, N).astype("float32")
    gdf = gpd.GeoDataFrame(
        {"label": [f"point-{i}" for i in range(N)]},
        geometry=[Point(x, y) for x, y in zip(lon, lat)],
        crs="EPSG:4326",
    )
    return ScatterplotLayer.from_geopandas(
        gdf,
        get_fill_color=colors,
        get_radius=radii,
        radius_units="meters",
        pickable=True,
    )


VIEW = {"longitude": -122.45, "latitude": 37.75, "zoom": 10}
# "auto" included so the *default* path is covered too - at N=20,000 this
# layer is over AUTO_COMPRESSION_THRESHOLD, so it must resolve to Parquet.
MODES = ["auto", None, "gzip", "zstd", "parquet"]

# NOTE: one shared cached layer object on purpose - this also exercises
# serialize_layer_cached's encoding-aware cache keying (four different
# encodings of the same layer object in a single script run).
layer = build_layer()

cols = st.columns(2)
for i, mode in enumerate(MODES):
    with cols[i % 2]:
        st.subheader(f"compression={mode!r}")
        st_lonboard(
            layers=[layer],
            view_state=VIEW,
            height=350,
            compression=mode,
            tooltip=["label"],
            key=f"map-{mode}",
        )
