/**
 * Parses the binary payload built by `streamlit_lonboard.serialize.pack_payload`.
 *
 * Layout: `<u32 header_len little-endian><utf-8 json header><concatenated
 * per-layer bodies>`, where each body is an Arrow IPC stream (optionally with
 * ZSTD-compressed buffers) or a Parquet file, per that layer's
 * `bodyEncoding`. The whole body is additionally gzipped as one blob when
 * `header.compression === "gzip"`.
 * See serialize.py for why this custom framing exists instead of relying on
 * CCv2's automatic dataframe-in-dict serialization.
 */

import { compressionRegistry, CompressionType, tableFromIPC, type Table } from "apache-arrow";
import { ZSTDDecoder } from "zstddec";
import type { ExtensionHeader } from "./extensions";
import { fnv1a, fnv1aString } from "./fingerprint";
import { parquetToIPC } from "./parquet";

// Bump in lockstep with streamlit_lonboard.serialize.PAYLOAD_FORMAT_VERSION.
const SUPPORTED_PAYLOAD_FORMAT_VERSION = 1;

// Lets `tableFromIPC` transparently read the ZSTD-compressed record-batch
// buffers produced by `st_lonboard(compression="zstd")` (the IPC spec's
// per-buffer body compression - arrow-js strips each buffer's 8-byte
// uncompressed-length prefix itself and hands the codec raw ZSTD frames).
//
// zstddec is a WASM build of the reference decoder, ~3.7x faster than the
// pure-JS alternative on a real 358k-row payload (26ms vs 95ms). Its WASM is
// base64-inlined into its own JS, so unlike parquet-wasm it needs no separate
// asset and no extra network request - it just costs ~24KB gzipped in the
// bundle, paid by every user whether or not they ship zstd payloads.
const zstdDecoder = new ZSTDDecoder();
let zstdReady: Promise<void> | null = null;

/**
 * Instantiates the ZSTD WASM module (idempotent). Must be awaited before any
 * `tableFromIPC` call on a zstd-compressed stream: arrow-js's codec interface
 * is synchronous, so the decoder has to be live before parsing starts.
 */
function ensureZstdReady(): Promise<void> {
  zstdReady ??= zstdDecoder.init();
  return zstdReady;
}

compressionRegistry.set(CompressionType.ZSTD, {
  decode(data: Uint8Array): Uint8Array {
    // Second arg is the uncompressed size, which we do not have - arrow-js
    // consumes the 8-byte length prefix before calling us. 0 makes zstddec
    // read the size from the frame header instead.
    const out = zstdDecoder.decode(data, 0);
    // zstddec copies out of the WASM heap (`heap.slice(...)`), so each call
    // returns an independent, offset-0 buffer that a later decode cannot
    // clobber. The guard is therefore a no-op today, kept because arrow-js
    // wraps this chunk in typed arrays directly (Float64Array etc.) and a
    // future codec returning a heap *view* would break on alignment.
    return out.byteOffset % 8 === 0 ? out : out.slice();
  },
});

export interface LayerHeader {
  id: string;
  type: string;
  props: Record<string, unknown>;
  /**
   * How this layer's byte range in the body is encoded - see
   * `streamlit_lonboard.serialize.BodyEncoding`. Absent means "ipc". It is
   * per-layer, not per-payload, because `compression="auto"` resolves
   * against each layer's own size, so one map can mix encodings.
   * "ipc-zstd" needs no handling beyond the codec registered above;
   * "parquet" routes the bytes through parquet-wasm first.
   */
  bodyEncoding?: "ipc" | "ipc-zstd" | "parquet";
  /** Layer extensions attached to this lonboard layer, e.g. `PathStyleExtension`. Omitted when none. */
  extensions?: ExtensionHeader[];
  /** Column names appended to this layer's Arrow table for hover tooltips (see `st_lonboard(tooltip=...)`). Omitted when none. */
  tooltipColumns?: string[];
  byteOffset: number;
  byteLength: number;
}

export interface MapViewState {
  longitude: number;
  latitude: number;
  zoom: number;
  pitch?: number;
  bearing?: number;
}

/** One `lonboard.Map.controls` entry - see `streamlit_lonboard.serialize.serialize_controls`. */
export interface ControlHeader {
  /** lonboard `BaseControl._control_type`, e.g. "navigation". */
  type: string;
  position: "top-left" | "top-right" | "bottom-left" | "bottom-right" | null;
  options: Record<string, unknown>;
}

