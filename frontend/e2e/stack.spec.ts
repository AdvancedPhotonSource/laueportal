import {
  expect,
  test,
  type APIRequestContext,
  type Locator,
  type Page,
} from "@playwright/test"

import type {
  Health,
  IndexingRunPage,
  ReconstructionRunPage,
  ScanPage,
  SystemStatus,
} from "@/api"

import { prefillAsked } from "./prefill"
import { seededCruxJob } from "./stack-tools"

// The smoke test against the real stack that `make e2e` starts (deploy/supervisor/e2e.conf): the API
// serving the built SPA on 2100 and the worker, on a freshly seeded demo database. Nothing is
// mocked here; e2e/smoke.spec.ts covers the shell's behaviour with a mocked backend.

test("the API serves the built SPA, also for client routes", async ({
  page,
}) => {
  const response = await page.goto("/")
  expect(response?.status()).toBe(200)
  expect(response?.headers()["content-type"]).toContain("text/html")
  await expect(page).toHaveTitle("Status · 3DMN Portal")
  await expect(page.getByRole("link", { name: "3DMN Portal" })).toBeVisible()

  // A deep link loads index.html (the SPA fallback) and the router shows the page.
  await page.goto("/runs")
  await expect(page).toHaveTitle("Run Monitor · 3DMN Portal")
  await expect(page.getByRole("heading", { name: "Run Monitor" })).toBeVisible()
})

/** The first rows of a run list, newest first (the whole list grows on a stack not reseeded). */
const FIRST_ROWS = 5
const FIRST_PAGE = (list: string) =>
  `/api/v1/${list}?limit=${String(FIRST_ROWS)}`

/**
 * The first rows of a run list oldest first, with a filter: in a full `make e2e`, the specs before this one
 * (e2e/live.spec.ts's sessions, the job specs) add runs to the stack's database, so the seeded runs are found by
 * their place among the oldest, never on the newest page.
 */
const OLDEST_PAGE = (list: string, filter?: Record<string, unknown>) =>
  `${FIRST_PAGE(list)}&sort=id${
    filter ? `&filter=${encodeURIComponent(JSON.stringify(filter))}` : ""
  }`

/**
 * Sort the run list's grid by ID ascending, oldest first: click its ID header until the header reads ascending
 * (from the initial descending sort, AG Grid's cycle is none, then ascending), each click after the previous one
 * has shown; when a click was needed, wait for the grid's request of that sort.
 */
async function sortOldestFirst(page: Page, list: string) {
  const header = page.locator(
    '.ag-header-cell[col-id="id"]:not(.ag-floating-filter)'
  )
  await expect(header).toHaveAttribute(
    "aria-sort",
    /^(ascending|descending|none)$/
  )
  let sorted: Promise<unknown> | undefined
  for (let clicks = 0; clicks < 3; clicks += 1) {
    const state = (await header.getAttribute("aria-sort")) ?? ""
    if (state === "ascending") break
    sorted ??= page.waitForResponse((response) => {
      const url = new URL(response.url())
      return (
        url.pathname === `/api/v1/${list}` &&
        url.searchParams.get("sort") === "id"
      )
    })
    await header.locator(".ag-header-cell-label").click()
    await expect(header).not.toHaveAttribute("aria-sort", state)
  }
  await expect(header).toHaveAttribute("aria-sort", "ascending")
  await sorted
}

/** The grid shows these IDs first, in this order. */
async function expectFirstIds(page: Page, expected: string[]) {
  const ids = page.locator('.ag-row [col-id="id"]')
  await expect(ids.first()).toBeVisible()
  await expect
    .poll(async () => (await ids.allTextContents()).slice(0, expected.length))
    .toEqual(expected)
}

/**
 * A term's (first) definition in a card: its `dd` after the `dt` ("Database" and its badge, before
 * the file; "Queued" and 1).
 */
function definition(card: Locator, term: string) {
  return card.locator(`div:has(> dt:text-is("${term}")) > dd`).first()
}

async function systemStatus(request: APIRequestContext): Promise<SystemStatus> {
  const response = await request.get("/api/v1/system/status")
  expect(response.ok()).toBe(true)
  return (await response.json()) as SystemStatus
}

