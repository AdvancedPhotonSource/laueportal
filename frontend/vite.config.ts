import tailwindcss from "@tailwindcss/vite"
import { tanstackRouter } from "@tanstack/router-plugin/vite"
import react from "@vitejs/plugin-react"
import { configDefaults, defineConfig } from "vitest/config"

// The slow set: the heaviest page tests (half of the tests' time), which `make check-frontend` leaves out and `make
// test-frontend-slow` runs (as the Full tests workflow does before a release). Each page's logic has fast tests.
const SLOW_TESTS = [
  "src/features/live/LivePage.test.tsx",
  "src/features/scans/ScanDetailPage.test.tsx",
  "src/components/DataGrid/DataGrid.test.tsx",
  "src/features/program-form/CreateRunPage.test.tsx",
  "src/app/router.test.tsx",
  "src/features/reconstruction/DepthViewTab.test.tsx",
]

// Ports: 2100 API (and the built SPA in production), 2101 this dev server.
const API_ORIGIN = "http://127.0.0.1:2100"

export default defineConfig({
  plugins: [
    // Must come before react(): generates src/routeTree.gen.ts from src/routes.
    tanstackRouter({
      target: "react",
      autoCodeSplitting: true,
      // The plugin's default header adds `// @ts-nocheck`; ours keeps the route tree typechecked.
      routeTreeFileHeader: [
        "/* eslint-disable */",
        "// noinspection JSUnusedGlobalSymbols",
      ],
    }),
    react(),
    tailwindcss(),
  ],
  resolve: { tsconfigPaths: true },
  build: {
    // Chrome/Edge tested; Firefox ESR 140+ and Safari 16.5+ built for.
    target: ["chrome111", "edge111", "firefox140", "safari16.5"],
    rolldownOptions: {
      output: {
        // React in its own chunk: it changes rarely, so browsers keep it cached across deploys.
        codeSplitting: {
          groups: [
            {
              name: "react",
              test: /[\\/]node_modules[\\/](react|react-dom|scheduler)[\\/]/,
            },
          ],
        },
      },
    },
  },
  server: {
    host: "127.0.0.1",
    port: 2101,
    strictPort: true,
    proxy: { "/api": API_ORIGIN },
  },
  // `pnpm preview` serves dist/ for local checks on an experiments port (2103-2109).
  preview: {
    host: "127.0.0.1",
    port: 2103,
    strictPort: true,
    proxy: { "/api": API_ORIGIN },
  },
  test: {
    restoreMocks: true,
    unstubGlobals: true,
    // No transformed modules kept between runs: the cache kept a typed import.meta.glob's file list from a
    // previous run, so a test passed without seeing new files. Without it a run costs about 2 s more.
    fsModuleCache: false,
    // A .test.ts file runs in Node, without a DOM (jsdom costs about 1 s of a worker per file); one whose code reads
    // the DOM or the browser's storage, also on import (Node then warns that localStorage is not available), starts
    // with `// @vitest-environment jsdom`. Component tests (.test.tsx) run in jsdom.
    projects: [
      {
        extends: true,
        test: {
          name: "node",
          include: ["src/**/*.test.ts"],
          exclude: [...configDefaults.exclude, ...SLOW_TESTS],
          environment: "node",
        },
      },
      {
        extends: true,
        test: {
          name: "jsdom",
          include: ["src/**/*.test.tsx"],
          exclude: [...configDefaults.exclude, ...SLOW_TESTS],
          environment: "jsdom",
        },
      },
      {
        extends: true,
        test: { name: "slow", include: SLOW_TESTS, environment: "jsdom" },
      },
    ],
  },
})
