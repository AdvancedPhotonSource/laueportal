import { expect, test, type Page, type Request } from "@playwright/test"

// The Detector View tab on the seeded stack (`make e2e`, nothing mocked but one held response): I1's
// frame with its overlays, the overlay toggles and the colormap redrawing it (the colormap without a
// request), the simulation drawing a reflection step 1's pattern 1 did not index, and the step moving
// through the scope's steps with back and forward, its frame and overlays shown as a pair and its
// pattern checklist reset.

const plot = (page: Page) => page.getByRole("img", { name: "Detector image" })
const card = (page: Page) => page.getByRole("region", { name: "Step summary" })
const toggle = (page: Page, name: string) =>
  page.getByRole("checkbox", { name, exact: true })

const isDetectorRequest = (request: Request) =>
  /\/detector\/\d+\/(image|overlays)$/.test(new URL(request.url()).pathname)

/**
 * The plot's pixels (a PNG), to compare. Playwright types a screenshot as Node's Buffer, a type this
 * program lacks (no @types/node: e2e/node.d.ts), so it is read as the Uint8Array it is.
 */
async function plotImage(page: Page): Promise<Uint8Array> {
  const png: unknown = await plot(page).screenshot()
  if (!(png instanceof Uint8Array)) throw new Error("A screenshot is not bytes")
  return png
}
const sameImage = (a: Uint8Array, b: Uint8Array) =>
  a.length === b.length && a.every((value, i) => value === b[i])

/** The canvas's own pixels, without the status shown over it (a data URL, to compare). */
const canvasPixels = (page: Page) =>
  plot(page).evaluate((element) => (element as HTMLCanvasElement).toDataURL())

/** Whether the canvas shows an indexed peak's red cross (rgb(220, 50, 47)). */
function hasIndexedMark(page: Page) {
  return plot(page).evaluate((element) => {
    const el = element as HTMLCanvasElement
    const context = el.getContext("2d")
    if (!context) return false
    const data = context.getImageData(0, 0, el.width, el.height).data
    for (let i = 0; i < data.length; i += 4) {
      const [r = 0, g = 0, b = 0] = data.slice(i, i + 3)
      if (r > 190 && g < 90 && b < 90) return true
    }
    return false
  })
}

/**
 * Whether the canvas shows a simulated reflection's purple triangle (rgb(128, 0, 128)), blended at
 * its edges: red and blue alike and well above green. The frame must be grey then (a grey frame's
 * pixels have no hue; inferno's or viridis's purples would pass).
 */
function hasSimulatedMark(page: Page) {
  return plot(page).evaluate((element) => {
    const el = element as HTMLCanvasElement
    const context = el.getContext("2d")
    if (!context) return false
    const data = context.getImageData(0, 0, el.width, el.height).data
    for (let i = 0; i < data.length; i += 4) {
      const [r = 0, g = 0, b = 0] = data.slice(i, i + 3)
      if (
        r > 70 &&
        b > 70 &&
        Math.abs(r - b) < 40 &&
        g < 0.6 * Math.min(r, b)
      ) {
        return true
      }
    }
    return false
  })
}

/** Open I1's detector tab and wait until its frame and overlays are drawn. */
async function openDetector(page: Page) {
  const image = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname.endsWith("/image") && response.ok()
  )
  await page.goto("/indexing/1?tab=detector")
  await image
  await expect(card(page)).toContainText("Step #0")
  // The marks are drawn on a later animation frame than the card shows, so the probe is retried.
  await expect.poll(() => hasIndexedMark(page)).toBe(true)
}

/** Change something and wait until the plot looks different. */
async function redraws(page: Page, change: () => Promise<void>) {
  const before = await plotImage(page)
  await change()
  await expect
    .poll(async () => sameImage(await plotImage(page), before))
    .toBe(false)
}

