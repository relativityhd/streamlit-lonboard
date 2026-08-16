"""Tests for streamlit_lonboard.serialize (importable directly, without pulling in component.py)."""

from __future__ import annotations

import gzip
import importlib.util
import json
import struct
import sys
import types
from pathlib import Path

import datetime

import geopandas as gpd
import numpy as np
import pyarrow as pa
import pytest
from lonboard import (
    A5Layer,
    ArcLayer,
    ColumnLayer,
    GeohashLayer,
    H3HexagonLayer,
    PathLayer,
    PointCloudLayer,
    S2Layer,
    ScatterplotLayer,
    SolidPolygonLayer,
    TripsLayer,
)
from lonboard.layer_extension import (
    BrushingExtension,
    CollisionFilterExtension,
    DataFilterExtension,
    PathStyleExtension,
)
from shapely.geometry import LineString, MultiPoint, MultiPolygon, Point, Polygon

# H3HexagonLayer without `h3-py` installed warns on every construction (it
# only affects lonboard's own auto-view-state computation, not
# serialization) - fake uint64 cell IDs sidestep the need for it entirely.
pytestmark = pytest.mark.filterwarnings("ignore::ImportWarning")


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


def test_check_payload_size_passes_under_limit():
    serialize.check_payload_size(b"x" * 1000, max_mb=200)  # should not raise


def test_check_payload_size_raises_actionable_error_over_limit():
    payload = b"x" * 5_000_000  # 5MB

    with pytest.raises(ValueError, match=r"5\.0MB.*exceeds.*server\.maxMessageSize.*4MB") as exc_info:
        serialize.check_payload_size(payload, max_mb=4)

    message = str(exc_info.value)
    assert "Downsample" in message
    assert "compression=" in message
    assert "server.maxMessageSize" in message


def test_serialize_layer_produces_valid_ipc_for_multi_chunk_polygon_layer():
    """Regression test for docs/agents/lonboard-multi-batch-bug.md: lonboard
    rechunks large tables into zero-copy slices, and pyarrow's C++ IPC writer
    can corrupt a sliced variable-size-list column (Polygon/MultiPolygon/Path
    geometry) by keeping absolute, non-rebased offsets into a truncated child
    array (https://github.com/apache/arrow/issues/46407). That produces an
    IPC stream that's invalid on read-back and renders nothing in the
    frontend, silently. `_rows_per_chunk=2` forces the same multi-chunk
    rechunk a large real table gets automatically.
    """

    def sq(x: int):
        return Polygon([(x, 0), (x + 0.5, 0), (x + 0.5, 0.5), (x, 0.5)])

    gdf = gpd.GeoDataFrame(geometry=[MultiPolygon([sq(i)]) for i in range(6)], crs="EPSG:4326")
    layer = SolidPolygonLayer.from_geopandas(gdf, get_fill_color=np.full((6, 4), 128, dtype="uint8"), _rows_per_chunk=2)
    assert pa.table(layer.table).column("geometry").num_chunks > 1  # precondition: the bug needs >1 chunk

    serialized = serialize.serialize_layer(layer, "layer-0")

    reader = pa.ipc.open_stream(serialized.ipc_bytes)
    batches = list(reader)
    assert len(batches) == 1  # combine_chunks() collapses the rechunk before writing
    for batch in batches:
        batch.validate(full=True)  # raises if offsets/child-array lengths are inconsistent
    assert sum(batch.num_rows for batch in batches) == 6


def test_serialize_layer_handles_null_geometry():
    """A null geometry mixed with real points must not raise in serialize_layer
    itself - the row is kept (with a null geometry entry), matching lonboard's
    own behavior. The NaN this produces in lonboard's auto-computed view state
    is a separate, later failure mode - see
    test_pack_payload_rejects_non_finite_numbers."""
    gdf = gpd.GeoDataFrame(geometry=[Point(0, 0), None, Point(1, 1)], crs="EPSG:4326")
    layer = ScatterplotLayer.from_geopandas(gdf, get_fill_color=[255, 0, 0])

    serialized = serialize.serialize_layer(layer, "layer-0")

    assert serialized.table.num_rows == 3


