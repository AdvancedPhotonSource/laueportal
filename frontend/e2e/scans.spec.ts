import { expect, test, type Locator, type Page } from "@playwright/test"

import { prefillAsked } from "./prefill"
import { databaseUnavailable } from "./problems"
import { makeScans, mockScans } from "./scans-api"

// The scans page and its DataGrid, with GET /api/v1/scans answered from many synthetic scans
// (e2e/scans-api.ts: the grid query protocol's sort, filters and paging); the demo database the e2e
// stack seeds has only a few. e2e/stack.spec.ts checks the real list.

// Dates show in the browser's time zone, and date filters send its offset.
test.use({ timezoneId: "America/Chicago" })

const HEALTH_OK = {
  db: "ok",
  worker: "running",
  version: "2.0.0.dev0",
}

async function openScans(page: Page, count = 250) {
  await page.route("**/api/v1/health", (route) =>
    route.fulfill({ json: HEALTH_OK })
  )
  const api = await mockScans(page, makeScans(count))
  await page.goto("/scans")
  return api
}

/** A scan's row (its unpinned part: the pinned checkbox column renders each row twice). */
function row(page: Page, scan: number) {
  return page
    .locator(`.ag-row[row-id="${String(scan)}"]`)
    .filter({ has: page.locator('[col-id="scan_number"]') })
}

function scanIds(page: Page) {
  return page.locator('.ag-row [col-id="scan_number"]')
}

function rowCheckbox(page: Page, scan: number) {
  return page.locator(
    `.ag-row[row-id="${String(scan)}"] input[type="checkbox"]`
  )
}

/**
 * A column's floating filter input: a text box, or a number box for a number column (which also
 * has a disabled text box, for filters too complex to edit there).
 */
function floatingFilter(page: Page, header: string) {
  return page.locator(`input[aria-label="${header} Filter Input"]:enabled`)
}

/**
 * How many px of a date input's value are out of view. Chromium scrolls the input's date editor (an
 * element of its user-agent shadow DOM, reached through the DevTools protocol) when the box is too
 * narrow for the date, which then shows as "…5/2026".
 */
async function hiddenDateWidth(page: Page, input: Locator): Promise<number> {
  await input.evaluate((element) => {
    element.setAttribute("data-measure", "date")
  })
  const client = await page.context().newCDPSession(page)
  try {
    await client.send("DOM.enable")
    const { result } = await client.send("Runtime.evaluate", {
      expression: 'document.querySelector("[data-measure=date]")',
    })
    if (!result.objectId) throw new Error("no date input")
    const { node } = await client.send("DOM.describeNode", {
      objectId: result.objectId,
      depth: -1,
      pierce: true,
    })
    type Node = typeof node
    const find = (from: Node): Node | undefined => {
      const attributes = from.attributes ?? []
      const pseudo = attributes[attributes.indexOf("pseudo") + 1]
      if (attributes.includes("pseudo") && pseudo === "-webkit-datetime-edit") {
        return from
      }
      for (const child of [
        ...(from.shadowRoots ?? []),
        ...(from.children ?? []),
      ]) {
        const found = find(child)
        if (found) return found
      }
      return undefined
    }
    const editor = find(node)
    if (!editor) throw new Error("no date editor in the input")
    const { object } = await client.send("DOM.resolveNode", {
      backendNodeId: editor.backendNodeId,
    })
    const { result: hidden } = await client.send("Runtime.callFunctionOn", {
      objectId: object.objectId,
      functionDeclaration:
        "function () { return this.scrollWidth - this.clientWidth }",
      returnByValue: true,
    })
    return Number(hidden.value)
  } finally {
    await client.detach()
  }
}

/** A column's header (its floating filter cell has the same col-id). */
function header(page: Page, field: string) {
  return page.locator(
    `.ag-header-cell[col-id="${field}"]:not(.ag-floating-filter)`
  )
}

/** The header labels, without the checkbox column's empty one. */
function headerLabels(page: Page) {
  return page.locator(
    '.ag-header-cell:not([col-id="ag-Grid-SelectionColumn"]) .ag-header-cell-text'
  )
}

