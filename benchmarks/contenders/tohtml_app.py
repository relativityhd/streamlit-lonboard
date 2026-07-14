"""Phase 4c contender: lonboard's Map.to_html() via components.v1.html.

Usage: BENCH_N=1000000 streamlit run benchmarks/contenders/tohtml_app.py

This is the workaround people use today to get lonboard maps into Streamlit
without a custom component: build a standalone lonboard Map, serialize the
*entire* page (including the pydeck.gl/MapLibre JS runtime, inlined) to one
HTML string, and hand it to components.v1.html as an opaque iframe blob.
Unlike st_lonboard, there is no caching of any kind here - every rerun
(unchanged data or not) regenerates and re-embeds the full HTML string from
scratch, and Streamlit has no way to know the iframe content didn't change.
Only a "Rerun (unchanged data)" button: to_html() output is fully static
(no Python<->JS channel at all), so "interaction-triggered rerun latency" is
architecturally N/A here too - nothing is ever sent back to Python.
"""

import os
import sys

import streamlit as st
import streamlit.components.v1 as components
from lonboard import Map, ScatterplotLayer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datagen import points_geodataframe

N = int(os.environ.get("BENCH_N", "10000"))

st.title(f"Contender: Map.to_html() ({N:,} points)")
st.button("Rerun (unchanged data)")

# Deliberately NOT @st.cache_resource'd for the HTML string itself - to_html()
# is regenerated on every single rerun in this contender, which is the whole
# point of the comparison (this is what "just call to_html() from Streamlit"
# looks like in practice, warts and all).
gdf = points_geodataframe(N)
layer = ScatterplotLayer.from_geopandas(
    gdf,
    get_fill_color=gdf[["r", "g", "b"]].to_numpy(),
    get_radius=gdf["radius"].to_numpy(),
    radius_units="meters",
    pickable=True,
)
html = Map(layers=[layer]).to_html()
components.html(html, height=500)
