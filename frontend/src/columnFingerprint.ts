/**
 * Content fingerprints for a layer's Arrow columns, so a rerun that changed only
 * accessor values (colours, radii, widths) can reuse the geometry work deck.gl
 * already did.
 *
 * Streamlit re-sends the whole component payload whenever anything in it changes, so
 * a colour-only change still arrives as a brand-new Arrow table with brand-new buffers
 * whose *geometry* content is byte-identical to the previous one. Comparing the raw
 * IPC bytes (container.ts) can only tell us "something changed"; these per-column
 * fingerprints tell us *what*.
 *
 * The fingerprints are used two ways in layers.ts:
 * - the combined geometry fingerprint is tagged onto each `RecordBatch` so a
 *   `dataComparator` can treat two different batch objects with identical geometry as
 *   unchanged, which is what stops deck.gl from re-deriving cell boundaries and
 *   re-tessellating;
 * - the per-column fingerprints become `updateTriggers` values, so exactly the
 *   accessors whose column content actually changed get re-uploaded to the GPU.
 */

import type { Data, RecordBatch, Schema, Table } from "apache-arrow";
import { fnv1aInto, toHex } from "./fingerprint";

const EXTENSION_NAME_KEY = "ARROW:extension:name";
const GEOARROW_EXTENSION_PREFIX = "geoarrow.";

/**
 * Accessor columns that carry a layer's geometry, per layer type.
 *
 * Keep in lockstep with `_REQUIRED_ACCESSORS` in
 * `src/streamlit_lonboard/serialize.py` - Python enforces that these arrive as
 * per-row arrays, and here we treat them as geometry rather than as ordinary
 * accessors (changing them means the shapes moved, so everything must be rebuilt).
 */
export const REQUIRED_GEOMETRY_ACCESSOR_COLUMNS: Record<string, readonly string[]> = {
  "h3-hexagon": ["get_hexagon"],
  s2: ["get_s2_token"],
  a5: ["get_pentagon"],
  geohash: ["get_geohash"],
  arc: ["get_source_position", "get_target_position"],
};

/**
 * Tag holding a batch's combined geometry fingerprint. A Symbol keeps it off anything
 * that enumerates or serializes the batch, and apache-arrow never looks at it.
 */
export const GEOMETRY_FINGERPRINT = Symbol.for("streamlit-lonboard.geometryFingerprint");

export type TaggedRecordBatch = RecordBatch & {
  [GEOMETRY_FINGERPRINT]?: string;
};

/**
 * Read the geometry tag off a batch, or off a deck.gl `{data, length}` wrapper.
 *
 * Checks the object itself before unwrapping, and that order is load-bearing: an
 * apache-arrow `RecordBatch` has its own `.data` property (the underlying
 * `Data<Struct>`), so unwrapping first would look for the tag on that and never find
 * it. The GeoArrow composite layers are compared on the bare batch, while the deck.gl
 * layers beneath them are compared on the `{data, length}` wrapper - both have to
 * resolve, or whichever level is missed re-tessellates anyway.
 */
export function geometryTag(value: unknown): string | undefined {
  if (value === null || typeof value !== "object") return undefined;
  const direct = (value as TaggedRecordBatch)[GEOMETRY_FINGERPRINT];
  if (direct !== undefined) return direct;
  const wrapped = (value as { data?: unknown }).data;
  if (wrapped === null || typeof wrapped !== "object") return undefined;
  return (wrapped as TaggedRecordBatch)[GEOMETRY_FINGERPRINT];
}

function bytesOf(buffer: ArrayBufferView): Uint8Array {
  return new Uint8Array(buffer.buffer, buffer.byteOffset, buffer.byteLength);
}

/**
 * Fingerprint one Arrow column's contents, buffers and all.
 *
 * Hashes every buffer in full rather than sampling: a missed difference here would
 * silently keep stale geometry on screen, and at the payload sizes this component
 * deals with (a couple of MB) a full FNV pass costs single-digit milliseconds.
 * Recurses into `children` because the geometry types that matter are nested
 * (`list<list<fixed_size_list<f64>>>` for polygons, `fixed_size_list<u8>` for colours).
 */
export function fingerprintData(data: Data): string {
  let [h1, h2] = [0, 0];
  const visit = (node: Data) => {
    // `offset` and `length` matter independently of the buffers: a sliced Data shares
    // its parent's buffers and differs only by these.
    [h1, h2] = fnv1aInto(h1, h2, new Uint8Array(new Int32Array([node.offset, node.length, node.nullCount]).buffer));
    // `buffers` is keyed by BufferType rather than iterable, so name them; which
    // ones are populated depends on the column's type.
    for (const buffer of [node.values, node.valueOffsets, node.nullBitmap, node.typeIds]) {
      if (buffer && buffer.byteLength > 0) {
        [h1, h2] = fnv1aInto(h1, h2, bytesOf(buffer));
      }
    }
    if (node.dictionary) visit(node.dictionary.data[0]);
    for (const child of node.children) visit(child);
  };
  visit(data);
  return toHex(h1, h2);
}

/**
 * Names of the columns that determine a layer's geometry: any GeoArrow-encoded column
 * (mirrors `_geometry_column_names` in serialize.py) plus the accessor columns that
 * carry geometry for layer types that have no geometry column at all.
 */
export function geometryColumnNames(schema: Schema, layerType: string): string[] {
  const names = schema.fields
    .filter((field) => field.metadata.get(EXTENSION_NAME_KEY)?.startsWith(GEOARROW_EXTENSION_PREFIX))
    .map((field) => field.name);
  for (const name of REQUIRED_GEOMETRY_ACCESSOR_COLUMNS[layerType] ?? []) {
    if (schema.fields.some((field) => field.name === name)) names.push(name);
  }
  return names;
}

/**
 * Fingerprint every column of `table`, and tag each of its batches with the combined
 * fingerprint of its geometry columns.
 *
 * Returns column name -> fingerprint, for use as `updateTriggers` values. (Tables from
 * this component always hold exactly one batch - `serialize.py` calls
 * `combine_chunks()` before writing IPC - but nothing here depends on that.)
 */
export function tagBatches(table: Table, layerType: string): Map<string, string> {
  const geometryNames = new Set(geometryColumnNames(table.schema, layerType));
  const perColumn = new Map<string, string>();

  for (const batch of table.batches) {
    const geometryParts: string[] = [];
    for (const field of batch.schema.fields) {
      const column = batch.getChild(field.name);
      if (!column) continue;
      const fingerprint = column.data.map(fingerprintData).join("");
      // Batches are fingerprinted independently, so a multi-batch table's per-column
      // entry is the concatenation across batches.
      perColumn.set(field.name, (perColumn.get(field.name) ?? "") + fingerprint);
      if (geometryNames.has(field.name)) geometryParts.push(`${field.name}:${fingerprint}`);
    }
    // No geometry columns (an unknown or malformed layer) means we cannot prove the
    // geometry is unchanged, so leave the batch untagged and let deck.gl rebuild.
    if (geometryParts.length > 0) {
      (batch as TaggedRecordBatch)[GEOMETRY_FINGERPRINT] = geometryParts.join("|");
    }
  }

  return perColumn;
}