test("lists the scans newest first, with their columns", async ({ page }) => {
  const api = await openScans(page)
  await expect(page).toHaveTitle("Scans · 3DMN Portal")
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Scans")
  await expect(headerLabels(page)).toHaveText([
    "Scan ID",
    "Sample Name",
    "Aperture",
    "Scan Dim",
    "Technique",
    "User",
    "Date",
    "Notes",
  ])
  await expect(scanIds(page).first()).toHaveText("1249")
  expect(api.requests[0]?.search).toBe("?offset=0&limit=100&sort=-scan_number")

  const newest = row(page, 1249)
  // Scan 1249 started 2026-09-07 06:30 UTC: 01:30 in Chicago (daylight-saving time).
  await expect(newest.locator('[col-id="time"]')).toHaveText(
    "2026-09-07 01:30:00"
  )
  await expect(newest.locator('[col-id="n_dimensions"]')).toHaveText("2D")
  await expect(newest.locator('[col-id="technique"]')).toHaveText("energy")
  await newest.getByRole("link", { name: "1249" }).click()
  await expect(page).toHaveURL(/\/scans\/1249$/)
  await expect(page).toHaveTitle("Scan 1249 · 3DMN Portal")
})

test("sorts and filters on the server", async ({ page }) => {
  const api = await openScans(page)
  await expect(scanIds(page).first()).toHaveText("1249")

  // Descending, then unsorted (the list's own order), then ascending: AG Grid's cycle.
  const scanHeader = header(page, "scan_number")
  await scanHeader.click()
  await expect(scanHeader).toHaveAttribute("aria-sort", "none")
  await scanHeader.click()
  await expect(scanHeader).toHaveAttribute("aria-sort", "ascending")
  await expect(scanIds(page).first()).toHaveText("1000")
  expect(api.requests.at(-1)?.searchParams.get("sort")).toBe("scan_number")

  // Text: Sample Name contains "ni".
  await floatingFilter(page, "Sample Name").fill("ni")
  await expect
    .poll(() => api.lastFilter())
    .toEqual({
      sample_name: { filterType: "text", type: "contains", filter: "ni" },
    })
  await expect(row(page, 1000).locator('[col-id="sample_name"]')).toHaveText(
    "Ni (111) film"
  )
  await expect(row(page, 1001)).toHaveCount(0)

  // Number: Scan Dim equals 2 (with the text filter: both apply).
  await floatingFilter(page, "Scan Dim").fill("2")
  await expect
    .poll(() => api.lastFilter()?.n_dimensions)
    .toEqual({ filterType: "number", type: "equals", filter: 2 })
  await expect(scanIds(page).nth(1)).toHaveText("1040")
  await expect(scanIds(page).first()).toHaveText("1000")
  await expect(row(page, 1000).locator('[col-id="n_dimensions"]')).toHaveText(
    "2D"
  )

  // Date: the local day, sent as its start with the UTC offset (Chicago: -06:00 in January).
  await floatingFilter(page, "Sample Name").fill("")
  await floatingFilter(page, "Scan Dim").fill("")
  await page
    .locator('.ag-floating-filter[col-id="time"] input[type="date"]')
    .fill("2026-01-15")
  await expect
    .poll(() => api.lastFilter())
    .toEqual({
      time: {
        filterType: "date",
        type: "equals",
        dateFrom: "2026-01-15T00:00:00-06:00",
      },
    })
  // Scan 1014 started 2026-01-15 06:30 UTC, 00:30 that day in Chicago.
  await expect(scanIds(page)).toHaveText(["1014"])
  await expect(row(page, 1014).locator('[col-id="time"]')).toHaveText(
    "2026-01-15 00:30:00"
  )
})

