import { expect, test, type Page } from "@playwright/test"

// The Pole Figure tab on the seeded stack (`make e2e`, nothing mocked): a clicked pole switches
// the map to Pole HSV and "Reset color center" restores its mode; the "Set color center" tool
// places the centre off the poles; a lasso on the pole figure shows the selection's misorientation
// card and highlights the selection on the map.

const plot = (page: Page) => page.getByRole("img", { name: "Pole figure" })

/** The sidebar's Center and Reference values. */
const settings = (page: Page) =>
  page.getByRole("complementary", { name: "Pole figure settings" })

const isPath = (url: string, end: string) => new URL(url).pathname.endsWith(end)

async function openPoleFigure(page: Page, query = "") {
  const first = page.waitForResponse(
    (response) => isPath(response.url(), "/pole-figure") && response.ok()
  )
  await page.goto(`/indexing/1?tab=pole${query}`)
  await first
  await expect(plot(page)).toBeVisible()
}

/**
 * A pole, in page coordinates: inside the first saturated marker the canvas shows, from the top. The
 * points are stamped on a later animation frame than the canvas appears, so the scan is retried.
 */
async function aPole(page: Page) {
  const canvas = plot(page)
  const box = await canvas.boundingBox()
  if (!box) throw new Error("the pole figure has no box")
  // Read through an object: TypeScript would narrow a `let` assigned only in the callback to null.
  const found: { point: { x: number; y: number } | null } = { point: null }
  await expect(async () => {
    found.point = await scanForPole(page)
    expect(found.point, "a pole on the figure").not.toBeNull()
  }).toPass()
  const at = found.point
  if (!at) throw new Error("no pole on the figure")
  const { x, y } = at
  // 3 px below the 12 px marker's top edge.
  return { x: box.x + x + 1, y: box.y + y + 3 }
}

/** The first saturated pixel of the pole figure (canvas CSS px), or null. */
function scanForPole(page: Page) {
  return plot(page).evaluate((element) => {
    const el = element as HTMLCanvasElement
    const context = el.getContext("2d")
    if (!context) return null
    const { width, height } = el
    const data = context.getImageData(0, 0, width, height).data
    for (let y = 0; y < height; y += 2) {
      for (let x = 0; x < width; x += 2) {
        const i = 4 * (y * width + x)
        const [r = 0, g = 0, b = 0, a = 0] = data.slice(i, i + 4)
        if (a > 200 && Math.max(r, g, b) - Math.min(r, g, b) > 120) {
          return {
            x: (x * el.clientWidth) / width,
            y: (y * el.clientHeight) / height,
          }
        }
      }
    }
    return null
  })
}

test("a clicked pole switches the map to Pole HSV, and Reset color center restores its mode", async ({
  page,
}) => {
  await openPoleFigure(page, "&color=goodness")
  await expect(settings(page).getByText("(0, 0)")).toBeVisible()
  await expect(
    page.getByRole("button", { name: "Reset color center" })
  ).toHaveCount(0)

  const at = await aPole(page)
  const recoloured = page.waitForResponse(
    (response) =>
      isPath(response.url(), "/pole-figure") &&
      new URL(response.url()).searchParams.has("center")
  )
  await page.mouse.click(at.x, at.y)
  await expect(page).toHaveURL(/color=pole_hsv/)
  // The pole's pattern is the reference now.
  await expect(settings(page).getByText(/^step \d+ \/ \d+$/)).toBeVisible()
  // The pole figure's HSV colours are centred on the pole now.
  expect((await recoloured).ok()).toBe(true)

  // The map draws Pole HSV, centred on the clicked pole.
  const map = page.waitForResponse(
    (response) =>
      isPath(response.url(), "/map") &&
      new URL(response.url()).searchParams.get("color") === "pole_hsv"
  )
  await page.getByRole("tab", { name: "Color Map" }).click()
  const centre = new URL((await map).url()).searchParams.get("pole_center")
  expect(centre).not.toBe("0,0")
  await expect(page.getByRole("combobox", { name: "Color by" })).toContainText(
    "Pole Figure HSV"
  )

  await page.getByRole("tab", { name: "Pole Figure" }).click()
  await page.getByRole("button", { name: "Reset color center" }).click()
  await expect(page).toHaveURL(/color=goodness/)
  await expect(
    page.getByRole("button", { name: "Reset color center" })
  ).toHaveCount(0)
  await expect(settings(page).getByText("(0, 0)")).toBeVisible()
})

