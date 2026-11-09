import { expect, test, type Page } from "@playwright/test"

import { STATUS_OK } from "./runs-api"

// Shell smoke tests. The health API (and the status page's GET /api/v1/system/status) is mocked here
// so they run without a backend; e2e/status.spec.ts covers the status page, e2e/stack.spec.ts the
// real stack (the database and the worker) that `make e2e` starts.

const HEALTH_OK = {
  db: "ok",
  worker: "running",
  version: "2.0.0.dev0",
}

async function mockHealth(page: Page, status: number, body: unknown) {
  await page.route("**/api/v1/health", (route) =>
    route.fulfill({ status, json: body })
  )
  await page.route("**/api/v1/system/status", (route) =>
    route.fulfill({ json: STATUS_OK })
  )
}

test("the shell loads with its navbar", async ({ page }) => {
  await mockHealth(page, 200, HEALTH_OK)
  await page.goto("/")

  await expect(page).toHaveTitle("Status · 3DMN Portal")
  await expect(page.getByRole("link", { name: "3DMN Portal" })).toBeVisible()
  const nav = page.getByRole("navigation", { name: "Main" })
  // The four main links in order, then Live: GET /programs is not mocked here, and the e2e stack has
  // live processing on (`[live] enabled`, deploy/supervisor/e2e.sh), so it lists the live programs.
  await expect(nav.getByRole("link")).toHaveText([
    "Scans",
    "Reconstructions",
    "Indexations",
    "Run Monitor",
    "Live",
  ])

  await nav.getByRole("button", { name: "Menu" }).click()
  await expect(page.getByRole("menuitem")).toHaveText([
    "Import Scans",
    "New Wire Reconstruction",
    "New Indexation",
  ])
  await page.getByRole("menuitem", { name: "Import Scans" }).click()
  await expect(page).toHaveURL(/\/scans\/import$/)
  await expect(page).toHaveTitle("Import Scans · 3DMN Portal")

  await nav.getByRole("link", { name: "Run Monitor" }).click()
  await expect(page).toHaveURL(/\/runs$/)
  await expect(page).toHaveTitle("Run Monitor · 3DMN Portal")
  await expect(page.getByRole("heading", { name: "Run Monitor" })).toBeVisible()
})

test("unknown URLs show the 404 page", async ({ page }) => {
  await mockHealth(page, 200, HEALTH_OK)
  for (const path of ["/no-such-page", "/scans/7/no-such-tab"]) {
    await page.goto(path)
    await expect(
      page.getByRole("heading", { name: "Page not found" })
    ).toBeVisible()
    await expect(page.getByRole("main")).toContainText(path)
    await expect(page).toHaveTitle("Page not found · 3DMN Portal")
  }

  // The title comes from the router alone, so leaving the 404 page replaces it.
  await page.getByRole("link", { name: "Go to the status page" }).click()
  await expect(page).toHaveURL(/\/$/)
  await expect(page).toHaveTitle("Status · 3DMN Portal")
})

test("the theme toggle switches to dark and persists", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" })
  await mockHealth(page, 200, HEALTH_OK)
  await page.goto("/")
  const html = page.locator("html")
  await expect(html).not.toHaveClass(/\bdark\b/)

  await page.getByRole("button", { name: "Theme" }).click()
  await page.getByRole("menuitemradio", { name: "Dark" }).click()
  await expect(html).toHaveClass(/\bdark\b/)

  await page.reload()
  await expect(html).toHaveClass(/\bdark\b/)

  await page.getByRole("button", { name: "Theme" }).click()
  await page.getByRole("menuitemradio", { name: "System" }).click()
  await expect(html).not.toHaveClass(/\bdark\b/)
  await page.emulateMedia({ colorScheme: "dark" })
  await expect(html).toHaveClass(/\bdark\b/)
})

// The browser the specs run in has WebGL (the 3D view and its checks need it), and Canvas2D works (the 2D views
// draw with it). The no-GPU project is to check the opposite: no WebGL, Canvas2D still works.
test("WebGL and Canvas2D both work", async ({ page }) => {
  await mockHealth(page, 200, HEALTH_OK)
  await page.goto("/")
  const { webgl, pixel } = await page.evaluate(() => {
    // A canvas keeps its first context type, so each probe gets its own canvas.
    const canvas = () => document.createElement("canvas")
    const webgl =
      canvas().getContext("webgl2") !== null ||
      canvas().getContext("webgl") !== null
    const ctx = canvas().getContext("2d")
    if (!ctx) return { webgl, pixel: [] }
    ctx.fillStyle = "rgb(255, 0, 0)"
    ctx.fillRect(0, 0, 4, 4)
    return { webgl, pixel: Array.from(ctx.getImageData(1, 1, 1, 1).data) }
  })
  expect(webgl).toBe(true)
  expect(pixel).toEqual([255, 0, 0, 255])
})
