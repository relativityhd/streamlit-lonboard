import { resolve } from "node:path";
import { defineConfig } from "vite";
import license from "rollup-plugin-license";

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
      plugins: [
        // index.js bundles deck.gl/apache-arrow/maplibre-gl/math.gl code
        // directly into the wheel (see pyproject.toml's frontend_dist
        // artifacts inclusion) - that's redistribution of their compiled
        // output, not just a dependency edge, so their license texts must
        // ship alongside it. Minification strips source comments (only
        // maplibre-gl's `@license` banner survives that), so this generates
        // the notices file fresh from each package's license metadata on
        // every build instead of relying on manually-maintained text.
        license({
          thirdParty: {
            includePrivate: false,
            output: {
              file: resolve(__dirname, "../src/streamlit_lonboard/frontend_dist/THIRD-PARTY-NOTICES.txt"),
              encoding: "utf-8",
              template(dependencies) {
                return dependencies
                  .map((dep) => {
                    const header = `${dep.name}@${dep.version} - ${dep.license}`;
                    const rule = "=".repeat(header.length);
                    const text = dep.licenseText ?? "(license text not found - see package metadata)";
                    return `${header}\n${rule}\n${text}`;
                  })
                  .join("\n\n");
              },
            },
          },
        }),
      ],
    },
  },
});
