/**
 * Dispatches a serialized lonboard layer to the matching GeoArrow*Layer class
 * and resolves `{"@@arrowColumn": name}` markers to real Arrow column data.
 *
 * `@geoarrow/deck.gl-geoarrow` composite layers each operate on a single
 * `arrow.RecordBatch` (not a whole multi-batch Table), so a lonboard layer
 * with N batches becomes N deck.gl sub-layers here, each keeping the row
 * offset needed to translate a pick's batch-local index back into the row
 * index of the original table (see `SubLayerInfo`).
 */

import type { Layer } from "@deck.gl/core";
import {
  GeoArrowA5Layer,
  GeoArrowArcLayer,
  GeoArrowColumnLayer,
  GeoArrowGeohashLayer,
  GeoArrowH3HexagonLayer,
  GeoArrowHeatmapLayer,
  GeoArrowPathLayer,
  GeoArrowPointCloudLayer,
  GeoArrowPolygonLayer,
  GeoArrowS2Layer,
  GeoArrowScatterplotLayer,
  GeoArrowSolidPolygonLayer,
  GeoArrowTripsLayer,
} from "@geoarrow/deck.gl-geoarrow";
import type { RecordBatch, Table } from "apache-arrow";
import type { LayerHeader } from "./container";
import { buildLayerExtensions } from "./extensions";

// deck.gl composite layer constructors aren't uniformly typed across GeoArrow*Layer classes.
type AnyLayerClass = new (props: any) => Layer;

const LAYER_CLASSES: Record<string, AnyLayerClass> = {
  scatterplot: GeoArrowScatterplotLayer,
  path: GeoArrowPathLayer,
  polygon: GeoArrowPolygonLayer,
  "solid-polygon": GeoArrowSolidPolygonLayer,
  heatmap: GeoArrowHeatmapLayer,
  "h3-hexagon": GeoArrowH3HexagonLayer,
  s2: GeoArrowS2Layer,
  a5: GeoArrowA5Layer,
  geohash: GeoArrowGeohashLayer,
  arc: GeoArrowArcLayer,
  column: GeoArrowColumnLayer,
  "point-cloud": GeoArrowPointCloudLayer,
  trip: GeoArrowTripsLayer,
};

const ARROW_COLUMN_MARKER = "@@arrowColumn";

function isArrowColumnMarker(value: unknown): value is { [ARROW_COLUMN_MARKER]: string } {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    ARROW_COLUMN_MARKER in (value as Record<string, unknown>)
  );
}

function resolveProps(
  props: Record<string, unknown>,
  batch: RecordBatch,
): Record<string, unknown> {
  const resolved: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(props)) {
    if (isArrowColumnMarker(value)) {
      const vector = batch.getChild(value[ARROW_COLUMN_MARKER]);
      resolved[key] = vector ? vector.data[0] : undefined;
    } else {
      resolved[key] = value;
    }
  }
  return resolved;
}

export interface SubLayerInfo {
  lonboardLayerId: string;
  rowOffset: number;
}

export function buildDeckLayers(
  layerHeader: LayerHeader,
  table: Table,
  subLayerLookup: Map<string, SubLayerInfo>,
): Layer[] {
  const markPrefix = `st-lonboard:buildDeckLayers[${layerHeader.id}]`;
  performance.mark(`${markPrefix}:start`);

  const LayerClass = LAYER_CLASSES[layerHeader.type];
  if (!LayerClass) {
    console.warn(
      `streamlit-lonboard: unsupported layer type "${layerHeader.type}" (layer ${layerHeader.id})`,
    );
    return [];
  }

  // Built once per lonboard layer and reused across its per-batch
  // sub-layers below - deck.gl extension instances carry no batch-specific
  // state, and each sub-layer's own prop diffing (keyed by `id`) handles
  // sharing the same instance across multiple deck.gl layers correctly.
  const extensions = buildLayerExtensions(layerHeader.extensions, layerHeader.id);

  const layers: Layer[] = [];
  let rowOffset = 0;
  table.batches.forEach((batch, batchIndex) => {
    const subLayerId = `${layerHeader.id}-${batchIndex}`;
    const props = resolveProps(layerHeader.props, batch);
    layers.push(
      new LayerClass({
        ...props,
        ...(extensions.length > 0 ? { extensions } : {}),
        id: subLayerId,
        data: batch,
      }),
    );
    subLayerLookup.set(subLayerId, { lonboardLayerId: layerHeader.id, rowOffset });
    rowOffset += batch.numRows;
  });

  performance.mark(`${markPrefix}:end`);
  performance.measure(markPrefix, `${markPrefix}:start`, `${markPrefix}:end`);
  return layers;
}
