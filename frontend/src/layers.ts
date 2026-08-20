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
import { geometryTag } from "./columnFingerprint";
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

/**
 * Treat two different `RecordBatch` objects as the same data when their geometry
 * content is identical (see columnFingerprint.ts for how batches get tagged).
 *
 * Streamlit re-sends the whole payload on any change, so a colour-only rerun hands
 * deck.gl a brand-new batch. deck.gl's default check is reference equality, which
 * would report "a new data container was supplied" and invalidate everything -
 * re-deriving every cell boundary and re-running earcut, which is seconds of
 * main-thread work at DGGS scale. With this comparator that work is skipped and only
 * the accessors whose `updateTriggers` fingerprint changed are re-uploaded.
 *
 * Also handles the `{data: batch, length}` wrapper that the GeoArrow composite layers
 * build fresh on every render, which is what the leaf layers actually diff.
 *
 * A single module-level instance: deck.gl diffs prop values, so a fresh closure per
 * render would itself register as a prop change.
 */
const geometryStableComparator = (a: unknown, b: unknown): boolean => {
  if (a === b) return true;
  const tag = geometryTag(a);
  return tag !== undefined && tag === geometryTag(b);
};

/**
 * Where each layer family needs the comparator re-injected on the way down.
 *
 * `CompositeLayer.getSubLayerProps` copies only an explicit allowlist of props into
 * its sublayers, and `dataComparator` is not on it - so passing it to the top-level
 * GeoArrow layer alone would stop at the first composite boundary. `_subLayerProps`
 * is the supported override channel, keyed by the sublayer ids deck.gl uses
 * internally:
 * - `GeoCellLayer` (base of deck.gl's A5/S2/Geohash layers) renders one `PolygonLayer`
 *   with id `cell`, which in turn renders `fill` (SolidPolygonLayer) and `stroke`
 *   (PathLayer).
 * - `H3HexagonLayer` renders either `hexagon-cell-hifi` (PolygonLayer) or
 *   `hexagon-cell` (ColumnLayer) depending on its `high_precision` mode.
 * - `GeoArrowPolygonLayer` renders `fill`/`stroke` directly.
 * Every other family's leaf receives the comparator through the GeoArrow layer's
 * `...otherProps` spread, so it needs no entry here.
 *
 * If a future deck.gl/geoarrow release renames these ids, the comparator simply stops
 * matching and rendering falls back to today's full rebuild - slower, never wrong.
 * The one thing to re-check on a deck.gl or geoarrow bump is that the recolour
 * benchmark still reports no re-tessellation (benchmarks/RESULTS.md).
 */
const SUB_LAYER_COMPARATOR_PROPS: Record<string, Record<string, unknown>> = {
  a5: cellSubLayerProps(),
  s2: cellSubLayerProps(),
  geohash: cellSubLayerProps(),
  "h3-hexagon": {
    "hexagon-cell-hifi": {
      dataComparator: geometryStableComparator,
      _subLayerProps: fillAndStroke(),
    },
    "hexagon-cell": { dataComparator: geometryStableComparator },
  },
  polygon: fillAndStroke(),
};

function fillAndStroke(): Record<string, unknown> {
  return {
    fill: { dataComparator: geometryStableComparator },
    stroke: { dataComparator: geometryStableComparator },
  };
}

function cellSubLayerProps(): Record<string, unknown> {
  return {
    cell: {
      dataComparator: geometryStableComparator,
      _subLayerProps: fillAndStroke(),
    },
  };
}

/**
 * `updateTriggers` for every accessor backed by an Arrow column, keyed on that
 * column's content fingerprint.
 *
 * With `dataChanged` suppressed by the comparator above, these are what tell deck.gl
 * an accessor's values actually changed - a stable fingerprint means "don't touch this
 * attribute", a changed one invalidates exactly that attribute.
 */
function buildUpdateTriggers(
  props: Record<string, unknown>,
  columnFingerprints: Map<string, string>,
): Record<string, string | undefined> {
  const triggers: Record<string, string | undefined> = {};
  for (const [key, value] of Object.entries(props)) {
    if (isArrowColumnMarker(value)) {
      triggers[key] = columnFingerprints.get(value[ARROW_COLUMN_MARKER]);
    }
  }
  return triggers;
}

export interface SubLayerInfo {
  lonboardLayerId: string;
  rowOffset: number;
}

export function buildDeckLayers(
  layerHeader: LayerHeader,
  table: Table,
  subLayerLookup: Map<string, SubLayerInfo>,
  columnFingerprints: Map<string, string>,
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

  const updateTriggers = buildUpdateTriggers(layerHeader.props, columnFingerprints);
  const subLayerProps = SUB_LAYER_COMPARATOR_PROPS[layerHeader.type];

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
        dataComparator: geometryStableComparator,
        updateTriggers,
        ...(subLayerProps ? { _subLayerProps: subLayerProps } : {}),
      }),
    );
    subLayerLookup.set(subLayerId, { lonboardLayerId: layerHeader.id, rowOffset });
    rowOffset += batch.numRows;
  });

  performance.mark(`${markPrefix}:end`);
  performance.measure(markPrefix, `${markPrefix}:start`, `${markPrefix}:end`);
  return layers;
}