def test_serialize_layer_handles_multipoint():
    gdf = gpd.GeoDataFrame(
        geometry=[MultiPoint([(0, 0), (1, 1)]), MultiPoint([(2, 2), (3, 3)])],
        crs="EPSG:4326",
    )
    layer = ScatterplotLayer.from_geopandas(gdf, get_fill_color=[255, 0, 0])

    serialized = serialize.serialize_layer(layer, "layer-0")

    assert serialized.table.num_rows == 2
    assert serialized.table.schema.field("geometry").metadata[serialize._EXTENSION_NAME_KEY] == (b"geoarrow.multipoint")


@pytest.fixture
def attribute_table():
    """A GeoArrow-free table, for layer types whose geometry lives entirely
    in accessor columns (H3/S2/A5/Geohash cell IDs, Arc position pairs)."""
    return pa.table({"attribute": [1, 2, 3]})


def test_serialize_layer_h3_hexagon_ships_uint64_cells(attribute_table):
    cells = np.array([0x8928308280FFFFF, 0x8928308280FFFFE, 0x8928308280FFFFD], dtype=np.uint64)
    layer = H3HexagonLayer(table=attribute_table, get_hexagon=cells, get_fill_color=[255, 0, 0])

    serialized = serialize.serialize_layer(layer, "layer-0")

    assert serialized.layer_type == "h3-hexagon"
    assert serialized.table.num_rows == 3
    assert "geometry" not in serialized.table.schema.names
    assert serialized.table.schema.field("get_hexagon").type == pa.uint64()
    assert serialized.props["getHexagon"] == {"@@arrowColumn": "get_hexagon"}

    with pa.ipc.open_stream(serialized.ipc_bytes) as reader:
        for batch in reader:
            batch.validate(full=True)


def test_serialize_layer_s2_ships_string_tokens(attribute_table):
    layer = S2Layer(table=attribute_table, get_s2_token=np.array(["89c25c", "89c25d", "89c25e"]))

    serialized = serialize.serialize_layer(layer, "layer-0")

    assert serialized.layer_type == "s2"
    assert serialized.table.schema.field("get_s2_token").type == pa.string()
    assert serialized.props["getS2Token"] == {"@@arrowColumn": "get_s2_token"}


def test_serialize_layer_a5_ships_uint64_pentagons(attribute_table):
    layer = A5Layer(table=attribute_table, get_pentagon=np.array([1, 2, 3], dtype=np.uint64))

    serialized = serialize.serialize_layer(layer, "layer-0")

    assert serialized.layer_type == "a5"
    assert serialized.table.schema.field("get_pentagon").type == pa.uint64()


def test_serialize_layer_geohash_ships_string_hashes(attribute_table):
    layer = GeohashLayer(table=attribute_table, get_geohash=np.array(["u4pruy", "u4pruz", "u4prv0"]))

    serialized = serialize.serialize_layer(layer, "layer-0")

    assert serialized.layer_type == "geohash"
    assert serialized.table.schema.field("get_geohash").type == pa.string()


def test_serialize_layer_rejects_scalar_required_accessor(attribute_table):
    """`S2Layer(get_s2_token="89c25c")` is valid lonboard - it broadcasts one
    string to every row via `TextAccessor`. But a scalar can't carry per-row
    geometry, and `build_layer_props` would ship it as a plain JSON prop
    (not an Arrow column), which the frontend can't render. This must raise
    before it gets that far."""
    layer = S2Layer(table=attribute_table, get_s2_token="89c25c")

    with pytest.raises(ValueError, match="must be set to an array"):
        serialize.serialize_layer(layer, "layer-0")


def test_serialize_layer_arc_ships_position_pairs(attribute_table):
    layer = ArcLayer(
        table=attribute_table,
        get_source_position=np.array([[0.0, 0.0], [1.0, 1.0], [2.0, 2.0]]),
        get_target_position=np.array([[1.0, 1.0], [2.0, 2.0], [3.0, 3.0]]),
    )

    serialized = serialize.serialize_layer(layer, "layer-0")

    assert serialized.layer_type == "arc"
    assert serialized.table.num_rows == 3
    xy_pair_type = pa.list_(pa.field("xy", pa.float64()), 2)
    assert serialized.table.schema.field("get_source_position").type == xy_pair_type
    assert serialized.table.schema.field("get_target_position").type == xy_pair_type


