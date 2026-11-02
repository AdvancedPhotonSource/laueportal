import { expect, test, type Page } from "@playwright/test"

import type { CreatedRun, ProgramPrefill } from "@/api"
import { prefillAsked, sourceIds, sourceKind } from "./prefill"
import { untilLocalRunsEnd } from "./stack-tools"

// The wire form on the seeded stack (`make e2e`, nothing mocked): a run from the demo scan selected on
// /scans (New Recon), validated (a warning), confirmed and submitted, which the seed's worker
// reconstructs, then indexed from the list's New Index (the daily chain: the created run's folder comes
// from `[output_templates]`, unlike the seeded R1's); a clone of the seeded R1. The demo
// scan's catalog points at the indexing frames; the wire data is R1's folder, so the run's folder differs
// from the catalog's (the warning).

const DEMO = 990001

/** The seeded R1's saved values, as its clone gives them (its folder and templates hold wire data). */
async function cloneOfR1(page: Page): Promise<ProgramPrefill["values"]> {
  const response = await page.request.get(
    "/api/v1/programs/wire/prefill?reconstruction=1"
  )
  expect(response.ok()).toBe(true)
  return ((await response.json()) as ProgramPrefill).values
}

function field(page: Page, label: string) {
  return page.getByLabel(label, { exact: true })
}

/** The status of a job on the Run Monitor, once it is Finished. */
async function waitFinished(page: Page, job: string) {
  await page.goto("/runs")
  const row = page
    .locator(`.ag-row[row-id="${job}"]`)
    .filter({ has: page.locator('[col-id="job_id"]') })
  await expect(row.locator('[col-id="status"]')).toHaveText("Finished", {
    timeout: 90_000,
  })
}

test("from /scans: New Recon on the demo scan, validate with a warning, confirm, submit one run, it finishes, and New Index on it finishes too", async ({
  page,
}) => {
  // The worker idle first, whatever the specs before this one left running: the run is to finish in 90 s.
  await untilLocalRunsEnd(page.request)
  const r1 = await cloneOfR1(page)
  await page.goto("/scans")
  await page
    .locator(`.ag-row[row-id="${String(DEMO)}"] input[type="checkbox"]`)
    .check()
  expect(
    await prefillAsked(page, "wire", () =>
      page.getByRole("button", { name: "New Recon" }).click()
    )
  ).toEqual({ scan: String(DEMO) })
  await expect(page).toHaveTitle("New Wire Reconstruction · 3DMN Portal")
  // The prefill is taken once and leaves the URL.
  await expect(page).toHaveURL(/\/reconstructions\/new$/)
  await expect(sourceKind(page, "Scan")).toHaveAttribute("aria-pressed", "true")
  await expect(field(page, "Scan Number")).toHaveValue(`SN${String(DEMO)}`)
  await expect(field(page, "Folder Path")).not.toHaveValue("")

  await field(page, "Folder Path").fill(String(r1.input_path))
  await field(page, "Filename").fill(String(r1.filename_prefixes))
  await page.getByRole("button", { name: "Find Indices" }).click()
  await expect(field(page, "Scan Indices")).toHaveValue(String(r1.scan_points))
  await field(page, "Author").fill("e2e")

  await page.getByRole("button", { name: "Validate" }).click()
  const validation = page.getByRole("region", { name: "Validation" })
  await expect(validation).toContainText("1 warning")
  await expect(validation).toContainText("different source path")

  const created = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/reconstruction-runs" &&
      response.request().method() === "POST"
  )
  await page.getByRole("button", { name: "Submit", exact: true }).click()
  const confirm = page.getByRole("alertdialog")
  await expect(confirm).toContainText("Submit with warnings?")
  await confirm.getByRole("button", { name: "Submit" }).click()
  const answer = await created
  expect(answer.status()).toBe(201)
  const { run, job_id: job } = (await answer.json()) as CreatedRun
  // The confirmation closes (until it has, its own Submit is a second button of that name).
  await expect(confirm).toBeHidden()

  // Created and queued: the run's page opens, where its link points.
  await expect(page).toHaveURL(
    new RegExp(`/reconstructions/${String(run.id)}$`)
  )
  await expect(page).toHaveTitle(`Reconstruction ${run.label} · 3DMN Portal`)
  await waitFinished(page, String(job))

  // The chain: New Index on the finished run gives its scan.h5 (every depth), and that run finishes.
  const id = String(run.id)
  await page.goto("/reconstructions")
  await page.locator(`.ag-row[row-id="${id}"] input[type="checkbox"]`).check()
  expect(
    await prefillAsked(page, "lauego", () =>
      page.getByRole("button", { name: "New Index" }).click()
    )
  ).toEqual({ reconstruction: id, scan: String(DEMO) })
  await expect(page).toHaveURL(/\/indexing\/new$/)
  await expect(sourceKind(page, "Reconstruction")).toHaveAttribute(
    "aria-pressed",
    "true"
  )
  await expect(field(page, "Reconstruction ID")).toHaveValue(run.label)
  await expect(sourceIds(page)).toContainText(`SN${String(DEMO)}`)
  await expect(field(page, "Folder Path")).toHaveValue(
    new RegExp(`/rec_${id}/data/reconstruction/scan\\.h5$`)
  )
  await expect(field(page, "Depth Indices")).toHaveValue("")
  await field(page, "Author").fill("e2e")
  const indexed = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/indexing-runs" &&
      response.request().method() === "POST"
  )
  await page.getByRole("button", { name: "Submit", exact: true }).click()
  const indexAnswer = await indexed
  expect(indexAnswer.status()).toBe(201)
  const indexing = (await indexAnswer.json()) as CreatedRun
  await expect(page).toHaveURL(
    new RegExp(`/indexing/${String(indexing.run.id)}$`)
  )
  await waitFinished(page, String(indexing.job_id))
})

test("cloning R1 copies its parameters and resets the author and notes", async ({
  page,
}) => {
  await page.goto("/reconstructions/new")
  await sourceKind(page, "Clone").click()
  const clone = field(page, "Clone of")
  await clone.fill("R1")
  await page.getByRole("option", { name: /^R1\b/ }).click()
  await expect(clone).toHaveValue("R1")
  // The clone sets its scan, shown read-only (no Scan Number picker).
  await expect(sourceIds(page)).toContainText(`SN${String(DEMO)}`)
  await expect(field(page, "Scan Number")).toHaveCount(0)
  // R1 (by `laueportal seed demo`): depths -25 to 25 µm, its wire file, author "demo" and notes.
  await expect(field(page, "Depth Start")).toHaveValue("-25")
  await expect(field(page, "Depth End")).toHaveValue("25")
  await expect(field(page, "Filename")).toHaveValue("wire_%d.h5")
  await expect(field(page, "Author")).toHaveValue("")
  await expect(field(page, "Notes")).toHaveValue("")
})
