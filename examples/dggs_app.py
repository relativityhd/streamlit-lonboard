"""Demo: DGGS (H3) and Arc layers - the "geometry lives in accessor
columns" case, not a GeoArrow-encoded geometry column.

H3/S2/A5/Geohash and Arc layers have no lonboard-computed default view
state without extra deps (H3 needs `h3-py`; S2/A5/Geohash/Arc never get one)
- `st_lonboard` warns and falls back to `{0, 0, 0}` in that case, so we pass
an explicit `view_state=` here. See README.md for the full layer support
matrix.
"""

import numpy as np
import pyarrow as pa
import streamlit as st
from lonboard import ArcLayer, H3HexagonLayer

from streamlit_lonboard import st_lonboard

st.title("streamlit-lonboard DGGS demo")

st.button("Rerun (view state / pan+zoom should survive this)")


@st.cache_resource
def build_h3_layer() -> H3HexagonLayer:
    # Ten resolution-9 siblings under the same parent hexagon in San
    # Francisco (Uber's canonical H3 example cell, "8928308280fffff", and
    # its neighbors) - a valid H3 index is just a bit-packed integer, so
    # these render correctly with no `h3-py` install required.
    cells = pa.array(
        [
            "8928308280fffff",
            "8928308280effff",
            "8928308280dffff",
            "8928308280cffff",
            "8928308280bffff",
            "8928308280affff",
            "89283082809ffff",
            "89283082808ffff",
            "89283082807ffff",
            "89283082806ffff",
        ]
    )
    fill_colors = np.array(
        [[int(255 * i / 10), 100, 255 - int(255 * i / 10)] for i in range(10)], dtype="uint8"
    )
    return H3HexagonLayer(
        table=pa.table({"cell": cells}),
        get_hexagon=cells,
        get_fill_color=fill_colors,
        opacity=0.7,
        pickable=True,
    )


@st.cache_resource
def build_arc_layer() -> ArcLayer:
    # A few arcs radiating from downtown San Francisco.
    sources = np.array([[-122.4194, 37.7749]] * 3)
    targets = np.array([[-122.27, 37.80], [-122.42, 37.87], [-122.5, 37.7]])
    return ArcLayer(
        table=pa.table({"id": [0, 1, 2]}),
        get_source_position=sources,
        get_target_position=targets,
        get_source_color=[0, 200, 0],
        get_target_color=[200, 0, 0],
        get_width=4,
        pickable=True,
    )


h3_layer = build_h3_layer()
arc_layer = build_arc_layer()

result = st_lonboard(
    layers=[h3_layer, arc_layer],
    view_state={"longitude": -122.41, "latitude": 37.78, "zoom": 10},
    height=600,
    on_click=True,
    on_hover=True,
    return_view_state=True,
    key="dggs-map",
)

col1, col2, col3 = st.columns(3)
col1.write({"clicked": result.clicked})
col2.write({"hovered": result.hovered})
col3.write({"view_state": result.view_state})
