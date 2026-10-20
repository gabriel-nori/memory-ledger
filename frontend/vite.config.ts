/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

// The build output (frontend/dist) is NOT committed: `python3 scripts/serve.py`
// runs this build on first start, and again when the source is newer, so npm is
// needed for the UI but never for the API. `base: "./"` keeps the asset URLs
// relative so the same dist works whether it is served from / or from a
// sub-path.
export default defineConfig({
  base: "./",
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    // One JS file and one CSS file. A local server with no HTTP/2 and no CDN
    // gains nothing from chunking, and a stable pair of names keeps the
    // committed diff readable when only the app code changed.
    rollupOptions: {
      output: {
        entryFileNames: "assets/app.js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: "assets/app.[ext]",
      },
    },
  },
  server: {
    port: 5173,
    // `npm run dev` talks to a real serve.py on 8770, so the dev server is a
    // hot-reloading front end over the actual brain, not a mock.
    proxy: { "/api": { target: "http://127.0.0.1:8770", changeOrigin: true } },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    css: false,
    // Node 22+ ships its own experimental localStorage; without a backing file
    // it is an empty getter that shadows jsdom's, so window.localStorage is
    // undefined in every test. Turn it off so jsdom's applies. Older Node
    // (the CI runs 20) rejects the flag and kills the worker, so pass it only
    // where it exists.
    poolOptions: {
      forks: {
        execArgv: process.allowedNodeEnvironmentFlags.has("--experimental-webstorage")
          ? ["--no-experimental-webstorage"]
          : [],
      },
    },
    coverage: {
      provider: "v8",
      reporter: ["text", "lcov"],
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/**/*.test.{ts,tsx}", "src/test/**", "src/main.tsx"],
    },
  },
});