def test_serialize_layer_column_keeps_geometry_column():
    gdf = gpd.GeoDataFrame(geometry=[Point(0, 0), Point(1, 1), Point(2, 2)], crs="EPSG:4326")
    layer = ColumnLayer.from_geopandas(gdf, get_fill_color=[255, 0, 0])

    serialized = serialize.serialize_layer(layer, "layer-0")

    assert serialized.layer_type == "column"
    assert "geometry" in serialized.table.schema.names


def test_serialize_layer_point_cloud_keeps_3d_geometry_column():
    gdf = gpd.GeoDataFrame(geometry=gpd.GeoSeries.from_xy([0, 1, 2], [0, 1, 2], [0, 1, 2]), crs="EPSG:4326")
    layer = PointCloudLayer.from_geopandas(gdf, get_color=[255, 0, 0])

    serialized = serialize.serialize_layer(layer, "layer-0")

    assert serialized.layer_type == "point-cloud"
    assert "geometry" in serialized.table.schema.names


def test_serialize_layer_trip_rescales_timestamps_to_float32():
    """deck.gl uploads trip timestamps as a float32 vertex attribute, so raw
    microsecond epoch values (which need 64 bits) must be shifted into
    float32's exact-integer range first - see `_rescale_trip_timestamps`."""
    gdf = gpd.GeoDataFrame(geometry=[LineString([(0, 0), (1, 1)]), LineString([(2, 2), (3, 3)])], crs="EPSG:4326")
    timestamps = pa.array(
        [
            [datetime.datetime(2024, 1, 1, 0, 0, 0), datetime.datetime(2024, 1, 1, 0, 0, 10)],
            [datetime.datetime(2024, 1, 1, 0, 0, 5), datetime.datetime(2024, 1, 1, 0, 0, 15)],
        ]
    )
    layer = TripsLayer.from_geopandas(gdf, get_timestamps=timestamps)

    serialized = serialize.serialize_layer(layer, "layer-0")

    assert serialized.table.schema.field("get_timestamps").type == pa.list_(pa.float32())
    flattened = [v for row in serialized.table.column("get_timestamps").to_pylist() for v in row]
    assert min(flattened) == serialize._TRIP_TIMESTAMP_OFFSET_BASE
    assert serialized.props["currentTime"] == 0.0  # not "CurrentTime" (leading-underscore trait)


def test_serialize_layer_still_rejects_geometry_required_layer_with_no_geometry_column(monkeypatch):
    """Scatterplot/Path/Polygon/... must still fail loudly with no GeoArrow
    geometry column - only the accessor-geometry layer types (H3/S2/A5/
    Geohash/Arc) are exempt from this check. Lonboard's own trait validation
    prevents constructing a real PathLayer with no geometry column, so this
    patches `_geometry_column_names` to simulate the empty-schema case."""
    layer = PathLayer.from_geopandas(
        gpd.GeoDataFrame(geometry=[LineString([(0, 0), (1, 1)])], crs="EPSG:4326"),
        get_color=[0, 0, 0],
    )
    monkeypatch.setattr(serialize, "_geometry_column_names", lambda schema: [])

    with pytest.raises(ValueError, match="no GeoArrow-encoded geometry column"):
        serialize.serialize_layer(layer, "layer-0")


def test_pack_payload_rejects_non_finite_numbers(points_gdf):
    """Regression test: lonboard's auto-computed view state (and, in general,
    any prop derived from data) can be NaN - e.g. when a layer's geometry
    column has null entries, lonboard's centroid/bbox math propagates NaN
    through the nulls rather than skipping them. `json.dumps` otherwise
    happily emits a bare `NaN` token, which is invalid JSON and crashes the
    frontend's `JSON.parse` with a cryptic, hard-to-debug error instead of a
    clear Python-side one."""
    layer = ScatterplotLayer.from_geopandas(points_gdf, get_fill_color=[255, 0, 0])
    serialized = serialize.serialize_layer(layer, "layer-0")

    with pytest.raises(ValueError, match="non-finite number"):
        serialize.pack_payload(
            [serialized],
            view_state={"longitude": float("nan"), "latitude": 1.0, "zoom": 7},
            map_options={},
        )


@pytest.fixture
def path_gdf():
    return gpd.GeoDataFrame(
        geometry=[LineString([(0, 0), (1, 1)]), LineString([(1, 0), (0, 1)])],
        crs="EPSG:4326",
    )