test("the status page shows the database and its file, the worker and its threads, and the queue", async ({
  page,
  request,
}) => {
  const status = await systemStatus(request)
  expect(status.database.status).toBe("ok")
  expect(status.queue).not.toBeNull()
  const worker = status.worker
  expect(worker?.state).toBe("running")
  expect(worker?.pid).toBeGreaterThan(0)
  const threads = Object.fromEntries(
    (worker?.threads ?? []).map((thread) => [thread.name, thread.state])
  )
  expect(threads).toMatchObject({ dispatcher: "ok", reconciler: "ok" })

  await page.goto("/")
  const connection = page.getByRole("region", { name: "Connection" })
  await expect(
    definition(connection, "Database").locator("[data-tone]")
  ).toHaveText("OK")
  await expect(connection).toContainText(status.database.path)
  await expect(definition(connection, "API").locator("[data-tone]")).toHaveText(
    "OK"
  )
  await expect(connection).toContainText(`Version ${status.version}`)
  await expect(page.locator("main")).not.toContainText(/redis/i)

  const workerCard = page.getByRole("region", { name: "Worker" })
  await expect(workerCard.locator("[data-tone]").first()).toHaveText("Running")
  await expect(definition(workerCard, "Host")).toHaveText(worker?.host ?? "")
  await expect(definition(workerCard, "PID")).toHaveText(String(worker?.pid))
  for (const name of ["Dispatcher", "Reconciler"]) {
    await expect(
      definition(workerCard, name).locator("[data-tone]")
    ).toHaveText("OK")
  }
  await expect(
    definition(workerCard, "Remote poller").locator("[data-tone]")
  ).toHaveText(threads.poller === "off" ? "Off" : "OK")

  // The queue and the active runs change while jobs run (other tests queue some on this stack): the
  // page shows what the API reports, which its next poll (5 s) catches up with.
  const active = page.getByRole("region", { name: "Active Runs" })
  const queue = page.getByRole("region", { name: "Queue" })
  await expect(async () => {
    const now = await systemStatus(request)
    const quick = { timeout: 1000 }
    await expect(definition(queue, "Queued runs")).toHaveText(
      String(now.queue?.queued_runs),
      quick
    )
    await expect(definition(queue, "Running")).toHaveText(
      String(now.queue?.running),
      quick
    )
    await expect(queue.locator("[data-item]")).toHaveCount(
      now.next_up?.length ?? 0,
      quick
    )
    const runs = now.active_runs ?? []
    if (runs.length === 0) {
      await expect(active).toContainText("No queued or running runs.", quick)
      return
    }
    await expect(active.locator("tr[data-job]")).toHaveCount(runs.length, quick)
    for (const run of runs) {
      const row = active.locator(`tr[data-job="${String(run.job_id)}"]`)
      await expect(
        row.getByRole("link", { name: String(run.job_id), exact: true })
      ).toHaveAttribute("href", `/jobs/${String(run.job_id)}`, quick)
      if (run.run) {
        await expect(
          row.getByRole("link", { name: run.run.label, exact: true })
        ).toBeVisible(quick)
      }
    }
  }).toPass({ timeout: 20_000 })
  await expect(page.getByRole("alert")).toHaveCount(0)
})

test("the navbar's live indicator shows the worker running: the quiet dot", async ({
  page,
  request,
}) => {
  const response = await request.get("/api/v1/health")
  const health = (await response.json()) as Health
  expect(health).toMatchObject({ db: "ok", worker: "running" })

  await page.goto("/runs")
  const indicator = page
    .getByRole("group", { name: "Live system status" })
    .getByRole("link")
  await expect(indicator).toHaveAccessibleName("Local Worker: running")
  await expect(indicator).toHaveAttribute("data-tone", "ok")
  await expect(indicator.locator('[data-slot="status-dot"]')).toBeVisible()
  // The dot with "Local" alone: no state words (its name is screen-reader-only text); words show only when
  // something is wrong (as e2e/shell.spec.ts's dot states).
  expect(
    await indicator.evaluate((link) => {
      const copy = link.cloneNode(true) as HTMLElement
      for (const hidden of copy.querySelectorAll(".sr-only")) hidden.remove()
      return copy.textContent.trim()
    })
  ).toBe("Local")
})

