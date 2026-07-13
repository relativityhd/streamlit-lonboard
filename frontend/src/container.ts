/**
 * Parses the binary payload built by `streamlit_lonboard.serialize.pack_payload`.
 *
 * Layout: `<u32 header_len little-endian><utf-8 json header><concatenated ipc streams>`.
 * See serialize.py for why this custom framing exists instead of relying on
 * CCv2's automatic dataframe-in-dict serialization.
 */

import { tableFromIPC, type Table } from "apache-arrow";

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
}

export interface ContainerHeader {
  layers: LayerHeader[];
  viewState: MapViewState | null;
  mapOptions: MapOptions;
}

export interface ParsedLayer {
  header: LayerHeader;
  table: Table;
}

export interface ParsedContainer {
  header: ContainerHeader;
  layers: ParsedLayer[];
}

export function parseContainer(bytes: Uint8Array): ParsedContainer {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const headerLen = view.getUint32(0, true);
  const headerJson = new TextDecoder().decode(bytes.subarray(4, 4 + headerLen));
  const header: ContainerHeader = JSON.parse(headerJson);
  const body = bytes.subarray(4 + headerLen);

  const layers: ParsedLayer[] = header.layers.map((layerHeader) => {
    const ipcBytes = body.subarray(
      layerHeader.byteOffset,
      layerHeader.byteOffset + layerHeader.byteLength,
    );
    const table = tableFromIPC(ipcBytes);
    return { header: layerHeader, table };
  });

  return { header, layers };
}
