import { expect, test, type Page } from "@playwright/test"

import type {
  IndexingArtifact,
  IndexingRunDetail,
  IndexingRunSummary,
  ReconstructionArtifact,
  ReconstructionRunDetail,
  ReconstructionRunSummary,
} from "@/api"
import {
  indexingRun,
  mockRunList,
  mockStatus,
  reconstructionRun,
  running,
  STATUS_OK,
} from "./runs-api"
import { advanceUntil, SHORT } from "./clock"
import { prefillAsked } from "./prefill"

// The reconstructions and indexations pages, with their lists answered from synthetic runs
// (e2e/runs-api.ts) whose counters a test moves: live refresh, the buttons' rules and the create
// forms' URLs (live refresh's 5 s poll on Playwright's fake clock, e2e/clock.ts). e2e/stack.spec.ts
// checks both lists against the seeded demo database.

test.use({ timezoneId: "America/Chicago" })

/** R14 queued, R13 running, R12 to R1 finished; R9 is unlinked (no scan). */
function reconstructions(): ReconstructionRunSummary[] {
  return [
    reconstructionRun(14, {
      ...running(0),
      status: "queued",
      start_time: null,
    }),
    reconstructionRun(13, running(212)),
    ...Array.from({ length: 12 }, (_, index) =>
      reconstructionRun(12 - index, index === 3 ? { scan_number: null } : {})
    ),
  ]
}

/** I75 running, I74 to I61 finished; I69 is unlinked, every third run indexes its scan directly. */
function indexations(): IndexingRunSummary[] {
  return [
    indexingRun(75, running(37)),
    ...Array.from({ length: 14 }, (_, index) =>
      indexingRun(
        74 - index,
        index === 5 ? { scan_number: null, reconstruction_id: null } : {}
      )
    ),
  ]
}

async function open(page: Page, path: string) {
  await mockStatus(page, () => STATUS_OK)
  await page.goto(path)
}

/** A run's row (its unpinned part: the pinned checkbox column renders each row twice). */
function row(page: Page, id: number) {
  return page
    .locator(`.ag-row[row-id="${String(id)}"]`)
    .filter({ has: page.locator('[col-id="id"]') })
}

function cell(page: Page, id: number, field: string) {
  return row(page, id).locator(`[col-id="${field}"]`)
}

function rowCheckbox(page: Page, id: number) {
  return page.locator(`.ag-row[row-id="${String(id)}"] input[type="checkbox"]`)
}

function headerLabels(page: Page) {
  return page.locator(
    '.ag-header-cell:not([col-id="ag-Grid-SelectionColumn"]) .ag-header-cell-text'
  )
}

/** Matches a URL ending in `pathAndSearch` exactly. */
function endsWith(pathAndSearch: string) {
  return new RegExp(`${pathAndSearch.replace(/[?.]/g, "\\$&")}$`)
}

/**
 * Whether an element shows on screen: the topmost element at its center is it or inside it (a cell
 * scrolled under a pinned column, or out of the grid, is not).
 */
function onScreen(page: Page, selector: string) {
  return page.evaluate((selector) => {
    const element = document.querySelector(selector)
    if (!element) return false
    const box = element.getBoundingClientRect()
    if (box.width === 0) return false
    const top = document.elementFromPoint(
      box.left + box.width / 2,
      box.top + box.height / 2
    )
    return top !== null && element.contains(top)
  }, selector)
}

/** The columns AG Grid pins now: the left side's last and the right side's first. */
function pinnedEdges(page: Page) {
  return page.evaluate(() => {
    const colId = (selector: string) =>
      document
        .querySelector(`.ag-header-row-column ${selector}`)
        ?.getAttribute("col-id") ?? null
    return {
      left: colId(
        ".ag-column-last-left-pinned, .ag-header-cell-last-left-pinned"
      ),
      right: colId(
        ".ag-column-first-right-pinned, .ag-header-cell-first-right-pinned"
      ),
    }
  })
}

