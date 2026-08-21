/**
 * streamlit-lonboard frontend entry point.
 *
 * Mounted by Streamlit's Custom Components v2 runtime, which calls this
 * default export again on every `data` change (not just once at mount) while
 * keeping `parentElement` alive across calls. We therefore stash the
 * MapLibre/deck.gl instances on `parentElement` and reuse them (`ensureMount`)
 * rather than re-creating the map on every rerun — this is what lets the
 * user's pan/zoom survive a Streamlit rerun (see IMPLEMENTATION_PLAN.md
 * "Rerun model").
 */

import type { Layer, PickingInfo } from "@deck.gl/core";
import { MapboxOverlay } from "@deck.gl/mapbox";
import type { Table } from "apache-arrow";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { tagBatches } from "./columnFingerprint";
import { type ContainerHeader, type ControlHeader, type MapOptions, parseContainer } from "./container";
import { buildDeckLayers, type SubLayerInfo } from "./layers";

interface ComponentApi {
  name: string;
  data: unknown;
  key: string;
  parentElement: Element & Record<string, unknown>;
  setStateValue: (stateKey: string, value: unknown) => void;
  setTriggerValue: (eventKey: string, value: unknown) => void;
}

/**
 * What a `buildDeckLayers` call produced for one lonboard layer, kept around
 * so a rerun where nothing changed (both fingerprints match, see
 * container.ts) can be answered without re-parsing or reconstructing
 * anything - see Phase 4b in IMPLEMENTATION_PLAN.md. Note the common
 * "nothing at all changed" case already costs ~0 without this (CCv2 skips
 * calling `mount()` entirely then, confirmed in Phase 4.0) - this cache only
 * matters when *some* layers changed and others didn't within a single
 * `mount()` invocation.
 *
 * `table` is retained (not just the built `deckLayers`) so that a rerun
 * where only `headerFingerprint` changed - e.g. a slider-driven prop, with
 * the Arrow bytes byte-identical - can rebuild deck.gl layers from the
 * already-parsed table instead of needing `tableFromIPC` again.
 */
interface LayerCacheEntry {
  bytesFingerprint: string;
  headerFingerprint: string;
  table: Table;
  deckLayers: Layer[];
  subLayerEntries: [string, SubLayerInfo][];
  /**
   * Per-column content fingerprints, used as `updateTriggers` values so that a
   * rerun re-uploads only the accessor columns that actually changed. Retained
   * alongside `table` because the props-only path below reuses that table (and
   * its batches keep their geometry tags), so its fingerprints stay valid too.
   */
  columnFingerprints: Map<string, string>;
}

interface MountState {
  map: maplibregl.Map;
  overlay: MapboxOverlay;
  container: HTMLDivElement;
  subLayerLookup: Map<string, SubLayerInfo>;
  layerCache: Map<string, LayerCacheEntry>;
  /**
   * Refreshed from `header.mapOptions` on every `mount()` call (see below) -
   * read by the overlay callbacks below instead of the `header` parameter
   * `createMount` closes over, since `createMount` only runs once per
   * component instance while `mount()` runs on every rerun. Reading from
   * `header` directly there would freeze onClick/onHover/returnViewState at
   * whatever they were on the very first mount.
   */
  mapOptions: MapOptions;
  lastBasemapStyle?: string;
  hoverThrottle?: ReturnType<typeof setTimeout>;
  viewStateThrottle?: ReturnType<typeof setTimeout>;
  /** Present only while `mapOptions.customAttribution` is set - maplibre-gl has no "update attribution" API, so a change means remove-and-recreate. */
  attributionControl?: maplibregl.AttributionControl;
  lastCustomAttributionJson?: string;
  /** Every control built from `mapOptions.controls` (NOT including the deck.gl overlay itself), so a rerun can remove-and-rebuild them on change. */
  mapControls: maplibregl.IControl[];
  lastControlsJson?: string;
  /**
   * lonboard layer id -> tooltip column names, rebuilt fresh from
   * `header.layers` on every `mount()` call (cheap - no fingerprint gating
   * needed). Read by `getTooltip`, a callback set once at `MapboxOverlay`
   * construction and invoked by deck.gl's own hover handling *between*
   * `mount()` calls - it must read this off `state` (updated in place) rather
   * than close over a single `header`, or it would keep answering with
   * whatever tooltip columns were configured at the very first mount.
   */
  layerTooltipColumns: Map<string, string[]>;
}

