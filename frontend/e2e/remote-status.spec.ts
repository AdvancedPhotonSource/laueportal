import { expect, test } from "@playwright/test"

import type { RemoteReadiness } from "../src/api"

function ready(): RemoteReadiness {
  return {
    enabled: true,
    state: "ok",
    checked_at: "2026-01-01T12:00:00Z",
    stale: false,
    checking: false,
    retry_after_seconds: 300,
    checks: [
      { name: "dm", state: "ok", detail: "Available" },
      { name: "flows", state: "ok", detail: "Available" },
      { name: "compute", state: "ok", detail: "Available" },
      { name: "aps", state: "ok", detail: "Accessible" },
      { name: "eagle", state: "ok", detail: "Accessible" },
      { name: "crux", state: "ok", detail: "Online" },
    ],
  }
}

for (const theme of ["light", "dark"] as const) {
  test(`ALCF status: details, refresh and login/offline states in ${theme}`, async ({
    page,
  }, testInfo) => {
    await page.addInitScript((theme) => {
      localStorage.setItem("laueportal:theme", theme)
    }, theme)
    let answer = ready()
    const writes: string[] = []
    page.on("request", (request) => {
      if (!["GET", "HEAD", "OPTIONS"].includes(request.method()))
        writes.push(request.url())
    })
    await page.route("**/api/v1/system/remote-status", (route) =>
      route.fulfill({ json: answer })
    )
    await page.route("**/api/v1/health", (route) =>
      route.fulfill({
        json: { db: "ok", worker: "running", version: "2.0.0.dev0" },
      })
    )
    await page.goto("/")
    const indicator = page.getByRole("button", {
      name: "ALCF: Available",
      exact: true,
    })
    await expect(indicator).toBeVisible()
    await expect(indicator.getByText("ALCF", { exact: true })).toBeVisible()
    const local = page.getByRole("link", {
      name: "Local Worker: running",
      exact: true,
    })
    await expect(local.getByText("Local", { exact: true })).toBeVisible()
    await local.hover()
    const tooltip = page.locator('[data-slot="tooltip-content"][data-open]')
    await expect(tooltip).toContainText("Worker: running")
    await indicator.hover()
    await expect(tooltip).toContainText("Crux: Online · 6 checks passed")
    await expect(tooltip).toContainText("Checked at")
    await page.screenshot({
      path: testInfo.outputPath(`remote-hover-${theme}.png`),
    })
    await indicator.click()
    const dialog = page.getByRole("dialog")
    await expect(
      dialog.getByRole("heading", { name: "ALCF status" })
    ).toBeVisible()
    await expect(
      dialog.getByText("Crux endpoint", { exact: true })
    ).toBeVisible()
    await expect(dialog.getByText("Online", { exact: true })).toBeVisible()
    await page.screenshot({
      path: testInfo.outputPath(`remote-status-${theme}.png`),
    })

    answer = {
      ...ready(),
      state: "warning",
      checks: ready().checks.map((c) =>
        c.name === "eagle"
          ? { ...c, state: "login_required", detail: "Sign in to Globus" }
          : c
      ),
    }
    await dialog.getByRole("button", { name: "Refresh", exact: true }).click()
    await expect(
      dialog.getByText("Sign in to Globus", { exact: true })
    ).toBeVisible()
    await expect(dialog.getByText("Online", { exact: true })).toBeVisible()
    await expect(
      page.getByRole("button", {
        name: "ALCF: Login needed",
        exact: true,
        includeHidden: true,
      })
    ).toHaveAttribute("data-tone", "warning")
    await dialog.getByRole("button", { name: "Close", exact: true }).click()
    const login = page.getByRole("button", {
      name: "ALCF: Login needed",
      exact: true,
    })
    await login.hover()
    await expect(tooltip).toContainText("Eagle storage: Login needed")
    await login.click()

    answer = {
      ...ready(),
      state: "error",
      checks: ready().checks.map((c) =>
        c.name === "crux"
          ? { ...c, state: "unavailable", detail: "Offline" }
          : c
      ),
    }
    await dialog.getByRole("button", { name: "Refresh", exact: true }).click()
    await expect(
      page.getByRole("button", {
        name: "ALCF: Unavailable",
        exact: true,
        includeHidden: true,
      })
    ).toHaveAttribute("data-tone", "error")
    await expect(dialog.getByText("Offline", { exact: true })).toBeVisible()

    answer = { ...ready(), stale: true }
    await dialog.getByRole("button", { name: "Refresh", exact: true }).click()
    await expect(
      page.getByRole("button", {
        name: "ALCF: Status stale",
        exact: true,
        includeHidden: true,
      })
    ).toHaveAttribute("data-tone", "warning")
    expect(writes).toEqual([])
  })
}

test("ALCF status polls every five minutes, and disabled status hides the indicator", async ({
  page,
}) => {
  await page.clock.install()
  let calls = 0
  let answer = ready()
  await page.route("**/api/v1/system/remote-status", (route) => {
    calls++
    return route.fulfill({ json: answer })
  })
  await page.goto("/")
  await expect(
    page.getByRole("button", { name: "ALCF: Available", exact: true })
  ).toBeVisible()
  await page.setViewportSize({ width: 768, height: 720 })
  const box = await page
    .getByRole("button", { name: "ALCF: Available", exact: true })
    .boundingBox()
  expect(box).not.toBeNull()
  if (!box) throw new Error("ALCF indicator has no bounding box")
  expect(box.x).toBeGreaterThanOrEqual(0)
  expect(box.x + box.width).toBeLessThanOrEqual(768)
  const first = calls
  await page.clock.fastForward(299_000)
  expect(calls).toBe(first)
  answer = { ...ready(), enabled: false, state: "disabled", checks: [] }
  await page.clock.fastForward(2_000)
  await expect(page.getByRole("button", { name: /^ALCF:/ })).toHaveCount(0)
})
