import { expect, test, type Page, type Request } from "@playwright/test"

// The Color Map tab on the seeded stack (`make e2e`, nothing mocked): every colour mode on I1, a scalar
// mode's colorbar recolouring without fetching the map again, the hover readout and the click details
// card, the crystal-orientation card, a failed product keeping the last plot as stale, the 2D/3D switch (I1's frames have no depths, so 3D plots
// the motors' X, Y, Z), and (also in the `no-gpu` project) I2's (the demo's map, with depths) 3D view on the lab X,
// Y, Z with its hover tooltip, or the "needs WebGL" message.

const STORE_KEY = "laueportal:view:indexing-viewer:v1"

const plot2d = (page: Page) =>
  page.getByRole("img", { name: "Colour map", exact: true })
const plot3d = (page: Page) =>
  page.getByRole("img", { name: "Colour map (3D)" })
/**
 * The 2D plot's pixels (a PNG), to compare. Playwright types a screenshot as Node's Buffer, a type
 * this program lacks (no @types/node: e2e/node.d.ts), so it is read as the Uint8Array it is.
 */
async function plotImage(page: Page): Promise<Uint8Array> {
  const png: unknown = await plot2d(page).screenshot()
  if (!(png instanceof Uint8Array)) throw new Error("A screenshot is not bytes")
  return png
}
const sameImage = (a: Uint8Array, b: Uint8Array) =>
  a.length === b.length && a.every((value, i) => value === b[i])
const readout = (page: Page) =>
  page.getByRole("term").filter({ hasText: /^Step$/ })

const isMapRequest = (request: Request) =>
  new URL(request.url()).pathname.endsWith("/map")

/** Choose an option of a labelled select. */
async function choose(page: Page, label: string, option: string | RegExp) {
  await page.getByRole("combobox", { name: label }).click()
  await page.getByRole("option", { name: option }).click()
}

/** The response of the map request for a colour mode. */
function mapResponse(page: Page, color: string) {
  return page.waitForResponse(
    (response) =>
      isMapRequest(response.request()) &&
      new URL(response.url()).searchParams.get("color") === color
  )
}

/**
 * A point on the 2D map, in page coordinates: inside the lowest marker (a saturated colour) the
 * canvas shows, scanning up from the bottom (away from the plot's toolbar). The points are stamped on
 * a later animation frame than the canvas appears, so the scan is retried.
 */
async function aMarker(page: Page) {
  const canvas = plot2d(page)
  await expect(canvas).toBeVisible()
  const box = await canvas.boundingBox()
  if (!box) throw new Error("the map has no box")
  // Read through an object: TypeScript would narrow a `let` assigned only in the callback to null.
  const found: { point: { x: number; y: number } | null } = { point: null }
  await expect(async () => {
    found.point = await scanForMarker(page)
    expect(found.point, "a marker on the map").not.toBeNull()
  }).toPass()
  const corner = found.point
  if (!corner) throw new Error("no marker on the map")
  const { x, y } = corner
  // 8 px inside the 40 px marker's bottom-left corner.
  return { x: box.x + x + 8, y: box.y + y - 8 }
}

