"""Minimal demo: synthetic point + path data, Arrow all the way down."""

import geopandas as gpd
import numpy as np
import streamlit as st
from lonboard import PathLayer, ScatterplotLayer, SolidPolygonLayer
from shapely.geometry import LineString, Point, Polygon

from streamlit_lonboard import st_lonboard

st.title("streamlit-lonboard demo")

st.button("Rerun (view state / pan+zoom should survive this)")

rng = np.random.default_rng(0)
n = 2_000
lon = rng.uniform(-122.6, -122.3, n)
lat = rng.uniform(37.6, 37.9, n)
colors = rng.integers(0, 255, size=(n, 3)).astype("uint8")
radii = rng.uniform(20, 200, size=n).astype("float32")

points_gdf = gpd.GeoDataFrame(geometry=[Point(x, y) for x, y in zip(lon, lat)], crs="EPSG:4326")
scatter_layer = ScatterplotLayer.from_geopandas(
    points_gdf,
    get_fill_color=colors,
    get_radius=radii,
    radius_units="meters",
    pickable=True,
)

paths_gdf = gpd.GeoDataFrame(
    geometry=[
        LineString([(-122.6, 37.6), (-122.3, 37.9)]),
        LineString([(-122.6, 37.9), (-122.3, 37.6)]),
    ],
    crs="EPSG:4326",
)
path_layer = PathLayer.from_geopandas(
    paths_gdf,
    get_color=[0, 100, 200],
    get_width=50,
    width_units="meters",
    pickable=True,
)

polygon_gdf = gpd.GeoDataFrame(
    geometry=[Polygon([(-122.5, 37.7), (-122.5, 37.8), (-122.4, 37.8), (-122.4, 37.7)])],
    crs="EPSG:4326",
)
polygon_layer = SolidPolygonLayer.from_geopandas(
    polygon_gdf,
    get_fill_color=[0, 150, 0, 100],
    pickable=True,
)

result = st_lonboard(
    layers=[scatter_layer, path_layer, polygon_layer],
    height=600,
    on_click=True,
    on_hover=True,
    return_view_state=True,
    key="map",
)

col1, col2, col3 = st.columns(3)
col1.write({"clicked": result.clicked})
col2.write({"hovered": result.hovered})
col3.write({"view_state": result.view_state})
