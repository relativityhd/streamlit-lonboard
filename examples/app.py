"""Minimal demo: synthetic point + path data, Arrow all the way down.

Layer construction is wrapped in `@st.cache_resource` functions: Streamlit
reruns this whole script on every interaction, and without caching that would
rebuild a fresh lonboard Layer object every time. streamlit_lonboard's own
serialization cache (see IMPLEMENTATION_PLAN.md Phase 4a) is keyed on layer
*object identity*, so it can only help across reruns if the *same* object
comes back - `st.cache_resource` is what makes that true here.
"""

import geopandas as gpd
import numpy as np
import streamlit as st
from lonboard import PathLayer, ScatterplotLayer, SolidPolygonLayer
from shapely.geometry import LineString, Point, Polygon

from streamlit_lonboard import st_lonboard

st.title("streamlit-lonboard demo")

st.button("Rerun (view state / pan+zoom should survive this)")

# Regression check: the map instance is only ever created once (see
# createMount in index.ts), so onClick/onHover/returnViewState must be read
# from state - refreshed every mount() call - rather than closed over from
# the first mount's header. Toggling this WITHOUT the map remounting and then
# clicking a feature must reflect the *current* toggle state.
if "on_click_enabled" not in st.session_state:
    st.session_state.on_click_enabled = True
if st.button("Toggle on_click (regression check, see comment above)"):
    st.session_state.on_click_enabled = not st.session_state.on_click_enabled
on_click_enabled = st.session_state.on_click_enabled
st.caption(f"on_click is currently **{'ON' if on_click_enabled else 'OFF'}**")


@st.cache_resource
def build_scatter_layer() -> ScatterplotLayer:
    rng = np.random.default_rng(0)
    n = 2_000
    lon = rng.uniform(-122.6, -122.3, n)
    lat = rng.uniform(37.6, 37.9, n)
    colors = rng.integers(0, 255, size=(n, 3)).astype("uint8")
    radii = rng.uniform(20, 200, size=n).astype("float32")
    gdf = gpd.GeoDataFrame(geometry=[Point(x, y) for x, y in zip(lon, lat)], crs="EPSG:4326")
    return ScatterplotLayer.from_geopandas(
        gdf,
        get_fill_color=colors,
        get_radius=radii,
        radius_units="meters",
        pickable=True,
    )


@st.cache_resource
def build_path_layer() -> PathLayer:
    gdf = gpd.GeoDataFrame(
        geometry=[
            LineString([(-122.6, 37.6), (-122.3, 37.9)]),
            LineString([(-122.6, 37.9), (-122.3, 37.6)]),
        ],
        crs="EPSG:4326",
    )
    return PathLayer.from_geopandas(
        gdf,
        get_color=[0, 100, 200],
        get_width=50,
        width_units="meters",
        pickable=True,
    )


@st.cache_resource
def build_polygon_layer() -> SolidPolygonLayer:
    gdf = gpd.GeoDataFrame(
        geometry=[Polygon([(-122.5, 37.7), (-122.5, 37.8), (-122.4, 37.8), (-122.4, 37.7)])],
        crs="EPSG:4326",
    )
    return SolidPolygonLayer.from_geopandas(gdf, get_fill_color=[0, 150, 0, 100], pickable=True)


scatter_layer = build_scatter_layer()
path_layer = build_path_layer()
polygon_layer = build_polygon_layer()

result = st_lonboard(
    layers=[scatter_layer, path_layer, polygon_layer],
    height=600,
    on_click=on_click_enabled,
    on_hover=True,
    return_view_state=True,
    key="map",
)

col1, col2, col3 = st.columns(3)
col1.write({"clicked": result.clicked})
col2.write({"hovered": result.hovered})
col3.write({"view_state": result.view_state})