test("the Set color center tool places the centre off the poles, and the map's Pole HSV follows it", async ({
  page,
}) => {
  await openPoleFigure(page, "&color=goodness")
  const box = await plot(page).boundingBox()
  if (!box) throw new Error("the pole figure has no box")
  await page
    .getByRole("toolbar", { name: "Plot tools" })
    .getByRole("button", { name: "Set color center" })
    .click()

  // Just inside the rim, straight up: I1's few poles are not there.
  const placed = page.waitForResponse(
    (response) =>
      isPath(response.url(), "/pole-figure") &&
      new URL(response.url()).searchParams.has("center")
  )
  const cx = box.x + box.width / 2
  const cy = box.y + box.height / 2
  await page.mouse.click(cx, cy - 0.45 * Math.min(box.width, box.height))
  const centre = new URL((await placed).url()).searchParams.get("center")
  const [x, y] = (centre ?? "").split(",").map(Number)
  expect(Math.abs(x ?? 1)).toBeLessThan(0.05)
  expect(y).toBeGreaterThan(0.6)
  await expect(page).toHaveURL(/color=pole_hsv/)
  // No pole was clicked: no reference.
  await expect(settings(page).getByText(/^step \d+ \/ \d+$/)).toHaveCount(0)

  const map = page.waitForResponse(
    (response) =>
      isPath(response.url(), "/map") &&
      new URL(response.url()).searchParams.get("color") === "pole_hsv"
  )
  await page.getByRole("tab", { name: "Color Map" }).click()
  expect(new URL((await map).url()).searchParams.get("pole_center")).toBe(
    centre
  )
})

test("a lasso on the pole figure shows the misorientation card and highlights the map", async ({
  page,
}) => {
  await openPoleFigure(page)
  const box = await plot(page).boundingBox()
  if (!box) throw new Error("the pole figure has no box")

  // Lasso (the default tool) around the whole unit circle: every pole of I1's three patterns.
  const stats = page.waitForResponse((response) =>
    isPath(response.url(), "/selection-stats")
  )
  const cx = box.x + box.width / 2
  const cy = box.y + box.height / 2
  const radius = 0.49 * Math.min(box.width, box.height)
  await page.mouse.move(cx + radius, cy)
  await page.mouse.down()
  for (let step = 1; step <= 24; step++) {
    const angle = (step * Math.PI) / 12
    await page.mouse.move(
      cx + radius * Math.cos(angle),
      cy + radius * Math.sin(angle),
      { steps: 2 }
    )
  }
  await page.mouse.up()

  const response = await stats
  expect(response.ok()).toBe(true)
  const sent = response.request().postDataJSON() as { patterns: number[][] }
  expect(sent.patterns).toHaveLength(3)
  const card = page.getByRole("region", { name: "Selection" })
  await expect(card).toContainText("9 poles from 3 patterns")
  await expect(card.getByRole("term")).toHaveText([
    "Mean",
    "Min",
    "Max",
    "Pairs",
  ])
  await expect(card).toContainText(/3 \(cubic symmetry\)/)

  // The map shares the selection: it draws it highlighted, with its own Clear selection.
  await page.getByRole("tab", { name: "Color Map" }).click()
  const mapPlot = page.getByRole("img", { name: "Colour map", exact: true })
  await expect(mapPlot).toBeVisible()
  await expect(
    page
      .getByRole("toolbar", { name: "Plot tools" })
      .getByRole("button", { name: "Clear selection" })
  ).toBeVisible()
})
