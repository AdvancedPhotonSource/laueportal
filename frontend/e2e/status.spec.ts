import { expect, test, type Locator, type Page } from "@playwright/test"

import type { ActiveRun, SystemStatus } from "@/api"
import { makeJobDetail } from "../src/test/jobs"
import { makeWorker } from "../src/test/system"
import { advanceUntil, SHORT } from "./clock"
import { mockJob } from "./jobs-api"
import { ACTIVE_RUNS, mockStatus, STATUS_OK } from "./runs-api"

// The status page with GET /api/v1/system/status mocked (e2e/runs-api.ts): the cards, the worker's and its
// threads' states, the queue's next items, the active runs' badges and links, the database down, an unreachable
// API, and the 5 s poll (on Playwright's fake clock, e2e/clock.ts). e2e/stack.spec.ts checks the page against
// the real stack.

/** A running local run whose heartbeat is lost and whose stop was requested (job 38, R11). */
const STALE_RUN: ActiveRun = {
  job_id: 38,
  run: { kind: "reconstruction", id: 11, label: "R11" },
  status: "running",
  phase: "running",
  n_inputs: 441,
  n_succeeded: 97,
  n_failed: 0,
  n_not_run: 0,
  heartbeat_age_seconds: 905,
  stale: true,
  stop_requested: true,
  remote: null,
}

function region(page: Page, name: string) {
  return page.getByRole("region", { name })
}

/** A term's (first) definition in a card: its `dd` after the `dt` ("Queued" and 1). */
function definition(card: Locator, term: string) {
  return card.locator(`div:has(> dt:text-is("${term}")) > dd`).first()
}

/** A term's state badge in a card ("Database" and OK, "Dispatcher" and OK). */
function state(card: Locator, term: string) {
  return definition(card, term).locator("[data-tone]")
}

/** An active run's row in the Active Runs card, by its job. */
function runRow(page: Page, job: number) {
  return region(page, "Active Runs").locator(`tr[data-job="${String(job)}"]`)
}

test("the status page shows the connection, the worker, the queue and the quick actions", async ({
  page,
}) => {
  await mockStatus(page, () => STATUS_OK)
  await page.goto("/")
  await expect(page).toHaveTitle("Status · 3DMN Portal")

  const connection = region(page, "Connection")
  await expect(state(connection, "Database")).toHaveText("OK")
  await expect(connection).toContainText("/home/user/portal/laueportal.sqlite")
  await expect(state(connection, "API")).toHaveText("OK")
  await expect(connection).toContainText("Version 2.0.0.dev0")
  await expect(page.locator("body")).not.toContainText(/redis/i)

  // The worker: where and since when it runs, and its threads with their last marks.
  const worker = region(page, "Worker")
  await expect(worker.locator("[data-tone]").first()).toHaveText("Running")
  await expect(definition(worker, "Host")).toHaveText("beamline-host")
  await expect(definition(worker, "PID")).toHaveText("4321")
  await expect(definition(worker, "Started")).toHaveText(
    /^2026-10-2[45] \d\d:00:00$/
  )
  await expect(state(worker, "Dispatcher")).toHaveText("OK")
  await expect(definition(worker, "Dispatcher")).toContainText("4 s ago")
  await expect(state(worker, "Reconciler")).toHaveText("OK")
  await expect(state(worker, "Reconciler")).toHaveAttribute(
    "title",
    "Late after 3 min"
  )
  await expect(state(worker, "Remote poller")).toHaveText("Off")
  await expect(definition(worker, "Remote poller")).toContainText("None")

  // The queue: its counts and the next items in the dispatcher's order.
  const queue = region(page, "Queue")
  await expect(definition(queue, "Queued runs")).toHaveText("1")
  await expect(definition(queue, "Running")).toHaveText("1")
  await expect(definition(queue, "Queued conversions")).toHaveText("1")
  await expect(definition(queue, "Live slots")).toHaveText("1/2")
  const next = queue.locator("[data-item]")
  await expect(next).toHaveCount(2)
  await expect(next.nth(0)).toHaveText("1I70Conversion")
  await expect(next.nth(1)).toHaveText("2I73")
  await expect(next.nth(1).getByRole("link")).toHaveAttribute(
    "href",
    "/indexing/73"
  )
  await expect(page.getByRole("alert")).toHaveCount(0)

  const actions = region(page, "Quick Actions").getByRole("link")
  await expect(actions).toHaveText([
    "Scans",
    "Import Scans",
    "New Wire Reconstruction",
    "New LaueGo Indexation",
    "Run Monitor",
  ])
  const hrefs = await actions.evaluateAll((links) =>
    links.map((link) => link.getAttribute("href"))
  )
  expect(hrefs).toEqual([
    "/scans",
    "/scans/import",
    "/reconstructions/new",
    "/indexing/new",
    "/runs",
  ])
  await actions.filter({ hasText: "New LaueGo Indexation" }).click()
  await expect(page).toHaveURL(/\/indexing\/new$/)
  await expect(page).toHaveTitle("New LaueGo Indexation · 3DMN Portal")
})

