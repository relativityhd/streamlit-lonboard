import { resolve } from "node:path";
import { defineConfig } from "vite";

// Bundles to a single ES module (+ single CSS file) so it can be referenced
// by a fixed path from the component's pyproject.toml asset manifest
// (see IMPLEMENTATION_PLAN.md §6 and streamlit_lonboard/component.py).
export default defineConfig({
  // `threads` (used by @geoarrow/deck.gl-geoarrow's solid-polygon earcut pool)
  // has an unguarded top-level `process.on` reference meant for its Node.js
  // worker-runtime path; it still executes in the browser bundle and throws
  // `process is not defined` unless `process` resolves to *something*.
  define: {
    process: JSON.stringify({ env: { NODE_ENV: "production" } }),
  },
  build: {
    outDir: resolve(__dirname, "../src/streamlit_lonboard/frontend_dist"),
    emptyOutDir: true,
    cssCodeSplit: false,
    sourcemap: true,
    lib: {
      entry: resolve(__dirname, "src/index.ts"),
      formats: ["es"],
      fileName: () => "index.js",
    },
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
        assetFileNames: "index.[ext]",
      },
    },
  },
});
