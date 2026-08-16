/**
 * Instantiates deck.gl layer extensions from the wire-format extension
 * headers built by `streamlit_lonboard.serialize.serialize_extensions`.
 *
 * The layer-side props an extension injects (e.g. `getDashArray`,
 * `filterRange`) travel as ordinary entries in `LayerHeader.props` and need
 * no special handling here - see `layers.ts`'s `resolveProps`, which already
 * resolves any `get*`-prefixed arrow accessor the same way regardless of
 * whether a layer extension happens to be involved. This module only builds
 * the extension *instances* themselves (deck.gl needs the actual
 * `PathStyleExtension`/`DataFilterExtension`/... object on the layer's
 * `extensions` prop for its shader-injection machinery to activate) from
 * each extension's own config (e.g. `PathStyleExtension.dash`,
 * `DataFilterExtension.filterSize`).
 */

import type { LayerExtension } from "@deck.gl/core";
import {
  BrushingExtension,
  CollisionFilterExtension,
  DataFilterExtension,
  PathStyleExtension,
} from "@deck.gl/extensions";

export interface ExtensionHeader {
  /** lonboard `BaseExtension._extension_type`, e.g. "path-style". */
  type: string;
  props: Record<string, unknown>;
}

type ExtensionFactory = (props: Record<string, unknown>) => LayerExtension;

// Building a fresh instance per lonboard layer (not sharing one module-wide
// instance across layers) mirrors how buildDeckLayers already treats every
// prop as layer-specific, and keeps each layer's shader-compile-time options
// (e.g. DataFilterExtension's filterSize) independent of any other layer's.
const EXTENSION_FACTORIES: Record<string, ExtensionFactory> = {
  "path-style": (props) =>
    new PathStyleExtension({
      // Each field has its own default *parameter* on the deck.gl side
      // (`{dash = false, offset = false, highPrecisionDash = false} = {}}`),
      // which correctly resolves an absent/`undefined` value - no `??`
      // fallback needed here, unlike DataFilterExtension below.
      dash: props.dash as boolean | undefined,
      offset: props.offset as boolean | undefined,
      highPrecisionDash: props.highPrecisionDash as boolean | undefined,
    }),
  "data-filter": (props) =>
    new DataFilterExtension({
      // Unlike PathStyleExtension, DataFilterExtension's constructor merges
      // options via a plain object spread (`{...defaultOptions, ...opts}`),
      // which does NOT treat an explicit `undefined` specially - it would
      // silently clobber deck.gl's own default and disable filtering
      // entirely. So the fallback has to happen on our side, matching
      // deck.gl's own documented defaults exactly (filterSize: 1,
      // categorySize: 0 - see @deck.gl/extensions' data-filter-extension.js).
      // lonboard's own traits already constrain these to 1-4 / 1-4
      // (`filter_size`/`category_size`), matching deck.gl's `0|1|2|3|4`
      // union - cast rather than widen, since the value only exists as
      // `number` after a JSON round-trip.
      filterSize: (props.filterSize as 0 | 1 | 2 | 3 | 4 | undefined) ?? 1,
      categorySize: (props.categorySize as 0 | 1 | 2 | 3 | 4 | undefined) ?? 0,
    }),
  brushing: () => new BrushingExtension(),
  "collision-filter": () => new CollisionFilterExtension(),
};

/**
 * Builds one deck.gl extension instance per header entry, skipping (with a
 * console warning) any extension type this frontend build doesn't know
 * about yet - mirrors `buildDeckLayers`'s handling of an unsupported layer
 * type, so one unrecognized extension doesn't take down the whole layer.
 */
export function buildLayerExtensions(
  headers: ExtensionHeader[] | undefined,
  layerId: string,
): LayerExtension[] {
  if (!headers || headers.length === 0) return [];

  const extensions: LayerExtension[] = [];
  for (const header of headers) {
    const factory = EXTENSION_FACTORIES[header.type];
    if (!factory) {
      console.warn(
        `streamlit-lonboard: unsupported layer extension "${header.type}" (layer ${layerId})`,
      );
      continue;
    }
    extensions.push(factory(header.props));
  }
  return extensions;
}
