"""Phase 4c contender: st_lonboard.

Usage: BENCH_N=1000000 streamlit run benchmarks/contenders/lonboard_app.py

Renders N points via st_lonboard with return_view_state=True (so a pan/zoom
triggers a real Streamlit rerun - the only one of the three contenders where
"interaction-triggered rerun latency" is even a meaningful metric; see
benchmarks/RESULTS.md). One "Rerun (unchanged data)" button, matching the
other two contender apps so the Playwright driver can drive all three
identically.
"""

import os
import sys

import streamlit as st
from lonboard import ScatterplotLayer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datagen import points_geodataframe

from streamlit_lonboard import st_lonboard

N = int(os.environ.get("BENCH_N", "10000"))

st.title(f"Contender: st_lonboard ({N:,} points)")
st.button("Rerun (unchanged data)")


@st.cache_resource
def build_layer(n: int) -> ScatterplotLayer:
    gdf = points_geodataframe(n)
    return ScatterplotLayer.from_geopandas(
        gdf,
        get_fill_color=gdf[["r", "g", "b"]].to_numpy(),
        get_radius=gdf["radius"].to_numpy(),
        radius_units="meters",
        pickable=True,
    )


result = st_lonboard(
    layers=[build_layer(N)], height=500, return_view_state=True, key="map"
)
st.write({"view_state": result.view_state})