test("the active runs show their links, heartbeat, target and the stale and stop-requested badges", async ({
  page,
}) => {
  await mockStatus(page, () => ({
    ...STATUS_OK,
    active_runs: [...ACTIVE_RUNS, STALE_RUN],
  }))
  // Job 38's page (the seeded stack has no job 38).
  await mockJob(
    page,
    makeJobDetail({
      job_id: 38,
      run: { kind: "reconstruction", id: 11, label: "R11" },
    })
  )
  await page.goto("/")
  const card = region(page, "Active Runs")
  await expect(card).toContainText("3 queued or running")
  await expect(card.locator("thead th")).toHaveText([
    "Job",
    "Run",
    "Status",
    "Progress",
    "Heartbeat",
    "Target",
  ])

  // A local run: its heartbeat, no badges.
  const local = runRow(page, 41)
  await expect(local).toContainText("Running 123/441")
  await expect(local.getByRole("progressbar")).toHaveAttribute(
    "aria-valuetext",
    "123/441 processed, 3 failed"
  )
  await expect(local).toContainText("4 s ago")
  await expect(local).toContainText("Local")
  await expect(local).not.toContainText("Stale")
  await expect(local).not.toContainText("Stop requested")

  // A remote run: the poller's heartbeat, the target and DM's state; never stale.
  const remote = runRow(page, 40)
  await expect(remote).toContainText("Running 300/441")
  await expect(remote).toContainText("Polled 25 s ago")
  await expect(remote).toContainText("apsDM: running")

  // A stale local run whose stop was requested: both badges, and what stale means in its tooltip.
  const stale = runRow(page, 38)
  await expect(stale).toContainText("Stop requested")
  await expect(stale).toContainText("Stale")
  await expect(stale).toContainText("15 min 5 s ago")
  await expect(stale.getByText("Stale", { exact: true })).toHaveAttribute(
    "title",
    /^No recent heartbeat/
  )

  // Each row links to its job and its run.
  await stale.getByRole("link", { name: "38", exact: true }).click()
  await expect(page).toHaveURL(/\/jobs\/38$/)
  await expect(page).toHaveTitle("Job 38 · 3DMN Portal")
  await page.goBack()
  await runRow(page, 38).getByRole("link", { name: "R11" }).click()
  await expect(page).toHaveURL(/\/reconstructions\/11$/)
  await page.goBack()
  await runRow(page, 41).getByRole("link", { name: "I72" }).click()
  await expect(page).toHaveURL(/\/indexing\/72$/)
})

test("a worker not running or late is shown clearly", async ({ page }) => {
  let status: SystemStatus = {
    ...STATUS_OK,
    worker: makeWorker({
      state: "not_running",
      stopped_at: "2026-10-25T09:00:00Z",
      threads: makeWorker().threads.map((thread) => ({
        ...thread,
        state: "off",
        late_after_seconds: null,
      })),
    }),
  }
  await mockStatus(page, () => status)
  const indicator = page
    .getByRole("group", { name: "Live system status" })
    .getByRole("link")
  await page.goto("/")
  const alert = page.getByRole("alert")
  await expect(alert).toHaveCount(1)
  await expect(alert).toHaveText("The worker is not running")
  const worker = region(page, "Worker")
  await expect(worker.locator("[data-tone]").first()).toHaveText("Not running")
  await expect(definition(worker, "Stopped")).toHaveText(
    /^2026-10-2[45] \d\d:00:00$/
  )
  await expect(state(worker, "Dispatcher")).toHaveText("Off")
  await expect(indicator).toHaveText("Worker: not running")

  // Late: no alert; the late thread says when it counts as late.
  const [dispatcher, reconciler, poller] = makeWorker().threads
  if (!dispatcher || !reconciler || !poller) throw new Error("no threads")
  status = {
    ...STATUS_OK,
    worker: makeWorker({
      state: "late",
      threads: [
        { ...dispatcher, state: "late", last_mark_age_seconds: 75 },
        reconciler,
        poller,
      ],
    }),
  }
  await page.reload()
  await expect(worker.locator("[data-tone]").first()).toHaveText("Late")
  await expect(state(worker, "Dispatcher")).toHaveText("Late")
  await expect(state(worker, "Dispatcher")).toHaveAttribute(
    "title",
    "Late after 45 s"
  )
  await expect(definition(worker, "Dispatcher")).toContainText("1 min 15 s ago")
  await expect(page.getByRole("alert")).toHaveCount(0)
  await expect(indicator).toHaveText("Worker: late")
})

