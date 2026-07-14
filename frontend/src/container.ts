/**
 * Parses the binary payload built by `streamlit_lonboard.serialize.pack_payload`.
 *
 * Layout: `<u32 header_len little-endian><utf-8 json header><concatenated ipc streams>`.
 * See serialize.py for why this custom framing exists instead of relying on
 * CCv2's automatic dataframe-in-dict serialization.
 */

import { tableFromIPC, type Table } from "apache-arrow";
import { fnv1a } from "./fingerprint";

// Bump in lockstep with streamlit_lonboard.serialize.PAYLOAD_FORMAT_VERSION.
const SUPPORTED_PAYLOAD_FORMAT_VERSION = 1;

export interface LayerHeader {
  id: string;
  type: string;
  props: Record<string, unknown>;
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

export interface MapOptions {
  basemapStyle?: string;
  height?: number;
  onClick?: boolean;
  onHover?: boolean;
  returnViewState?: boolean;
  /** Mirrors ST_LONBOARD_PERF=1 on the Python side; logs a perf summary to the console. */
  perf?: boolean;
}

export interface ContainerHeader {
  v: number;
  layers: LayerHeader[];
  viewState: MapViewState | null;
  mapOptions: MapOptions;
}

/**
 * A layer whose byte slice's fingerprint matches the last time this same
 * `id` was parsed (per `previousFingerprints`) - `tableFromIPC` was skipped
 * entirely; the caller should reuse whatever it built from the previous
 * parse (see `index.ts`'s per-layer deck.gl layer cache).
 */
export interface UnchangedLayer {
  status: "unchanged";
  header: LayerHeader;
  fingerprint: string;
}

export interface ChangedLayer {
  status: "changed";
  header: LayerHeader;
  fingerprint: string;
  table: Table;
}

export type ParsedLayer = UnchangedLayer | ChangedLayer;

export interface ParsedContainer {
  header: ContainerHeader;
  layers: ParsedLayer[];
}

/**
 * `previousFingerprints` (layer id -> fingerprint from the last call) lets
 * this skip `tableFromIPC` for byte-identical layers, not just deck.gl layer
 * construction - `tableFromIPC` is the dominant per-rerun frontend cost at
 * scale (see benchmarks/RESULTS.md), so skipping the parse matters far more
 * than skipping layer construction alone.
 */
export function parseContainer(
  bytes: Uint8Array,
  previousFingerprints: ReadonlyMap<string, string> = new Map(),
): ParsedContainer {
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
  const body = bytes.subarray(4 + headerLen);

  const layers: ParsedLayer[] = header.layers.map((layerHeader) => {
    const ipcBytes = body.subarray(
      layerHeader.byteOffset,
      layerHeader.byteOffset + layerHeader.byteLength,
    );
    const fingerprint = fnv1a(ipcBytes);

    if (previousFingerprints.get(layerHeader.id) === fingerprint) {
      return { status: "unchanged", header: layerHeader, fingerprint };
    }

    performance.mark(`st-lonboard:tableFromIPC[${layerHeader.id}]:start`);
    const table = tableFromIPC(ipcBytes);
    performance.mark(`st-lonboard:tableFromIPC[${layerHeader.id}]:end`);
    performance.measure(
      `st-lonboard:tableFromIPC[${layerHeader.id}]`,
      `st-lonboard:tableFromIPC[${layerHeader.id}]:start`,
      `st-lonboard:tableFromIPC[${layerHeader.id}]:end`,
    );
    return { status: "changed", header: layerHeader, fingerprint, table };
  });

  performance.mark("st-lonboard:parseContainer:end");
  performance.measure(
    "st-lonboard:parseContainer",
    "st-lonboard:parseContainer:start",
    "st-lonboard:parseContainer:end",
  );

  return { header, layers };
}
