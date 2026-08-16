"""Demo: layer extensions and map-level features added alongside them.

- `PathStyleExtension`: a dashed path (a plain `[dash, gap]` pair here, not a
  per-row array - both are valid, see README.md).
- `DataFilterExtension` driven by an `st.slider`: the layer object is built
  once (`@st.cache_resource`) and its `filter_range` trait is mutated on every
  rerun to match the slider. The Arrow bytes never change across reruns (only
  `filter_range`, a JSON prop, does) - this is exactly the case the frontend's
  props-aware layer cache exists for; without it, the slider would silently
  have no visible effect.
- `custom_attribution` and a customized `NavigationControl` (`map.controls`,
  since there's no separate `controls=` argument - it only makes sense
  attached to a `Map`), demonstrated by passing `map=` instead of `layers=`.
- `tooltip=` showing a couple of attribute columns on hover.
"""

import geopandas as gpd
import numpy as np
import streamlit as st
from lonboard import Map, PathLayer, ScatterplotLayer
from lonboard.controls import NavigationControl
from lonboard.layer_extension import DataFilterExtension, PathStyleExtension
from shapely.geometry import LineString, Point

from streamlit_lonboard import st_lonboard

st.title("streamlit-lonboard extensions demo")

st.button("Rerun (view state / pan+zoom should survive this)")


@st.cache_resource
def build_dashed_path_layer() -> PathLayer:
    gdf = gpd.GeoDataFrame(
        geometry=[
            LineString([(-122.6, 37.6), (-122.3, 37.9)]),
            LineString([(-122.6, 37.9), (-122.3, 37.6)]),
        ],
        crs="EPSG:4326",
    )
    return PathLayer.from_geopandas(
        gdf,
        extensions=[PathStyleExtension(dash=True)],
        get_dash_array=[4, 2],  # a constant [dash, gap] pair - not a per-row array
        get_color=[200, 30, 30],
        get_width=30,
        width_units="meters",
        pickable=True,
    )


@st.cache_resource
def build_filterable_layer() -> ScatterplotLayer:
    rng = np.random.default_rng(0)
    n = 500
    lon = rng.uniform(-122.55, -122.35, n)
    lat = rng.uniform(37.65, 37.85, n)
    values = rng.uniform(0, 100, n).astype("float32")
    labels = np.array([f"point-{i}" for i in range(n)])
    gdf = gpd.GeoDataFrame(
        {"label": labels, "value": values},
        geometry=[Point(x, y) for x, y in zip(lon, lat)],
        crs="EPSG:4326",
    )
    return ScatterplotLayer.from_geopandas(
        gdf,
        extensions=[DataFilterExtension()],
        get_filter_value=values,
        filter_range=(0, 100),
        get_fill_color=[30, 120, 200],
        get_radius=150,
        radius_units="meters",
        pickable=True,
    )


dashed_path_layer = build_dashed_path_layer()
filterable_layer = build_filterable_layer()

filter_range = st.slider(
    "Filter scatterplot points by value",
    min_value=0.0,
    max_value=100.0,
    value=(0.0, 100.0),
)
# Mutating the cached layer's trait directly (rather than rebuilding the
# layer) is what exercises the byte-identical-but-props-changed case.
filterable_layer.filter_range = filter_range

# `controls=` only makes sense on a `Map` (there's no separate st_lonboard()
# parameter for it) - customizing one control here also demonstrates the
# `map=` calling style as an alternative to `layers=`.
demo_map = Map(
    layers=[dashed_path_layer, filterable_layer],
    controls=[NavigationControl(show_compass=False, position="top-left")],
)

result = st_lonboard(
    map=demo_map,
    height=600,
    on_click=True,
    on_hover=True,
    return_view_state=True,
    custom_attribution="streamlit-lonboard demo",
    tooltip=["label", "value"],
    key="extensions-map",
)

col1, col2, col3 = st.columns(3)
col1.write({"clicked": result.clicked})
col2.write({"hovered": result.hovered})
col3.write({"view_state": result.view_state})
