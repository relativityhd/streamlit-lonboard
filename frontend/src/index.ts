/**
 * streamlit-lonboard frontend.
 *
 * Pipeline (see IMPLEMENTATION_PLAN.md §2):
 *   1. receive { layers: [...], tables: {key: bytes}, view_state } from Python
 *   2. tableFromIPC(bytes) -> Arrow Table (GeoArrow geometry column)
 *   3. dispatch to GeoArrowScatterplotLayer / GeoArrowPathLayer / ... by type tag
 *   4. render Deck over a MapLibre basemap
 *   5. report picking + view state back via component state (bidi)
 *
 * TODO(phase-0/1): wire up to Streamlit components-v2 runtime API.
 */

import { tableFromIPC } from "apache-arrow";
// import { GeoArrowScatterplotLayer } from "@geoarrow/deck.gl-layers";
// import { Deck } from "@deck.gl/core";

export function parseLayerTable(ipcBytes: Uint8Array) {
  // Zero-copy parse of the Arrow IPC stream shipped from Python.
  return tableFromIPC(ipcBytes);
}

// TODO(phase-1): buildLayer(typeTag, table, props): deck.gl Layer
// TODO(phase-1): mount(container, data, setState): Deck + MapLibre
// TODO(phase-3): onClick/onHover -> setState({ clicked, hovered })
// TODO(phase-4): cache parsed tables by content hash across reruns
