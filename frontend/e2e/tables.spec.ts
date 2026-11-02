import { expect, test, type Page } from "@playwright/test"

// The Peaks and Patterns tabs on the seeded stack (`make e2e`, nothing mocked): I2's peaks (the demo's
// map) filtered by goodness > X and sorted by intensity, scrolled past the first blocks, with the
// heading's totals and the rows the API's; I1's patterns with their total and the Position group.

const GOODNESS = 200

// Wide enough for every default column: AG Grid renders only the columns in view.
test.use({ viewport: { width: 1600, height: 900 } })

interface ListPage<TRow> {
  rows: TRow[]
  total: number
}

interface PeakRow {
  id: number
  intensity: number | null
  goodness: number | null
}

interface PatternRow {
  id: number
  x_um: number | null
}

/** The heading's formatting of a count (formatCount, src/lib/format.ts): "550,773". */
const grouped = (value: number) => value.toLocaleString("en-US")

/** The grid's formatting of a measured value (formatSignificant, src/lib/format.ts). */
const significant = (value: number) => String(Number(value.toPrecision(5)))

/** A page of a run's grid list, straight from the API (the scope's defaults, as the page sends). */
async function list<TRow>(
  page: Page,
  path: string,
  query: Record<string, string>
): Promise<ListPage<TRow>> {
  const answer = await page.request.get(
    `/api/v1/indexing-runs/${path}?${new URLSearchParams(query).toString()}`
  )
  expect(answer.ok()).toBe(true)
  return (await answer.json()) as ListPage<TRow>
}

/** Scroll the grid to its last columns (AG Grid draws only the columns in view). */
async function scrollRight(page: Page) {
  await page
    .locator(".ag-body-horizontal-scroll-viewport")
    .evaluate((viewport) => {
      viewport.scrollLeft = viewport.scrollWidth
    })
}

const cell = (page: Page, id: number, field: string) =>
  page.locator(`.ag-row[row-id="${String(id)}"] [col-id="${field}"]`)

test("I2's peaks: filtered by goodness > X and sorted by intensity, scrolled, the totals right", async ({
  page,
}) => {
  const filter = JSON.stringify({
    goodness: { filterType: "number", type: "greaterThan", filter: GOODNESS },
  })
  const all = await list<PeakRow>(page, "2/peaks", { limit: "1" })
  const matching = await list<PeakRow>(page, "2/peaks", {
    limit: "1",
    filter,
  })
  expect(matching.total).toBeGreaterThan(500)
  expect(matching.total).toBeLessThan(all.total)

  await page.goto("/indexing/2?tab=peaks")
  const heading = page.getByRole("heading", { name: /^Indexed Peaks/ })
  await expect(heading).toHaveText(
    `Indexed Peaks (${grouped(all.total)} total)`
  )

  // Goodness > X, from the column's filter.
  await page
    .locator(
      '.ag-floating-filter[col-id="goodness"] .ag-floating-filter-button button'
    )
    .click()
  await page.locator(".ag-filter .ag-picker-field").first().click()
  await page.getByRole("option", { name: "Greater than", exact: true }).click()
  await page
    .locator('.ag-filter input[type="number"]')
    .first()
    .fill(String(GOODNESS))
  await page.keyboard.press("Escape")
  await expect(heading).toHaveText(
    `Indexed Peaks (${grouped(all.total)} total, ${grouped(matching.total)} match the filters)`
  )

  // Sorted by intensity, ascending.
  await page
    .locator(
      '.ag-header-cell[col-id="intensity"]:not(.ag-floating-filter) .ag-header-cell-label'
    )
    .click()
  await expect(
    page.locator('.ag-header-cell[col-id="intensity"]:not(.ag-floating-filter)')
  ).toHaveAttribute("aria-sort", "ascending")
  const sorted = { filter, sort: "intensity" }
  const first = await list<PeakRow>(page, "2/peaks", { ...sorted, limit: "1" })
  const top = first.rows[0]
  if (!top?.intensity) throw new Error("no first peak")
  await expect(cell(page, top.id, "intensity")).toHaveText(
    significant(top.intensity)
  )

  // Scrolled past the first blocks (100 rows each): the row there is the API's at that offset.
  const offset = 450
  const deep = await list<PeakRow>(page, "2/peaks", {
    ...sorted,
    offset: String(offset),
    limit: "1",
  })
  const row = deep.rows[0]
  if (!row?.intensity || !row.goodness) throw new Error("no peak at the offset")
  expect(row.goodness).toBeGreaterThan(GOODNESS)
  await page.locator(".ag-grid-viewport").evaluate((viewport, index) => {
    const height =
      viewport.querySelector<HTMLElement>(".ag-row")?.offsetHeight ?? 0
    viewport.scrollTop = index * height
  }, offset)
  await expect(
    page.locator(`.ag-row[row-index="${String(offset)}"]`).first()
  ).toHaveAttribute("row-id", String(row.id))
  await expect(cell(page, row.id, "intensity")).toHaveText(
    significant(row.intensity)
  )
  await expect(cell(page, row.id, "goodness")).toHaveText(
    significant(row.goodness)
  )
  // The totals stay those of the filter.
  await expect(heading).toHaveText(
    `Indexed Peaks (${grouped(all.total)} total, ${grouped(matching.total)} match the filters)`
  )
})

test("I1's patterns: the total, and the Position group shown from the checklist", async ({
  page,
}) => {
  const all = await list<PatternRow>(page, "1/patterns", { limit: "100" })
  const first = all.rows[0]
  if (!first || first.x_um === null)
    throw new Error("no pattern with an X motor")

  await page.goto("/indexing/1?tab=patterns")
  await expect(
    page.getByRole("heading", { name: /^Indexed Patterns/ })
  ).toHaveText(`Indexed Patterns (${grouped(all.total)} total)`)
  await expect(page.locator('.ag-row [col-id="pattern_index"]')).toHaveCount(
    all.rows.length
  )
  const header = page.locator(
    '.ag-header-cell[col-id="x_um"]:not(.ag-floating-filter)'
  )
  await expect(header).toHaveCount(0)

  const box = page.getByRole("checkbox", { name: "X motor", exact: true })
  await expect(box).not.toBeChecked()
  await box.click()
  await expect(box).toBeChecked()
  await scrollRight(page)
  await expect(header).toHaveText("X motor")
  await expect(cell(page, first.id, "x_um")).toHaveText(significant(first.x_um))
  // Kept with the grid's column state.
  await page.reload()
  await expect(box).toBeChecked()
  await scrollRight(page)
  await expect(header).toHaveText("X motor")
})