/** The grid's columns overflow it sideways by this many px (0 when they fit), and the page not. */
function overflow(page: Page) {
  return page.evaluate(() => {
    const viewport = document.querySelector(
      ".ag-body-horizontal-scroll-viewport"
    )
    const labels = Array.from(
      document.querySelectorAll<HTMLElement>(".ag-header-cell-text")
    )
    return {
      grid: viewport ? viewport.scrollWidth - viewport.clientWidth : -1,
      page:
        document.documentElement.scrollWidth -
        document.documentElement.clientWidth,
      truncated: labels
        .filter((label) => label.scrollWidth > label.clientWidth)
        .map((label) => label.textContent),
    }
  })
}

/**
 * The floating filters that do not fit their column: a filter button reaching past its cell, or an
 * input with less than 12 px for its text (about two digits).
 */
function crampedFilters(page: Page) {
  return page.evaluate(() =>
    Array.from(
      document.querySelectorAll<HTMLElement>(".ag-floating-filter")
    ).flatMap((cell) => {
      const button = cell.querySelector<HTMLElement>(
        ".ag-floating-filter-button"
      )
      const input = cell.querySelector<HTMLElement>("input:not([disabled])")
      if (!button || button.offsetParent === null || !input) return []
      const style = getComputedStyle(input)
      const text =
        input.getBoundingClientRect().width -
        Number.parseFloat(style.paddingLeft) -
        Number.parseFloat(style.paddingRight) -
        Number.parseFloat(style.borderLeftWidth) -
        Number.parseFloat(style.borderRightWidth)
      const out =
        button.getBoundingClientRect().right >
        cell.getBoundingClientRect().right + 0.5
      return out || text < 12 ? [cell.getAttribute("col-id")] : []
    })
  )
}

/** How far a status badge reaches past its cell's content box (0 or less when it shows whole). */
function badgeOverflow(page: Page, id: number) {
  return page.evaluate((id) => {
    const cell = document.querySelector<HTMLElement>(
      `.ag-row[row-id="${String(id)}"] .ag-cell[col-id="status"]`
    )
    const badge = cell?.querySelector('[data-slot="badge"]')
    if (!cell || !badge) return Number.NaN
    const content =
      cell.getBoundingClientRect().right -
      Number.parseFloat(getComputedStyle(cell).paddingRight)
    return Math.ceil(badge.getBoundingClientRect().right - content)
  }, id)
}

/** Mark the document: a reload would lose the mark. */
async function markDocument(page: Page) {
  await page.evaluate(() => {
    document.body.dataset.loaded = "once"
  })
}