/** The lowest saturated pixel of the 2D map (canvas CSS px), or null. */
function scanForMarker(page: Page) {
  return plot2d(page).evaluate((element) => {
    const el = element as HTMLCanvasElement
    const context = el.getContext("2d")
    if (!context) return null
    const { width, height } = el
    const data = context.getImageData(0, 0, width, height).data
    for (let y = height - 1; y >= 0; y -= 2) {
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

async function openMap(page: Page, query = "") {
  const first = page.waitForResponse(
    (response) => isMapRequest(response.request()) && response.ok()
  )
  await page.goto(`/indexing/1?tab=map${query}`)
  await first
  await expect(plot2d(page)).toBeVisible()
}

test("every colour mode of I1 draws its map with its legend", async ({
  page,
}) => {
  await openMap(page)
  await expect(
    page.getByRole("img", { name: "Cubic IPF colour key" })
  ).toBeVisible()

  for (const [label, color] of [
    ["Rodrigues RGB", "rodrigues"],
    ["Pole Figure HSV", "pole_hsv"],
    ["N Indexed", "n_indexed"],
    ["Goodness", "goodness"],
    ["RMS Error", "rms_error"],
    ["N Patterns", "n_patterns"],
    ["Cubic IPF", "cubic_ipf"],
  ] as const) {
    const response = mapResponse(page, color)
    await choose(page, "Color by", label)
    // 200, or 304 when the browser revalidates a map it holds.
    expect((await response).status()).toBeLessThan(400)
    await expect(page).toHaveURL(
      color === "cubic_ipf"
        ? /\/indexing\/1\?tab=map$/
        : new RegExp(`color=${color}`)
    )
    if (color === "pole_hsv") {
      await expect(
        page.getByRole("img", { name: "Stereographic HSV colour key" })
      ).toBeVisible()
    }
    if (["n_indexed", "goodness", "rms_error", "n_patterns"].includes(color)) {
      await expect(page.getByRole("slider", { name: "Maximum" })).toBeVisible()
    }
    await expect(page.getByRole("alert")).toHaveCount(0)
  }

  // Misorientation needs the pole figure's reference: it says how to set it, and that the map
  // shown is the previous mode's, and asks nothing.
  let asked = false
  page.on("request", (request) => {
    if (isMapRequest(request)) asked = true
  })
  await choose(page, "Color by", "Misorientation")
  const hint = page.getByText(/click a pole on the Pole Figure tab/).first()
  await expect(hint).toBeVisible()
  await expect(hint).toContainText("Showing the previous map.")
  await expect(plot2d(page)).toBeVisible()
  // The query would start right after the render that shows the hint: give it time to.
  await page.waitForTimeout(1000)
  expect(asked).toBe(false)
  await expect(page).toHaveURL(/color=misorientation/)

  // With a clicked pole of this run (here the stored settings carry it) it draws.
  await page.evaluate((key) => {
    localStorage.setItem(
      key,
      JSON.stringify({
        state: {
          pole: {
            reference: {
              indexingId: 1,
              step: 0,
              patternIndex: 0,
              center: [0, 0],
            },
          },
        },
        version: 1,
      })
    )
  }, STORE_KEY)
  const response = mapResponse(page, "misorientation")
  await page.reload()
  const answer = await response
  expect(answer.status()).toBeLessThan(400)
  expect(new URL(answer.url()).searchParams.get("ref_pattern")).toBe("0,0")
  // The sidebar shows the reference (the map's Reference row).
  await expect(
    page
      .getByRole("complementary", { name: "Color map settings" })
      .getByText("step 0 / 0", { exact: true })
  ).toBeVisible()
})

test("a scalar mode's colorbar recolours the map without fetching it again", async ({
  page,
}) => {
  await openMap(page, "&color=goodness")
  const maximum = page.getByRole("slider", { name: "Maximum" })
  await expect(maximum).toBeVisible()
  const requests: string[] = []
  page.on("request", (request) => {
    if (isMapRequest(request)) requests.push(request.url())
  })

  const before = await plotImage(page)
  await choose(page, "Palette", "Jet")
  await expect
    .poll(async () => sameImage(await plotImage(page), before))
    .toBe(false)

  const jet = await plotImage(page)
  await maximum.focus()
  for (let i = 0; i < 5; i++) await page.keyboard.press("ArrowLeft")
  await expect
    .poll(async () => sameImage(await plotImage(page), jet))
    .toBe(false)

  const dragged = await plotImage(page)
  await page.getByRole("checkbox", { name: "Reverse" }).click()
  await expect
    .poll(async () => sameImage(await plotImage(page), dragged))
    .toBe(false)

  const reversed = await plotImage(page)
  await page
    .getByRole("region", { name: "Colour scale" })
    .getByRole("button", { name: "Reset" })
    .click()
  await expect
    .poll(async () => sameImage(await plotImage(page), reversed))
    .toBe(false)
  // Every change has shown: a request any of them made would have been sent by now.
  expect(requests).toEqual([])
})

test("hovering a point shows its readout, and clicking it opens its details card", async ({
  page,
}) => {
  await openMap(page)
  const point = await aMarker(page)
  await page.mouse.move(point.x, point.y)
  await expect(readout(page)).toBeVisible()
  await expect(
    page.getByRole("term").filter({ hasText: /^Frame$/ })
  ).toBeVisible()

  const details = page.waitForResponse((response) =>
    /\/points\/\d+/.test(new URL(response.url()).pathname)
  )
  await page.mouse.click(point.x, point.y)
  expect((await details).status()).toBe(200)
  const card = page.getByRole("region", { name: "Clicked point details" })
  await expect(card.getByText(/^Step #\d+$/)).toBeVisible()
  await expect(card.getByText("(clicked)")).toBeVisible()
  await expect(page).toHaveURL(/point=\d+/)
  await card.getByRole("button", { name: "Close the point details" }).click()
  await expect(card).toHaveCount(0)
  await expect(page).not.toHaveURL(/point=/)
})

test("the crystal-orientation card shows a clicked point's cell, goes back to its line, and stays collapsed", async ({
  page,
}) => {
  await openMap(page)
  const cell = page.getByRole("region", { name: "Crystal orientation" })
  await expect(
    cell.getByText("Click a point to show its unit cell.")
  ).toBeVisible()

  const point = await aMarker(page)
  await page.mouse.click(point.x, point.y)
  await expect(
    cell.getByRole("img", { name: /^The unit cell of step \d+/ })
  ).toBeVisible()
  await expect(cell.getByText("Ni · SG 225")).toBeVisible()
  await expect(
    cell.getByRole("term").filter({ hasText: /^Rotation$/ })
  ).toBeVisible()

  // Closing the point's details clears the point: the card keeps its place and its line.
  await page
    .getByRole("region", { name: "Clicked point details" })
    .getByRole("button", { name: "Close the point details" })
    .click()
  await expect(
    cell.getByText("Click a point to show its unit cell.")
  ).toBeVisible()

  await cell
    .getByRole("button", { name: "Collapse the crystal orientation" })
    .click()
  await expect(cell).toHaveCount(0)
  await page.reload()
  const strip = page.getByRole("button", {
    name: "Show the crystal orientation",
  })
  await expect(strip).toBeVisible()
  await strip.click()
  await expect(cell).toBeVisible()
})

test("a product that fails keeps the last plot and says it is stale", async ({
  page,
}) => {
  await openMap(page, "&color=rodrigues")
  await choose(page, "Reference", "Step #")
  const failed = page.waitForResponse(
    (response) =>
      isMapRequest(response.request()) &&
      new URL(response.url()).searchParams.get("rodrigues_ref") === "99"
  )
  const field = page.getByRole("textbox", { name: "Reference step" })
  await field.fill("99")
  await field.press("Enter")
  expect((await failed).status()).toBe(422)
  const alert = page.getByRole("alert")
  await expect(alert).toContainText("Product failed")
  await expect(alert).toContainText("Step must be an integer")
  await expect(alert).toContainText("Showing the last data loaded.")
  await expect(plot2d(page)).toBeVisible()
})

test("the 2D/3D switch changes the view and its axes, and the URL", async ({
  page,
}) => {
  await openMap(page)
  await expect(page.getByRole("combobox", { name: "Z axis" })).toHaveCount(0)
  const threeD = mapResponse(page, "cubic_ipf")
  await page.getByRole("button", { name: "3D" }).click()
  const answer = await threeD
  // The default, the lab X, Y, Z, needs depths (Zlab): without, the motors' X, Y, Z.
  expect(new URL(answer.url()).searchParams.get("axes")).toBe("X,Y,Z")
  await expect(page).toHaveURL(/view=3d/)
  await expect(plot3d(page)).toBeVisible()
  await expect(page.getByRole("combobox", { name: "Z axis" })).toBeVisible()

  await page.getByRole("button", { name: "2D" }).click()
  await expect(page).not.toHaveURL(/view=3d/)
  await expect(plot2d(page)).toBeVisible()
  await expect(page.getByRole("combobox", { name: "Z axis" })).toHaveCount(0)
})

test("I2 in 3D: with WebGL the hover tooltip shows the point and its coordinates; without it the needs-WebGL message @webgl", async ({
  page,
}, testInfo) => {
  test.setTimeout(120_000)
  if (testInfo.project.name === "no-gpu") {
    await page.goto("/indexing/2?tab=map&view=3d")
    await expect(page.getByText("3D needs WebGL")).toBeVisible()
    // The 2D view stays available.
    await page.getByRole("button", { name: "2D" }).click()
    await expect(plot2d(page)).toBeVisible({ timeout: 60_000 })
    return
  }
  const map = page.waitForResponse((response) =>
    isMapRequest(response.request())
  )
  await page.goto("/indexing/2?tab=map&view=3d")
  // I2's map has depths: the default lab X, Y, Z.
  expect(new URL((await map).url()).searchParams.get("axes")).toBe(
    "Xlab,Ylab,Zlab"
  )
  const cloud = plot3d(page)
  await expect(cloud).toBeVisible({ timeout: 60_000 })
  await expect(page.getByText("Loading the 3D view")).toHaveCount(0, {
    timeout: 60_000,
  })
  const box = await cloud.boundingBox()
  if (!box) throw new Error("the 3D view has no box")
  // The camera fits the cloud, whose middle is the view's: look for a point around the centre.
  const tooltip = page.getByRole("term").filter({ hasText: /^Z lab/ })
  let found = false
  for (const [dx, dy] of [
    [0, 0],
    [10, -5],
    [-10, 5],
    [20, -10],
    [-20, 10],
    [0, 15],
    [0, -15],
  ] as const) {
    await page.mouse.move(
      box.x + box.width / 2 + dx,
      box.y + box.height / 2 + dy
    )
    const shown = await tooltip
      .waitFor({ state: "visible", timeout: 1500 })
      .then(() => true)
      .catch(() => false)
    if (shown) {
      found = true
      break
    }
  }
  expect(found).toBe(true)
  await expect(readout(page)).toBeVisible()
  await expect(
    page.getByRole("term").filter({ hasText: /^X lab/ })
  ).toBeVisible()
})
