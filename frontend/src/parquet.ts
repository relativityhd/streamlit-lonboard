/**
 * Lazy loader for parquet-wasm, used only when a payload arrives with
 * `bodyEncoding: "parquet"` (see `st_lonboard(compression="parquet")`).
 *
 * The 6.5MB WASM binary (~1.8MB gzipped on the wire, since Streamlit's
 * gzip middleware covers this asset route) is deliberately NOT bundled
 * into index.js - it is
 * copied next to it in frontend_dist by the `copy-wasm` npm script and
 * fetched on first use via `import.meta.url` (CCv2 serves the whole
 * `asset_dir`, and loads index.js from a real URL, so sibling-relative
 * resolution works). Apps that never use `compression="parquet"` never pay
 * the download.
 */

import initWasm, { readParquet, type Table as WasmTable } from "parquet-wasm/esm";

let wasmReady: Promise<unknown> | null = null;

// Built dynamically (not a single string literal) so vite's static
// `new URL(<literal>, import.meta.url)` asset detection does NOT see it -
// in lib mode vite force-inlines every detected asset as base64, which
// would put ~8.7MB of encoded WASM into index.js for every user, exactly
// what this lazy path exists to avoid.
const WASM_FILENAME = ["parquet_wasm_bg", "wasm"].join(".");

function ensureParquetWasm(): Promise<unknown> {
  wasmReady ??= initWasm({
    module_or_path: new URL(WASM_FILENAME, import.meta.url),
  });
  return wasmReady;
}

/**
 * parquet-wasm's default `batchSize` is 1024 rows, which would fragment a
 * 1M-row layer into ~1000 record batches - each becoming a separate chunk in
 * the parsed Arrow table and a separate render pass in the GeoArrow deck.gl
 * layers. A huge batch size instead yields one batch per Parquet row group
 * (pyarrow's writer defaults to ~1M-row row groups), matching the
 * single-record-batch layout the plain-IPC path ships.
 */
const READ_ALL_ONE_BATCH = 0x7fffffff;

/** Decode one layer's Parquet file bytes into an Arrow IPC stream (bytes). */
export async function parquetToIPC(parquetBytes: Uint8Array): Promise<Uint8Array> {
  await ensureParquetWasm();
  const wasmTable: WasmTable = readParquet(parquetBytes, { batchSize: READ_ALL_ONE_BATCH });
  return wasmTable.intoIPCStream();
}