test("I1's detector tab draws the frame with its overlays; the toggles and the colormap redraw it", async ({
  page,
}) => {
  await openDetector(page)
  await expect(page.getByRole("region", { name: "Colour scale" })).toBeVisible()

  await redraws(page, () => toggle(page, "Indexed peaks").click())
  await expect.poll(() => hasIndexedMark(page)).toBe(false)
  await redraws(page, () => toggle(page, "Detected peaks").click())
  await redraws(page, () => toggle(page, "Indexed peaks").click())
  await expect.poll(() => hasIndexedMark(page)).toBe(true)
  await redraws(page, () => toggle(page, "hkl labels").click())

  // The colormap recolours the frame in the browser: nothing is fetched. (A grey one, so the
  // simulated marks' purple below is told from the frame.)
  const requests: string[] = []
  page.on("request", (request) => {
    if (isDetectorRequest(request)) requests.push(request.url())
  })
  await redraws(page, async () => {
    await page.getByRole("combobox", { name: "Colormap" }).click()
    await page.getByRole("option", { name: "Gray reversed" }).click()
  })
  expect(requests).toEqual([])

  // The simulation is asked for only when its overlay is on (off by default); it takes seconds a
  // pattern, so it shows as loading. On step 1 it finds one reflection pattern 1 did not index,
  // drawn as a purple triangle.
  await page.getByRole("button", { name: "Next step" }).click()
  await expect(card(page)).toContainText("Step #1")
  await expect.poll(() => hasIndexedMark(page)).toBe(true)
  expect(await hasSimulatedMark(page)).toBe(false)
  const simulated = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname.endsWith("/detector/1/overlays") &&
      new URL(response.url()).searchParams.get("simulate") === "true",
    { timeout: 60_000 }
  )
  await toggle(page, "Simulated missing peaks").click()
  await expect(
    page.getByRole("status").filter({ hasText: /^Simulating the missing/ })
  ).toBeVisible()
  expect((await simulated).ok()).toBe(true)
  await expect(card(page)).toContainText(/\b1 simulated missing/)
  await expect.poll(() => hasSimulatedMark(page)).toBe(true)
})

test("the step moves through the scope's steps, and back and forward return to them", async ({
  page,
}) => {
  await openDetector(page)
  const step = page.getByRole("textbox", { name: "Step" })
  await expect(step).toHaveValue("0")
  await expect(
    page.getByRole("button", { name: "Previous step" })
  ).toBeDisabled()
  // Step 0's pattern 0 unchecked: it is checked again when step 0 is shown again (below).
  const pattern0 = page.getByRole("checkbox", { name: /^Pattern 0 / })
  await redraws(page, () => pattern0.click())
  await expect(pattern0).not.toBeChecked()

  // The frame and the overlays show as a pair: while step 1's overlays are held back (its frame
  // is not), step 0's pair stays, pixels and card, and the plot says step 1 is loading.
  let release = () => {}
  const held = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route(/\/detector\/1\/overlays/, async (route) => {
    await held
    await route.continue()
  })
  const before = await canvasPixels(page)
  const frame = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname.endsWith("/detector/1/image") &&
      response.ok()
  )
  await page.getByRole("button", { name: "Next step" }).click()
  await (await frame).finished()
  await expect(
    page.getByRole("status").filter({ hasText: "Loading step 1…" })
  ).toBeVisible()
  // Nothing signals that the frame was decoded; give it time to (wrongly) show.
  await page.waitForTimeout(500)
  await expect(card(page)).toContainText("Step #0")
  expect(await canvasPixels(page)).toBe(before)
  release()
  await expect(page).toHaveURL(/[?&]step=1(&|$)/)
  await expect(card(page)).toContainText("Step #1")
  await expect.poll(() => canvasPixels(page)).not.toBe(before)
  await page.unrouteAll()
  // A step outside the scope is marked while typed and refused: the field is restored.
  await step.fill("2")
  await expect(step).toHaveAttribute("aria-invalid", "true")
  await step.press("Enter")
  await expect(step).toHaveValue("1")
  await expect(page).toHaveURL(/[?&]step=1(&|$)/)
  await page.getByRole("button", { name: "Next step" }).click()
  await expect(page).toHaveURL(/[?&]step=3(&|$)/)
  await expect(card(page)).toContainText("Step #3")
  await expect(page.getByRole("button", { name: "Next step" })).toBeDisabled()

  await page.goBack()
  await expect(page).toHaveURL(/[?&]step=1(&|$)/)
  await expect(card(page)).toContainText("Step #1")
  await expect(step).toHaveValue("1")
  // The default step is not written.
  await page.goBack()
  await expect(page).not.toHaveURL(/step=/)
  await expect(card(page)).toContainText("Step #0")
  await expect(pattern0).toBeChecked()
  await page.goForward()
  await expect(card(page)).toContainText("Step #1")
})
