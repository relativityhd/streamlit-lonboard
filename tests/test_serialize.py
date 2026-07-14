"""Tests for streamlit_lonboard.serialize (importable directly, without pulling in component.py)."""

from __future__ import annotations

import gzip
import importlib.util
import json
import struct
import sys
import types
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyarrow as pa
import pytest
from lonboard import PathLayer, ScatterplotLayer
from shapely.geometry import LineString, Point


def _load_module(pkg_dir: Path, name: str):
    spec = importlib.util.spec_from_file_location(f"streamlit_lonboard.{name}", pkg_dir / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"streamlit_lonboard.{name}"] = module
    spec.loader.exec_module(module)
    return module


def _load_serialize_module():
    """Load serialize.py (and its `._perf` dependency) without triggering
    streamlit_lonboard/__init__.py -> component.py, which registers a CCv2
    component and requires the full Streamlit runtime.
    """
    pkg_dir = Path(__file__).resolve().parents[1] / "src" / "streamlit_lonboard"
    if "streamlit_lonboard" not in sys.modules:
        pkg = types.ModuleType("streamlit_lonboard")
        pkg.__path__ = [str(pkg_dir)]
        sys.modules["streamlit_lonboard"] = pkg
    _load_module(pkg_dir, "_perf")
    return _load_module(pkg_dir, "serialize")


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


def test_serialize_layer_is_byte_deterministic():
    """Regression test: arro3 builds extension metadata from a Rust HashMap,
    whose key iteration order is randomized per construction - so the *same*
    GeoDataFrame serialized twice independently used to produce different
    Arrow IPC bytes (different FlatBuffers layout) despite identical content.
    That breaks every content-hash cache downstream, including Streamlit's
    own ForwardMsgCache. `_canonicalize_table` sorts field metadata keys to
    fix this; this test constructs everything from scratch each iteration
    (rather than reusing one layer object) to catch it.
    """

    def build_and_pack() -> bytes:
        gdf = gpd.GeoDataFrame(geometry=[Point(0, 0), Point(1, 1), Point(2, 2)], crs="EPSG:4326")
        layer = ScatterplotLayer.from_geopandas(gdf, get_fill_color=[255, 0, 0])
        serialized = serialize.serialize_layer(layer, "layer-0")
        return serialize.pack_payload(
            [serialized],
            view_state={"longitude": 1.0, "latitude": 1.0, "zoom": 7},
            map_options={"height": 500},
        )

    blobs = [build_and_pack() for _ in range(20)]
    assert len({blob for blob in blobs}) == 1


def test_serialize_layer_cached_hits_on_same_object(points_gdf):
    layer = ScatterplotLayer.from_geopandas(points_gdf, get_fill_color=[255, 0, 0])

    first = serialize.serialize_layer_cached(layer, "layer-0")
    second = serialize.serialize_layer_cached(layer, "layer-0")

    assert first is second


def test_serialize_layer_cached_misses_on_different_object(points_gdf):
    layer_a = ScatterplotLayer.from_geopandas(points_gdf, get_fill_color=[255, 0, 0])
    layer_b = ScatterplotLayer.from_geopandas(points_gdf, get_fill_color=[255, 0, 0])

    first = serialize.serialize_layer_cached(layer_a, "layer-0")
    second = serialize.serialize_layer_cached(layer_b, "layer-0")

    assert first is not second
    assert first.ipc_bytes == second.ipc_bytes  # same content, just not cached across objects


def test_serialize_layer_cached_misses_on_different_layer_id(points_gdf):
    layer = ScatterplotLayer.from_geopandas(points_gdf, get_fill_color=[255, 0, 0])

    first = serialize.serialize_layer_cached(layer, "layer-0")
    second = serialize.serialize_layer_cached(layer, "layer-1")

    assert first is not second
    assert second.layer_id == "layer-1"


def test_serialize_layer_cached_invalidates_on_trait_change(points_gdf):
    layer = ScatterplotLayer.from_geopandas(points_gdf, get_fill_color=[255, 0, 0])

    first = serialize.serialize_layer_cached(layer, "layer-0")
    layer.get_fill_color = [0, 255, 0]
    second = serialize.serialize_layer_cached(layer, "layer-0")

    assert first is not second
    assert first.props["getFillColor"] == [255, 0, 0]
    assert second.props["getFillColor"] == [0, 255, 0]


def _large_points_gdf(n: int = 100_000) -> gpd.GeoDataFrame:
    """~1.6MB serialized (bare geometry, no accessors) - big enough to cross
    AUTO_COMPRESSION_THRESHOLD (1MB)."""
    rng = np.random.default_rng(0)
    lon = rng.uniform(-122.6, -122.3, n)
    lat = rng.uniform(37.6, 37.9, n)
    return gpd.GeoDataFrame(geometry=[Point(x, y) for x, y in zip(lon, lat)], crs="EPSG:4326")


def _unpack(blob: bytes) -> tuple[dict, bytes]:
    header_len = struct.unpack("<I", blob[:4])[0]
    header = json.loads(blob[4 : 4 + header_len])
    return header, blob[4 + header_len :]


def test_pack_payload_compression_none_never_compresses(points_gdf):
    layer = ScatterplotLayer.from_geopandas(_large_points_gdf(), get_fill_color=[255, 0, 0])
    serialized = serialize.serialize_layer(layer, "layer-0")

    blob = serialize.pack_payload([serialized], view_state=None, map_options={}, compression=None)

    header, body = _unpack(blob)
    assert header["compression"] is None
    assert len(body) == len(serialized.ipc_bytes)


def test_pack_payload_compression_gzip_always_compresses_even_when_tiny(points_gdf):
    layer = ScatterplotLayer.from_geopandas(points_gdf, get_fill_color=[255, 0, 0])
    serialized = serialize.serialize_layer(layer, "layer-0")

    blob = serialize.pack_payload([serialized], view_state=None, map_options={}, compression="gzip")

    header, body = _unpack(blob)
    assert header["compression"] == "gzip"
    assert gzip.decompress(body) == serialized.ipc_bytes


def test_pack_payload_compression_auto_skips_small_payloads(points_gdf):
    layer = ScatterplotLayer.from_geopandas(points_gdf, get_fill_color=[255, 0, 0])
    serialized = serialize.serialize_layer(layer, "layer-0")

    blob = serialize.pack_payload([serialized], view_state=None, map_options={}, compression="auto")

    header, body = _unpack(blob)
    assert header["compression"] is None
    assert body == serialized.ipc_bytes


def test_pack_payload_compression_auto_compresses_large_payloads():
    layer = ScatterplotLayer.from_geopandas(_large_points_gdf(), get_fill_color=[255, 0, 0])
    serialized = serialize.serialize_layer(layer, "layer-0")
    assert len(serialized.ipc_bytes) >= serialize.AUTO_COMPRESSION_THRESHOLD

    blob = serialize.pack_payload([serialized], view_state=None, map_options={}, compression="auto")

    header, body = _unpack(blob)
    assert header["compression"] == "gzip"
    assert len(body) < len(serialized.ipc_bytes)
    assert gzip.decompress(body) == serialized.ipc_bytes


def test_pack_payload_compression_rejects_unknown_value(points_gdf):
    layer = ScatterplotLayer.from_geopandas(points_gdf, get_fill_color=[255, 0, 0])
    serialized = serialize.serialize_layer(layer, "layer-0")

    with pytest.raises(ValueError, match="compression"):
        serialize.pack_payload([serialized], view_state=None, map_options={}, compression="brotli")


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
    assert header["v"] == serialize.PAYLOAD_FORMAT_VERSION
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
