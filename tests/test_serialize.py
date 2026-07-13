"""Tests for streamlit_lonboard.serialize (importable directly, without pulling in component.py)."""

from __future__ import annotations

import importlib.util
import json
import struct
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyarrow as pa
import pytest
from lonboard import PathLayer, ScatterplotLayer
from shapely.geometry import LineString, Point


def _load_serialize_module():
    path = Path(__file__).resolve().parents[1] / "src" / "streamlit_lonboard" / "serialize.py"
    spec = importlib.util.spec_from_file_location("streamlit_lonboard.serialize", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["streamlit_lonboard.serialize"] = module
    spec.loader.exec_module(module)
    return module


serialize = _load_serialize_module()


@pytest.fixture
def points_gdf():
    return gpd.GeoDataFrame(
        {"attribute": [1, 2, 3]},
        geometry=[Point(0, 0), Point(1, 1), Point(2, 2)],
        crs="EPSG:4326",
    )


def test_build_layer_props_splits_scalar_and_accessor(points_gdf):
    colors = np.array([[255, 0, 0], [0, 255, 0], [0, 0, 255]], dtype="uint8")
    layer = ScatterplotLayer.from_geopandas(points_gdf, get_fill_color=colors, radius_min_pixels=2, pickable=True)

    props, accessor_columns = serialize.build_layer_props(layer)

    assert props["radiusMinPixels"] == 2.0
    assert props["pickable"] is True
    assert props["getFillColor"] == {"@@arrowColumn": "get_fill_color"}
    assert "get_fill_color" in accessor_columns
    assert isinstance(accessor_columns["get_fill_color"], pa.ChunkedArray)


def test_serialize_layer_drops_attribute_columns(points_gdf):
    layer = ScatterplotLayer.from_geopandas(points_gdf, get_fill_color=[255, 0, 0])

    serialized = serialize.serialize_layer(layer, "layer-0")

    assert serialized.layer_type == "scatterplot"
    assert serialized.table.num_rows == 3
    assert "attribute" not in serialized.table.schema.names
    assert "geometry" in serialized.table.schema.names


def test_serialize_layer_rejects_unsupported_type(points_gdf):
    layer = PathLayer.from_geopandas(
        gpd.GeoDataFrame(geometry=[LineString([(0, 0), (1, 1)])], crs="EPSG:4326"),
        get_color=[0, 0, 0],
    )
    layer._layer_type = "unsupported-type"

    with pytest.raises(ValueError, match="does not yet support layer type"):
        serialize.serialize_layer(layer, "layer-0")


def test_pack_payload_roundtrip(points_gdf):
    layer = ScatterplotLayer.from_geopandas(points_gdf, get_fill_color=[255, 0, 0])
    serialized = serialize.serialize_layer(layer, "layer-0")

    blob = serialize.pack_payload(
        [serialized],
        view_state={"longitude": 1.0, "latitude": 1.0, "zoom": 7},
        map_options={"basemapStyle": "https://example.com/style.json", "height": 500},
    )

    header_len = struct.unpack("<I", blob[:4])[0]
    header = json.loads(blob[4 : 4 + header_len])
    assert header["viewState"] == {"longitude": 1.0, "latitude": 1.0, "zoom": 7}
    assert header["mapOptions"]["height"] == 500

    layer_header = header["layers"][0]
    assert layer_header["id"] == "layer-0"
    assert layer_header["type"] == "scatterplot"

    body = blob[4 + header_len :]
    ipc_bytes = body[layer_header["byteOffset"] : layer_header["byteOffset"] + layer_header["byteLength"]]
    with pa.ipc.open_stream(ipc_bytes) as reader:
        table = reader.read_all()
    assert table.num_rows == 3
