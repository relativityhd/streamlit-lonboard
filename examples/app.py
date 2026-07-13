"""Minimal demo: 1M-point scatterplot, Arrow all the way down."""

import geopandas as gpd
import streamlit as st
from lonboard import ScatterplotLayer

from streamlit_lonboard import st_lonboard

st.title("streamlit-lonboard demo")

url = "https://raw.githubusercontent.com/geoarrow/geoarrow-data/main/example/example-point.parquet"  # placeholder dataset
gdf = gpd.read_parquet(url)
layer = ScatterplotLayer.from_geopandas(gdf, get_fill_color=[200, 30, 30], radius_min_pixels=2)

result = st_lonboard(layers=[layer], height=600, key="map")
if result.clicked:
    st.json(result.clicked)
