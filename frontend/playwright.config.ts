import { defineConfig, devices } from "@playwright/test"

// Node's `process`, typed here because the frontend tsconfig has browser types only.
declare const process: { env: Partial<Record<string, string>> }

// `make e2e` serves the built SPA and the API on 2100. E2E_BASE_URL points the specs elsewhere, e.g.
// at `vite preview` on an experiments port (2103-2109); the stack behind it must serve the e2e
// database (e2e/global-setup.ts checks it before any spec runs).
const baseURL = process.env.E2E_BASE_URL ?? "http://127.0.0.1:2100"

export default defineConfig({
  testDir: "e2e",
  globalSetup: "./e2e/global-setup.ts",
  // One browser at a time, for development machines with little memory.
  workers: 1,
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  // In CI, also the HTML report (frontend/playwright-report, with the traces of failed tests), which the manual
  // workflow keeps as an artifact.
  reporter: process.env.CI
    ? [["list"], ["html", { open: "never" }]]
    : [["list"]],
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  // Chromium only. `default` runs every spec; `no-gpu` runs only the WebGL rendering checks (tests
  // tagged @webgl) in a Chromium without any WebGL: --disable-gpu and --disable-software-rasterizer,
  // and without Playwright's default --enable-unsafe-swiftshader. Uses the browsers in
  // ~/.cache/ms-playwright (rev 1248 = 1.64).
  projects: [
    { name: "default", use: { ...devices["Desktop Chrome"] } },
    {
      name: "no-gpu",
      grep: /@webgl/,
      use: {
        ...devices["Desktop Chrome"],
        launchOptions: {
          args: ["--disable-gpu", "--disable-software-rasterizer"],
          ignoreDefaultArgs: ["--enable-unsafe-swiftshader"],
        },
      },
    },
  ],
})
