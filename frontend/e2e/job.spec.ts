import { expect, test, type Locator, type Page } from "@playwright/test"

import type { JobDetail } from "@/api"
import {
  emptyFailurePage,
  endedJob,
  failurePage,
  makeConfiguration,
  makeFailure,
  makeJobDetail,
  makeCruxJob,
  makeJobLog,
  makeRunFile,
} from "../src/test/jobs"
import { advanceUntil, SHORT } from "./clock"
import { mockJob } from "./jobs-api"
import { internalError } from "./problems"
import { mockStatus, STATUS_OK } from "./runs-api"
import { seededCruxJob } from "./stack-tools"

// The job page with its job, failure report and log answered by the test (e2e/jobs-api.ts), for what the seeded demo
// database cannot show: a running job followed every 2 s (on Playwright's fake clock, e2e/clock.ts), a remote run, a
// migrated run with its history and files not on this host, a failed run's report, 900 px. e2e/run-monitor.spec.ts
// shows a stopped demo job's page on the stack; an unknown job and the demo's run on CRUX (its remote phase) are asked
// of the stack itself.

test.use({ timezoneId: "America/Chicago" })

function region(page: Page, name: string) {
  return page.getByRole("region", { name })
}

function definition(root: Locator, term: string) {
  return root.locator(`div:has(> dt:text-is("${term}")) > dd`).first()
}

/** Mark the document: a reload would lose the mark. */
async function markDocument(page: Page) {
  await page.evaluate(() => {
    document.body.dataset.loaded = "once"
  })
}

async function open(page: Page, job: JobDetail) {
  await mockStatus(page, () => STATUS_OK)
  await page.goto(`/jobs/${String(job.job_id)}`)
  await expect(page).toHaveTitle(`Job ${String(job.job_id)} · 3DMN Portal`)
}

test("a running job is followed every 2 s without a reload, until it ends", async ({
  page,
}) => {
  const running = makeJobDetail()
  const api = await mockJob(page, running, {
    // While it runs, its failure report is not recorded yet.
    failures: () =>
      emptyFailurePage(
        3,
        makeRunFile("failures.jsonl", { state: "not_recorded", path: null })
      ),
    log: makeJobLog(41, [], {
      run_log: {
        lines: [],
        truncated: false,
        n_lines: null,
        file: makeRunFile("run.json", { state: "missing" }),
      },
    }),
  })
  await page.clock.install()
  await open(page, running)
  await markDocument(page)
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Job 41")
  await expect(page.getByRole("link", { name: "I72" })).toHaveAttribute(
    "href",
    "/indexing/72"
  )
  await expect(page.getByRole("link", { name: "990012" })).toHaveAttribute(
    "href",
    "/scans/990012"
  )

  const details = region(page, "Job Details")
  await expect(definition(details, "Status")).toHaveText("Running 123/441")
  await expect(definition(details, "Computer")).toHaveText("compute-host")
  // Submitted 2026-10-19 14:58:20 UTC: 09:58:20 in Chicago (CDT).
  await expect(definition(details, "Submit Time")).toHaveText(
    "2026-10-19 09:58:20"
  )
  await expect(definition(details, "Duration")).toHaveText("00:12:34 (running)")
  await expect(details).not.toContainText("Priority")

  const progress = region(page, "Run Progress")
  await expect(progress.locator("dt")).toHaveText([
    "Inputs",
    "Processed",
    "Succeeded",
    "Failed",
    "Not run",
    "Pending",
  ])
  await expect(progress.locator("dd")).toHaveText([
    "441",
    "123",
    "120",
    "3",
    "0",
    "318",
  ])
  await expect(progress.getByRole("progressbar")).toHaveAttribute(
    "aria-valuetext",
    "123/441 processed, 3 failed"
  )
  await expect(progress).toContainText(
    "Last heartbeat 4 s ago (stale after 5 min)."
  )
  await expect(page.locator('[data-file="run.json"]')).toContainText(
    "Not written yet"
  )
  await expect(region(page, "Failed Inputs")).toContainText(
    "3 inputs failed so far: the failure report is written when the run ends."
  )

  // It progresses, a stop is requested, it goes on finalizing: each poll shows it.
  api.update({
    ...running,
    duration_seconds: 800,
    progress: {
      ...running.progress,
      n_processed: 300,
      n_succeeded: 297,
      n_pending: 141,
      heartbeat_age_seconds: 1,
      stop_requested: true,
      stop_requested_at: "2026-10-19T15:13:00Z",
    },
  })
  await advanceUntil(page, 2_000, () =>
    expect(progress.locator("dd").nth(1)).toHaveText("300", SHORT)
  )
  await expect(progress).toContainText("Stop requested at 2026-10-19 10:13:00.")
  api.update({ ...running, phase: "finalizing" })
  await advanceUntil(page, 2_000, () =>
    expect(details.getByTestId("phase-note")).toHaveText("finalizing", SHORT)
  )

  // It ends: the log and the failure report are read again, and polling stops.
  const ended = endedJob(running, "cancelled", {
    succeeded: 297,
    failed: 3,
    notRun: 141,
  })
  api.updateLog(
    makeJobLog(41, [
      "admission stopped: cancel requested",
      "results written: 297 frames",
    ])
  )
  api.update(ended)
  await advanceUntil(page, 2_000, () =>
    expect(definition(details, "Status")).toHaveText("Cancelled", SHORT)
  )
  await expect(
    region(page, "Run Configuration")
      .getByRole("group", { name: "Run log" })
      .locator("pre")
  ).toHaveText(
    "admission stopped: cancel requested\nresults written: 297 frames"
  )
  await expect(page.getByRole("button", { name: "Stop Run" })).toBeDisabled()
  const polls = () =>
    api.requests.filter((url) => url.pathname === "/api/v1/jobs/41").length
  const settled = polls()
  // Five 2 s polls' worth of time, every timer fired; then a moment for a request it sent to arrive.
  await page.clock.runFor(10_000)
  await page.waitForTimeout(500)
  expect(polls()).toBe(settled)
  await expect(page.locator("body")).toHaveAttribute("data-loaded", "once")
})

