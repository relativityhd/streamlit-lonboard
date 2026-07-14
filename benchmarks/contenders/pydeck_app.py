"""Phase 4c contender: st.pydeck_chart.

Usage: BENCH_N=1000000 streamlit run benchmarks/contenders/pydeck_app.py

Renders N points via pydeck's ScatterplotLayer (GeoJSON/JSON row-oriented
data, not GeoArrow - pydeck has no Arrow support). Only a "Rerun (unchanged
data)" button: pydeck has no view-state-change callback wired up to
st.pydeck_chart, so panning/zooming never triggers a Streamlit rerun at all -
"interaction-triggered rerun latency" is architecturally N/A for this
contender (not a limitation of this benchmark - there's nothing to measure).
"""

import os
import sys

import pydeck as pdk
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datagen import points_dataframe

N = int(os.environ.get("BENCH_N", "10000"))

st.title(f"Contender: st.pydeck_chart ({N:,} points)")
st.button("Rerun (unchanged data)")


@st.cache_resource
def build_deck(n: int) -> pdk.Deck:
    df = points_dataframe(n)
    layer = pdk.Layer(
        "ScatterplotLayer",
        data=df,
        get_position=["lon", "lat"],
        get_fill_color=["r", "g", "b"],
        get_radius="radius",
        radius_units="meters",
        pickable=True,
    )
    view_state = pdk.ViewState(
        longitude=float(df["lon"].mean()),
        latitude=float(df["lat"].mean()),
        zoom=9,
    )
    return pdk.Deck(layers=[layer], initial_view_state=view_state, map_provider="carto")


st.pydeck_chart(build_deck(N), height=500)