for (const width of [1280, 900]) {
  test(`a date shows whole in the Date filter at ${String(width)} px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 700 })
    await openScans(page)
    await expect(scanIds(page).first()).toHaveText("1249")
    const input = page.locator(
      '.ag-floating-filter[col-id="time"] input[type="date"]'
    )
    // Empty, it shows "mm/dd/yyyy" whole too.
    expect(await hiddenDateWidth(page, input)).toBe(0)
    await input.fill("2026-01-15")
    await expect(scanIds(page)).toHaveText(["1014"])
    await input.blur()
    expect(await hiddenDateWidth(page, input)).toBe(0)
  })
}

test("says when the filters match nothing, and clears them", async ({
  page,
}) => {
  const api = await openScans(page)
  await expect(scanIds(page).first()).toHaveText("1249")
  await floatingFilter(page, "Notes").fill("no such note")
  await expect(page.getByText("No scans match the filters")).toBeVisible()
  // The filters stay usable over the empty grid.
  await expect(floatingFilter(page, "Notes")).toBeEditable()

  await page.getByRole("button", { name: "Clear filters" }).click()
  await expect(scanIds(page).first()).toHaveText("1249")
  await expect(floatingFilter(page, "Notes")).toHaveValue("")
  expect(api.requests.at(-1)?.searchParams.has("filter")).toBe(false)
})

test("New Recon and New Index take the one selected scan", async ({ page }) => {
  await openScans(page)
  const newRecon = page.getByRole("button", { name: "New Recon" })
  const newIndex = page.getByRole("button", { name: "New Index" })
  await expect(scanIds(page).first()).toHaveText("1249")
  await expect(newRecon).toBeDisabled()
  await expect(newIndex).toBeDisabled()

  // One checked row: checking 1247 unchecks 1249.
  await rowCheckbox(page, 1249).check()
  await rowCheckbox(page, 1247).check()
  await expect(rowCheckbox(page, 1249)).not.toBeChecked()
  await expect(page.getByText("1 selected")).toBeVisible()
  await expect(newIndex).toBeEnabled()
  // The request's decoded parameters; the LaueGo form takes its prefill once and removes it from
  // the URL.
  expect(await prefillAsked(page, "lauego", () => newIndex.click())).toEqual({
    scan: "1247",
  })
  await expect(page).toHaveURL(/\/indexing\/new$/)
  await expect(page).toHaveTitle("New LaueGo Indexation · 3DMN Portal")

  await page.goBack()
  await expect(scanIds(page).first()).toHaveText("1249")
  await rowCheckbox(page, 1245).check()
  await expect(newRecon).toBeEnabled()
  // The wire form takes its prefill once and removes it from the URL.
  expect(await prefillAsked(page, "wire", () => newRecon.click())).toEqual({
    scan: "1245",
  })
  await expect(page).toHaveURL(/\/reconstructions\/new$/)
  await expect(page).toHaveTitle("New Wire Reconstruction · 3DMN Portal")
})

test("the selection is kept across filtering, paging and sorting", async ({
  page,
}) => {
  // 1500 scans: 15 blocks of 100, more than the 10 the grid keeps.
  const api = await openScans(page, 1500)
  await expect(scanIds(page).first()).toHaveText("2499")
  await rowCheckbox(page, 2499).check()
  const count = page.getByText(/^\d+ selected$/)
  await expect(count).toHaveText("1 selected")

  // Filtered out, the selected scan stays selected (and counted); checking another replaces it.
  await floatingFilter(page, "Sample Name").fill("ni")
  await expect(scanIds(page).first()).toHaveText("2496")
  await expect(count).toHaveText("1 selected")
  await rowCheckbox(page, 2496).check()
  await expect(count).toHaveText("1 selected")
  await floatingFilter(page, "Sample Name").fill("")
  await expect(rowCheckbox(page, 2496)).toBeChecked()
  await expect(rowCheckbox(page, 2499)).not.toBeChecked()

  // Page through every block to the last, select there, and come back.
  await row(page, 2499).hover()
  for (let block = 1; block < 15; block += 1) {
    await page.mouse.wheel(0, 3200)
    await expect(row(page, 2499 - block * 100)).toBeVisible()
  }
  expect(api.requests.map((url) => url.searchParams.get("offset"))).toContain(
    "1400"
  )
  await page.mouse.wheel(0, 3200)
  await rowCheckbox(page, 1000).check()
  await expect(count).toHaveText("1 selected")
  await page.mouse.wheel(0, -60_000)
  await expect(rowCheckbox(page, 2496)).not.toBeChecked()
  await expect(rowCheckbox(page, 2499)).not.toBeChecked()

  // Sorted the other way, every row is loaded again: the selection holds.
  const scanHeader = header(page, "scan_number")
  await scanHeader.click()
  await expect(scanHeader).toHaveAttribute("aria-sort", "none")
  await scanHeader.click()
  await expect(scanIds(page).first()).toHaveText("1000")
  await expect(rowCheckbox(page, 1000)).toBeChecked()
  await row(page, 1000).locator('[col-id="sample_name"]').click()
  await page.keyboard.press("Control+End")
  await expect(rowCheckbox(page, 2496)).not.toBeChecked()
  await expect(count).toHaveText("1 selected")

  await page.getByRole("button", { name: "Clear selection" }).click()
  await page.mouse.wheel(0, -60_000)
  await expect(rowCheckbox(page, 1000)).not.toBeChecked()
  await expect(count).toHaveCount(0)
  await expect(page.getByRole("button", { name: "New Index" })).toBeDisabled()
})

test("keeps a resized column's width across visits", async ({ page }) => {
  await openScans(page)
  await expect(scanIds(page).first()).toHaveText("1249")
  const sample = header(page, "sample_name")
  const before = (await sample.boundingBox())?.width ?? 0
  const handle = sample.locator(".ag-header-cell-resize")
  const box = await handle.boundingBox()
  if (!box) throw new Error("no resize handle")
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.mouse.down()
  await page.mouse.move(box.x + 80, box.y + box.height / 2, { steps: 5 })
  await page.mouse.up()
  await expect
    .poll(async () => (await sample.boundingBox())?.width ?? 0)
    .toBeGreaterThan(before + 60)
  const resized = (await sample.boundingBox())?.width ?? 0
  const stored = await page.evaluate(() =>
    window.localStorage.getItem("laueportal:grid:scans:v1")
  )
  expect(stored).toContain('"sample_name"')

  await page.reload()
  await expect(scanIds(page).first()).toHaveText("1249")
  await expect
    .poll(async () => Math.round((await sample.boundingBox())?.width ?? 0))
    .toBe(Math.round(resized))
})

/**
 * WCAG contrast of a selected row, as Chromium paints it (colours drawn on a canvas, over their
 * backdrop, and read back): its tint against an unselected row, its selection bar (3:1) and its
 * link (4.5:1) against it.
 */
/**
 * The theme's --background as a colour (the grid's, the fields'): the page itself is --canvas, on
 * which the grid and the cards are surfaces.
 */
function themeBackground(page: Page): Promise<string> {
  return page.evaluate(() => {
    const probe = document.createElement("div")
    probe.style.backgroundColor = "var(--background)"
    document.body.append(probe)
    const colour = getComputedStyle(probe).backgroundColor
    probe.remove()
    return colour
  })
}

function selectionContrast(page: Page, scan: number) {
  return page.evaluate((id) => {
    const canvas = document.createElement("canvas")
    canvas.width = canvas.height = 1
    const context = canvas.getContext("2d", { willReadFrequently: true })
    if (!context) throw new Error("no canvas")
    const paint = (...layers: string[]) => {
      context.clearRect(0, 0, 1, 1)
      for (const layer of layers) {
        context.fillStyle = "#000"
        context.fillStyle = layer
        context.fillRect(0, 0, 1, 1)
      }
      return Array.from(context.getImageData(0, 0, 1, 1).data.slice(0, 3))
    }
    const luminance = (rgb: number[]) => {
      const [r = 0, g = 0, b = 0] = rgb.map((value) => {
        const c = value / 255
        return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
      })
      return 0.2126 * r + 0.7152 * g + 0.0722 * b
    }
    const ratio = (a: number[], b: number[]) => {
      const [high, low] = [luminance(a), luminance(b)].sort((x, y) => y - x)
      return ((high ?? 0) + 0.05) / ((low ?? 0) + 0.05)
    }
    const grid = document.querySelector(".ag-root-wrapper")
    if (!grid) throw new Error("no grid")
    const style = getComputedStyle(grid)
    const background = paint(style.getPropertyValue("--ag-background-color"))
    const selected = paint(
      style.getPropertyValue("--ag-background-color"),
      style.getPropertyValue("--ag-selected-row-background-color")
    )
    const cell = document.querySelector(
      `.ag-row[row-id="${String(id)}"] .ag-cell[col-id="ag-Grid-SelectionColumn"]`
    )
    const bar = cell
      ? (/(?:oklch|rgb|lab)\([^)]*\)/.exec(
          getComputedStyle(cell).boxShadow
        )?.[0] ?? "")
      : ""
    const link = document.querySelector(
      `.ag-row[row-id="${String(id)}"] [col-id="scan_number"] a`
    )
    return {
      tint: ratio(selected, background),
      bar: ratio(paint(bar), selected),
      link: ratio(paint(link ? getComputedStyle(link).color : ""), selected),
    }
  }, scan)
}

const ALL_HEADERS = [
  "Scan ID",
  "Sample Name",
  "Aperture",
  "Scan Dim",
  "Technique",
  "User",
  "Date",
  "Notes",
]

test("the columns fit the grid from 900 px up, and scroll sideways below", async ({
  page,
}) => {
  await page.setViewportSize({ width: 900, height: 700 })
  await openScans(page)
  await expect(scanIds(page).first()).toHaveText("1249")
  const layout = () =>
    page.evaluate(() => {
      const viewport = document.querySelector<HTMLElement>(
        ".ag-body-horizontal-scroll-viewport"
      )
      const grid = document.querySelector(".ag-root-wrapper")
      const notes = document.querySelector(
        '.ag-header-cell[col-id="notes"]:not(.ag-floating-filter)'
      )
      const labels = Array.from(
        document.querySelectorAll<HTMLElement>(".ag-header-cell-text")
      )
      return {
        // Columns wider than the grid: it scrolls sideways.
        overflow: viewport ? viewport.scrollWidth - viewport.clientWidth : -1,
        truncated: labels
          .filter((label) => label.scrollWidth > label.clientWidth)
          .map((label) => label.textContent),
        notesShown:
          grid && notes
            ? notes.getBoundingClientRect().right <=
              grid.getBoundingClientRect().right
            : false,
        pageOverflow:
          document.documentElement.scrollWidth -
          document.documentElement.clientWidth,
      }
    })
  expect(await layout()).toEqual({
    overflow: 0,
    truncated: [],
    notesShown: true,
    pageOverflow: 0,
  })
  await page.setViewportSize({ width: 768, height: 700 })
  await expect.poll(async () => (await layout()).overflow).toBeGreaterThan(0)
  expect((await layout()).pageOverflow).toBe(0)
})

test("dragging a header off the grid keeps its column", async ({ page }) => {
  await openScans(page)
  await expect(scanIds(page).first()).toHaveText("1249")
  for (const field of ["aperture", "scan_number"]) {
    const label = header(page, field).locator(".ag-header-cell-label")
    const box = await label.boundingBox()
    if (!box) throw new Error(`no ${field} header`)
    const [x, y] = [box.x + box.width / 2, box.y + box.height / 2]
    // Up and out of the grid, onto the page's title row.
    await page.mouse.move(x, y)
    await page.mouse.down()
    for (let step = 1; step <= 10; step += 1) {
      await page.mouse.move(x + step * 5, y - step * 8)
    }
    await page.mouse.up()
    await expect(headerLabels(page)).toHaveText(ALL_HEADERS)
  }
  const stored = await page.evaluate(() =>
    window.localStorage.getItem("laueportal:grid:scans:v1")
  )
  const hidden =
    (
      JSON.parse(stored ?? "{}") as {
        columnVisibility?: { hiddenColIds: string[] }
      }
    ).columnVisibility?.hiddenColIds ?? []
  expect(hidden).toEqual([])
  await page.reload()
  await expect(headerLabels(page)).toHaveText(ALL_HEADERS)
})

test("the Scan ID links work from the keyboard", async ({ page }) => {
  await openScans(page)
  await expect(scanIds(page).first()).toHaveText("1249")
  const focused = page.locator(":focus")

  // Into the grid, then left onto the Scan ID cell.
  await row(page, 1249).locator('[col-id="sample_name"]').click()
  await page.keyboard.press("ArrowLeft")
  await expect(focused).toHaveAttribute("col-id", "scan_number")
  // Tab reaches the cell's link, then moves on to the next cell; Shift+Tab comes back to it.
  await page.keyboard.press("Tab")
  await expect(focused).toHaveAttribute("href", "/scans/1249")
  await page.keyboard.press("Tab")
  await expect(focused).toHaveAttribute("col-id", "sample_name")
  await page.keyboard.press("Shift+Tab")
  await expect(focused).toHaveAttribute("col-id", "scan_number")
  await page.keyboard.press("Shift+Tab")
  await expect(focused).toHaveAttribute("href", "/scans/1249")
  // Enter on the link follows it.
  await page.keyboard.press("Enter")
  await expect(page).toHaveURL(/\/scans\/1249$/)

  // Enter on the cell itself follows its link too.
  await page.goBack()
  await expect(scanIds(page).first()).toHaveText("1249")
  await row(page, 1248).locator('[col-id="sample_name"]').click()
  await page.keyboard.press("ArrowLeft")
  await expect(focused).toHaveAttribute("col-id", "scan_number")
  await page.keyboard.press("Enter")
  await expect(page).toHaveURL(/\/scans\/1248$/)
  await expect(page).toHaveTitle("Scan 1248 · 3DMN Portal")
})

test("shows a failed load, and retries", async ({ page }) => {
  await page.route("**/api/v1/health", (route) =>
    route.fulfill({ json: HEALTH_OK })
  )
  const api = await mockScans(page, makeScans(20))
  api.failWith(true)
  await page.goto("/scans")
  const alert = page.getByRole("alert")
  await expect(alert).toContainText("Database unavailable")
  await expect(alert).toContainText("laueportal.db does not exist")

  api.failWith(false)
  await alert.getByRole("button", { name: "Retry" }).click()
  await expect(scanIds(page).first()).toHaveText("1019")
  await expect(page.getByRole("alert")).toHaveCount(0)
})

test("Retry clears the error of a block the grid has dropped since", async ({
  page,
}) => {
  await page.route("**/api/v1/health", (route) =>
    route.fulfill({ json: HEALTH_OK })
  )
  // 1500 scans: 15 blocks of 100, more than the 10 the grid keeps. The block at row 300 fails once.
  const api = await mockScans(page, makeScans(1500))
  let failed = false
  await page.route("**/api/v1/scans?*", (route) => {
    const offset = new URL(route.request().url()).searchParams.get("offset")
    if (offset !== "300" || failed) return route.fallback()
    failed = true
    return route.fulfill(databaseUnavailable())
  })
  await page.goto("/scans")
  await expect(scanIds(page).first()).toHaveText("2499")
  await row(page, 2499).hover()
  const alert = page.getByRole("alert")
  for (let block = 1; block <= 3; block += 1) {
    await page.mouse.wheel(0, 3200)
  }
  await expect(alert).toContainText("Database unavailable")
  await expect(alert).toContainText("Showing the last data loaded")
  // The banner moved the grid down: back over its rows.
  const rows = await page.locator(".ag-grid-viewport").boundingBox()
  if (!rows) throw new Error("no grid rows")
  await page.mouse.move(rows.x + rows.width / 2, rows.y + rows.height - 40)

  // On to the end: AG Grid drops the least recently used blocks, the failed one among them.
  for (let block = 4; block < 15; block += 1) {
    await page.mouse.wheel(0, 3200)
    await expect(row(page, 2499 - block * 100)).toBeVisible()
  }
  const asked = api.requests.length
  const reloaded = () =>
    api.requests
      .slice(asked)
      .map((url) => Number(url.searchParams.get("offset")))
      .sort((a, b) => a - b)
  await alert.getByRole("button", { name: "Retry" }).click()
  await expect(page.getByRole("alert")).toHaveCount(0)
  // Retry reloads the 10 blocks the grid holds (rows 500 to 1499), not the dropped one at row 300,
  // which loads when it is scrolled to.
  await expect
    .poll(reloaded)
    .toEqual([500, 600, 700, 800, 900, 1000, 1100, 1200, 1300, 1400])
  await page.mouse.wheel(0, -60_000)
  await expect(scanIds(page).first()).toHaveText("2499")
  for (let block = 1; block <= 3; block += 1) {
    await page.mouse.wheel(0, 3200)
  }
  await expect(row(page, 2199)).toBeVisible()
  await expect(page.getByRole("alert")).toHaveCount(0)
})

test("the grid follows the dark theme", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" })
  await openScans(page)
  await expect(scanIds(page).first()).toHaveText("1249")
  await expect(page.locator("html")).toHaveAttribute(
    "data-ag-theme-mode",
    "dark"
  )
  const grid = await page.evaluate(() => {
    const wrapper = document.querySelector(".ag-root-wrapper")
    return wrapper ? getComputedStyle(wrapper).backgroundColor : ""
  })
  expect(grid).toBe(await themeBackground(page))

  // A selected row stands out in the dark theme too: its tint, and its bar beside the checkbox.
  await rowCheckbox(page, 1248).check()
  const selection = await selectionContrast(page, 1248)
  expect(selection.tint).toBeGreaterThan(1.3)
  expect(selection.bar).toBeGreaterThanOrEqual(3)
  expect(selection.link).toBeGreaterThanOrEqual(4.5)

  await page.emulateMedia({ colorScheme: "light" })
  await expect(page.locator("html")).toHaveAttribute(
    "data-ag-theme-mode",
    "light"
  )
  await expect
    .poll(() =>
      page.evaluate(() => {
        const grid = document.querySelector(".ag-root-wrapper")
        return grid ? getComputedStyle(grid).backgroundColor : ""
      })
    )
    .toBe(await themeBackground(page))
})
