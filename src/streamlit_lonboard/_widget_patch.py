"""Stop lonboard widgets from serializing their state for a Jupyter comm that never exists.

lonboard layers, extensions, controls and `Map` are all ipywidgets `Widget`s, and
`Widget.__init__` unconditionally calls `open()`. `open()` in turn calls `get_state()`,
which fires every trait's `to_json` serializer - and lonboard's table/accessor
serializers parquet-encode (ZSTD-7) the entire table plus every accessor column to fill
a comm-open message.

Under Streamlit there is no kernel, so `comm.create_comm()` falls back to a `DummyComm`
whose `publish_msg` is a no-op: the message is computed at full cost and then discarded.
Importing `streamlit_lonboard` means you are in a Streamlit app rather than a notebook,
so this module makes that work a no-op on lonboard's two base classes. Measured on an
A5 layer with 359,600 rows: 0.76 s of construction becomes 0.001 s, and what
`serialize.py` puts on the wire is byte-identical either way (we never read lonboard's
parquet output - `serialize.py` does its own Arrow IPC encoding straight off the
traits).

Set `STREAMLIT_LONBOARD_KEEP_WIDGET_COMM=1` before importing `streamlit_lonboard` to
opt out and get stock ipywidgets behaviour back.

Two classes cover everything lonboard constructs: `BaseWidget` (layers via `BaseLayer`,
extensions via `BaseExtension`, and controls) and `BaseAnyWidget` (`Map`). Patching
`Map`'s base is not optional: `Map.open()` serializes its `layers` trait through
ipywidgets' `widget_serialization`, which reads `layer.model_id` -> `self.comm.comm_id`
and would raise `AttributeError` for comm-less layers.

Known consequence: with no comm, `widget.comm is None` and `widget.model_id` is
unavailable, so `Map.to_html()`/`Map.as_html()` (which go through
`ipywidgets.embed`) raise. Nothing in streamlit-lonboard itself reads either attribute.
Call `ipywidgets.Widget.open(widget)` to give a single widget a comm back, or use the
environment variable above to disable the patch process-wide.
"""

from __future__ import annotations

import os
import sys
import warnings
from typing import Any

KEEP_COMM_ENV = "STREAMLIT_LONBOARD_KEEP_WIDGET_COMM"

# Set on a patched class's own __dict__ so applying twice is a no-op and removal knows
# what it may delete.
_PATCH_MARKER = "_st_lonboard_comm_patch"

_PATCHED_METHODS = ("open", "send_state", _PATCH_MARKER)


def _patched_open(self: Any) -> None:
    """Do not open a Jupyter comm (streamlit-lonboard patch).

    Stock `ipywidgets.Widget.open` serializes the widget's full state - for lonboard
    that means parquet-encoding the table and every accessor - to build a comm-open
    message. Under Streamlit nothing consumes it. Leaving `self.comm` as None also
    keeps the widget out of ipywidgets' module-global `_instances` registry, which
    under Streamlit is never drained (`close()` only runs from `__del__`, and the
    registry holds a strong reference that stops `__del__` from firing).

    Call `ipywidgets.Widget.open(widget)` to open a comm for one widget anyway, or set
    STREAMLIT_LONBOARD_KEEP_WIDGET_COMM=1 before import to disable the patch entirely.
    """
    return None


def _patched_send_state(self: Any, key: Any = None) -> None:
    """Skip state serialization when there is no comm to send it to.

    `ipywidgets.Widget.send_state` calls `get_state()` before the comm check inside
    `_send`, so without this, `BaseLayer.__init__`'s trailing `send_state(added_names)`
    (used to sync extension-injected traits) parquet-encodes accessors a second time
    for a message that is then dropped.
    """
    if self.comm is None:
        return None
    import ipywidgets

    return ipywidgets.Widget.send_state(self, key)


def _target_classes() -> list[type] | None:
    """Return lonboard's widget base classes, or None if the internals moved."""
    try:
        import ipywidgets
        from lonboard._base import BaseAnyWidget, BaseWidget
    except ImportError as exc:
        warnings.warn(
            f"streamlit-lonboard: could not patch out lonboard's unused Jupyter comm setup ({exc}). "
            "Layer construction will be slower than necessary, but everything still works.",
            RuntimeWarning,
            stacklevel=3,
        )
        return None

    version = getattr(ipywidgets, "__version__", "")
    problems: list[str] = []
    if not version.startswith("8."):
        problems.append(f"expected ipywidgets 8.x, found {version or 'an unknown version'}")
    for cls in (BaseWidget, BaseAnyWidget):
        if not issubclass(cls, ipywidgets.Widget):
            problems.append(f"{cls.__name__} is no longer an ipywidgets.Widget subclass")
    for name in ("open", "send_state"):
        if not callable(getattr(ipywidgets.Widget, name, None)):
            problems.append(f"ipywidgets.Widget.{name} is missing")

    if problems:
        warnings.warn(
            "streamlit-lonboard: skipping the widget-comm patch because ipywidgets/lonboard "
            f"internals look different than expected ({'; '.join(problems)}). Layer construction "
            "will be slower than necessary, but everything still works.",
            RuntimeWarning,
            stacklevel=3,
        )
        return None

    return [BaseWidget, BaseAnyWidget]


def _in_ipython_kernel() -> bool:
    """True when a real IPython kernel is running, so comms actually go somewhere.

    Only checks an already-imported IPython - importing it here would be a needless
    cost in the common Streamlit case.
    """
    ipython_module = sys.modules.get("IPython")
    get_ipython = getattr(ipython_module, "get_ipython", None) if ipython_module else None
    if get_ipython is None:
        return False
    try:
        return get_ipython() is not None
    except Exception:  # pragma: no cover - defensive; get_ipython() is not supposed to raise
        return False


def apply_widget_comm_patch() -> None:
    """Make lonboard widgets skip comm state serialization. Idempotent."""
    if os.environ.get(KEEP_COMM_ENV) == "1":
        return
    if _in_ipython_kernel():
        return

    classes = _target_classes()
    if classes is None:
        return

    for cls in classes:
        if _PATCH_MARKER in cls.__dict__:
            continue
        cls.open = _patched_open  # type: ignore[method-assign]
        cls.send_state = _patched_send_state  # type: ignore[method-assign]
        setattr(cls, _PATCH_MARKER, True)


def remove_widget_comm_patch() -> None:
    """Restore stock ipywidgets behaviour on lonboard's base classes.

    Only affects widgets constructed afterwards; widgets that were built without a comm
    keep `comm is None` until something calls `ipywidgets.Widget.open` on them.
    """
    try:
        from lonboard._base import BaseAnyWidget, BaseWidget
    except ImportError:
        return

    for cls in (BaseWidget, BaseAnyWidget):
        if _PATCH_MARKER not in cls.__dict__:
            continue
        for name in _PATCHED_METHODS:
            try:
                delattr(cls, name)
            except AttributeError:  # pragma: no cover - only if something else removed it first
                pass
