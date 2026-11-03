import { expect, test, type BrowserContext, type Page } from "@playwright/test"

import type {
  ReconstructionArtifact,
  ReconstructionPointList,
  ReconstructionRunDetail,
  ReconstructionTrace,
} from "@/api"

// The wire reconstruction viewer on the seeded stack (`make e2e`): R1, the synthetic one-point
// reconstruction, opened with its header links and parameters (the wire form's section cards, one column; long
// values on a narrow window); its point in the bar and the Points tab; the flow through its Depth View and ROI
// Inspector. R2, the fine one, times a depth change.
// R3, the seeded three-point one (each point brightest at its own depth), switches points. The URL's
// point tests answer R1's points and artifact with a second complete point, a copy of R1's, whose ID
// needs encoding in the URL.

const R1 = "/reconstructions/1"
const SECOND = "wire 2&x"

/** The page's path and its search values (in any order). */
function at(page: Page) {
  const url = new URL(page.url())
  return {
    path: url.pathname,
    search: Object.fromEntries(url.searchParams),
  }
}

/** Waits until the page is at `path` with exactly these search values. */
async function expectAt(page: Page, search: Record<string, string>, path = R1) {
  await expect.poll(() => at(page)).toEqual({ path, search })
}

const tab = (page: Page, name: string) => page.getByRole("tab", { name })
const bar = (page: Page) => page.getByRole("region", { name: "Reconstruction" })
const statusCell = (page: Page, id: string) =>
  page.locator(`[row-id="${id}"] [col-id="status"]`)
// By the point's cell: AG Grid escapes the row-id attribute (`&` is `&amp;`).
const checkbox = (page: Page, id: string) =>
  page
    .getByRole("row")
    .filter({ has: page.getByRole("gridcell", { name: id, exact: true }) })
    .getByRole("checkbox")

/** R1's points and artifact with a second complete point (`SECOND`), on a page or a whole context. */
async function addSecondPoint(target: Page | BrowserContext) {
  await target.route(
    `**/api/v1/reconstruction-runs/1/points`,
    async (route) => {
      const response = await route.fetch()
      const list = (await response.json()) as ReconstructionPointList
      const [first] = list.rows
      if (!first) throw new Error("R1 has no point")
      await route.fulfill({
        json: {
          ...list,
          rows: [...list.rows, { ...first, point_id: SECOND, index: 1 }],
        },
      })
    }
  )
  await target.route(
    `**/api/v1/reconstruction-runs/1/artifact`,
    async (route) => {
      const response = await route.fetch()
      const artifact = (await response.json()) as ReconstructionArtifact
      await route.fulfill({
        json: { ...artifact, points_complete: 2, points_total: 2 },
      })
    }
  )
}

test("R1 opens with its job and scan, its parameters, and its point", async ({
  page,
}) => {
  const answer = await page.request.get("/api/v1/reconstruction-runs/1")
  const run = (await answer.json()) as ReconstructionRunDetail
  expect(run.parameters).not.toBeNull()
  const points = await page.request.get("/api/v1/reconstruction-runs/1/points")
  const [point] = ((await points.json()) as ReconstructionPointList).rows
  expect(point?.status).toBe("complete")
  const pointId = point?.point_id ?? ""

  await page.goto(R1)
  await expect(page).toHaveTitle("Reconstruction R1 · 3DMN Portal")
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    "Reconstruction R1"
  )
  const related = page.getByRole("navigation", { name: "Related" })
  await expect(related.getByRole("link")).toHaveText([
    `Job ${String(run.job_id)}`,
    "Scan 990001",
  ])
  // The stored parameters (the database's).
  await expect(
    page.getByRole("heading", { name: "Wire Reconstruction Parameters" })
  ).toBeVisible()
  const folder = page.getByRole("term").filter({ hasText: "Folder Path" })
  await expect(folder.locator("xpath=following-sibling::dd[1]")).toContainText(
    run.input_path
  )
  // The first complete point, without a choice.
  await expect(bar(page)).toContainText(`Point ${pointId}`)
  await expect(bar(page).getByRole("status")).toHaveText(
    "1 of 1 points complete"
  )

  await tab(page, "Points").click()
  await expectAt(page, { tab: "points" })
  await expect(statusCell(page, pointId)).toHaveText("Complete")
  await expect(checkbox(page, pointId)).toBeChecked()
  await tab(page, "Parameters").click()
  await expectAt(page, {})
})