export interface MapOptions {
  basemapStyle?: string;
  height?: number;
  onClick?: boolean;
  onHover?: boolean;
  returnViewState?: boolean;
  /** Mirrors ST_LONBOARD_PERF=1 on the Python side; logs a perf summary to the console. */
  perf?: boolean;
  /** Extra pixels around the pointer to include while picking. deck.gl default: 0. */
  pickingRadius?: number;
  /** deck.gl GPU parameters (`luma.gl`'s `setParameters`), e.g. `{depthTest: false}`. */
  parameters?: Record<string, unknown>;
  /** `false`/a number <= 1 improves performance on high-DPI displays. deck.gl default: `true`. */
  useDevicePixels?: boolean | number;
  customAttribution?: string | string[];
  controls?: ControlHeader[];
}

export interface ContainerHeader {
  v: number;
  layers: LayerHeader[];
  viewState: MapViewState | null;
  mapOptions: MapOptions;
  /** Set when `streamlit_lonboard.serialize.pack_payload`'s `compression=` gzipped the body. */
  compression: "gzip" | null;
}

/**
 * A parsed layer carries two independent fingerprints, because the two
 * halves of a lonboard layer change at very different costs:
 *
 * - `bytesFingerprint` covers the Arrow IPC bytes (geometry + accessor
 *   columns). When it matches the last parse (per `previousBytesFingerprints`),
 *   `tableFromIPC` - the dominant per-rerun cost at scale, see
 *   benchmarks/RESULTS.md - is skipped entirely, and `table` is omitted here;
 *   the caller must reuse its previously-parsed table.
 * - `headerFingerprint` covers everything else that affects rendering (JSON
 *   props, extensions - anything NOT `id`/`type`/byte range, since byte
 *   offsets shift whenever an earlier layer's size changes even though this
 *   layer's own content didn't). It is always computed, independently of
 *   whether the bytes changed, so a rerun that only flips a prop (e.g. a
 *   slider-driven `filter_range` or `opacity`) is still detected even when
 *   the geometry bytes are byte-identical.
 *
 * `table` is therefore present whenever the bytes changed (freshly parsed
 * here) and absent only when they didn't. Callers reconstruct deck.gl layers
 * whenever *either* fingerprint changed since the last build, reusing the
 * previous table when only the header changed.
 */
export interface ParsedLayer {
  header: LayerHeader;
  bytesFingerprint: string;
  headerFingerprint: string;
  table?: Table;
}

export interface ParsedContainer {
  header: ContainerHeader;
  layers: ParsedLayer[];
}

/** Decompress a gzip-compressed body via the browser's native streaming decoder. */
async function decompressGzip(bytes: Uint8Array): Promise<Uint8Array> {
  if (typeof DecompressionStream === "undefined") {
    throw new Error(
      "streamlit-lonboard: this payload is gzip-compressed (compression='gzip' in " +
        "st_lonboard()), but this browser has no DecompressionStream support. Pass " +
        "compression='auto' (the default, which never uses gzip) or compression=None " +
        "to st_lonboard(), or use a modern browser.",
    );
  }
  // TS types Uint8Array.buffer as ArrayBuffer | SharedArrayBuffer; ours is
  // always a real ArrayBuffer in practice (bytes come from CCv2's `data`,
  // never a SharedArrayBuffer), which is what Blob's constructor requires.
  const arrayBuffer = bytes.buffer.slice(
    bytes.byteOffset,
    bytes.byteOffset + bytes.byteLength,
  ) as ArrayBuffer;
  const stream = new Blob([arrayBuffer]).stream().pipeThrough(new DecompressionStream("gzip"));
  const buffer = await new Response(stream).arrayBuffer();
  return new Uint8Array(buffer);
}

/**
 * The subset of a `LayerHeader` that should trigger a deck.gl layer rebuild
 * when it changes - i.e. everything except identity (`id`, `type`) and byte
 * range (`byteOffset`/`byteLength`, which shift whenever an earlier layer's
 * size changes and carry no rendering information of their own).
 */
function headerFingerprintPayload(layerHeader: LayerHeader): unknown {
  return { props: layerHeader.props, extensions: layerHeader.extensions };
}

