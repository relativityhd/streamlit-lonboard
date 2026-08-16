/**
 * Parses the binary payload built by `streamlit_lonboard.serialize.pack_payload`.
 *
 * Layout: `<u32 header_len little-endian><utf-8 json header><concatenated ipc streams>`.
 * See serialize.py for why this custom framing exists instead of relying on
 * CCv2's automatic dataframe-in-dict serialization.
 */

import { tableFromIPC, type Table } from "apache-arrow";
import type { ExtensionHeader } from "./extensions";
import { fnv1a, fnv1aString } from "./fingerprint";

// Bump in lockstep with streamlit_lonboard.serialize.PAYLOAD_FORMAT_VERSION.
const SUPPORTED_PAYLOAD_FORMAT_VERSION = 1;

export interface LayerHeader {
  id: string;
  type: string;
  props: Record<string, unknown>;
  /** Layer extensions attached to this lonboard layer, e.g. `PathStyleExtension`. Omitted when none. */
  extensions?: ExtensionHeader[];
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
      "streamlit-lonboard: this payload is gzip-compressed (compression='gzip'/'auto' in " +
        "st_lonboard()), but this browser has no DecompressionStream support. Pass " +
        "compression=None to st_lonboard() to disable compression, or use a modern browser.",
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
  performance.mark("st-lonboard:parseContainer:start");
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
    performance.mark("st-lonboard:decompress:start");
    body = await decompressGzip(rawBody);
    performance.mark("st-lonboard:decompress:end");
    performance.measure(
      "st-lonboard:decompress",
      "st-lonboard:decompress:start",
      "st-lonboard:decompress:end",
    );
  }

  const layers: ParsedLayer[] = header.layers.map((layerHeader) => {
    const ipcBytes = body.subarray(
      layerHeader.byteOffset,
      layerHeader.byteOffset + layerHeader.byteLength,
    );
    const bytesFingerprint = fnv1a(ipcBytes);
    const headerFingerprint = fnv1aString(JSON.stringify(headerFingerprintPayload(layerHeader)));

    if (previousBytesFingerprints.get(layerHeader.id) === bytesFingerprint) {
      return { header: layerHeader, bytesFingerprint, headerFingerprint };
    }

    performance.mark(`st-lonboard:tableFromIPC[${layerHeader.id}]:start`);
    const table = tableFromIPC(ipcBytes);
    performance.mark(`st-lonboard:tableFromIPC[${layerHeader.id}]:end`);
    performance.measure(
      `st-lonboard:tableFromIPC[${layerHeader.id}]`,
      `st-lonboard:tableFromIPC[${layerHeader.id}]:start`,
      `st-lonboard:tableFromIPC[${layerHeader.id}]:end`,
    );
    return { header: layerHeader, bytesFingerprint, headerFingerprint, table };
  });

  performance.mark("st-lonboard:parseContainer:end");
  performance.measure(
    "st-lonboard:parseContainer",
    "st-lonboard:parseContainer:start",
    "st-lonboard:parseContainer:end",
  );

  return { header, layers };
}