/** The texts of the Parameters tab's cards and values that are wider than their box (clipped or overflowing). */
function overflowing(page: Page) {
  return page.getByRole("tabpanel").evaluate((panel) =>
    Array.from(panel.querySelectorAll('[data-slot="card"], dd'))
      .filter((element) => element.scrollWidth > element.clientWidth + 1)
      .map((element) => element.textContent.slice(0, 60))
  )
}

test("the Parameters tab shows the wire form's section cards in one column; long paths wrap on a narrow window", async ({
  page,
}) => {
  await page.goto(R1)
  const titles = [
    "Identity",
    "File Paths",
    "Wire Reconstruction Parameters",
    "Notes",
    "Other recorded values",
  ]
  const cards = page.getByRole("tabpanel").locator('[data-slot="card"]')
  await expect(cards.getByRole("heading", { level: 2 })).toHaveText(titles)
  const boxes = await Promise.all(
    titles.map((_, index) => cards.nth(index).boundingBox())
  )
  for (const box of boxes) {
    expect(box && [box.x, box.width]).toEqual(
      boxes[0] && [boxes[0].x, boxes[0].width]
    )
  }

  // A folder inside a file root, its root-relative form as long.
  const relative =
    "export/s34data1/2026-3/Some_User_Experiment_With_A_Long_Name_And_No_Break/data/wire_scan_with_a_long_name"
  await page.route(
    (url) => url.pathname === "/api/v1/reconstruction-runs/1",
    async (route) => {
      const response = await route.fetch()
      const run = (await response.json()) as ReconstructionRunDetail
      await route.fulfill({
        response,
        json: {
          ...run,
          input_path: `/net/s34data/${relative}`,
          relative_paths: {
            ...run.relative_paths,
            input_path: { root: "s34data", relative },
          },
        },
      })
    }
  )
  await page.setViewportSize({ width: 390, height: 844 })
  await page.reload()
  await expect(cards.getByRole("heading", { level: 2 })).toHaveText(titles)
  await expect(
    page.getByRole("tabpanel").getByTitle("Relative to its file root").first()
  ).toHaveText(`s34data/${relative}`)
  expect(await overflowing(page)).toEqual([])
})

test("a selected point shows in the bar, and the URL keeps it across a reload", async ({
  page,
}) => {
  await addSecondPoint(page)
  await page.goto(`${R1}?tab=points`)
  await expect(bar(page).getByRole("status")).toHaveText(
    "2 of 2 points complete"
  )
  await expect(checkbox(page, SECOND)).not.toBeChecked()

  await checkbox(page, SECOND).check()
  await expect(bar(page)).toContainText(`Point ${SECOND}`)
  await expectAt(page, { tab: "points", point: SECOND })
  expect(page.url()).toContain("point=wire+2%26x")

  await page.reload()
  await expect(bar(page)).toContainText(`Point ${SECOND}`)
  await expect(checkbox(page, SECOND)).toBeChecked()
})