def test_serialize_extensions_empty_when_none_attached(points_gdf):
    layer = ScatterplotLayer.from_geopandas(points_gdf, get_fill_color=[255, 0, 0])

    assert serialize.serialize_extensions(layer) == []


def test_serialize_extensions_path_style(path_gdf):
    """`dash`/`high_precision_dash`/`offset` live on the extension object and
    are the only things `serialize_extensions` needs to handle - the layer
    props an extension injects (`get_dash_array`, `dash_justified`, ...) are
    ordinary layer traits by the time lonboard's `BaseLayer.__init__` returns
    (see `lonboard/layer/_base.py` `_add_extension_traits`), so
    `build_layer_props` already ships them with no changes needed."""
    ext = PathStyleExtension(dash=True, high_precision_dash=False)
    layer = PathLayer.from_geopandas(path_gdf, extensions=[ext], get_dash_array=[4, 2], dash_justified=True)

    serialized = serialize.serialize_layer(layer, "layer-0")

    assert serialized.extensions == [{"type": "path-style", "props": {"dash": True, "highPrecisionDash": False}}]
    # `offset` was never set (stays `None`) - correctly omitted, not shipped as null.
    assert "offset" not in serialized.extensions[0]["props"]
    assert serialized.props["getDashArray"] == [4, 2]
    assert serialized.props["dashJustified"] is True


def test_serialize_extensions_path_style_array_dash(path_gdf):
    """`get_dash_array` also accepts a per-row Arrow array (not just a
    constant [dash, gap] pair) - it's a `get_`-prefixed accessor trait like
    any other, so it goes through `build_layer_props`'s existing arrow-vs-json
    branch unmodified."""
    ext = PathStyleExtension(dash=True)
    dash_arrays = np.array([[4, 2], [1, 1]], dtype="float32")
    layer = PathLayer.from_geopandas(path_gdf, extensions=[ext], get_dash_array=dash_arrays)

    serialized = serialize.serialize_layer(layer, "layer-0")

    assert serialized.props["getDashArray"] == {"@@arrowColumn": "get_dash_array"}
    assert serialized.table.schema.field("get_dash_array").type == pa.list_(pa.field("", pa.float32()), 2)


def test_serialize_extensions_data_filter_ctor_args_and_layer_props(points_gdf):
    """`filter_size`/`category_size` are shader-compile-time constructor
    options on the extension; everything else (`filter_range`,
    `get_filter_value`, ...) is a layer prop, already handled generically."""
    ext = DataFilterExtension(filter_size=2, category_size=1)
    layer = ScatterplotLayer.from_geopandas(
        points_gdf,
        extensions=[ext],
        get_filter_value=np.array([[1.0, 10.0], [2.0, 20.0], [3.0, 30.0]]),
        filter_range=[(0, 5), (0, 50)],
    )

    serialized = serialize.serialize_layer(layer, "layer-0")

    assert serialized.extensions == [{"type": "data-filter", "props": {"filterSize": 2, "categorySize": 1}}]
    assert serialized.props["filterRange"] == ((0.0, 5.0), (0.0, 50.0))
    assert serialized.props["getFilterValue"] == {"@@arrowColumn": "get_filter_value"}
    assert serialized.table.schema.field("get_filter_value").type == pa.list_(pa.field("", pa.float32()), 2)


def test_serialize_extensions_data_filter_scalar_value(points_gdf):
    """filter_size defaults to 1, so a plain scalar `get_filter_value` (one
    number for the whole layer, not one per row) is valid lonboard usage and
    should ship as a JSON number, not an Arrow column."""
    ext = DataFilterExtension()
    layer = ScatterplotLayer.from_geopandas(points_gdf, extensions=[ext], get_filter_value=2.5, filter_range=(0, 5))

    serialized = serialize.serialize_layer(layer, "layer-0")

    assert serialized.extensions == [{"type": "data-filter", "props": {"filterSize": 1}}]
    assert serialized.props["getFilterValue"] == 2.5