test("the database down is shown clearly", async ({ page }) => {
  await mockStatus(page, () => ({
    ...STATUS_OK,
    database: { status: "error", path: "/data/laueportal.sqlite" },
    worker: null,
    queue: null,
    next_up: null,
    active_runs: null,
  }))
  await page.goto("/")
  const alert = page.getByRole("alert")
  await expect(alert).toHaveCount(1)
  await expect(alert).toContainText("The database does not answer")
  await expect(alert).toContainText("/data/laueportal.sqlite cannot be read")
  await expect(state(region(page, "Connection"), "Database")).toHaveText(
    "Error"
  )
  await expect(region(page, "Active Runs")).toContainText(
    "The runs cannot be read: the database does not answer."
  )
  await expect(region(page, "Worker")).toHaveText("WorkerUnknown")
  await expect(region(page, "Queue")).toContainText("Unknown")
  await expect(
    page.getByRole("group", { name: "Live system status" }).getByRole("link")
  ).toHaveText("Worker: unknown")
})

test("the status page reports the API stopped, keeps saying so through the polls, and recovers on Retry", async ({
  page,
}) => {
  // The API is stopped: its requests get no answer, a refused connection (the API never answers
  // this with a 503). The page's own files still load.
  let stopped = true
  let requests = 0
  await page.route("**/api/v1/system/status", (route) => {
    requests += 1
    return stopped
      ? route.abort("connectionrefused")
      : route.fulfill({ json: STATUS_OK })
  })
  await page.route("**/api/v1/health", (route) =>
    stopped
      ? route.abort("connectionrefused")
      : route.fulfill({
          json: { db: "ok", worker: "running", version: "x" },
        })
  )
  await page.clock.install()
  await page.goto("/")

  // A failed request is retried twice (after 1 and 2 s) before the error shows.
  const alert = page.getByRole("alert")
  await advanceUntil(page, 1_000, () =>
    expect(alert).toContainText("The server cannot be reached", SHORT)
  )
  await expect(region(page, "Connection")).toHaveCount(0)

  // The polls go on (every 5 s, each retried twice). Through them the same alert stays (a new one
  // would be announced again), the loading skeleton does not come back, and a focused Retry keeps
  // the focus.
  const retry = alert.getByRole("button", { name: "Retry" })
  await retry.focus()
  await page.evaluate(() => {
    const shown = document.querySelector('[role="alert"]')
    const seen = { alertGone: false, skeleton: false, focusLost: false }
    Object.assign(window, { seen })
    new MutationObserver(() => {
      if (!shown?.isConnected) seen.alertGone = true
      if (document.querySelector('main [role="status"][aria-busy="true"]')) {
        seen.skeleton = true
      }
      if (document.activeElement === document.body) seen.focusLost = true
    }).observe(document.body, {
      childList: true,
      subtree: true,
      attributes: true,
    })
  })
  const before = requests
  await advanceUntil(page, 1_000, () => {
    expect(requests).toBeGreaterThanOrEqual(before + 3)
  })
  expect(
    await page.evaluate(() => (window as unknown as { seen: object }).seen)
  ).toEqual({ alertGone: false, skeleton: false, focusLost: false })
  await expect(retry).toBeFocused()
  await expect(retry).toBeEnabled()

  stopped = false
  await alert.getByRole("button", { name: "Retry" }).click()
  await expect(state(region(page, "Connection"), "Database")).toHaveText("OK")
  await expect(page.getByRole("alert")).toHaveCount(0)
})

test("polls every 5 s: an active run's progress moves without a reload", async ({
  page,
}) => {
  let status: SystemStatus = STATUS_OK
  let requests = 0
  await page.route("**/api/v1/system/status", (route) => {
    requests += 1
    return route.fulfill({ json: status })
  })
  await page.route("**/api/v1/health", (route) =>
    route.fulfill({
      json: { db: "ok", worker: "running", version: "x" },
    })
  )
  await page.clock.install()
  await page.goto("/")
  await expect(runRow(page, 41)).toContainText("Running 123/441")
  await page.evaluate(() => {
    document.body.dataset.loaded = "once"
  })

  const [local, remote] = ACTIVE_RUNS
  if (!local || !remote) throw new Error("no active runs")
  status = {
    ...STATUS_OK,
    active_runs: [
      { ...local, n_succeeded: 200, heartbeat_age_seconds: 2 },
      remote,
    ],
  }
  await advanceUntil(page, 5_000, () =>
    expect(runRow(page, 41)).toContainText("Running 203/441", SHORT)
  )
  await expect(runRow(page, 41)).toContainText("2 s ago")
  // The same page, updated in place.
  await expect(page.locator("body")).toHaveAttribute("data-loaded", "once")
  expect(requests).toBeGreaterThanOrEqual(2)

  // The run finished: it leaves the list.
  status = { ...STATUS_OK, active_runs: [remote] }
  await advanceUntil(page, 5_000, () =>
    expect(runRow(page, 41)).toHaveCount(0, SHORT)
  )
  await expect(region(page, "Active Runs")).toContainText("1 queued or running")
})
