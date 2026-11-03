import { expect, test, type Page } from "@playwright/test"

import type { IndexingRunDetail } from "@/api"

// The indexing viewer on the seeded stack (`make e2e`, nothing mocked but long values on a narrow window): I1
// opened with its header links, its Parameters tab (the create form's section cards, one column), a tab in the
// URL, the scope kept across a reload, a copied link opening the same view, and back/forward changing only the tab
// and the step.

const I1 = "/indexing/1"

/** The page's path and its search values (in any order). */
function at(page: Page) {
  const url = new URL(page.url())
  return {
    path: url.pathname,
    search: Object.fromEntries(url.searchParams),
  }
}

/** Waits until the page is at `path` with exactly these search values. */
async function expectAt(page: Page, search: Record<string, string>, path = I1) {
  await expect.poll(() => at(page)).toEqual({ path, search })
}

const scopeBar = (page: Page) => page.getByRole("group", { name: "Data scope" })
const pattern0 = (page: Page) =>
  scopeBar(page).getByRole("checkbox", { name: "Load only pattern 0" })
const skipPeaks = (page: Page) =>
  scopeBar(page).getByRole("spinbutton", { name: "Skip steps with peaks ≤" })
const tab = (page: Page, name: string) => page.getByRole("tab", { name })

/** The steps the summary keeps for the scope shown. */
async function stepsInScope(page: Page) {
  const text = await scopeBar(page).getByRole("status").textContent()
  return Number((text ?? "").replace(/\D/g, ""))
}

test("I1 opens with its job and scan, its parameters, and its tab in the URL", async ({
  page,
}) => {
  const answer = await page.request.get("/api/v1/indexing-runs/1")
  const run = (await answer.json()) as IndexingRunDetail
  expect(run.parameters).not.toBeNull()

  await page.goto(I1)
  await expect(page).toHaveTitle("Indexing I1 · 3DMN Portal")
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    "Indexing I1"
  )
  const related = page.getByRole("navigation", { name: "Related" })
  await expect(related.getByRole("link")).toHaveText([
    `Job ${String(run.job_id)}`,
    "Scan 990001",
  ])
  // The stored parameters (the database's, not request.json).
  await expect(
    page.getByRole("heading", { name: "Peak Search Parameters" })
  ).toBeVisible()
  const crystal = page.getByRole("term").filter({ hasText: "Structures File" })
  await expect(crystal.locator("xpath=following-sibling::dd[1]")).toHaveText(
    run.parameters?.crystal_file ?? ""
  )
  await expect(scopeBar(page).getByRole("status")).toHaveText(
    /\d+ steps? in scope/
  )

  await tab(page, "Peaks").click()
  await expectAt(page, { tab: "peaks" })
  await expect(
    page.getByRole("heading", { name: /^Indexed Peaks \(\d+ total\)$/ })
  ).toBeVisible()
  await tab(page, "Parameters").click()
  await expectAt(page, {})

  await related.getByRole("link", { name: "Scan 990001" }).click()
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    "Scan 990001"
  )
  await page.goBack()
  await related.getByRole("link", { name: /^Job / }).click()
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    `Job ${String(run.job_id)}`
  )
  // The job that ran I1.
  await expect(
    page.getByRole("link", { name: "I1", exact: true })
  ).toBeVisible()
})

/** The Parameters tab's cards. */
const parameterCards = (page: Page) =>
  page.getByRole("tabpanel").locator('[data-slot="card"]')

/** The texts of the cards and values that are wider than their box (clipped or overflowing). */
function overflowing(page: Page) {
  return page.getByRole("tabpanel").evaluate((panel) =>
    Array.from(panel.querySelectorAll('[data-slot="card"], dd'))
      .filter((element) => element.scrollWidth > element.clientWidth + 1)
      .map((element) => element.textContent.slice(0, 60))
  )
}

const LONG_FOLDER =
  "/net/s34data/export/s34data1/2026-3/Some_User_Experiment_With_A_Long_Name_And_No_Break/data"

