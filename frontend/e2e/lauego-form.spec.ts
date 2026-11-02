import { expect, test, type Page } from "@playwright/test"

import type { CreatedRun, IndexingRunDetail, ProgramPrefill } from "@/api"
import { prefillAsked, sourceIds, sourceKind } from "./prefill"

// The LaueGo form on the seeded stack (`make e2e`, nothing mocked): a run from the finished R1 (its
// scan.h5, every depth), submitted and indexed by the seed's worker; a clone of the seeded I1. Also the
// idle /indexing list: a run queued through the API appears without a reload.

const DEMO = 990001

function field(page: Page, label: string) {
  return page.getByLabel(label, { exact: true })
}

test("from the finished R1: New Index gives its scan.h5 with every depth, and the run finishes", async ({
  page,
}) => {
  await page.goto("/reconstructions")
  await page.locator('.ag-row[row-id="1"] input[type="checkbox"]').check()
  expect(
    await prefillAsked(page, "lauego", () =>
      page.getByRole("button", { name: "New Index" }).click()
    )
  ).toEqual({ reconstruction: "1", scan: String(DEMO) })
  await expect(page).toHaveURL(/\/indexing\/new$/)
  await expect(page).toHaveTitle("New LaueGo Indexation · 3DMN Portal")
  await expect(sourceKind(page, "Reconstruction")).toHaveAttribute(
    "aria-pressed",
    "true"
  )
  await expect(field(page, "Reconstruction ID")).toHaveValue("R1")
  // The reconstruction sets its scan, shown read-only.
  await expect(sourceIds(page)).toContainText(`SN${String(DEMO)}`)
  await expect(field(page, "Folder Path")).toHaveValue(
    /\/rec_1\/reconstruction\/scan\.h5$/
  )
  await expect(field(page, "Depth Indices")).toHaveValue("")
  await field(page, "Author").fill("e2e")

  const created = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/indexing-runs" &&
      response.request().method() === "POST"
  )
  await page.getByRole("button", { name: "Submit", exact: true }).click()
  const answer = await created
  expect(answer.status()).toBe(201)
  const { run, job_id: job } = (await answer.json()) as CreatedRun

  // Created and queued: the run's page opens, where its link points.
  await expect(page).toHaveURL(new RegExp(`/indexing/${String(run.id)}$`))
  await expect(page).toHaveTitle(`Indexing ${run.label} · 3DMN Portal`)

  await page.goto("/runs")
  const row = page
    .locator(`.ag-row[row-id="${String(job)}"]`)
    .filter({ has: page.locator('[col-id="job_id"]') })
  await expect(row.locator('[col-id="status"]')).toHaveText("Finished", {
    timeout: 90_000,
  })
})

test("cloning I1 copies its parameters and resets the author and notes", async ({
  page,
}) => {
  const response = await page.request.get("/api/v1/indexing-runs/1")
  expect(response.ok()).toBe(true)
  const i1 = (await response.json()) as IndexingRunDetail
  const saved = i1.parameters
  if (saved === null) throw new Error("the seeded I1 has no parameters")
  // The seed gives I1 an author and notes, which the clone does not copy.
  expect(i1.author).not.toBe("")
  expect(i1.notes).not.toBe("")

  await page.goto("/indexing/new")
  await sourceKind(page, "Clone").click()
  const clone = field(page, "Clone of")
  await clone.fill("I1")
  await page.getByRole("option", { name: /^I1\b/ }).click()
  await expect(clone).toHaveValue("I1")
  await expect(sourceIds(page)).toContainText(`SN${String(DEMO)}`)
  await expect(field(page, "Folder Path")).toHaveValue(i1.input_path)
  await expect(field(page, "Filename")).toHaveValue(
    String(saved.filename_prefixes)
  )
  await expect(field(page, "Scan Indices")).toHaveValue(saved.scan_points)
  await expect(field(page, "Threshold Ratio")).toHaveValue(
    String(saved.threshold_ratio)
  )
  await expect(field(page, "Author")).toHaveValue("")
  await expect(field(page, "Notes")).toHaveValue("")
})

test("an idle /indexing list shows a run queued through the API within its 15 s cadence", async ({
  page,
}) => {
  const prefill = await page.request.get(
    "/api/v1/programs/lauego/prefill?reconstruction=1"
  )
  expect(prefill.ok()).toBe(true)
  const { values } = (await prefill.json()) as ProgramPrefill

  // The page's timers on Playwright's clock: each jump is one 5 s tick of the list's polling.
  await page.clock.install()
  await page.goto("/indexing")
  const statuses = page.locator('.ag-row [col-id="status"]')
  await expect(statuses.first()).toBeVisible()
  await expect(statuses.filter({ hasText: /Queued|Running/ })).toHaveCount(0)

  const headers = { "X-Laue-Portal": "1" }
  const created = await page.request.post("/api/v1/indexing-runs", {
    headers: {
      ...headers,
      "Idempotency-Key": `e2e-idle-${String(Date.now())}`,
    },
    data: { ...values, author: "e2e" },
  })
  expect(created.status()).toBe(201)
  const { run, job_id } = (await created.json()) as CreatedRun
  const changes = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return (
      url.pathname === "/api/v1/indexing-runs" &&
      url.searchParams.has("changed_since")
    )
  })
  // Three ticks (15 s of the page's time) and no more: the changes are asked by then, and the run
  // shows without another jump.
  for (let tick = 0; tick < 3; tick += 1) await page.clock.fastForward(5_000)
  await changes
  await expect(
    page.locator(`.ag-row[row-id="${String(run.id)}"]`).first()
  ).toBeVisible()
  // Not left to the worker: the other specs' runs need it.
  const cancel = await page.request.post(
    `/api/v1/jobs/${String(job_id)}/cancel`,
    { headers }
  )
  expect(cancel.ok()).toBe(true)
})