test("a copied link opens the same view, then leaves the URL", async ({
  page,
  browser,
  context,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"])
  await addSecondPoint(context)
  await page.goto(`${R1}?tab=points`)
  await checkbox(page, SECOND).check()
  await expectAt(page, { tab: "points", point: SECOND })

  await page.getByRole("button", { name: "Copy link to this view" }).click()
  await expect(page.getByText("Link copied")).toBeVisible()
  const link = await page.evaluate(() => navigator.clipboard.readText())
  expect(link).toMatch(/\/reconstructions\/1\?link=[A-Za-z0-9_-]{20,}$/)

  // Another browser.
  const other = await browser.newContext()
  await addSecondPoint(other)
  const opened = await other.newPage()
  await opened.goto(link)
  await expectAt(opened, { tab: "points", point: SECOND })
  await expect(tab(opened, "Points")).toHaveAttribute("aria-selected", "true")
  await expect(bar(opened)).toContainText(`Point ${SECOND}`)
  await other.close()
})

// The flow: R1 → Points → Depth View: the point opens at its brightest depth; stepping through the
// depths writes the depth to the URL (replacing the entry); the colormap recolours without a request;
// a zoom stays when the depth changes. → ROI Inspector: three ROIs added on the reference image, the
// traces switched to log and normalized, one ROI deleted (the others keep their colours), and a reload
// restores them.

/** formatSignificant's text (src/lib/format.ts), for the readout. */
const um = (value: number) => String(Number(value.toPrecision(5)))
const depthPlot = (page: Page) =>
  page.getByRole("region", { name: "Depth frame" })
const depthCanvas = (page: Page) => depthPlot(page).getByRole("img")
const depthReadout = (page: Page) =>
  page.getByRole("complementary", { name: "Depth view settings" })
const isFrameRequest = (url: string) =>
  /\/points\/[^/]+\/frames\/\d+$/.test(new URL(url).pathname)

/** The pixel the hover readout names at a point of the canvas (relative to its box). */
async function pixelAt(page: Page, x: number, y: number): Promise<string> {
  const box = await depthCanvas(page).boundingBox()
  if (!box) throw new Error("The depth frame is not shown")
  await page.mouse.move(box.x + x, box.y + y)
  // The heatmap's hover readout (the colorbar's value labels are monospace too).
  const text = await depthPlot(page)
    .locator("span.font-mono")
    .filter({ hasText: /^x \d+ {2}y \d+/ })
    .innerText()
  const match = /^x (\d+) {2}y (\d+)/.exec(text)
  if (!match) throw new Error(`No pixel in the readout: ${text}`)
  return `${match[1] ?? ""},${match[2] ?? ""}`
}

/** Waits until the plot shows a depth (its index in the panel, no status over the frame). */
async function showsDepth(page: Page, index: number, count: number) {
  await expect(depthReadout(page).getByRole("definition").first()).toHaveText(
    `${String(index)} (0 … ${String(count - 1)})`
  )
  await expect(depthPlot(page).getByRole("status")).toBeHidden()
}

const roiRows = (page: Page) =>
  page.getByRole("region", { name: "ROI list" }).locator("tbody tr")

/** The ROI list: each row's Y, X, Size and colour (not its number, its place in the list). */
async function roiList(page: Page): Promise<string[]> {
  const rows = await roiRows(page).all()
  return Promise.all(
    rows.map(async (row) => {
      const cells = await row.getByRole("cell").allInnerTexts()
      const color = await row.getByRole("img").getAttribute("aria-label")
      return [...cells.slice(2, 5), color].join(" ")
    })
  )
}

test("R1's flow: Points, the Depth View's depths, colormap and kept zoom, then the ROI Inspector's ROIs, kept across a reload", async ({
  page,
}) => {
  const answer = await page.request.get(
    "/api/v1/reconstruction-runs/1/points/wire_1/trace"
  )
  const trace = (await answer.json()) as ReconstructionTrace
  const count = trace.depth_um.length
  const brightest = trace.brightest_index
  expect(brightest).toBeLessThan(count - 1)
  const range = `(${um(trace.depth_um[0] ?? NaN)} … ${um(trace.depth_um[count - 1] ?? NaN)})`

  await page.goto(`${R1}?tab=points`)
  await tab(page, "Depth View").click()
  await expectAt(page, { tab: "depth" })
  // The brightest depth (the default: not in the URL).
  await showsDepth(page, brightest, count)
  await expect(depthReadout(page)).toContainText(
    `${um(trace.depth_um[brightest] ?? NaN)} µm ${range}`
  )
  await expect(page.getByRole("region", { name: "Colour scale" })).toBeVisible()

  // Step through the depths: the URL follows, with replace.
  const entries = await page.evaluate(() => history.length)
  await page.getByRole("button", { name: "Next depth" }).click()
  await showsDepth(page, brightest + 1, count)
  await expect(depthReadout(page)).toContainText(
    `${um(trace.depth_um[brightest + 1] ?? NaN)} µm ${range}`
  )
  await expectAt(page, { tab: "depth", depth: String(brightest + 1) })
  const slider = page.getByRole("slider", { name: "Depth index" })
  await slider.focus()
  await page.keyboard.press("ArrowRight")
  await showsDepth(page, brightest + 2, count)
  await expectAt(page, { tab: "depth", depth: String(brightest + 2) })
  await page.getByRole("button", { name: "Previous depth" }).click()
  await page.getByRole("button", { name: "Previous depth" }).click()
  await showsDepth(page, brightest, count)
  await expectAt(page, { tab: "depth" })
  expect(await page.evaluate(() => history.length)).toBe(entries)

  // The colormap recolours in the browser: no frame is fetched.
  const requests: string[] = []
  page.on("request", (request) => {
    if (isFrameRequest(request.url())) requests.push(request.url())
  })
  const before = await depthCanvas(page).evaluate((canvas) =>
    (canvas as HTMLCanvasElement).toDataURL()
  )
  await page.getByRole("combobox", { name: "Colormap" }).click()
  await page.getByRole("option", { name: "Inferno" }).click()
  await expect
    .poll(() =>
      depthCanvas(page).evaluate((canvas) =>
        (canvas as HTMLCanvasElement).toDataURL()
      )
    )
    .not.toBe(before)
  expect(requests).toEqual([])

  // Zoom in, then change depth: the same pixel stays under the cursor.
  const box = await depthCanvas(page).boundingBox()
  if (!box) throw new Error("The depth frame is not shown")
  const [x, y] = [box.width * 0.7, box.height * 0.3]
  const fitted = await pixelAt(page, x, y)
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.mouse.wheel(0, -600)
  const zoomed = await pixelAt(page, x, y)
  expect(zoomed).not.toBe(fitted)
  await page.getByRole("button", { name: "Next depth" }).click()
  await showsDepth(page, brightest + 1, count)
  expect(await pixelAt(page, x, y)).toBe(zoomed)

  // The ROI Inspector: the reference image (the sum of the reconstructed frames by default).
  await tab(page, "ROI Inspector").click()
  await expect.poll(() => at(page).search.tab).toBe("roi")
  const reference = page.getByRole("region", { name: "Reference image" })
  await expect(
    reference.getByRole("region", { name: "Colour scale" })
  ).toBeVisible()
  await expect(reference.getByRole("status")).toBeHidden()
  await expect(page.getByRole("combobox", { name: "Image" })).toContainText(
    "Reconstructed sum"
  )

  // Three clicks, three ROIs of 9 pixels, each in its own colour; their traces in the chart.
  const image = reference.locator("canvas[role='img']").first()
  const at3 = await image.boundingBox()
  if (!at3) throw new Error("The reference image is not shown")
  const clicks = [
    [0.45, 0.4],
    [0.55, 0.55],
    [0.5, 0.7],
  ] as const
  for (const [i, [fx, fy]] of clicks.entries()) {
    await page.mouse.click(at3.x + at3.width * fx, at3.y + at3.height * fy)
    await expect(roiRows(page)).toHaveCount(i + 1)
  }
  const three = await roiList(page)
  expect(three.map((row) => row.split(" ")[2])).toEqual(["9", "9", "9"])
  expect(new Set(three.map((row) => row.split(" ")[3])).size).toBe(3)
  const chart = page.getByRole("img", { name: "ROI traces" })
  await expect(chart.locator(".recharts-line-curve")).toHaveCount(3)
  await expect(chart).toContainText("Stored intensity")

  // Log and normalized: the traces are asked for again, normalized.
  await page.getByRole("combobox", { name: "Y axis" }).click()
  await page.getByRole("option", { name: "Log" }).click()
  const normalized = page.waitForRequest(
    (request) =>
      request.url().endsWith("/points/wire_1/roi-traces") &&
      (request.postDataJSON() as { normalize?: boolean }).normalize === true
  )
  await page.getByRole("combobox", { name: "Values" }).click()
  await page.getByRole("option", { name: "Normalized" }).click()
  await normalized
  await expect(chart).toContainText("Normalized intensity")
  await expect(chart.locator(".recharts-line-curve")).toHaveCount(3)

  // Delete the second: the others keep their colours.
  await roiRows(page)
    .nth(1)
    .getByRole("button", { name: /^Delete ROI 2 at/ })
    .click()
  await expect(roiRows(page)).toHaveCount(2)
  const kept = [three[0], three[2]]
  expect(await roiList(page)).toEqual(kept)
  await expect(chart.locator(".recharts-line-curve")).toHaveCount(2)

  // A reload: the ROIs (kept for this run and point) and the tab's settings are restored.
  await page.reload()
  await expect(roiRows(page)).toHaveCount(2)
  expect(await roiList(page)).toEqual(kept)
  await expect(page.getByRole("combobox", { name: "Y axis" })).toContainText(
    "Log"
  )
  await expect(page.getByRole("combobox", { name: "Values" })).toContainText(
    "Normalized"
  )
  await expect(chart.locator(".recharts-line-curve")).toHaveCount(2)
})

// R3, the seeded multi-point reconstruction: another point selected in the Points tab moves the bar
// and the Depth View to it, at its own brightest depth, and a reload keeps it; each point keeps its
// own ROIs (saved per run and point).
test("R3: another point moves the bar and the Depth View to it, a reload keeps it, and each point keeps its own ROIs", async ({
  page,
}) => {
  const R3 = "/reconstructions/3"
  const [first, second] = ["synthetic_wire_scan_1", "synthetic_wire_scan_2"]
  const traceOf = async (point: string) => {
    const answer = await page.request.get(
      `/api/v1/reconstruction-runs/3/points/${point}/trace`
    )
    return (await answer.json()) as ReconstructionTrace
  }
  const [one, two] = [await traceOf(first), await traceOf(second)]
  const count = two.depth_um.length
  expect(two.brightest_index).not.toBe(one.brightest_index)

  await page.goto(`${R3}?tab=points`)
  await expect(bar(page).getByRole("status")).toHaveText(
    "3 of 3 points complete"
  )
  await expect(bar(page)).toContainText(`Point ${first}`)
  await expect(checkbox(page, first)).toBeChecked()

  await checkbox(page, second).check()
  await expect(bar(page)).toContainText(`Point ${second}`)
  await expectAt(page, { tab: "points", point: second }, R3)

  // The Depth View opens the second point at its own brightest depth.
  const frame = page.waitForResponse(
    (response) =>
      isFrameRequest(response.url()) &&
      new URL(response.url()).pathname.endsWith(
        `/points/${second}/frames/${String(two.brightest_index)}`
      )
  )
  await tab(page, "Depth View").click()
  expect((await frame).status()).toBe(200)
  await showsDepth(page, two.brightest_index, count)
  await expectAt(page, { tab: "depth", point: second }, R3)

  await page.reload()
  await expect(bar(page)).toContainText(`Point ${second}`)
  await showsDepth(page, two.brightest_index, count)
  await expectAt(page, { tab: "depth", point: second }, R3)

  // An ROI added on the second point is not the first point's, and is there again back on the second.
  const reference = page.getByRole("region", { name: "Reference image" })
  const roiCard = page.getByRole("region", { name: "ROI list" })
  const openRois = async (point: string) => {
    await tab(page, "Points").click()
    await checkbox(page, point).check()
    await expect(bar(page)).toContainText(`Point ${point}`)
    await tab(page, "ROI Inspector").click()
    await expect(
      reference.getByRole("region", { name: "Colour scale" })
    ).toBeVisible()
  }
  await tab(page, "ROI Inspector").click()
  await expect(
    reference.getByRole("region", { name: "Colour scale" })
  ).toBeVisible()
  await expect(roiCard).toContainText("No ROIs")
  const image = reference.locator("canvas[role='img']").first()
  const box = await image.boundingBox()
  if (!box) throw new Error("The reference image is not shown")
  await page.mouse.click(box.x + box.width * 0.5, box.y + box.height * 0.5)
  await expect(roiRows(page)).toHaveCount(1)
  const added = await roiList(page)

  await openRois(first)
  await expect(roiCard).toContainText("No ROIs")
  await expect(roiRows(page)).toHaveCount(0)

  await openRois(second)
  await expect(roiRows(page)).toHaveCount(1)
  expect(await roiList(page)).toEqual(added)
})

// The larger sample: R2, the fine synthetic point (101 frames of 256², uint16), registered by the demo
// seed. A depth change's time is reported (guidance), never asserted.
test("R2's Depth View: a depth change's time (reported)", async ({ page }) => {
  test.setTimeout(120_000)
  const timings: string[] = []
  const status = depthPlot(page).getByRole("status")
  let start = Date.now()
  await page.goto("/reconstructions/2?tab=depth")
  await expect(page.getByRole("region", { name: "Colour scale" })).toBeVisible({
    timeout: 60_000,
  })
  await expect(status).toBeHidden({ timeout: 60_000 })
  timings.push(
    `open (trace, brightest frame): ${String(Date.now() - start)} ms`
  )

  for (const run of [1, 2, 3]) {
    const fetched = page.waitForResponse((response) =>
      isFrameRequest(response.url())
    )
    start = Date.now()
    await page.getByRole("button", { name: "Next depth" }).click()
    expect((await fetched).status()).toBe(200)
    await expect(status).toBeHidden()
    timings.push(`new depth ${String(run)}: ${String(Date.now() - start)} ms`)
  }
  // A depth shown before: from the browser's cache, no request.
  const pixels = () =>
    depthCanvas(page).evaluate((canvas) =>
      (canvas as HTMLCanvasElement).toDataURL()
    )
  const before = await pixels()
  start = Date.now()
  await page.getByRole("button", { name: "Previous depth" }).click()
  await expect.poll(pixels, { intervals: [10] }).not.toBe(before)
  timings.push(`cached depth: ${String(Date.now() - start)} ms`)

  for (const timing of timings) {
    test.info().annotations.push({ type: "timing", description: timing })
    console.log(`R2 depth view: ${timing}`)
  }
})