test.describe("reconstructions", () => {
  test("lists the runs newest first with their columns, each linked with its scan", async ({
    page,
  }) => {
    const api = await mockRunList(
      page,
      "/api/v1/reconstruction-runs",
      reconstructions()
    )
    await open(page, "/reconstructions")
    await expect(page).toHaveTitle("Reconstructions · 3DMN Portal")
    await expect(headerLabels(page)).toHaveText([
      "ID",
      "Method",
      "Source",
      "Points",
      "Author",
      "Notes",
      "Date",
      "Start Time",
      "Finish Time",
      "Status",
    ])
    await expect(cell(page, 14, "id")).toHaveText("R14")
    expect(api.requests[0]?.search).toBe("?offset=0&limit=100&sort=-id")
    await expect(cell(page, 14, "status")).toHaveText("Queued")
    await expect(cell(page, 13, "status")).toHaveText("Running 212/441")
    await expect(cell(page, 12, "status")).toHaveText("Finished")
    await expect(cell(page, 9, "scan_number")).toHaveText("Unlinked")
    // R12 was created at 2026-10-02 02:00 UTC: 21:00 the day before in Chicago (CDT).
    await expect(cell(page, 12, "created_at")).toHaveText("2026-10-01 21:00:00")

    await cell(page, 12, "scan_number")
      .getByRole("link", { name: "SN990005" })
      .click()
    await expect(page).toHaveURL(/\/scans\/990005$/)
    await page.goBack()
    // The run's page reads the run, then its artifact: answered here, as the list is.
    const detail: ReconstructionRunDetail = {
      id: 12,
      method: "wire",
      scan_number: 990005,
      job_id: 112,
      input_path: "/data/scan_990005",
      output_path: "/runs/rec_12",
      author: "demo",
      notes: null,
      created_at: "2026-10-02T02:00:00Z",
      parameters: null,
      relative_paths: {
        input_path: null,
        output_path: null,
        geometry_file: null,
      },
    }
    const artifact: ReconstructionArtifact = {
      reconstruction_id: 12,
      state: "empty",
      format: null,
      path: null,
      catalog_status: null,
      points_complete: null,
      points_total: null,
      run_status: "finished",
      live_open: false,
    }
    await page.route("**/api/v1/reconstruction-runs/12", (route) =>
      route.fulfill({ json: detail })
    )
    await page.route("**/api/v1/reconstruction-runs/12/artifact", (route) =>
      route.fulfill({ json: artifact })
    )
    await cell(page, 12, "id").getByRole("link", { name: "R12" }).click()
    await expect(page).toHaveURL(/\/reconstructions\/12$/)
    await expect(page).toHaveTitle("Reconstruction R12 · 3DMN Portal")
  })

  test("live refresh moves a running run in place, without a reload, keeping the selection", async ({
    page,
  }) => {
    const rows = reconstructions()
    const api = await mockRunList(page, "/api/v1/reconstruction-runs", rows)
    await page.clock.install()
    await open(page, "/reconstructions")
    await expect(cell(page, 13, "status")).toHaveText("Running 212/441")
    await rowCheckbox(page, 11).check()
    await markDocument(page)

    // R13 progresses and R14 starts.
    api.update(
      rows.map((run) =>
        run.id === 13
          ? { ...run, ...running(300) }
          : run.id === 14
            ? {
                ...run,
                ...running(5),
                start_time: "2026-10-02T05:00:00Z",
              }
            : run
      )
    )
    await advanceUntil(page, 5_000, () =>
      expect(cell(page, 13, "status")).toHaveText("Running 300/441", SHORT)
    )
    await expect(cell(page, 14, "status")).toHaveText("Running 5/441")
    await expect(cell(page, 14, "start_time")).toHaveText("2026-10-02 00:00:00")
    // Asked as changes since the last answer, and updated in place.
    const changes = api.requests.filter((url) =>
      url.searchParams.has("changed_since")
    )
    expect(changes.length).toBeGreaterThan(0)
    expect(changes[0]?.searchParams.get("limit")).toBe("1000")
    await expect(page.locator("body")).toHaveAttribute("data-loaded", "once")
    await expect(rowCheckbox(page, 11)).toBeChecked()
    await expect(page.getByText("1 selected")).toBeVisible()

    // R13 finishes.
    api.update(
      rows.map((run) =>
        run.id === 13
          ? {
              ...run,
              status: "finished" as const,
              n_succeeded: 441,
              n_processed: 441,
              finish_time: "2026-10-02T05:30:00Z",
            }
          : run
      )
    )
    await advanceUntil(page, 5_000, () =>
      expect(cell(page, 13, "status")).toHaveText("Finished", SHORT)
    )
    await expect(cell(page, 13, "finish_time")).toHaveText(
      "2026-10-02 00:30:00"
    )
    await expect(page.locator("body")).toHaveAttribute("data-loaded", "once")
  })

  test("at 900 px, ID and Status stay in view while the middle columns scroll", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 900, height: 700 })
    await mockRunList(page, "/api/v1/reconstruction-runs", reconstructions())
    await open(page, "/reconstructions")
    await expect(cell(page, 13, "status")).toHaveText("Running 212/441")
    // The columns need about 1200 px: the middle ones scroll, ID and Status are pinned.
    await expect
      .poll(() => pinnedEdges(page))
      .toEqual({
        left: "id",
        right: "status",
      })
    expect((await overflow(page)).page).toBe(0)
    const status = '.ag-header-cell[col-id="status"]:not(.ag-floating-filter)'
    const running = '.ag-row[row-id="13"] [col-id="status"] [data-slot="badge"]'
    const id = '.ag-row[row-id="13"] [col-id="id"] a'
    expect(await onScreen(page, status)).toBe(true)
    expect(await onScreen(page, running)).toBe(true)
    expect(await onScreen(page, id)).toBe(true)
    // Finish Time is beyond the grid's right edge until the middle scrolls.
    const finish = '.ag-row[row-id="13"] [col-id="finish_time"]'
    expect(await onScreen(page, finish)).toBe(false)

    // Scrolled to the end of the middle columns: ID still shows, and Status.
    await cell(page, 13, "method").hover()
    await page.mouse.wheel(2000, 0)
    await expect
      .poll(() =>
        page.evaluate(() => {
          const viewport = document.querySelector(
            ".ag-body-horizontal-scroll-viewport"
          )
          return viewport
            ? viewport.scrollLeft + viewport.clientWidth >=
                viewport.scrollWidth - 1
            : false
        })
      )
      .toBe(true)
    expect(await onScreen(page, id)).toBe(true)
    expect(await onScreen(page, status)).toBe(true)
    expect(await onScreen(page, running)).toBe(true)
    expect(
      await onScreen(page, '.ag-row[row-id="12"] [col-id="finish_time"]')
    ).toBe(true)
    // The method has scrolled under the pinned ID.
    expect(await onScreen(page, '.ag-row[row-id="13"] [col-id="method"]')).toBe(
      false
    )
  })

  test("where every column fits, nothing more than the checkboxes is pinned", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 700 })
    await mockRunList(page, "/api/v1/reconstruction-runs", reconstructions())
    await open(page, "/reconstructions")
    await expect(cell(page, 13, "status")).toHaveText("Running 212/441")
    expect(await overflow(page)).toEqual({ grid: 0, page: 0, truncated: [] })
    expect(await crampedFilters(page)).toEqual([])
    expect(await pinnedEdges(page)).toEqual({
      left: "ag-Grid-SelectionColumn",
      right: null,
    })

    // Narrower, ID and Status pin; wide again, they are unpinned, in their places.
    await page.setViewportSize({ width: 1024, height: 700 })
    await expect
      .poll(() => pinnedEdges(page))
      .toEqual({
        left: "id",
        right: "status",
      })
    await page.setViewportSize({ width: 1280, height: 700 })
    await expect
      .poll(() => pinnedEdges(page))
      .toEqual({
        left: "ag-Grid-SelectionColumn",
        right: null,
      })
    await expect(headerLabels(page)).toHaveText([
      "ID",
      "Method",
      "Source",
      "Points",
      "Author",
      "Notes",
      "Date",
      "Start Time",
      "Finish Time",
      "Status",
    ])
    // Pinning is not kept with the columns' state.
    const stored = await page.evaluate(() =>
      window.localStorage.getItem("laueportal:grid:reconstructions:v1")
    )
    expect(stored ?? "").not.toContain("Pinning")
  })

  test("a large run's progress shows whole in its status", async ({ page }) => {
    await page.setViewportSize({ width: 900, height: 700 })
    await mockRunList(page, "/api/v1/reconstruction-runs", [
      reconstructionRun(15, running(4567, 10201)),
      reconstructionRun(14, running(12345, 40401)),
      ...reconstructions().slice(2),
    ])
    await open(page, "/reconstructions")
    await expect(cell(page, 15, "status")).toHaveText("Running 4567/10201")
    await expect(cell(page, 14, "status")).toHaveText("Running 12345/40401")
    expect(await badgeOverflow(page, 15)).toBeLessThanOrEqual(0)
    expect(await badgeOverflow(page, 14)).toBeLessThanOrEqual(0)
  })

  test("New Wire Recon and New Index take the one selected run, with its scan", async ({
    page,
  }) => {
    await mockRunList(page, "/api/v1/reconstruction-runs", reconstructions())
    await open(page, "/reconstructions")
    const newWireRecon = page.getByRole("button", { name: "New Wire Recon" })
    const newIndex = page.getByRole("button", { name: "New Index" })
    await expect(cell(page, 14, "id")).toHaveText("R14")
    await expect(newWireRecon).toBeDisabled()
    await expect(newIndex).toBeDisabled()

    // One checked row: checking R12 unchecks R10.
    await rowCheckbox(page, 10).check()
    await rowCheckbox(page, 12).check()
    await expect(rowCheckbox(page, 10)).not.toBeChecked()
    await expect(page.getByText("1 selected")).toBeVisible()
    await expect(newWireRecon).toBeEnabled()
    // The wire form takes its prefill once and removes it from the URL.
    expect(
      await prefillAsked(page, "wire", () => newWireRecon.click())
    ).toEqual({ reconstruction: "12", scan: "990005" })
    await expect(page).toHaveURL(endsWith("/reconstructions/new"))
    await expect(page).toHaveTitle("New Wire Reconstruction · 3DMN Portal")

    await page.goBack()
    await expect(cell(page, 14, "id")).toHaveText("R14")
    await rowCheckbox(page, 12).check()
    expect(await prefillAsked(page, "lauego", () => newIndex.click())).toEqual({
      reconstruction: "12",
      scan: "990005",
    })
    await expect(page).toHaveURL(endsWith("/indexing/new"))
    await expect(page).toHaveTitle("New LaueGo Indexation · 3DMN Portal")

    // An unlinked run has no scan to pass on.
    await page.goBack()
    await expect(cell(page, 14, "id")).toHaveText("R14")
    await rowCheckbox(page, 9).check()
    expect(await prefillAsked(page, "lauego", () => newIndex.click())).toEqual({
      reconstruction: "9",
    })
    await expect(page).toHaveURL(endsWith("/indexing/new"))
  })
})