test("Stop Run asks first, and the toast says the queued run is cancelled", async ({
  page,
}) => {
  const queued = makeJobDetail({
    status: "queued",
    phase: "queued",
    start_time: null,
    duration_seconds: null,
    progress: {
      ...makeJobDetail().progress,
      n_processed: 0,
      n_succeeded: 0,
      n_failed: 0,
      n_pending: 441,
      heartbeat_age_seconds: null,
    },
  })
  await mockJob(page, queued)
  const posts: string[] = []
  await page.route("**/api/v1/jobs/41/cancel", (route) => {
    posts.push(route.request().method())
    return route.fulfill({
      json: {
        job_id: 41,
        state: "cancelled",
        n_pending: 441,
      },
    })
  })
  await open(page, queued)
  await expect(region(page, "Run Progress")).not.toContainText("heartbeat")
  await page.getByRole("button", { name: "Stop Run" }).click()
  const dialog = page.getByRole("alertdialog")
  await expect(dialog).toContainText("Stop job 41?")
  await expect(dialog).toContainText("A queued run leaves the queue")
  await dialog.getByRole("button", { name: "Cancel" }).click()
  await expect(dialog).toHaveCount(0)
  expect(posts).toEqual([])
  await page.getByRole("button", { name: "Stop Run" }).click()
  await dialog.getByRole("button", { name: "Stop Run" }).click()
  const toast = page.locator("[data-sonner-toast]").first()
  await expect(toast.locator("[data-title]")).toHaveText("Stop Run")
  await expect(toast).toContainText(
    "Job 41 (I72) is cancelled; none of its inputs were processed."
  )
  expect(posts).toEqual(["POST"])
})

