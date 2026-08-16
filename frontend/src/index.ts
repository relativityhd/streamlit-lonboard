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
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { type ContainerHeader, type MapOptions, parseContainer } from "./container";
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
 * so an unchanged rerun (fingerprint match, see container.ts) can be
 * answered without re-parsing or reconstructing anything - see Phase 4b in
 * IMPLEMENTATION_PLAN.md. Note the common "nothing at all changed" case
 * already costs ~0 without this (CCv2 skips calling `mount()` entirely then,
 * confirmed in Phase 4.0) - this cache only matters when *some* layers
 * changed and others didn't within a single `mount()` invocation.
 */
interface LayerCacheEntry {
  fingerprint: string;
  deckLayers: Layer[];
  subLayerEntries: [string, SubLayerInfo][];
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
  };

  const overlay = new MapboxOverlay({
    interleaved: false,
    layers: [],
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
  performance.mark("st-lonboard:mount:start");

  const bytes = toUint8Array(component.data);

  // Peek at any existing mount's layer cache *before* parsing, so
  // parseContainer can skip tableFromIPC for layers whose bytes haven't
  // changed since last time (see container.ts).
  const existingState = component.parentElement[MOUNT_KEY] as MountState | undefined;
  const previousFingerprints = new Map<string, string>();
  if (existingState) {
    for (const [layerId, entry] of existingState.layerCache) {
      previousFingerprints.set(layerId, entry.fingerprint);
    }
  }

  // parseContainer is async because a gzip-compressed payload
  // (compression="auto"/"gzip" in st_lonboard()) needs to go through the
  // browser's native (Promise-based) DecompressionStream before it can be
  // sliced into per-layer Arrow IPC ranges. CCv2 awaits this default export,
  // so an async mount() is supported (confirmed against the bundled runtime).
  const { header, layers } = await parseContainer(bytes, previousFingerprints);
  const state = existingState ?? createMount(component, header);
  state.mapOptions = header.mapOptions;

  state.subLayerLookup.clear();
  const deckLayers: Layer[] = [];
  const newLayerCache = new Map<string, LayerCacheEntry>();

  for (const parsedLayer of layers) {
    const { header: layerHeader, fingerprint } = parsedLayer;

    if (parsedLayer.status === "unchanged") {
      const cached = state.layerCache.get(layerHeader.id);
      if (!cached) {
        // Can't happen: "unchanged" is only returned when previousFingerprints
        // (built from state.layerCache just above) already had this id+fingerprint.
        throw new Error(
          `streamlit-lonboard: internal error - layer ${layerHeader.id} reported unchanged ` +
            "but has no cached build to reuse",
        );
      }
      for (const [subId, info] of cached.subLayerEntries) {
        state.subLayerLookup.set(subId, info);
      }
      deckLayers.push(...cached.deckLayers);
      newLayerCache.set(layerHeader.id, cached);
      continue;
    }

    let built: Layer[];
    try {
      built = buildDeckLayers(layerHeader, parsedLayer.table, state.subLayerLookup);
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
    newLayerCache.set(layerHeader.id, { fingerprint, deckLayers: built, subLayerEntries });
  }
  state.layerCache = newLayerCache;

  performance.mark("st-lonboard:setProps:start");
  state.overlay.setProps({ layers: deckLayers });
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

  performance.mark("st-lonboard:mount:end");
  performance.measure("st-lonboard:mount", "st-lonboard:mount:start", "st-lonboard:mount:end");
  if (header.mapOptions.perf) {
    logPerfSummary();
  }

  return () => {
    state.map.remove();
  };
}