const MOUNT_KEY = "__stLonboardMount";
const DEFAULT_BASEMAP_STYLE =
  "https://basemaps.cartocdn.com/gl/positron-nolabels-gl-style/style.json";
const THROTTLE_MS = 200;

function toUint8Array(data: unknown): Uint8Array {
  if (data instanceof Uint8Array) return data;
  if (data instanceof ArrayBuffer) return new Uint8Array(data);
  throw new Error("streamlit-lonboard: expected a raw bytes payload from Python");
}

/** Mirrors the ST_LONBOARD_PERF=1 spans logged Python-side (see _perf.py). */
function logPerfSummary(): void {
  const measures = performance
    .getEntriesByType("measure")
    .filter((entry) => entry.name.startsWith("st-lonboard:"))
    .map((entry) => ({ span: entry.name.replace(/^st-lonboard:/, ""), ms: entry.duration.toFixed(2) }));
  if (measures.length > 0) {
    console.table(measures);
  }
  performance.clearMarks();
  performance.clearMeasures();
}

/**
 * Instantiates the maplibre-gl control matching one `lonboard.Map.controls`
 * entry. Unrecognized types are skipped (with a warning) rather than thrown -
 * `serialize_controls` already filters these Python-side, so this only fires
 * on a frontend/Python version mismatch; mirrors `buildDeckLayers`/
 * `buildLayerExtensions`'s "skip the one broken thing, keep the map usable"
 * handling of an unsupported type.
 *
 * Every field below gets an explicit `?? <maplibre's own documented default>`
 * fallback rather than passing a possibly-`undefined` value straight through.
 * This matters: `ScaleControl`'s constructor merges options via
 * `{...defaultOptions, ...options}` and `NavigationControl`'s via a `for...in`
 * copy (`extend()`, in maplibre-gl's util) - both treat an explicit
 * `key: undefined` as "present," clobbering the default with `undefined`
 * rather than leaving it alone (confirmed against maplibre-gl's own source,
 * and against a live map: leaving this unfallback'd silently produced a
 * `NavigationControl` with *no* zoom buttons, since an absent `show_zoom` on
 * the Python side arrives here as `options.showZoom === undefined`, which
 * then overwrote the constructor's own `showZoom: true` default).
 */
function buildMaplibreControl(header: ControlHeader): maplibregl.IControl | null {
  const options = header.options;
  switch (header.type) {
    case "scale":
      return new maplibregl.ScaleControl({
        maxWidth: (options.maxWidth as number | undefined) ?? 100,
        unit: (options.unit as maplibregl.Unit | undefined) ?? "metric",
      });
    case "navigation":
      // `visualizeRoll` (a lonboard NavigationControl trait) has no
      // equivalent in the maplibre-gl version this project currently pins
      // (^4.7.1 - NavigationControlOptions there has no such field) - not
      // forwarded. A future maplibre-gl upgrade would pick it up once added
      // here; until then it's silently a no-op rather than an error, since
      // every *other* navigation option still works fine without it.
      return new maplibregl.NavigationControl({
        showCompass: (options.showCompass as boolean | undefined) ?? true,
        showZoom: (options.showZoom as boolean | undefined) ?? true,
        visualizePitch: (options.visualizePitch as boolean | undefined) ?? false,
      });
    case "fullscreen":
      return new maplibregl.FullscreenControl();
    default:
      console.warn(`streamlit-lonboard: unsupported map control "${header.type}"`);
      return null;
  }
}

/** Replaces every non-overlay control on the map with a fresh set built from `controlHeaders`. */
function applyControls(state: MountState, controlHeaders: ControlHeader[] | undefined): void {
  for (const ctrl of state.mapControls) {
    state.map.removeControl(ctrl);
  }
  state.mapControls = [];
  for (const controlHeader of controlHeaders ?? []) {
    const ctrl = buildMaplibreControl(controlHeader);
    if (!ctrl) continue;
    state.map.addControl(ctrl, controlHeader.position ?? undefined);
    state.mapControls.push(ctrl);
  }
}

/**
 * maplibre-gl has no way to update an existing `AttributionControl`'s
 * `customAttribution` in place, so a change means removing the old instance
 * (if any) and adding a new one (if `customAttribution` is now set).
 */
