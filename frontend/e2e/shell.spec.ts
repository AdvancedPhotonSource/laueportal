import { expect, test, type Page, type Route } from "@playwright/test"

// The app shell with the health API mocked: the navbar's pages and Menu, the live Worker indicator
// and its tooltip, and the 404 page.
// e2e/stack.spec.ts checks the indicators against the real stack.

const HEALTH_OK = {
  db: "ok",
  worker: "running",
  version: "2.0.0.dev0",
}

type HealthAnswer = object | "unreachable"

/** Answer GET /api/v1/health with `answer()`, read on every request (so a test can change it). */
async function mockHealth(page: Page, answer: () => HealthAnswer) {
  await page.route("**/api/v1/health", (route: Route) => {
    const body = answer()
    return body === "unreachable"
      ? route.abort("connectionrefused")
      : route.fulfill({ json: body })
  })
}

/** The open tooltip (Base UI's tooltips have no role; a closing one stays until it has faded). */
function openTooltip(page: Page) {
  return page.locator('[data-slot="tooltip-content"][data-open]')
}

function indicators(page: Page) {
  return page
    .getByRole("group", { name: "Live system status" })
    .getByRole("link")
}

test("the navbar and its Menu reach every page", async ({ page }) => {
  await mockHealth(page, () => HEALTH_OK)
  await page.goto("/")
  const nav = page.getByRole("navigation", { name: "Main" })
  const heading = page.getByRole("heading", { level: 1 })

  for (const [label, path] of [
    ["Scans", "/scans"],
    ["Reconstructions", "/reconstructions"],
    ["Indexations", "/indexing"],
    ["Run Monitor", "/runs"],
    // The e2e stack has live processing on (deploy/supervisor/e2e.sh), so GET /programs lists the live programs.
    ["Live", "/live"],
  ] as const) {
    const link = nav.getByRole("link", { name: label, exact: true })
    await link.click()
    await expect(page).toHaveURL(new RegExp(`${path}$`))
    await expect(page).toHaveTitle(`${label} · 3DMN Portal`)
    await expect(heading).toHaveText(label)
    await expect(link).toHaveAttribute("aria-current", "page")
    await expect(nav.locator('[aria-current="page"]')).toHaveCount(1)
  }

  for (const [item, path, section] of [
    ["Import Scans", "/scans/import", "Scans"],
    ["New Wire Reconstruction", "/reconstructions/new", "Reconstructions"],
    ["New LaueGo Indexation", "/indexing/new", "Indexations"],
  ] as const) {
    await nav.getByRole("button", { name: "Menu" }).click()
    await expect(page.getByRole("menuitem")).toHaveText([
      "Import Scans",
      "New Wire Reconstruction",
      "New LaueGo Indexation",
    ])
    await page.getByRole("menuitem", { name: item }).click()
    await expect(page).toHaveURL(new RegExp(`${path}$`))
    await expect(page).toHaveTitle(`${item} · 3DMN Portal`)
    await expect(heading).toHaveText(item)
    // Below its section's page, the section's link stays highlighted.
    const sectionLink = nav.getByRole("link", { name: section, exact: true })
    await expect(sectionLink).toHaveAttribute("aria-current", "true")
    await expect(sectionLink).toHaveAttribute("data-section", "active")
    await expect(nav.locator('[aria-current="page"]')).toHaveCount(0)
  }

  await page.getByRole("link", { name: "3DMN Portal" }).click()
  await expect(page).toHaveURL(/\/$/)
  await expect(page).toHaveTitle("Status · 3DMN Portal")
  await expect(heading).toHaveText("Status")
})

test("below 1024 px the Live link is the Menu's first item", async ({
  page,
}) => {
  await mockHealth(page, () => HEALTH_OK)
  await page.setViewportSize({ width: 900, height: 600 })
  await page.goto("/runs")
  const nav = page.getByRole("navigation", { name: "Main" })
  await expect(
    nav.getByRole("link", { name: "Live", exact: true })
  ).toBeHidden()
  await nav.getByRole("button", { name: "Menu" }).click()
  await expect(page.getByRole("menuitem")).toHaveText([
    "Live",
    "Import Scans",
    "New Wire Reconstruction",
    "New LaueGo Indexation",
  ])
  await page.getByRole("menuitem", { name: "Live" }).click()
  await expect(page).toHaveURL(/\/live$/)
  await expect(page).toHaveTitle("Live · 3DMN Portal")
})

