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
import { type ContainerHeader, parseContainer } from "./container";
import { buildDeckLayers, type SubLayerInfo } from "./layers";

interface ComponentApi {
  name: string;
  data: unknown;
  key: string;
  parentElement: Element & Record<string, unknown>;
  setStateValue: (stateKey: string, value: unknown) => void;
  setTriggerValue: (eventKey: string, value: unknown) => void;
}

interface MountState {
  map: maplibregl.Map;
  overlay: MapboxOverlay;
  container: HTMLDivElement;
  subLayerLookup: Map<string, SubLayerInfo>;
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
  };

  const overlay = new MapboxOverlay({
    interleaved: false,
    layers: [],
    onClick: (info: PickingInfo) => {
      if (!header.mapOptions.onClick) return;
      component.setTriggerValue("clicked", pickPayload(state.subLayerLookup, info));
    },
    onHover: (info: PickingInfo) => {
      if (!header.mapOptions.onHover) return;
      if (state.hoverThrottle !== undefined) return;
      state.hoverThrottle = setTimeout(() => {
        state.hoverThrottle = undefined;
      }, THROTTLE_MS);
      component.setTriggerValue("hovered", pickPayload(state.subLayerLookup, info));
    },
    onViewStateChange: (params: { viewState: Record<string, unknown> }) => {
      if (!header.mapOptions.returnViewState) return;
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

function ensureMount(component: ComponentApi, header: ContainerHeader): MountState {
  const existing = component.parentElement[MOUNT_KEY] as MountState | undefined;
  return existing ?? createMount(component, header);
}

export default function mount(component: ComponentApi): () => void {
  const bytes = toUint8Array(component.data);
  const { header, layers } = parseContainer(bytes);
  const state = ensureMount(component, header);

  state.subLayerLookup.clear();
  const deckLayers: Layer[] = [];
  for (const { header: layerHeader, table } of layers) {
    deckLayers.push(...buildDeckLayers(layerHeader, table, state.subLayerLookup));
  }
  state.overlay.setProps({ layers: deckLayers });

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

  return () => {
    state.map.remove();
  };
}
