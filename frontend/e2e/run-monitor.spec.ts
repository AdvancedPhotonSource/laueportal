import { expect, test, type Locator, type Page } from "@playwright/test"

import type { JobLog, JobSummary } from "@/api"
import { makeJobSummary } from "../src/test/jobs"
import { mockJobList } from "./jobs-api"
import { problem, type ProblemAnswer } from "./problems"
import { advanceUntil, SHORT } from "./clock"
import { mockStatus, STATUS_OK } from "./runs-api"
import {
  checkStackDatabase,
  lastJobId,
  queueSlowRuns,
  seededCruxJob,
  stopJobsAfter,
  untilLocalRunsEnd,
} from "./stack-tools"

// The run monitor. On the seeded stack (`make e2e`, nothing mocked): long demo jobs queued
// (`laueportal seed slow-runs`), one moved to the front, then stopped with confirmation, the statuses
// following without a reload, and the stopped job's page; the demo's run on CRUX with its remote phase
// in its Status cell, first when sorted by Execution (no Remote Phase column). With the list answered
// by the test (e2e/jobs-api.ts): the columns with a remote run, 900 px, the toasts' reasons (409, 503,
// an unreachable queue), and live refresh (its 5 s poll on Playwright's fake clock, e2e/clock.ts).

test.use({ timezoneId: "America/Chicago" })

/** A job's status cell (in the pinned or the scrolling part of its row). */
function status(page: Page, jobId: number) {
  return page.locator(`.ag-row[row-id="${String(jobId)}"] [col-id="status"]`)
}

function checkbox(page: Page, jobId: number) {
  return page.locator(
    `.ag-row[row-id="${String(jobId)}"] input[type="checkbox"]`
  )
}

function cell(page: Page, jobId: number, field: string) {
  return page.locator(`.ag-row[row-id="${String(jobId)}"] [col-id="${field}"]`)
}

function button(page: Page, name: string) {
  return page.getByRole("button", { name, exact: true })
}

/**
 * The page's count of selected jobs: wait for the whole selection before an action (AG Grid
 * reports a selection change from a later task than the click).
 */
function selectedCount(page: Page, count: number) {
  return page.getByText(`${String(count)} selected`, { exact: true })
}

/** The newest toast with this title. */
function toastTitled(page: Page, title: string): Locator {
  return page
    .locator("[data-sonner-toast]")
    .filter({ has: page.locator(`[data-title]:text-is("${title}")`) })
    .first()
}

/** Mark the document: a reload would lose the mark. */
async function markDocument(page: Page) {
  await page.evaluate(() => {
    document.body.dataset.loaded = "once"
  })
}

function definition(root: Locator, term: string) {
  return root.locator(`div:has(> dt:text-is("${term}")) > dd`).first()
}

