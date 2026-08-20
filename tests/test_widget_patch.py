"""Tests for streamlit_lonboard._widget_patch (importable directly, without pulling in component.py)."""

from __future__ import annotations

import importlib.util
import sys
import warnings
from pathlib import Path

import geopandas as gpd
import ipywidgets
import numpy as np
import pyarrow as pa
import pytest
from ipywidgets.widgets.widget import _instances
from lonboard import A5Layer, Map, ScatterplotLayer
from lonboard.layer_extension import DataFilterExtension
from shapely.geometry import Point

pytestmark = pytest.mark.filterwarnings("ignore::ImportWarning")


def _load_module(pkg_dir: Path, name: str):
    spec = importlib.util.spec_from_file_location(f"streamlit_lonboard.{name}", pkg_dir / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"streamlit_lonboard.{name}"] = module
    spec.loader.exec_module(module)
    return module


def _load_modules():
    """Load _widget_patch.py and serialize.py without triggering
    streamlit_lonboard/__init__.py -> component.py, which registers a CCv2
    component and requires the full Streamlit runtime.
    """
    pkg_dir = Path(__file__).resolve().parents[1] / "src" / "streamlit_lonboard"
    package = sys.modules.get("streamlit_lonboard")
    if package is None:
        import types

        package = types.ModuleType("streamlit_lonboard")
        package.__path__ = [str(pkg_dir)]  # type: ignore[attr-defined]
        sys.modules["streamlit_lonboard"] = package
    _load_module(pkg_dir, "_perf")
    return _load_module(pkg_dir, "_widget_patch"), _load_module(pkg_dir, "serialize")


widget_patch, serialize = _load_modules()


@pytest.fixture(autouse=True)
def _restore_patch_state(monkeypatch):
    """The patch mutates lonboard's classes process-wide, so every test starts and
    ends from stock behaviour regardless of what it did in between.
    """
    monkeypatch.delenv(widget_patch.KEEP_COMM_ENV, raising=False)
    widget_patch.remove_widget_comm_patch()
    yield
    widget_patch.remove_widget_comm_patch()


@pytest.fixture
def points_gdf() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"attribute": [1, 2, 3]},
        geometry=[Point(0, 0), Point(1, 1), Point(2, 2)],
        crs="EPSG:4326",
    )


def _scatterplot(points_gdf: gpd.GeoDataFrame) -> ScatterplotLayer:
    return ScatterplotLayer.from_geopandas(
        points_gdf,
        get_fill_color=np.array([[255, 0, 0], [0, 255, 0], [0, 0, 255]], dtype=np.uint8),
    )


def test_unpatched_layer_opens_a_comm(points_gdf):
    """Baseline: stock ipywidgets gives every layer a (dummy) comm."""
    assert _scatterplot(points_gdf).comm is not None


def test_patched_layer_has_no_comm(points_gdf):
    widget_patch.apply_widget_comm_patch()
    assert _scatterplot(points_gdf).comm is None


def test_patched_layer_is_not_registered_in_ipywidgets_instances(points_gdf):
    """Under Streamlit nothing ever drains ipywidgets' global registry: `close()` only
    runs from `__del__`, which the registry's own strong reference prevents. Without a
    comm the layer never gets registered in the first place.
    """
    widget_patch.apply_widget_comm_patch()
    before = set(_instances)
    layer = _scatterplot(points_gdf)
    added = [type(_instances[key]).__name__ for key in set(_instances) - before]
    assert type(layer).__name__ not in added


def test_serialized_output_is_identical_with_and_without_the_patch(points_gdf):
    """The patch must only remove work, never change what reaches the browser -
    serialize.py reads the traits directly and does its own Arrow IPC encoding.
    """
    unpatched = serialize.serialize_layer(_scatterplot(points_gdf), "layer-0")

    widget_patch.apply_widget_comm_patch()
    patched = serialize.serialize_layer(_scatterplot(points_gdf), "layer-0")

    assert patched.ipc_bytes == unpatched.ipc_bytes
    assert patched.props == unpatched.props
    assert patched.layer_type == unpatched.layer_type


def test_extension_traits_still_serialize_without_a_comm(points_gdf):
    """BaseLayer.__init__ ends with `send_state(added_names)` for extension-injected
    traits; the patch skips that when there is no comm, but the trait values must still
    be set on the layer (and reach the payload).
    """
    widget_patch.apply_widget_comm_patch()
    layer = ScatterplotLayer.from_geopandas(
        points_gdf,
        extensions=[DataFilterExtension(filter_size=1)],
        get_filter_value=np.array([1.0, 2.0, 3.0], dtype=np.float32),
        filter_range=(0.0, 5.0),
    )

    assert layer.comm is None
    serialized = serialize.serialize_layer(layer, "layer-0")
    assert serialized.extensions == [{"type": "data-filter", "props": {"filterSize": 1}}]
    assert serialized.props["getFilterValue"] == {"@@arrowColumn": "get_filter_value"}
    assert "get_filter_value" in serialized.table.schema.names


def test_map_construction_survives_comm_less_layers(points_gdf):
    """`Map.open()` serializes its `layers` trait via ipywidgets' widget_serialization,
    which reads `layer.model_id` -> `comm.comm_id`. Patching Map's base class too is
    what stops that from raising AttributeError on comm-less layers.
    """
    widget_patch.apply_widget_comm_patch()
    layer = _scatterplot(points_gdf)
    map_ = Map(layers=[layer])

    assert map_.comm is None
    assert map_.layers == (layer,)


def test_accessor_geometry_layer_round_trips_without_a_comm():
    widget_patch.apply_widget_comm_patch()
    cells = np.array([0x1234, 0x5678, 0x9ABC], dtype=np.uint64)
    layer = A5Layer(table=pa.table({"value": [1.0, 2.0, 3.0]}), get_pentagon=cells)

    assert layer.comm is None
    serialized = serialize.serialize_layer(layer, "layer-0")
    assert serialized.layer_type == "a5"
    assert serialized.table.column("get_pentagon").to_pylist() == [0x1234, 0x5678, 0x9ABC]


def test_close_and_delete_are_safe_without_a_comm(points_gdf):
    widget_patch.apply_widget_comm_patch()
    layer = _scatterplot(points_gdf)
    layer.close()
    layer.close()
    del layer


def test_patch_is_idempotent_and_reversible(points_gdf):
    widget_patch.apply_widget_comm_patch()
    widget_patch.apply_widget_comm_patch()
    assert _scatterplot(points_gdf).comm is None

    widget_patch.remove_widget_comm_patch()
    assert _scatterplot(points_gdf).comm is not None

    widget_patch.remove_widget_comm_patch()
    assert _scatterplot(points_gdf).comm is not None


def test_env_var_opts_out(points_gdf, monkeypatch):
    monkeypatch.setenv(widget_patch.KEEP_COMM_ENV, "1")
    widget_patch.apply_widget_comm_patch()
    assert _scatterplot(points_gdf).comm is not None


def test_unexpected_ipywidgets_version_warns_and_does_not_patch(points_gdf, monkeypatch):
    monkeypatch.setattr(ipywidgets, "__version__", "9.0.0", raising=False)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        widget_patch.apply_widget_comm_patch()

    assert any(issubclass(w.category, RuntimeWarning) for w in caught)
    assert _scatterplot(points_gdf).comm is not None