test("a remote run shows its target, DM's state and its Globus run, and its PBS output", async ({
  page,
}) => {
  // A crux run without an observation yet: DM's state.
  const remote = makeCruxJob(null)
  await mockJob(page, remote, {
    log: makeJobLog(40, [], {
      run_log: {
        lines: [],
        truncated: false,
        n_lines: null,
        file: makeRunFile("run.json", { state: "missing" }),
      },
      pbs_output: {
        lines: Array.from(
          { length: 50 },
          (_, i) => `rank 0: point ${String(i + 1)} reconstructed`
        ),
        truncated: true,
        n_lines: null,
        file: makeRunFile("pbs-output.log", {
          path: "/net/fileserver/export/data1/runs/rec_12/incoming/7d3c/pbs-output.log",
        }),
      },
    }),
  })
  await open(page, remote)
  await expect(page.getByRole("link", { name: "R12" })).toHaveAttribute(
    "href",
    "/reconstructions/12"
  )
  const card = region(page, "Remote Run")
  await expect(definition(card, "Target")).toHaveText("ALCF via DM")
  await expect(card.locator("dt")).toHaveText([
    "Target",
    "DM state",
    "Globus run",
    "Last polled",
  ])
  await expect(definition(card, "DM state")).toHaveText("running")
  await expect(definition(card, "Last polled")).toHaveText("25 s ago")
  const globus = card.getByRole("link", { name: /Globus run/ })
  await expect(globus).toHaveAttribute(
    "href",
    "https://app.globus.org/runs/0c5e7a12-1d3b-4f6a-8e2c-9b7d5f3a1e60"
  )
  await expect(globus).toHaveAttribute("target", "_blank")
  await expect(globus).toHaveAttribute("rel", "noopener noreferrer")
  // No heartbeat check for a remote run.
  await expect(region(page, "Run Progress")).not.toContainText("heartbeat")
  const pbs = region(page, "Run Configuration").getByRole("group", {
    name: "PBS output",
  })
  await expect(pbs.locator("h3")).toHaveText("PBS output (last 50 lines)")
  await expect(pbs.locator("pre")).toContainText(
    "rank 0: point 50 reconstructed"
  )
})

test("a migrated run shows its history and the files that are not on this host", async ({
  page,
}) => {
  const base = endedJob(
    makeJobDetail({
      job_id: 30,
      run: { kind: "indexing", id: 61, label: "I61" },
      computer_name: "analysis-host",
      messages: "Migrated from v1's per-input records (21 Finished, 6 Failed)",
    }),
    "failed",
    { succeeded: 435, failed: 6, notRun: 0 }
  )
  const migrated: JobDetail = {
    ...base,
    configuration: makeConfiguration({
      run_directory: {
        path: "/net/fileserver/export/data1/runs/scan_990001/index_61",
        state: "missing",
        detail: null,
      },
      files: [
        makeRunFile("request.json", { state: "not_recorded", path: null }),
        makeRunFile("run.json", {
          state: "missing",
          path: "/net/fileserver/export/data1/runs/scan_990001/index_61/run.json",
        }),
        makeRunFile("history.json", {
          path: "/data/laue/laueportal.db.migration/job_30/history.json",
        }),
      ],
      request: null,
      history: {
        original_status_name: "Failed",
        first_input_at: "2025-03-02 10:00:01.123456",
        last_input_at: "2025-03-02 11:40:12.654321",
        message_classes: [
          { message: "No peaks above the threshold", count: 4 },
          { message: "Indexing found too few peaks", count: 2 },
        ],
      },
      artifacts: [
        {
          name: "results",
          path: "/net/fileserver/export/data1/runs/scan_990001/index_61/output.h5",
          viewer: "indexing_results",
        },
      ],
    }),
  }
  await mockJob(page, migrated, {
    failures: (offset, limit) =>
      failurePage(
        6,
        offset,
        limit,
        (index) =>
          makeFailure(index, {
            index: null,
            input_id: `Ni_${String(index + 7)}`,
            category: "error",
            error_type: null,
          }),
        makeRunFile("failures.jsonl", {
          path: "/data/laue/laueportal.db.migration/job_30/failures.jsonl",
        })
      ),
  })
  await open(page, migrated)
  const configuration = region(page, "Run Configuration")
  await expect(
    configuration.locator('[data-file="run directory"]')
  ).toContainText("Not available on this host")
  await expect(
    configuration.locator('[data-file="request.json"]')
  ).toContainText("Not recorded")
  await expect(configuration.locator('[data-file="run.json"]')).toContainText(
    "Not available on this host"
  )
  await expect(
    configuration.locator('[data-file="history.json"]')
  ).toContainText("Available")
  // Its history is what it has (the note on runs without files shows only without one).
  await expect(configuration).not.toContainText(
    "No request or run summary recorded."
  )
  const history = configuration.getByRole("group", {
    name: "Historical per-input records",
  })
  await expect(history.locator("dt")).toHaveText([
    "recorded status",
    "per-input work",
    "4 inputs",
    "2 inputs",
  ])
  await expect(history.locator("dd")).toHaveText([
    "Failed",
    "2025-03-02 10:00:01.123456 to 2025-03-02 11:40:12.654321",
    "No peaks above the threshold",
    "Indexing found too few peaks",
  ])
  // Its results link to the indexing's viewer.
  await expect(
    configuration
      .locator('[data-artifact="results"]')
      .getByRole("link", { name: "View I61" })
  ).toHaveAttribute("href", "/indexing/61")
  const failures = region(page, "Failed Inputs")
  await expect(failures).toContainText("Showing 1–6 of 6")
  // No position nor error type: a dash, read "none" by screen readers.
  await expect(failures.locator("tbody tr").first().locator("td")).toHaveText([
    "—none",
    "Ni_7",
    "error",
    "—none",
    "No pattern indexed within the angle tolerance (0)",
  ])
})