test.describe("on the seeded stack", () => {
  // The newest job before the test queued its runs: the jobs after it are the test's.
  let queuedAfter: number | undefined

  // Stop the test's runs that are still queued or running, also after a failure or a timeout, and
  // those a command queued before it failed: they would hold the worker for minutes.
  test.afterEach(async ({ request }) => {
    if (queuedAfter === undefined) return
    await stopJobsAfter(request, queuedAfter)
    queuedAfter = undefined
  })

  test("queue long demo jobs, move one to the front, stop them; the statuses change without a reload", async ({
    page,
    request,
  }) => {
    test.setTimeout(240_000)
    await checkStackDatabase(request)
    // The worker idle first, whatever the specs before this one left running (in a full `make e2e`, e.g. the
    // demo's R1 and I1, or the end of e2e/live.spec.ts's sessions).
    await untilLocalRunsEnd(request)
    queuedAfter = await lastJobId(request)
    // Three runs: the worker starts the first at once, so two wait and Move to Front can reorder them.
    const runs = queueSlowRuns(3, 2000)
    const [first, second, third] = runs
    if (!first || !second || !third) throw new Error("three runs were queued")
    const label = (run: { indexing_id: number }) =>
      `I${String(run.indexing_id)}`
    const dialog = page.getByRole("alertdialog")
    await page.goto("/runs")
    await expect(page).toHaveTitle("Run Monitor · 3DMN Portal")
    await markDocument(page)
    await expect(status(page, first.job_id)).toHaveText(/^Running/, {
      timeout: 30_000,
    })
    await expect(status(page, second.job_id)).toHaveText("Queued")
    await expect(status(page, third.job_id)).toHaveText("Queued")
    await expect(button(page, "Stop")).toBeDisabled()
    await expect(button(page, "Move to Front")).toBeDisabled()

    // The newest queued job goes to the front, ahead of the older one.
    await checkbox(page, third.job_id).check()
    await expect(selectedCount(page, 1)).toBeVisible()
    await expect(button(page, "Move to Front")).toBeEnabled()
    await expect(button(page, "Stop")).toBeEnabled()
    await button(page, "Move to Front").click()
    await expect(dialog).toContainText("Move 1 queued run to the front?")
    await expect(dialog.locator("li[data-job]")).toHaveText([
      `Job ${String(third.job_id)}${label(third)}Queued`,
    ])
    await dialog.getByRole("button", { name: "Move to Front" }).click()
    await expect(dialog).toHaveCount(0)
    await expect(toastTitled(page, "Move to Front")).toContainText(
      `Job ${String(third.job_id)} (${label(third)}) moved to the front of the queue.`
    )
    await checkbox(page, third.job_id).uncheck()

    // The running job's page follows it every 2 s: its counters move, its heartbeat is recent, and
    // its run.json is still to come.
    await cell(page, first.job_id, "job_id").getByRole("link").click()
    await expect(page).toHaveTitle(`Job ${String(first.job_id)} · 3DMN Portal`)
    const progress = page.getByRole("region", { name: "Run Progress" })
    const processed = definition(progress, "Processed")
    await expect(processed).toHaveText(/^\d+$/)
    const before = Number(await processed.textContent())
    await expect
      .poll(async () => Number(await processed.textContent()), {
        timeout: 30_000,
      })
      .toBeGreaterThan(before)
    await expect(progress).toContainText(
      /Last heartbeat \d+ s ago \(stale after 5 min\)\./
    )
    await expect(page.locator('[data-file="run.json"]')).toContainText(
      "Not written yet"
    )
    await page.goBack()
    await expect(page).toHaveTitle("Run Monitor · 3DMN Portal")

    // Stop the running job, with confirmation.
    await checkbox(page, first.job_id).check()
    await expect(selectedCount(page, 1)).toBeVisible()
    await button(page, "Stop").click()
    await expect(dialog).toContainText("Stop 1 run?")
    await expect(dialog.locator("li[data-job]")).toHaveText([
      new RegExp(`^Job ${String(first.job_id)}${label(first)}Running`),
    ])
    await dialog.getByRole("button", { name: "Stop Run" }).click()
    await expect(dialog).toHaveCount(0)
    await expect(toastTitled(page, "Stop Runs")).toContainText(
      `Job ${String(first.job_id)} (${label(first)}): stop requested.`
    )
    // It drains and ends Cancelled; the worker then takes the job moved to the front, not the older.
    await expect(status(page, first.job_id)).toHaveText("Cancelled", {
      timeout: 30_000,
    })
    await expect(button(page, "Stop")).toBeDisabled()
    await expect(status(page, third.job_id)).toHaveText(/^Running/, {
      timeout: 30_000,
    })
    await expect(status(page, second.job_id)).toHaveText("Queued")
    await checkbox(page, first.job_id).uncheck()

    // Stop the other two: one waiting, one running.
    await checkbox(page, second.job_id).check()
    await checkbox(page, third.job_id).check()
    await expect(selectedCount(page, 2)).toBeVisible()
    await button(page, "Stop").click()
    await expect(dialog).toContainText("Stop 2 runs?")
    await expect(dialog.locator("li[data-job]")).toHaveCount(2)
    await dialog.getByRole("button", { name: "Stop Runs" }).click()
    const stopped = toastTitled(page, "Stop Runs")
    await expect(
      stopped.locator(`[data-job="${String(second.job_id)}"]`)
    ).toHaveText(
      `Job ${String(second.job_id)} (${label(second)}) is cancelled; none of its inputs were processed.`
    )
    await expect(
      stopped.locator(`[data-job="${String(third.job_id)}"]`)
    ).toContainText(
      `Job ${String(third.job_id)} (${label(third)}): stop requested.`
    )
    await expect(status(page, second.job_id)).toHaveText("Cancelled")
    await expect(status(page, third.job_id)).toHaveText("Cancelled", {
      timeout: 30_000,
    })
    await expect(page.locator("body")).toHaveAttribute("data-loaded", "once")

    // The first job's page: its counters, its log tail and its failed inputs (those not run).
    await cell(page, first.job_id, "job_id").getByRole("link").click()
    await expect(page).toHaveTitle(`Job ${String(first.job_id)} · 3DMN Portal`)
    await expect(
      definition(page.getByRole("region", { name: "Job Details" }), "Status")
    ).toHaveText("Cancelled")
    const done = page.getByRole("region", { name: "Run Progress" })
    await expect(definition(done, "Inputs")).toHaveText("2000")
    const notRun = Number(await definition(done, "Not run").textContent())
    expect(notRun).toBeGreaterThan(0)
    await expect(done).toContainText(
      /Processed \d+ of 2000 inputs: \d+ succeeded, 0 failed, \d+ not run\./
    )
    // The log's last lines, as the API has them (a local run's run.json log is often empty).
    const response = await request.get(
      `/api/v1/jobs/${String(first.job_id)}/log?tail=50`
    )
    const { run_log: runLog } = (await response.json()) as JobLog
    expect(runLog.file.state).toBe("available")
    const log = page
      .getByRole("region", { name: "Run Configuration" })
      .getByRole("group", { name: "Run log" })
    if (runLog.lines.length === 0) {
      await expect(log).toHaveText(
        `Run logThe log is empty (${String(runLog.file.path)}).`
      )
    } else {
      await expect(log.locator("pre")).toHaveText(runLog.lines.join("\n"))
    }
    const failures = page.getByRole("region", { name: "Failed Inputs" })
    await expect(failures).toContainText(`Showing 1–50 of ${String(notRun)}`)
    await expect(failures.locator("tbody tr").first()).toContainText(
      "cancelled"
    )
    await failures.getByRole("button", { name: "Next" }).click()
    await expect(failures).toContainText(`Showing 51–100 of ${String(notRun)}`)
  })

  test("the seeded crux run shows its remote phase in its Status cell beside DM's state, first when sorted by Execution", async ({
    page,
    request,
  }) => {
    // Wide enough for every column (about 1410 px).
    await page.setViewportSize({ width: 1900, height: 800 })
    const job = await seededCruxJob(request)
    await page.goto("/runs")
    await expect(
      page.locator('.ag-row [col-id="job_id"]').first()
    ).toBeVisible()
    // Ascending, ALCF (the crux run) comes before APS.
    const header = page.locator(
      '.ag-header-cell[col-id="execution"] .ag-header-cell-label'
    )
    const sorted = page.waitForRequest((sent) =>
      new URL(sent.url()).searchParams.get("sort")?.startsWith("execution")
        ? true
        : false
    )
    await header.click()
    await sorted
    await expect(page.locator('.ag-row [col-id="job_id"]').first()).toHaveText(
      String(job.job_id)
    )
    // The Status cell shows the remote phase (not "Running 0/2"), its notes the tooltip; the seeded
    // observation is fixed in time, so delayed.
    const remote = status(page, job.job_id).locator("[data-remote-status]")
    await expect(remote).toHaveText("Running")
    await expect(remote).toHaveAttribute(
      "title",
      "Processing · PBS 123456 · status delayed"
    )
    await expect(cell(page, job.job_id, "dm_state")).toHaveText("running")
    await expect(cell(page, job.job_id, "execution")).toHaveText("ALCF")
  })
})