test("the Parameters tab shows the create form's section cards in one column; long values wrap on a narrow window", async ({
  page,
}) => {
  await page.goto(I1)
  const titles = [
    "Identity",
    "File Paths",
    "Peak Search Parameters",
    "Indexing Parameters",
    "Notes",
    "Other recorded values",
  ]
  const cards = parameterCards(page)
  await expect(cards.getByRole("heading", { level: 2 })).toHaveText(titles)
  const boxes = await Promise.all(
    titles.map((_, index) => cards.nth(index).boundingBox())
  )
  // One column: each card under the one before, as wide and as far left.
  for (const [index, box] of boxes.entries()) {
    const first = boxes[0]
    const before = boxes[index - 1]
    expect(box && first && [box.x, box.width]).toEqual(
      first && [first.x, first.width]
    )
    if (box && before) expect(box.y).toBeGreaterThan(before.y + before.height)
  }

  await page.route(
    (url) => url.pathname === "/api/v1/indexing-runs/1",
    async (route) => {
      const response = await route.fetch()
      const run = (await response.json()) as IndexingRunDetail
      if (!run.parameters) throw new Error("I1 has no parameters")
      await route.fulfill({
        response,
        json: {
          ...run,
          input_path: `${LONG_FOLDER}/scan_990001_with_a_long_suffix`,
          notes: `${"A note with words. ".repeat(12)}\nSecond line: ${"x".repeat(90)}`,
          parameters: {
            ...run.parameters,
            filename_prefixes: [
              "Ni_sample_with_a_long_prefix_%d.h5",
              "Ni_sample_b_with_a_long_prefix_%d.h5",
            ],
            geometry_file: `${LONG_FOLDER}/calibration/geometry_with_a_long_name.xml`,
          },
        },
      })
    }
  )
  await page.setViewportSize({ width: 390, height: 844 })
  await page.reload()
  await expect(cards.getByRole("heading", { level: 2 })).toHaveText(titles)
  const folder = page.getByRole("term").filter({ hasText: "Folder Path" })
  await expect(folder.locator("xpath=following-sibling::dd[1]")).toHaveText(
    `${LONG_FOLDER}/scan_990001_with_a_long_suffix`
  )
  expect(await overflowing(page)).toEqual([])
})

test("the scope stays across a reload", async ({ page }) => {
  await page.goto(I1)
  await expect(scopeBar(page).getByRole("status")).toBeVisible()
  const before = await stepsInScope(page)

  await pattern0(page).click()
  await skipPeaks(page).fill("0")
  // Every step: at least as many as with pattern 0 and peaks > 3.
  await expect.poll(() => stepsInScope(page)).toBeGreaterThanOrEqual(before)
  await expect(pattern0(page)).not.toBeChecked()

  await page.reload()
  await expect(pattern0(page)).not.toBeChecked()
  await expect(skipPeaks(page)).toHaveValue("0")

  await scopeBar(page).getByRole("button", { name: "Reset" }).click()
  await expect(pattern0(page)).toBeChecked()
  await expect(skipPeaks(page)).toHaveValue("3")
})

test("a copied link opens the same view, then leaves the URL", async ({
  page,
  browser,
  context,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"])
  await page.goto(`${I1}?view=3d`)
  await tab(page, "Pole Figure").click()
  await pattern0(page).click()
  await skipPeaks(page).fill("5")
  await expectAt(page, { tab: "pole", view: "3d" })

  await page.getByRole("button", { name: "Copy link to this view" }).click()
  await expect(page.getByText("Link copied")).toBeVisible()
  const link = await page.evaluate(() => navigator.clipboard.readText())
  expect(link).toMatch(/\/indexing\/1\?link=[A-Za-z0-9_-]{20,}$/)

  // Another browser: none of this page's settings.
  const other = await browser.newContext()
  const opened = await other.newPage()
  await opened.goto(link)
  await expectAt(opened, { tab: "pole", view: "3d" })
  await expect(tab(opened, "Pole Figure")).toHaveAttribute(
    "aria-selected",
    "true"
  )
  await expect(pattern0(opened)).not.toBeChecked()
  await expect(skipPeaks(opened)).toHaveValue("5")
  await other.close()
})

test("back and forward change the tab and the step, and keep the rest", async ({
  page,
}) => {
  const kept = { step: "1", view: "3d", hkl: "110" }
  await page.goto(`${I1}?step=1&view=3d&hkl=110`)
  await tab(page, "Color Map").click()
  await expectAt(page, { ...kept, tab: "map" })
  await tab(page, "Detector View").click()
  await expectAt(page, { ...kept, tab: "detector" })

  await page.goBack()
  await expectAt(page, { ...kept, tab: "map" })
  await expect(tab(page, "Color Map")).toHaveAttribute("aria-selected", "true")
  await page.goBack()
  await expectAt(page, kept)
  await expect(tab(page, "Parameters")).toHaveAttribute("aria-selected", "true")
  await page.goForward()
  await expectAt(page, { ...kept, tab: "map" })
  await expect(tab(page, "Color Map")).toHaveAttribute("aria-selected", "true")
})