test("a failed run pages its failed inputs on the server, with its notes and log tail", async ({
  page,
}) => {
  const failed = endedJob(makeJobDetail(), "failed", {
    succeeded: 321,
    failed: 120,
    notRun: 0,
  })
  const job: JobDetail = {
    ...failed,
    configuration: {
      ...failed.configuration,
      validation_error: "output.h5 has no frames group",
      warnings: ["XML export failed: disk quota exceeded"],
    },
  }
  const categories = ["numerical", "input", "memory"]
  const api = await mockJob(page, job, {
    failures: (offset, limit) =>
      failurePage(120, offset, limit, (index) =>
        makeFailure(index, {
          category: categories[index % 3] ?? "error",
          error_type: index % 3 === 1 ? "OSError" : "IndexingError",
        })
      ),
    log: makeJobLog(
      41,
      Array.from({ length: 50 }, (_, i) => `frame ${String(i + 71)} indexed`),
      {
        run_log: {
          lines: Array.from(
            { length: 50 },
            (_, i) => `frame ${String(i + 71)} indexed`
          ),
          truncated: true,
          n_lines: 120,
          file: makeRunFile("run.json"),
        },
      }
    ),
  })
  await open(page, job)
  const configuration = region(page, "Run Configuration")
  await expect(
    configuration.locator('[role="note"][data-tone="error"]')
  ).toHaveText("Output validation failed: output.h5 has no frames group")
  await expect(
    configuration.locator('[role="note"][data-tone="warning"]')
  ).toHaveText("XML export failed: disk quota exceeded")
  await expect(
    configuration.locator('[role="note"][data-tone="info"]')
  ).toContainText("Partial results: the inputs that succeeded.")
  await expect(
    configuration.getByRole("group", { name: "Run log" }).locator("h3")
  ).toHaveText("Run log (last 50 of 120 lines)")

  const failures = region(page, "Failed Inputs")
  await expect(failures).toContainText("Showing 1–50 of 120")
  await expect(failures.locator("thead th")).toHaveText([
    "#",
    "Input",
    "Category",
    "Error",
    "Message",
  ])
  await expect(failures.locator("tbody tr")).toHaveCount(50)
  await expect(
    failures.locator("tbody tr").nth(1).locator('[data-tone="warning"]')
  ).toHaveText("input")
  await expect(
    failures.locator("tbody tr").nth(2).locator('[data-tone="error"]')
  ).toHaveText("memory")
  await failures.getByRole("button", { name: "Next" }).click()
  await expect(failures).toContainText("Showing 51–100 of 120")
  await expect(
    failures.locator("tbody tr").first().locator("td").first()
  ).toHaveText("50")
  await failures.getByRole("button", { name: "Next" }).click()
  await expect(failures).toContainText("Showing 101–120 of 120")
  await expect(failures.getByRole("button", { name: "Next" })).toBeDisabled()
  await failures.getByRole("button", { name: "Previous" }).click()
  await expect(failures).toContainText("Showing 51–100 of 120")
  expect(
    api.requests
      .filter((url) => url.pathname.endsWith("/failures"))
      .map((url) => url.search)
  ).toEqual([
    "?offset=0&limit=50",
    "?offset=50&limit=50",
    "?offset=100&limit=50",
  ])
})