function applyAttribution(state: MountState, customAttribution: string | string[] | undefined): void {
  if (state.attributionControl) {
    state.map.removeControl(state.attributionControl);
    state.attributionControl = undefined;
  }
  if (customAttribution !== undefined) {
    state.attributionControl = new maplibregl.AttributionControl({ customAttribution });
    state.map.addControl(state.attributionControl);
  }
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** Renders one tooltip column's Arrow-decoded value as display text. */
function formatTooltipValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "bigint") return value.toString();
  if (typeof value === "object") {
    // A struct/list-typed tooltip column (uncommon, but not disallowed) -
    // JSON-stringify rather than showing "[object Object]".
    try {
      return JSON.stringify(value, (_key, v) => (typeof v === "bigint" ? v.toString() : v));
    } catch {
      return String(value);
    }
  }
  return String(value);
}

/**
 * Builds hover-tooltip HTML for a pick, or `null` for "no tooltip" (nothing
 * picked, or the picked layer has no `tooltipColumns`). `TooltipWidget` (the
 * consumer of this return value - see @deck.gl/core's tooltip-widget.js)
 * sets `el.innerHTML` directly from the `html` field, so every value is
 * HTML-escaped here - tooltip columns are arbitrary user data, which must
 * never be interpolated into innerHTML unescaped.
 */
function buildTooltipContent(state: MountState, info: PickingInfo): { html: string } | null {
  if (!info.picked || !info.object || !info.layer) return null;

  const subLayerInfo = state.subLayerLookup.get(info.layer.id);
  const lonboardLayerId = subLayerInfo?.lonboardLayerId ?? info.layer.id;
  const columns = state.layerTooltipColumns.get(lonboardLayerId);
  if (!columns || columns.length === 0) return null;

  // `info.object` is an apache-arrow `StructRowProxy` for this row (see
  // @geoarrow/deck.gl-geoarrow's getPickingInfo, which does `batch.get(index)`
  // on a RecordBatch that includes geometry, accessor, *and* tooltip columns)
  // - each tooltip column name is a valid property to read off it directly.
  const row = info.object as Record<string, unknown>;
  const rows = columns
    .map((col) => `<div>${escapeHtml(col)}: ${escapeHtml(formatTooltipValue(row[col]))}</div>`)
    .join("");
  return { html: rows };
}

function pickPayload(subLayerLookup: Map<string, SubLayerInfo>, info: PickingInfo | null) {
  if (!info || !info.picked || !info.layer) return null;
  const subLayerInfo = subLayerLookup.get(info.layer.id);
  return {
    layer_id: subLayerInfo?.lonboardLayerId ?? info.layer.id,
    index: subLayerInfo ? subLayerInfo.rowOffset + (info.index ?? -1) : (info.index ?? -1),
    coordinate: info.coordinate ?? null,
  };
}

function createMount(component: ComponentApi, header: ContainerHeader): MountState {
  const container = document.createElement("div");
  container.style.width = "100%";
  container.style.height = "100%";
  container.style.position = "relative";
  component.parentElement.appendChild(container);

  const vs = header.viewState;
  const map = new maplibregl.Map({
    container,
    style: header.mapOptions.basemapStyle ?? DEFAULT_BASEMAP_STYLE,
    center: vs ? [vs.longitude, vs.latitude] : [0, 0],
    zoom: vs?.zoom ?? 1,
    pitch: vs?.pitch ?? 0,
    bearing: vs?.bearing ?? 0,
    attributionControl: false,
  });

  const state: MountState = {
    map,
    overlay: null as unknown as MapboxOverlay,
    container,
    subLayerLookup: new Map<string, SubLayerInfo>(),
    layerCache: new Map<string, LayerCacheEntry>(),
    mapOptions: header.mapOptions,
    mapControls: [],
    layerTooltipColumns: new Map<string, string[]>(),
  };

  const overlay = new MapboxOverlay({
    interleaved: false,
    layers: [],
    pickingRadius: header.mapOptions.pickingRadius,
    parameters: header.mapOptions.parameters,
    useDevicePixels: header.mapOptions.useDevicePixels,
    getTooltip: (info: PickingInfo) => buildTooltipContent(state, info),
    onClick: (info: PickingInfo) => {
      if (!state.mapOptions.onClick) return;
      component.setTriggerValue("clicked", pickPayload(state.subLayerLookup, info));
    },
    onHover: (info: PickingInfo) => {
      if (!state.mapOptions.onHover) return;
      if (state.hoverThrottle !== undefined) return;
      state.hoverThrottle = setTimeout(() => {
        state.hoverThrottle = undefined;
      }, THROTTLE_MS);
      component.setTriggerValue("hovered", pickPayload(state.subLayerLookup, info));
    },
    onViewStateChange: (params: { viewState: Record<string, unknown> }) => {
      if (!state.mapOptions.returnViewState) return;
      if (state.viewStateThrottle !== undefined) return;
      state.viewStateThrottle = setTimeout(() => {
        state.viewStateThrottle = undefined;
      }, THROTTLE_MS);
      const viewState = params.viewState as {
        longitude: number;
        latitude: number;
        zoom: number;
        pitch: number;
        bearing: number;
      };
      component.setStateValue("view_state", {
        longitude: viewState.longitude,
        latitude: viewState.latitude,
        zoom: viewState.zoom,
        pitch: viewState.pitch,
        bearing: viewState.bearing,
      });
    },
  });
  map.addControl(overlay as unknown as maplibregl.IControl);
  state.overlay = overlay;
  state.lastBasemapStyle = header.mapOptions.basemapStyle ?? DEFAULT_BASEMAP_STYLE;

  component.parentElement[MOUNT_KEY] = state;
  return state;
}