test("the Worker indicator shows the worker running, with details in a tooltip", async ({
  page,
}) => {
  await mockHealth(page, () => HEALTH_OK)
  await page.goto("/runs")
  await expect(indicators(page)).toHaveText(["Worker: running"])
  await expect(indicators(page)).toHaveAttribute("data-tone", "ok")
  await expect(page.getByRole("banner")).not.toContainText(/redis/i)

  const tooltip = openTooltip(page)
  await indicators(page).hover()
  await expect(tooltip).toContainText(/^Worker: runningChecked at \d/)

  // The indicator leads to the status page, which has the full details.
  await indicators(page).click()
  await expect(page).toHaveURL(/\/$/)
  await expect(page).toHaveTitle("Status · 3DMN Portal")
})

// The other states' texts and tones: health-indicators.test.ts (each state) and e2e/status.spec.ts.
test("the Worker indicator shows a worker not running, with its note in the tooltip", async ({
  page,
}) => {
  await mockHealth(page, () => ({ ...HEALTH_OK, worker: "not_running" }))
  await page.goto("/scans")
  await expect(indicators(page)).toHaveText(["Worker: not running"])
  await expect(indicators(page)).toHaveAttribute("data-tone", "error")
  await indicators(page).hover()
  await expect(openTooltip(page)).toContainText(
    "Queued runs wait until it starts"
  )
})

/**
 * At `width`: no sideways scrolling; brand, indicator, links and theme toggle side by side, none
 * clipped. Returns whether the indicator's label is cut to an ellipsis.
 */
async function navbarFits(page: Page, width: number): Promise<boolean> {
  const parts = [
    page.getByRole("link", { name: "3DMN Portal" }),
    page.getByRole("group", { name: "Live system status" }),
    page.getByRole("navigation", { name: "Main" }),
    page.getByRole("button", { name: "Theme" }),
  ]
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
    `page width at ${String(width)} px`
  ).toBeLessThanOrEqual(width)
  expect(
    await parts[0]?.evaluate((brand) => brand.scrollWidth - brand.clientWidth)
  ).toBe(0)
  let left = 0
  for (const part of parts) {
    const box = await part.boundingBox()
    expect(box, `${String(width)} px`).not.toBeNull()
    expect(box?.x ?? -1, `${String(width)} px`).toBeGreaterThanOrEqual(left)
    left = (box?.x ?? 0) + (box?.width ?? 0)
  }
  expect(left).toBeLessThanOrEqual(width)
  return indicators(page)
    .locator("span.truncate")
    .evaluate((span) => span.scrollWidth > span.clientWidth)
}

test("the navbar fits narrow desktop widths, the Worker label whole", async ({
  page,
}) => {
  await mockHealth(page, () => HEALTH_OK)
  for (const width of [1280, 900, 768]) {
    await page.setViewportSize({ width, height: 600 })
    await page.goto("/runs")
    // The name keeps the whole label, however much of it shows.
    await expect(indicators(page)).toHaveText("Worker: running")
    // "Worker" fits down to 768 px.
    expect(
      await navbarFits(page, width),
      `worker label truncated at ${String(width)} px`
    ).toBe(false)
  }

  // The tooltip keeps the details.
  await indicators(page).hover()
  await expect(openTooltip(page)).toContainText("Worker: running")
})

test("a longer label gives way on a narrow navbar: API unreachable at 768 px", async ({
  page,
}) => {
  await page.clock.install()
  await mockHealth(page, () => "unreachable")
  await page.setViewportSize({ width: 1280, height: 600 })
  await page.goto("/runs")
  await page.clock.fastForward(5_000) // the check's two retries
  await expect(indicators(page)).toHaveText(["API unreachable"])
  expect(await navbarFits(page, 1280), "truncated at 1280 px").toBe(false)

  await page.setViewportSize({ width: 768, height: 600 })
  await expect(indicators(page)).toHaveAccessibleName("API unreachable")
  expect(await navbarFits(page, 768), "truncated at 768 px").toBe(true)
})

test("the indicators say when the API is unreachable, and recover on the next poll", async ({
  page,
}) => {
  await page.clock.install()
  let health: HealthAnswer = "unreachable"
  await mockHealth(page, () => health)
  await page.goto("/scans")

  // The check is retried twice (after 1 and 2 s) before it counts as failed.
  await page.clock.fastForward(5_000)
  await expect(indicators(page)).toHaveText(["API unreachable"])
  await indicators(page).hover()
  await expect(openTooltip(page)).toContainText("The server cannot be reached")

  health = HEALTH_OK
  await page.clock.fastForward(10_000)
  await expect(indicators(page)).toHaveText(["Worker: running"])
})

test("unknown URLs show the 404 page", async ({ page }) => {
  await mockHealth(page, () => HEALTH_OK)
  for (const path of ["/scan", "/job?job_id=abc", "/create-recon"]) {
    await page.goto(path)
    await expect(
      page.getByRole("heading", { name: "Page not found" })
    ).toBeVisible()
    await expect(page).toHaveTitle("Page not found · 3DMN Portal")
  }
})