test("the scans page lists the seeded scans; a date filter keeps the local day", async ({
  browser,
  baseURL,
  request,
}) => {
  const response = await request.get("/api/v1/scans")
  expect(response.ok()).toBe(true)
  const list = (await response.json()) as ScanPage
  const newest = list.rows[0]
  if (!newest) throw new Error("the demo database has no scans")

  // A time zone where the scan's local day is not its UTC day: an offset-less date filter would be
  // read as the UTC day and miss it, so this checks the offset end to end. Neither zone has
  // daylight-saving time, so each day's offset is the zone's one.
  const hour = new Date(newest.time).getUTCHours()
  const [timezoneId, offset] =
    hour < 10
      ? ["Pacific/Honolulu", "-10:00"]
      : ["Pacific/Kiritimati", "+14:00"]
  const context = await browser.newContext({ baseURL, timezoneId })
  const page = await context.newPage()
  await page.goto("/scans")

  const scanIds = page.locator('.ag-row [col-id="scan_number"]')
  // Newest first, as the list's default order.
  await expect(scanIds.first()).toHaveText(String(newest.scan_number))
  const row = page
    .locator(`.ag-row[row-id="${String(newest.scan_number)}"]`)
    .filter({ has: page.locator('[col-id="scan_number"]') })
  await expect(row.locator('[col-id="technique"]')).toHaveText(newest.technique)
  await expect(row.locator('[col-id="n_dimensions"]')).toHaveText(
    `${String(newest.n_dimensions)}D`
  )

  const localDay = await page.evaluate((time) => {
    const date = new Date(time)
    const pad = (value: number) => String(value).padStart(2, "0")
    return `${String(date.getFullYear())}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
  }, newest.time)
  expect(localDay).not.toBe(newest.time.slice(0, 10))
  await expect(row.locator('[col-id="time"]')).toContainText(localDay)

  // The filtered answer itself: the grid keeps the old rows on screen until it arrives.
  const filtered = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return (
      url.pathname === "/api/v1/scans" &&
      (url.searchParams.get("filter") ?? "").includes('"time"')
    )
  })
  await page
    .locator('.ag-floating-filter[col-id="time"] input[type="date"]')
    .fill(localDay)
  const filteredResponse = await filtered
  const filter = JSON.parse(
    new URL(filteredResponse.url()).searchParams.get("filter") ?? "{}"
  ) as Record<string, unknown>
  expect(filter).toEqual({
    time: {
      filterType: "date",
      type: "equals",
      dateFrom: `${localDay}T00:00:00${offset}`,
    },
  })
  const answer = (await filteredResponse.json()) as ScanPage
  expect(answer.rows.map((scan) => scan.scan_number)).toContain(
    newest.scan_number
  )
  await expect(scanIds.first()).toHaveText(String(newest.scan_number))
  await expect(page.getByText("No scans match the filters")).toHaveCount(0)

  await page
    .locator(
      `.ag-row[row-id="${String(newest.scan_number)}"] input[type="checkbox"]`
    )
    .check()
  // The LaueGo form takes its prefill once and removes it from the URL.
  expect(
    await prefillAsked(page, "lauego", () =>
      page.getByRole("button", { name: "New Index" }).click()
    )
  ).toEqual({ scan: String(newest.scan_number) })
  await expect(page).toHaveURL(/\/indexing\/new$/)
  await context.close()
})

/** A run's row in a run list (its unpinned part: the pinned checkbox column renders each row twice). */
function runRow(page: Page, id: number) {
  return page
    .locator(`.ag-row[row-id="${String(id)}"]`)
    .filter({ has: page.locator('[col-id="id"]') })
}

function runCheckbox(page: Page, id: number) {
  return page.locator(`.ag-row[row-id="${String(id)}"] input[type="checkbox"]`)
}

test("the reconstructions page lists the seeded runs; New Wire Recon clones one", async ({
  page,
  request,
}) => {
  const response = await request.get(FIRST_PAGE("reconstruction-runs"))
  expect(response.ok()).toBe(true)
  const list = (await response.json()) as ReconstructionRunPage
  expect(list.total).toBeGreaterThan(0)
  // R4: recorded as running on CRUX (`laueportal seed demo --crux-run`, as the e2e stack seeds it).
  const crux = (await seededCruxJob(request)).run
  expect(crux).toMatchObject({ kind: "reconstruction", label: "R4" })
  // The seeded runs: R1 to R4 (R4, the seed's last; the other specs' runs are newer, their statuses their own).
  const seededResponse = await request.get(
    OLDEST_PAGE("reconstruction-runs", {
      id: {
        filterType: "number",
        type: "lessThanOrEqual",
        filter: crux?.id ?? 0,
      },
    })
  )
  expect(seededResponse.ok()).toBe(true)
  const seeded = (await seededResponse.json()) as ReconstructionRunPage
  expect(seeded.rows.map((run) => run.id)).toContain(1)

  const expectRows = async (
    rows: ReconstructionRunPage["rows"],
    withStatus: boolean
  ) => {
    for (const run of rows) {
      const row = runRow(page, run.id)
      await expect(row.locator('[col-id="method"]')).toHaveText(run.method)
      await expect(row.locator('[col-id="scan_number"]')).toHaveText(
        run.scan_number === null ? "Unlinked" : `SN${String(run.scan_number)}`
      )
      if (withStatus) {
        await expect(row.locator('[col-id="status"]')).toHaveText(
          run.id === crux?.id ? "Running 0/1" : "Finished"
        )
      }
    }
  }

  await page.goto("/reconstructions")
  // Newest (highest ID) first, as the list's default order: the API's first page, whichever runs it holds.
  await expectFirstIds(
    page,
    list.rows.map((run) => `R${String(run.id)}`)
  )
  await expectRows(list.rows, false)
  // Oldest first: the seeded runs, each with its status.
  await sortOldestFirst(page, "reconstruction-runs")
  await expectFirstIds(
    page,
    seeded.rows.map((run) => `R${String(run.id)}`)
  )
  await expectRows(seeded.rows, true)

  // R1, the demo's wire run (R4, newer, is linked too).
  const linked = seeded.rows.find((run) => run.id === 1)
  if (!linked || linked.scan_number === null) {
    throw new Error("the demo database has no R1 linked to its scan")
  }
  await runCheckbox(page, linked.id).check()
  // The wire form takes its prefill once and removes it from the URL.
  expect(
    await prefillAsked(page, "wire", () =>
      page.getByRole("button", { name: "New Wire Recon" }).click()
    )
  ).toEqual({
    reconstruction: String(linked.id),
    scan: String(linked.scan_number),
  })
  await expect(page).toHaveURL(/\/reconstructions\/new$/)
})

test("the indexations page lists the seeded runs and their sources; New Index clones one", async ({
  page,
  request,
}) => {
  const response = await request.get(FIRST_PAGE("indexing-runs"))
  expect(response.ok()).toBe(true)
  const list = (await response.json()) as IndexingRunPage
  expect(list.total).toBeGreaterThan(0)

  await page.goto("/indexing")
  await expectFirstIds(
    page,
    list.rows.map((run) => `I${String(run.id)}`)
  )
  for (const run of list.rows) {
    const source = runRow(page, run.id).locator('[col-id="scan_number"]')
    const parts = [
      run.scan_number === null ? "" : `SN${String(run.scan_number)}`,
      run.reconstruction_id === null ? "" : `R${String(run.reconstruction_id)}`,
    ].join("")
    await expect(source).toHaveText(parts || "Unlinked")
  }

  // A linked run among the oldest, the seeded ones (I1, the demo's, is linked to its scan): the newest page
  // holds whichever runs the other specs added.
  const oldestResponse = await request.get(OLDEST_PAGE("indexing-runs"))
  expect(oldestResponse.ok()).toBe(true)
  const oldest = (await oldestResponse.json()) as IndexingRunPage
  await sortOldestFirst(page, "indexing-runs")
  await expectFirstIds(
    page,
    oldest.rows.map((run) => `I${String(run.id)}`)
  )
  const linked = oldest.rows.find((run) => run.scan_number !== null)
  if (!linked) throw new Error("the demo database has no linked indexation")
  await runRow(page, linked.id)
    .locator('[col-id="scan_number"]')
    .getByRole("link", { name: `SN${String(linked.scan_number)}` })
    .click()
  await expect(page).toHaveURL(
    new RegExp(`/scans/${String(linked.scan_number)}$`)
  )
  // Going back before the scan page has loaded would leave the router on this page, with its sort kept.
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    `Scan ${String(linked.scan_number)}`
  )
  await page.goBack()
  // The grid comes back in its default order (its sort is not kept).
  await sortOldestFirst(page, "indexing-runs")
  await runCheckbox(page, linked.id).check()
  expect(
    await prefillAsked(page, "lauego", () =>
      page.getByRole("button", { name: "New Index" }).click()
    )
  ).toEqual({ indexing: String(linked.id) })
  await expect(page).toHaveURL(/\/indexing\/new$/)
})