/**
 * `previousBytesFingerprints` (layer id -> bytes fingerprint from the last
 * call) lets this skip `tableFromIPC` for byte-identical layers, not just
 * deck.gl layer construction - `tableFromIPC` is the dominant per-rerun
 * frontend cost at scale (see benchmarks/RESULTS.md), so skipping the parse
 * matters far more than skipping layer construction alone. The (much
 * cheaper) header fingerprint is always computed, regardless of whether the
 * bytes matched - see `ParsedLayer`.
 */
export async function parseContainer(
  bytes: Uint8Array,
  previousBytesFingerprints: ReadonlyMap<string, string> = new Map(),
): Promise<ParsedContainer> {
  // Timestamp-based like every other measure spanning an `await` here - see
  // the decompress measure below for why named marks are unsafe in this file.
  const parseStart = performance.now();
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const headerLen = view.getUint32(0, true);
  const headerJson = new TextDecoder().decode(bytes.subarray(4, 4 + headerLen));
  const header: ContainerHeader = JSON.parse(headerJson);
  if (header.v !== SUPPORTED_PAYLOAD_FORMAT_VERSION) {
    throw new Error(
      `streamlit-lonboard: payload format v${header.v} is not supported by this frontend ` +
        `build (expects v${SUPPORTED_PAYLOAD_FORMAT_VERSION}). The installed streamlit_lonboard ` +
        "Python package and its frontend_dist build are out of sync - reinstall the package.",
    );
  }
  const rawBody = bytes.subarray(4 + headerLen);

  let body = rawBody;
  if (header.compression === "gzip") {
    // Timestamp-based measure (not named marks): this span crosses an
    // `await`, during which another concurrently-mounting map instance may
    // call `logPerfSummary()` -> `performance.clearMarks()` and wipe a
    // start mark out from under us (global `performance` state is shared
    // across every component instance on the page).
    const start = performance.now();
    body = await decompressGzip(rawBody);
    performance.measure("st-lonboard:decompress", { start, end: performance.now() });
  }

  const layers: ParsedLayer[] = [];
  for (const layerHeader of header.layers) {
    const bodyEncoding = layerHeader.bodyEncoding ?? "ipc";
    // For "parquet" these are Parquet file bytes, not an IPC stream - the
    // fingerprint is over the *wire* bytes either way, so the byte-identical
    // skip below works for every encoding.
    const bodyBytes = body.subarray(
      layerHeader.byteOffset,
      layerHeader.byteOffset + layerHeader.byteLength,
    );
    const bytesFingerprint = fnv1a(bodyBytes);
    const headerFingerprint = fnv1aString(JSON.stringify(headerFingerprintPayload(layerHeader)));

    if (previousBytesFingerprints.get(layerHeader.id) === bytesFingerprint) {
      layers.push({ header: layerHeader, bytesFingerprint, headerFingerprint });
      continue;
    }

    let ipcBytes = bodyBytes;
    if (bodyEncoding === "ipc-zstd") {
      // The codec is synchronous, so the WASM module must be live *before*
      // tableFromIPC starts. One-time; subsequent layers resolve instantly.
      const start = performance.now();
      await ensureZstdReady();
      performance.measure(`st-lonboard:zstdInit[${layerHeader.id}]`, {
        start,
        end: performance.now(),
      });
    }
    if (bodyEncoding === "parquet") {
      // Timestamp-based for the same reason as the decompress measure above
      // (the WASM init this may await is exactly when other instances run).
      const start = performance.now();
      ipcBytes = await parquetToIPC(bodyBytes);
      performance.measure(`st-lonboard:parquetDecode[${layerHeader.id}]`, {
        start,
        end: performance.now(),
      });
    }

    // For "ipc-zstd", per-buffer ZSTD decompression happens inside this call
    // (via the codec registered above) - its cost shows up in this measure.
    const parseStartedAt = performance.now();
    const table = tableFromIPC(ipcBytes);
    performance.measure(`st-lonboard:tableFromIPC[${layerHeader.id}]`, {
      start: parseStartedAt,
      end: performance.now(),
    });
    layers.push({ header: layerHeader, bytesFingerprint, headerFingerprint, table });
  }

  performance.measure("st-lonboard:parseContainer", { start: parseStart, end: performance.now() });

  return { header, layers };
}