/** Job 12 running locally, 11 queued, 10 running on APS (DM state running), 9 to 1 ended. */
function jobs(): JobSummary[] {
  return [
    makeJobSummary(12, {
      status: "running",
      phase: "running",
      n_succeeded: 120,
      n_failed: 3,
      n_processed: 123,
      duration_seconds: 754,
    }),
    makeJobSummary(11, {
      status: "queued",
      phase: "queued",
      start_time: null,
      duration_seconds: null,
      n_succeeded: 0,
      n_processed: 0,
      message: "Queued on laue-run-1",
    }),
    makeJobSummary(10, {
      status: "running",
      phase: "running",
      run: { kind: "reconstruction", id: 4, label: "R4" },
      target: "aps",
      dm_state: "running",
      n_succeeded: 0,
      n_processed: 0,
    }),
    ...Array.from({ length: 9 }, (_, index) =>
      makeJobSummary(
        9 - index,
        index === 2
          ? {
              status: "failed",
              phase: "interrupted",
              n_succeeded: 40,
              n_failed: 0,
              n_not_run: 401,
              n_processed: 40,
            }
          : {}
      )
    ),
  ]
}

async function open(page: Page) {
  await mockStatus(page, () => STATUS_OK)
  await page.goto("/runs")
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

test.describe("with the jobs answered by the test", () => {
  test("lists every job newest first with its columns, where it computes and DM's state", async ({
    page,
  }) => {
    // Wide enough for every column (about 1410 px): none is pinned, so they keep the page's order
    // (the Job ID first, as in every list).
    await page.setViewportSize({ width: 1900, height: 800 })
    const api = await mockJobList(page, jobs())
    await open(page)
    await expect(
      page.getByRole("heading", { name: "Run Monitor" })
    ).toBeVisible()
    await expect(
      page.locator(
        '.ag-header-cell:not([col-id="ag-Grid-SelectionColumn"]) .ag-header-cell-text'
      )
    ).toHaveText([
      "Job ID",
      "Run",
      "Author",
      "Status",
      "Run Progress",
      "Start Time",
      "Messages",
      "Duration",
      "Scan ID",
      "Execution",
      "DM State",
    ])
    expect(api.requests[0]?.url.search).toBe("?offset=0&limit=100&sort=-job_id")
    await expect(page.locator('.ag-row [col-id="job_id"]').first()).toHaveText(
      "12"
    )
    await expect(status(page, 12)).toHaveText("Running 123/441")
    await expect(status(page, 11)).toHaveText("Queued")
    await expect(cell(page, 12, "n_processed")).toContainText(
      "123/441 processed, 3 failed"
    )
    await expect(cell(page, 12, "duration_seconds")).toHaveText(
      "00:12:34 (running)"
    )
    await expect(cell(page, 11, "message")).toHaveText("Queued on laue-run-1")
    // Where each run computes, by the API's name, shown as it is.
    await expect(cell(page, 10, "execution")).toHaveText("APS")
    await expect(cell(page, 10, "dm_state")).toHaveText("running")
    await expect(cell(page, 10, "run")).toHaveText("R4")
    // Job 12 started 2026-10-02 02:00 UTC: 21:00 the day before in Chicago (CDT).
    await expect(cell(page, 12, "start_time")).toHaveText("2026-10-01 21:00:00")
    // The cells' text can be selected and copied (AG Grid's enableCellTextSelection).
    expect(
      await cell(page, 11, "message").evaluate(
        (element) => getComputedStyle(element).userSelect
      )
    ).toBe("text")

    // Each job links to its page, its run and its scan.
    await cell(page, 10, "run").getByRole("link", { name: "R4" }).click()
    await expect(page).toHaveURL(/\/reconstructions\/4$/)
    await page.goBack()
    await cell(page, 12, "scan_number")
      .getByRole("link", { name: "990012" })
      .click()
    await expect(page).toHaveURL(/\/scans\/990012$/)
    await page.goBack()
    await cell(page, 12, "job_id").getByRole("link", { name: "12" }).click()
    await expect(page).toHaveURL(/\/jobs\/12$/)
  })

  test("live refresh follows the jobs and the buttons follow the selected ones", async ({
    page,
  }) => {
    const rows = jobs()
    const api = await mockJobList(page, rows)
    await page.clock.install()
    await open(page)
    await expect(status(page, 11)).toHaveText("Queued")
    await markDocument(page)
    await checkbox(page, 11).check()
    await expect(selectedCount(page, 1)).toBeVisible()
    await expect(button(page, "Stop")).toBeEnabled()
    await expect(button(page, "Move to Front")).toBeEnabled()

    // Job 11 starts: Move to Front goes off, Stop stays on.
    api.update(
      rows.map((job) =>
        job.job_id === 11
          ? {
              ...job,
              status: "running" as const,
              phase: "running",
              start_time: "2026-10-20T13:00:00Z",
            }
          : job
      )
    )
    await advanceUntil(page, 5_000, () =>
      expect(status(page, 11)).toHaveText(/^Running/, SHORT)
    )
    await expect(button(page, "Move to Front")).toBeDisabled()
    await expect(button(page, "Stop")).toBeEnabled()
    await expect(checkbox(page, 11)).toBeChecked()
    await expect(page.locator("body")).toHaveAttribute("data-loaded", "once")
  })

  test("Stop says what happened to each job", async ({ page }) => {
    const api = await mockJobList(page, jobs(), (jobId) => ({
      status: 200,
      json:
        jobId === 11
          ? {
              job_id: 11,
              state: "cancelled",
              n_pending: 441,
            }
          : jobId === 10
            ? {
                job_id: 10,
                state: "requested",
                n_pending: 441,
              }
            : {
                job_id: jobId,
                state: "already_terminal",
                n_pending: 0,
              },
    }))
    await open(page)
    await expect(status(page, 10)).toHaveText(/^Running/)
    for (const id of [11, 10, 9]) await checkbox(page, id).check()
    await expect(selectedCount(page, 3)).toBeVisible()
    await button(page, "Stop").click()
    const dialog = page.getByRole("alertdialog")
    await expect(dialog).toContainText("Stop 2 runs?")
    await expect(dialog.locator("li[data-job]")).toHaveText([
      "Job 10R4on APSRunning",
      "Job 11I42Queued",
    ])
    await expect(dialog).toContainText(
      "A remote run's stop is delivered to its scheduler by the remote poller."
    )
    await expect(dialog).toContainText(
      "1 selected run has ended and is left alone."
    )
    // Cancel asks nothing.
    await dialog.getByRole("button", { name: "Cancel" }).click()
    await expect(dialog).toHaveCount(0)
    expect(api.requests.filter((r) => r.method === "POST")).toHaveLength(0)

    await button(page, "Stop").click()
    await dialog.getByRole("button", { name: "Stop Runs" }).click()
    const toast = toastTitled(page, "Stop Runs")
    await expect(toast.locator("[data-job]")).toHaveText([
      "Job 10 (R4): stop requested; the remote poller delivers it to APS. Completed results are kept; up to 441 pending inputs will be recorded as not run.",
      "Job 11 (I42) is cancelled; none of its inputs were processed.",
    ])
    expect(
      api.requests.filter((r) => r.method === "POST").map((r) => r.url.pathname)
    ).toEqual(["/api/v1/jobs/10/cancel", "/api/v1/jobs/11/cancel"])
  })

  test("Move to Front says why a job did not move: not queued, remote", async ({
    page,
  }) => {
    const queued = [13, 14, 15].map((id) =>
      makeJobSummary(id, {
        status: "queued",
        phase: "queued",
        ...(id === 14
          ? { target: "crux", execution: "ALCF", dm_state: "pending" }
          : {}),
      })
    )
    // The API's answers (jobs/service.py).
    const reasons: Record<number, ProblemAnswer> = {
      13: problem(
        409,
        "Job not queued",
        "Job 13 is not waiting in the queue (phase running): only a queued job moves"
      ),
      14: problem(
        409,
        "Job runs remotely",
        "Job 14 runs on a remote target, whose scheduler orders it: it cannot be moved to the front"
      ),
    }
    await mockJobList(
      page,
      queued,
      (jobId) => reasons[jobId] ?? { status: 204 }
    )
    await open(page)
    await expect(status(page, 15)).toHaveText("Queued")
    for (const id of [13, 14, 15]) await checkbox(page, id).check()
    await expect(selectedCount(page, 3)).toBeVisible()
    await button(page, "Move to Front").click()
    const dialog = page.getByRole("alertdialog")
    await expect(dialog).toContainText(
      "Remote runs are ordered by their scheduler: they do not move."
    )
    await dialog.getByRole("button", { name: "Move to Front" }).click()
    await expect(
      toastTitled(page, "Move to Front").locator("[data-job]")
    ).toHaveText([
      "Job 13 (I44) was not moved: it is no longer waiting in the queue.",
      "Job 14 (I45) was not moved: it runs on CRUX, whose scheduler orders it.",
      "Job 15 (I46) moved to the front of the queue.",
    ])
  })

  test("at 900 px, Job ID and Status stay in view while the middle columns scroll", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 900, height: 700 })
    await mockJobList(page, jobs())
    await open(page)
    await expect(status(page, 12)).toHaveText("Running 123/441")
    await expect
      .poll(() => pinnedEdges(page))
      .toEqual({ left: "job_id", right: "status" })
    const overflow = await page.evaluate(
      () =>
        document.documentElement.scrollWidth -
        document.documentElement.clientWidth
    )
    expect(overflow).toBe(0)
    // The page's buttons fit on the title's row.
    const title = await page
      .getByRole("heading", { name: "Run Monitor" })
      .boundingBox()
    const refresh = await button(page, "Refresh").boundingBox()
    expect(
      Math.abs(
        (title?.y ?? 0) +
          (title?.height ?? 0) / 2 -
          ((refresh?.y ?? 0) + (refresh?.height ?? 0) / 2)
      )
    ).toBeLessThan(8)
  })
})
