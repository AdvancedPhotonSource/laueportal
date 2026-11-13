import { expect, test, type Page } from "@playwright/test"

import { configuredRunsRoot } from "./stack-tools"

// The Calibration page on the seeded stack (`make e2e`, nothing mocked): the demo's synthetic Si frame
// (`<inputs>/calibration-demo/si_calibration.h5`, LaueGo's perfect-Si orientation on detector 0 of the
// demo geometry) indexed from the moved starting geometry of the geometry folder
// (`<inputs>/geometries`), two measured energies (the frame's true ones, `si_energies.txt`), the
// detector's fit, Match Again, and the geometry file saved into the folder.

const inputs = () => `${configuredRunsRoot()}/inputs`

async function chooseInputs(page: Page): Promise<void> {
  await page.goto("/calibration")
  await page.evaluate(() => {
    localStorage.removeItem("laueportal:calibration:v1")
  })
  await page.reload()
  const frame = page.getByRole("textbox", { name: "Frame", exact: true })
  await frame.fill(`${inputs()}/calibration-demo/si_calibration.h5`)
  await frame.blur()
  const crystal = page.getByRole("textbox", { name: "Crystal", exact: true })
  await crystal.fill(`${inputs()}/calibration/Si.xml`)
  await crystal.blur()
}

test("a Si frame is indexed, its detector fitted and saved", async ({
  page,
}) => {
  test.slow()
  await chooseInputs(page)
  await expect(
    page.getByRole("textbox", { name: "Starting geometry", exact: true })
  ).toHaveValue(/geometries\/geoN_2025-01-01_00-00-00\.xml$/)

  const inputsCard = page
    .getByRole("definition")
    .filter({ hasText: "PE1621 723-3335" })
  await expect(inputsCard).toHaveText("PE1621 723-3335 (detector 0)")
  const indexing = page.locator('dl[aria-label="Indexing"]')
  await expect(indexing).toContainText("21 of 21 peaks", { timeout: 60_000 })
  const surface = page.locator('dl[aria-label="Surface reflection"]').first()
  await expect(surface).toContainText("0 0 1")
  await expect(page.getByText("As expected").first()).toBeVisible()

  const refine = page.getByRole("button", { name: "Refine", exact: true })
  await expect(
    page.getByText("Enter at least one measured energy")
  ).toBeVisible()
  await expect(refine).toBeDisabled()
  for (const [hkl, kev] of [
    ["0 0 4", "6.4335"],
    ["-1 -1 5", "8.6900"],
  ] as const) {
    const field = page.getByLabel(`Measured energy of ${hkl}`, { exact: true })
    await field.fill(kev)
    await field.press("Enter")
    await page
      .getByLabel(`Where the energy of ${hkl} was measured`, { exact: true })
      .fill("scan 42")
  }

  await refine.click()
  const detector = page.locator('dl[aria-label="Detector"]')
  await expect(detector).toContainText("RMS pixel error", { timeout: 60_000 })
  await expect(page.getByText("Refined", { exact: true })).toBeVisible()
  await expect(detector).toContainText("21 (2 with energy)")
  const rms = await detector
    .getByRole("definition")
    .filter({ hasText: / px$/ })
    .textContent()
  expect(Number.parseFloat(rms ?? "")).toBeLessThan(0.2)

  // A changed input makes the fit stale; leaving the field applies it (no Enter); refining again restores it.
  const second = page.getByLabel("Measured energy of -1 -1 5", { exact: true })
  await second.fill("8.6901")
  await second.blur()
  await expect(page.getByText("Inputs changed: refine again")).toBeVisible()
  await refine.click()
  await expect(page.getByText("Refined", { exact: true })).toBeVisible({
    timeout: 60_000,
  })

  // Match Again indexes with the refined detector.
  await page.getByRole("button", { name: "Match Again" }).click()
  await expect(indexing).toContainText("Refined (Match Again)", {
    timeout: 60_000,
  })
  await expect(indexing).toContainText("21 of 21 peaks")
  await refine.click()
  await expect(page.getByText("Refined", { exact: true })).toBeVisible({
    timeout: 60_000,
  })

  await expect(page.getByRole("textbox", { name: "Note" })).toHaveValue(
    /^lauelab .* Si 21pk 2E rms /
  )
  await page
    .getByRole("textbox", { name: "File name" })
    .fill("geoN_e2e-calibration.xml")
  await page.getByRole("button", { name: "Save", exact: true }).click()
  const saved = page.locator('dl[aria-label="Saved"]')
  await expect(saved).toContainText(/geometries\/geoN_e2e-calibration\.xml$/, {
    timeout: 60_000,
  })
  // The starting geometry stays the file chosen; the folder now has the new file first.
  await expect(
    page.getByRole("textbox", { name: "Starting geometry", exact: true })
  ).toHaveValue(/geoN_2025-01-01_00-00-00\.xml$/)
})

test("the draft survives a reload", async ({ page }) => {
  await chooseInputs(page)
  const field = page.getByLabel("Measured energy of 0 0 4", { exact: true })
  await expect(field).toBeVisible({ timeout: 60_000 })
  await field.fill("6.4335")
  await field.press("Enter")
  const size = page.getByRole("textbox", { name: "Label size" })
  await size.fill("14")
  await size.press("Enter")
  await page.reload()
  await expect(
    page.getByLabel("Measured energy of 0 0 4", { exact: true })
  ).toHaveValue("6.4335", { timeout: 60_000 })
  await expect(page.getByRole("textbox", { name: "Label size" })).toHaveValue(
    "14"
  )
})