test.describe("indexations", () => {
  test("lists the runs newest first with their columns, their sources linked", async ({
    page,
  }) => {
    const api = await mockRunList(page, "/api/v1/indexing-runs", indexations())
    await open(page, "/indexing")
    await expect(page).toHaveTitle("Indexations · 3DMN Portal")
    await expect(headerLabels(page)).toHaveText([
      "ID",
      "Source",
      "Points",
      "Author",
      "Notes",
      "Box",
      "Date",
      "Status",
    ])
    await expect(cell(page, 75, "id")).toHaveText("I75")
    expect(api.requests[0]?.search).toBe("?offset=0&limit=100&sort=-id")
    await expect(cell(page, 75, "status")).toHaveText("Running 37/441")
    // A source is the scan and the reconstruction indexed, or the scan alone, or neither.
    await expect(cell(page, 74, "scan_number")).toHaveText("SN990004R74")
    await expect(cell(page, 72, "scan_number")).toHaveText("SN990002")
    await expect(cell(page, 69, "scan_number")).toHaveText("Unlinked")
    const links = await cell(page, 74, "scan_number")
      .getByRole("link")
      .evaluateAll((elements) =>
        elements.map((element) => element.getAttribute("href"))
      )
    expect(links).toEqual(["/scans/990004", "/reconstructions/74"])
    // The run's page reads the run, then its artifact: answered here, as the list is.
    const detail: IndexingRunDetail = {
      id: 74,
      method: "lauego",
      scan_number: 990004,
      reconstruction_id: 74,
      job_id: 174,
      input_path: "/data/scan_990004",
      output_path: "/runs/index_74",
      author: "demo",
      notes: null,
      created_at: "2026-10-01T08:00:00Z",
      parameters: null,
    }
    const artifact: IndexingArtifact = {
      indexing_id: 74,
      state: "empty",
      results_path: null,
      xml_path: null,
      xml_source: null,
      xml_candidates: [],
      conversion_job_id: null,
      conversion: null,
      problems: ["No results in /runs/index_74"],
      results_version: null,
      live_open: false,
    }
    await page.route("**/api/v1/indexing-runs/74", (route) =>
      route.fulfill({ json: detail })
    )
    await page.route("**/api/v1/indexing-runs/74/artifact", (route) =>
      route.fulfill({ json: artifact })
    )
    await cell(page, 74, "id").getByRole("link", { name: "I74" }).click()
    await expect(page).toHaveURL(/\/indexing\/74$/)
    await expect(page).toHaveTitle("Indexing I74 · 3DMN Portal")
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(
      "Indexing I74"
    )
  })

  test("the columns fit from 900 px up, the progress of a large run whole", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 900, height: 700 })
    await mockRunList(page, "/api/v1/indexing-runs", [
      indexingRun(76, { ...running(12345, 40401), reconstruction_id: 1234 }),
      ...indexations(),
    ])
    await open(page, "/indexing")
    await expect(cell(page, 76, "status")).toHaveText("Running 12345/40401")
    await expect(cell(page, 76, "scan_number")).toHaveText("SN990006R1234")
    expect(await overflow(page)).toEqual({ grid: 0, page: 0, truncated: [] })
    expect(await crampedFilters(page)).toEqual([])
    expect(await pinnedEdges(page)).toEqual({
      left: "ag-Grid-SelectionColumn",
      right: null,
    })
    expect(await badgeOverflow(page, 76)).toBeLessThanOrEqual(0)
    expect(
      await onScreen(
        page,
        '.ag-row[row-id="76"] [col-id="scan_number"] a[href="/reconstructions/1234"]'
      )
    ).toBe(true)

    // Narrower than 900 px, ID and Status stay in view while the middle scrolls.
    await page.setViewportSize({ width: 768, height: 700 })
    await expect
      .poll(() => pinnedEdges(page))
      .toEqual({
        left: "id",
        right: "status",
      })
    expect(
      await onScreen(
        page,
        '.ag-row[row-id="76"] [col-id="status"] [data-slot="badge"]'
      )
    ).toBe(true)
  })

  test("live refresh moves a running run in place, without a reload", async ({
    page,
  }) => {
    const rows = indexations()
    const api = await mockRunList(page, "/api/v1/indexing-runs", rows)
    await page.clock.install()
    await open(page, "/indexing")
    await expect(cell(page, 75, "status")).toHaveText("Running 37/441")
    await rowCheckbox(page, 74).check()
    await markDocument(page)

    api.update(
      rows.map((run) => (run.id === 75 ? { ...run, ...running(120) } : run))
    )
    await advanceUntil(page, 5_000, () =>
      expect(cell(page, 75, "status")).toHaveText("Running 120/441", SHORT)
    )
    expect(
      api.requests.some((url) => url.searchParams.has("changed_since"))
    ).toBe(true)
    await expect(page.locator("body")).toHaveAttribute("data-loaded", "once")
    await expect(rowCheckbox(page, 74)).toBeChecked()
  })

  test("New Recon and New Index take the one selected run", async ({
    page,
  }) => {
    await mockRunList(page, "/api/v1/indexing-runs", indexations())
    await open(page, "/indexing")
    const newRecon = page.getByRole("button", { name: "New Recon" })
    const newIndex = page.getByRole("button", { name: "New Index" })
    await expect(cell(page, 75, "id")).toHaveText("I75")
    await expect(newRecon).toBeDisabled()
    await expect(newIndex).toBeDisabled()

    // One checked row: checking I74 unchecks I72. I74 indexes R74 of scan 990004.
    await rowCheckbox(page, 72).check()
    await rowCheckbox(page, 74).check()
    await expect(rowCheckbox(page, 72)).not.toBeChecked()
    await expect(page.getByText("1 selected")).toBeVisible()
    expect(await prefillAsked(page, "wire", () => newRecon.click())).toEqual({
      scan: "990004",
      reconstruction: "74",
    })
    await expect(page).toHaveURL(endsWith("/reconstructions/new"))
    await expect(page).toHaveTitle("New Wire Reconstruction · 3DMN Portal")

    await page.goBack()
    await expect(cell(page, 75, "id")).toHaveText("I75")
    // I72 indexes scan 990002 directly; New Index clones it.
    await rowCheckbox(page, 72).check()
    expect(await prefillAsked(page, "lauego", () => newIndex.click())).toEqual({
      indexing: "72",
    })
    await expect(page).toHaveURL(endsWith("/indexing/new"))
    await expect(page).toHaveTitle("New LaueGo Indexation · 3DMN Portal")

    // An unlinked run has no source to reconstruct: New Recon waits; New Index clones it.
    await page.goBack()
    await expect(cell(page, 75, "id")).toHaveText("I75")
    await rowCheckbox(page, 69).check()
    await expect(newRecon).toBeDisabled()
    await expect(newIndex).toBeEnabled()
    await page.getByRole("button", { name: "Clear selection" }).click()
    await expect(rowCheckbox(page, 69)).not.toBeChecked()
    await expect(newIndex).toBeDisabled()
  })
})