test("at 900 px the job page fits the window", async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 800 })
  const failed = endedJob(makeJobDetail(), "failed", {
    succeeded: 321,
    failed: 120,
    notRun: 0,
  })
  await mockJob(page, failed, {
    failures: (offset, limit) => failurePage(120, offset, limit),
  })
  await open(page, failed)
  await expect(region(page, "Failed Inputs")).toContainText(
    "Showing 1–50 of 120"
  )
  const overflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth
  )
  expect(overflow).toBe(0)
  await expect(page.getByRole("button", { name: "Refresh" })).toBeInViewport()
  await expect(page.getByRole("button", { name: "Stop Run" })).toBeInViewport()
})

test("the seeded crux run shows its remote phase: the golden observation, delayed", async ({
  page,
  request,
}) => {
  const job = await seededCruxJob(request)
  await page.goto(`/jobs/${String(job.job_id)}`)
  const card = region(page, "Remote Run")
  await expect(definition(card, "Target")).toHaveText("ALCF via DM")
  const phase = definition(card, "Phase")
  await expect(phase.locator("[data-phase]")).toHaveText("Running")
  await expect(phase).toHaveText(
    "RunningProcessing · PBS 123456 · status delayed"
  )
  // Observed at the golden's fixed time, 12:00 UTC (07:00 in Chicago), so long ago.
  await expect(
    phase.getByText("status delayed", { exact: true })
  ).toHaveAttribute("title", "Observed 2026-10-25 07:00:00")
  await expect(definition(card, "DM state")).toHaveText("running")
  await expect(card.getByRole("link", { name: /Globus run/ })).toHaveAttribute(
    "href",
    "https://app.globus.org/runs/0c5e7a12-1d3b-4f6a-8e2c-9b7d5f3a1e60"
  )
})

test("an unknown job is a not-found page", async ({ page }) => {
  await page.goto("/jobs/987654321")
  await expect(
    page.getByRole("heading", { name: "Job 987654321 not found" })
  ).toBeVisible()
  await expect(page).toHaveTitle("Page not found · 3DMN Portal")
  await page.getByRole("link", { name: "Go to the run monitor" }).click()
  await expect(page).toHaveURL(/\/runs$/)
})

test("a job that cannot be read shows why and keeps its page", async ({
  page,
}) => {
  const job = makeJobDetail()
  const INSTANCE = "urn:uuid:0d3f6c1a-8b2e-4c57-9e41-7a5b2c8d9f03"
  let fail = false
  await mockStatus(page, () => STATUS_OK)
  await page.route(
    /\/api\/v1\/jobs\/41(\/failures|\/log)?(\?.*)?$/,
    (route) => {
      const url = new URL(route.request().url())
      if (url.pathname.endsWith("/failures"))
        return route.fulfill({ json: failurePage(3, 0, 50) })
      if (url.pathname.endsWith("/log"))
        return route.fulfill({ json: makeJobLog(41) })
      if (fail) {
        return route.fulfill(internalError(INSTANCE))
      }
      return route.fulfill({ json: job })
    }
  )
  await page.clock.install()
  await page.goto("/jobs/41")
  await expect(definition(region(page, "Job Details"), "Status")).toHaveText(
    "Running 123/441"
  )
  fail = true
  // The next polls fail (after their retries): the last job read stays, with the error above it.
  await advanceUntil(page, 1_000, () =>
    expect(page.getByRole("alert")).toContainText(
      "Internal Server Error",
      SHORT
    )
  )
  await expect(page.getByRole("alert")).toContainText(
    "Showing the last data loaded."
  )
  // The detail names the reference; it is not repeated.
  await expect(page.getByRole("alert")).toContainText(
    `The server log has the details under ${INSTANCE}.`
  )
  await expect(page.getByRole("alert")).not.toContainText("Reference")
  await expect(definition(region(page, "Job Details"), "Status")).toHaveText(
    "Running 123/441"
  )
  // The API answers again: the next poll takes the error away.
  fail = false
  await advanceUntil(page, 1_000, () =>
    expect(page.getByRole("alert")).toHaveCount(0, SHORT)
  )
})