export default async function mount(component: ComponentApi): Promise<() => void> {
  // Timestamp-based (not named marks): mount() spans awaits (gzip
  // decompression, parquet-wasm init), during which another component
  // instance's logPerfSummary() may clearMarks() - see container.ts.
  const mountStart = performance.now();

  const bytes = toUint8Array(component.data);

  // Peek at any existing mount's layer cache *before* parsing, so
  // parseContainer can skip tableFromIPC for layers whose bytes haven't
  // changed since last time (see container.ts).
  const existingState = component.parentElement[MOUNT_KEY] as MountState | undefined;
  const previousBytesFingerprints = new Map<string, string>();
  if (existingState) {
    for (const [layerId, entry] of existingState.layerCache) {
      previousBytesFingerprints.set(layerId, entry.bytesFingerprint);
    }
  }

  // parseContainer is async for three reasons, any of which can apply to a
  // given payload: a gzip-compressed body (compression="gzip") goes through
  // the browser's native (Promise-based) DecompressionStream; a zstd layer
  // needs its WASM codec instantiated before Arrow parsing; and a Parquet
  // layer is decoded by parquet-wasm. CCv2 awaits this default export, so an
  // async mount() is supported (confirmed against the bundled runtime).
  const { header, layers } = await parseContainer(bytes, previousBytesFingerprints);
  const state = existingState ?? createMount(component, header);
  state.mapOptions = header.mapOptions;

  state.subLayerLookup.clear();
  const deckLayers: Layer[] = [];
  const newLayerCache = new Map<string, LayerCacheEntry>();

  // Rebuilt fresh every call (cheap - no need to gate this on either
  // fingerprint, unlike deck.gl layer construction below) so `getTooltip`
  // (invoked by deck.gl between mount() calls, on hover) always answers with
  // this rerun's tooltip configuration.
  state.layerTooltipColumns = new Map(
    header.layers
      .filter((layerHeader) => layerHeader.tooltipColumns && layerHeader.tooltipColumns.length > 0)
      .map((layerHeader) => [layerHeader.id, layerHeader.tooltipColumns as string[]]),
  );

  for (const parsedLayer of layers) {
    const { header: layerHeader, bytesFingerprint, headerFingerprint, table: freshTable } = parsedLayer;
    const cached = state.layerCache.get(layerHeader.id);

    if (freshTable === undefined) {
      if (!cached) {
        // Can't happen: parseContainer only omits `table` when
        // previousBytesFingerprints (built from state.layerCache just above)
        // already had this id+fingerprint.
        throw new Error(
          `streamlit-lonboard: internal error - layer ${layerHeader.id} reported unchanged bytes ` +
            "but has no cached table to reuse",
        );
      }
      if (cached.headerFingerprint === headerFingerprint) {
        // Bytes AND props all unchanged - reuse the built deck.gl layers
        // verbatim, skipping buildDeckLayers entirely.
        for (const [subId, info] of cached.subLayerEntries) {
          state.subLayerLookup.set(subId, info);
        }
        deckLayers.push(...cached.deckLayers);
        newLayerCache.set(layerHeader.id, cached);
        continue;
      }
    }

    // Either the Arrow bytes changed (freshTable is the newly-parsed table)
    // or only the header (props) changed - reuse the cached table in that
    // case. Either way we need to rebuild the deck.gl layers, but only a
    // fresh `tableFromIPC` is the expensive part, and that already happened
    // (or was skipped) in parseContainer.
    const table = freshTable ?? cached?.table;
    if (!table) {
      // Can't happen: cached is only missing when freshTable is defined (see
      // the throw above), so this branch always has one or the other.
      throw new Error(
        `streamlit-lonboard: internal error - layer ${layerHeader.id} has neither a freshly ` +
          "parsed nor a cached table",
      );
    }

    // Only a freshly parsed table needs fingerprinting; a reused cached table
    // still carries the geometry tags and fingerprints from when it was parsed.
    let columnFingerprints: Map<string, string>;
    if (freshTable) {
      const markPrefix = `st-lonboard:columnFingerprints[${layerHeader.id}]`;
      performance.mark(`${markPrefix}:start`);
      columnFingerprints = tagBatches(freshTable, layerHeader.type);
      performance.mark(`${markPrefix}:end`);
      performance.measure(markPrefix, `${markPrefix}:start`, `${markPrefix}:end`);
    } else {
      columnFingerprints = cached!.columnFingerprints;
    }

    let built: Layer[];
    try {
      built = buildDeckLayers(layerHeader, table, state.subLayerLookup, columnFingerprints);
    } catch (error) {
      // One bad layer (e.g. a props/data mismatch) shouldn't take down every
      // other layer on the map via the BidiComponent error boundary - log and
      // skip it instead. Not cached: a fingerprint-unchanged rerun will retry
      // the build (and re-log) rather than silently staying broken forever.
      console.error(
        `streamlit-lonboard: failed to build layer "${layerHeader.id}" (type: "${layerHeader.type}") - skipping it.`,
        error,
      );
      continue;
    }
    const subLayerEntries = Array.from(state.subLayerLookup.entries()).filter(
      ([, info]) => info.lonboardLayerId === layerHeader.id,
    );
    deckLayers.push(...built);
    newLayerCache.set(layerHeader.id, {
      bytesFingerprint,
      headerFingerprint,
      table,
      deckLayers: built,
      subLayerEntries,
      columnFingerprints,
    });
  }
  state.layerCache = newLayerCache;

  performance.mark("st-lonboard:setProps:start");
  state.overlay.setProps({
    layers: deckLayers,
    pickingRadius: header.mapOptions.pickingRadius,
    parameters: header.mapOptions.parameters,
    useDevicePixels: header.mapOptions.useDevicePixels,
  });
  performance.mark("st-lonboard:setProps:end");
  performance.measure("st-lonboard:setProps", "st-lonboard:setProps:start", "st-lonboard:setProps:end");

  const basemapStyle = header.mapOptions.basemapStyle ?? DEFAULT_BASEMAP_STYLE;
  if (basemapStyle !== state.lastBasemapStyle) {
    state.map.setStyle(basemapStyle);
    state.lastBasemapStyle = basemapStyle;
  }

  const height = header.mapOptions.height ?? 500;
  if (state.container.style.height !== `${height}px`) {
    state.container.style.height = `${height}px`;
    state.map.resize();
  }

  // maplibre-gl has no in-place "update" for either of these, so both are
  // fully rebuilt on change - cheap (a handful of DOM nodes), and `state.lastX`
  // starts `undefined` on a fresh mount, so this same block also performs the
  // *initial* build (no separate seeding needed in createMount).
  const customAttributionJson = JSON.stringify(header.mapOptions.customAttribution ?? null);
  if (customAttributionJson !== state.lastCustomAttributionJson) {
    applyAttribution(state, header.mapOptions.customAttribution);
    state.lastCustomAttributionJson = customAttributionJson;
  }

  const controlsJson = JSON.stringify(header.mapOptions.controls ?? []);
  if (controlsJson !== state.lastControlsJson) {
    applyControls(state, header.mapOptions.controls);
    state.lastControlsJson = controlsJson;
  }

  performance.measure("st-lonboard:mount", { start: mountStart, end: performance.now() });
  if (header.mapOptions.perf) {
    logPerfSummary();
  }

  return () => {
    state.map.remove();
  };
}