def test_serialize_extensions_brushing_keeps_geometry_column_first(points_gdf):
    """`get_brushing_target` uses `PointAccessor`, the same trait type used
    for real geometry columns - regression test locking that the real
    geometry column is always selected into the output table *before*
    accessor columns are appended (`serialize_layer`: `table.select(...)`
    happens before the `append_column` loop), so a geoarrow-aware reader
    scanning fields in order always finds the real geometry first."""
    ext = BrushingExtension()
    layer = ScatterplotLayer.from_geopandas(
        points_gdf,
        extensions=[ext],
        get_brushing_target=np.array([[0.0, 0.0], [1.0, 1.0], [2.0, 2.0]]),
        brushing_target="custom",
        brushing_radius=5000,
    )

    serialized = serialize.serialize_layer(layer, "layer-0")

    assert serialized.extensions == [{"type": "brushing", "props": {}}]
    assert serialized.table.schema.names[0] == "geometry"
    assert "get_brushing_target" in serialized.table.schema.names
    assert serialized.props["brushingTarget"] == "custom"
    assert serialized.props["brushingRadius"] == 5000.0
    assert serialized.props["getBrushingTarget"] == {"@@arrowColumn": "get_brushing_target"}


def test_serialize_extensions_collision_filter(points_gdf):
    """`CollisionFilterExtension` has no config traits of its own - everything
    (`collision_group`, `get_collision_priority`) is a layer prop."""
    ext = CollisionFilterExtension()
    layer = ScatterplotLayer.from_geopandas(
        points_gdf,
        extensions=[ext],
        get_collision_priority=np.array([1.0, 2.0, 3.0]),
        collision_group="group-a",
    )

    serialized = serialize.serialize_layer(layer, "layer-0")

    assert serialized.extensions == [{"type": "collision-filter", "props": {}}]
    assert serialized.props["collisionGroup"] == "group-a"
    assert serialized.props["getCollisionPriority"] == {"@@arrowColumn": "get_collision_priority"}


def test_serialize_extensions_rejects_unknown_type(points_gdf):
    # A real extension with its `_extension_type` overridden after construction
    # (rather than a fake object) - `layer.extensions` is itself a traitlets
    # `Instance(BaseExtension)` trait, so a non-BaseExtension object would fail
    # lonboard's *own* validation before ever reaching `serialize_extensions`.
    ext = BrushingExtension()
    ext._extension_type = "clip"
    layer = ScatterplotLayer.from_geopandas(points_gdf, extensions=[ext])

    with pytest.raises(ValueError, match="does not yet support the 'clip' layer extension"):
        serialize.serialize_extensions(layer)


def test_pack_payload_omits_extensions_key_when_none_attached(points_gdf):
    layer = ScatterplotLayer.from_geopandas(points_gdf, get_fill_color=[255, 0, 0])
    serialized = serialize.serialize_layer(layer, "layer-0")

    blob = serialize.pack_payload([serialized], view_state=None, map_options={}, compression=None)

    header, _ = _unpack(blob)
    assert "extensions" not in header["layers"][0]


def test_pack_payload_includes_extensions_key_when_present(path_gdf):
    ext = PathStyleExtension(dash=True)
    layer = PathLayer.from_geopandas(path_gdf, extensions=[ext], get_dash_array=[4, 2])
    serialized = serialize.serialize_layer(layer, "layer-0")

    blob = serialize.pack_payload([serialized], view_state=None, map_options={}, compression=None)

    header, _ = _unpack(blob)
    assert header["layers"][0]["extensions"] == [{"type": "path-style", "props": {"dash": True}}]


def test_serialize_layer_cached_invalidates_on_extension_trait_change(path_gdf):
    ext = PathStyleExtension(dash=True)
    layer = PathLayer.from_geopandas(path_gdf, extensions=[ext], get_dash_array=[4, 2])

    first = serialize.serialize_layer_cached(layer, "layer-0")
    ext.dash = False
    second = serialize.serialize_layer_cached(layer, "layer-0")

    assert first is not second
    assert first.extensions[0]["props"]["dash"] is True
    assert second.extensions[0]["props"]["dash"] is False


def test_serialize_layer_cached_hits_when_extension_untouched(path_gdf):
    """A cache hit shouldn't re-attach a second observer to the same
    extension object on every call (that would mean N duplicate eviction
    callbacks after N cache hits) - not directly observable from the public
    API, so this just pins down that repeated calls keep hitting the cache
    once an extension is already being observed."""
    ext = PathStyleExtension(dash=True)
    layer = PathLayer.from_geopandas(path_gdf, extensions=[ext], get_dash_array=[4, 2])

    first = serialize.serialize_layer_cached(layer, "layer-0")
    for _ in range(3):
        assert serialize.serialize_layer_cached(layer, "layer-0") is first

