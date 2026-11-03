import {
  appendFileSync,
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  statSync,
} from "node:fs"

import {
  type APIRequestContext,
  expect,
  type Locator,
  test,
  type Page,
} from "@playwright/test"

import type {
  CreatedLiveRuns,
  JobDetail,
  LiveDaqList,
  LiveSessionState,
} from "@/api"
import {
  checkStackDatabase,
  configuredRunsRoot,
  fakeDMCatalogCalls,
  notifyLive,
  setFakeDM,
  untilLocalRunsEnd,
  type FakeCatalogRow,
  type FakeDAQ,
} from "./stack-tools"

// The Live page on the seeded stack (`make e2e`, nothing mocked; deploy/supervisor/e2e.sh turns `[live]` on with a
// stand-in DM that fails every call until a test switches it on, so the DAQ list is unavailable and the source is
// notifications): an Indexing session on the demo's synthetic frames, started from the page, its frames admitted with
// `laueportal live notify`, its counters moving, stopped from the page (sealed and drained: Finished; the page then
// shows no session), and the form then the session's clone for the next Start. With LIVE_SHOTS set to a folder, the
// page's three states are saved there, light and dark. Then a Both session on the demo's wire scan: Stop, and the
// session Stopping (Stop now in Stop's place) while the follower drains after its wire run has ended, until both have
// ended. Then a session on a Folder whose path has a `data` folder: its Output Folder the mirror (From data folder),
// missing until Start makes it, no Filename Template, and after Stop a second session on the same folders that starts
// with no confirmation, then the form following a new Folder. Then the viewers and the
// job page of live runs (after those two, whose first needs no session before it): the wire viewer's "Follow newest
// point" on a live wire run whose stacks arrive one by one, the indexing viewer of a live LaueGo run updating as it
// publishes, and the job page of a live run and of its follower (their screenshots too, with LIVE_SHOTS). Last (its
// session's clone would make a DAQ the next form's source), a session whose source is a DAQ: the stand-in DM switched
// on, the DAQ picked from the list, its files admitted from DM's catalog by the real client and API script, and Stop's
// last catalog read taking a file catalogued at the Stop.

async function shots(page: Page, name: string) {
  const folder = process.env.LIVE_SHOTS
  if (!folder) return
  // The page alone: a Stop's report toast gone first.
  await expect(page.locator("[data-sonner-toast]")).toHaveCount(0, {
    timeout: 30_000,
  })
  for (const colorScheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme })
    await page.mouse.move(0, 0)
    await page.screenshot({
      path: `${folder}/${name}-${colorScheme}.png`,
      fullPage: true,
    })
  }
  await page.emulateMedia({ colorScheme: null })
}

/** The Scan card alone (`LIVE_SHOTS`), light and dark. */
async function cardShots(card: Locator, name: string) {
  const folder = process.env.LIVE_SHOTS
  if (!folder) return
  const page = card.page()
  for (const colorScheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme })
    await page.mouse.move(0, 0)
    await card.screenshot({ path: `${folder}/${name}-${colorScheme}.png` })
  }
  await page.emulateMedia({ colorScheme: null })
}

/** The latest session as the API answers it (the page shows none once a stopped one has ended). */
async function latestSession(
  request: APIRequestContext
): Promise<LiveSessionState["session"]> {
  const answer = await request.get("/api/v1/live/session")
  return ((await answer.json()) as LiveSessionState).session
}

/** An output folder of its own for a session (an existing one that is not empty is a warning). */
function outputFolder(name: string): string {
  return `${configuredRunsRoot()}/live/${name}-${String(Date.now())}`
}

/** Stop a session a failed test left open: an open live run holds every batch run of the later specs. */
test.afterEach(async ({ request }) => {
  const response = await request.get("/api/v1/live/session")
  if (!response.ok()) return
  const { session } = (await response.json()) as LiveSessionState
  if (!session?.open) return
  for (const run of session.runs) {
    await request.post(`/api/v1/jobs/${String(run.job_id)}/cancel`, {
      headers: { "X-Laue-Portal": "1" },
    })
  }
  await expect
    .poll(
      async () => {
        const answer = await request.get("/api/v1/live/session")
        return ((await answer.json()) as LiveSessionState).session?.open
      },
      { timeout: 120_000 }
    )
    .toBe(false)
})

test("a live session: Start, notified frames counted, Stop to Finished and no session shown, and Start again prefilled", async ({
  page,
  request,
}) => {
  test.setTimeout(300_000)
  await checkStackDatabase(request)
  // The demo's batch runs end first: a live session would stop a running one.
  await untilLocalRunsEnd(request)
  const root = configuredRunsRoot()
  const folder = `${root}/inputs/synthetic-indexing`
  const output = outputFolder("indexing")

  await page.goto("/live")
  await expect(page).toHaveTitle("Live · 3DMN Portal")
  const session = page.getByRole("region", { name: "Session" })
  const scan = page.getByRole("region", { name: "Scan" })
  // The cards sit in one fieldset, disabled while a session is open: their fields with it.
  const folderField = scan.getByLabel("Folder", { exact: true })
  await expect(session).toContainText("No live session")
  await expect(folderField).toBeEnabled()
  // The stand-in DM failing every call: the DAQ list says so, and Notifications is the source.
  await expect(scan.getByRole("status")).toContainText("DAQ list unavailable")
  await shots(page, "live-idle")

  // Indexing alone, on raw frames: the Reconstruction card switched off.
  await page.getByRole("switch", { name: "Reconstruction" }).click()
  await expect(
    page.getByRole("region", { name: "Indexing" }).getByLabel("Depth Indices")
  ).toHaveCount(0)
  await folderField.fill(folder)
  await scan.getByLabel("Filename Template").fill("frame_%d.h5")
  await scan.getByLabel("Output Folder").fill(output)
  await scan.getByLabel("Scan Number").fill("990001")
  await scan.getByLabel("Author").fill("e2e")
  await session.getByRole("button", { name: "Validate" }).click()
  await expect(page.getByRole("region", { name: "Validation" })).toContainText(
    "No issues",
    { timeout: 15_000 }
  )

  const started = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/live-runs" &&
      response.request().method() === "POST"
  )
  await session.getByRole("button", { name: "Start" }).click()
  const answer = await started
  expect(answer.status()).toBe(201)
  const created = (await answer.json()) as CreatedLiveRuns
  expect(created.runs).toHaveLength(1)
  const job = created.runs[0]?.job_id ?? -1
  const row = session.locator(`[data-job="${String(job)}"]`)
  await expect(row).toBeVisible()
  await expect(row.getByRole("link", { name: /^I\d+$/ })).toBeVisible()
  await expect(folderField).toBeDisabled()
  await expect(page.getByRole("switch", { name: "Indexing" })).toBeDisabled()
  await expect(session.getByRole("button", { name: "Start" })).toBeDisabled()

  // Frames admitted as an acquisition hook would (the empty synthetic frame left out: no failed input).
  const count = (name: string) => row.locator(`[data-count="${name}"]`)
  notifyLive(job, `${folder}/frame_1.h5`)
  await expect(count("arrived")).toHaveText("1", { timeout: 30_000 })
  notifyLive(job, `${folder}/frame_2.h5`)
  notifyLive(job, `${folder}/frame_4.h5`)
  await expect(count("arrived")).toHaveText("3", { timeout: 30_000 })
  await expect(count("done")).not.toHaveText("0", { timeout: 120_000 })
  await expect(row.locator('[data-status="running"]')).toBeVisible()
  await shots(page, "live-open")

  await session.getByRole("button", { name: "Stop" }).click()
  const dialog = page.getByRole("alertdialog")
  await expect(dialog).toContainText(
    "No new files are taken; the files taken are finished."
  )
  await dialog.getByRole("button", { name: "Stop" }).click()
  // Once it has ended the page shows no session (Idle, no runs table); the run, Finished, is the API's.
  const badge = session.locator('[data-slot="card-title"] [data-status]')
  await expect(badge).toHaveAttribute("data-status", "idle", {
    timeout: 120_000,
  })
  await expect(row).toHaveCount(0)
  await expect(session).toContainText("No live session")
  const ended = await latestSession(request)
  expect(ended?.stopped).toBe(true)
  expect(
    ended?.runs.map((run) => [run.status, run.n_succeeded, run.n_failed])
  ).toEqual([["finished", 3, 0]])

  // Start again: the form is the ended session's clone, editable (no Resume).
  await expect(folderField).toBeEnabled()
  await expect(folderField).toHaveValue(/\/inputs\/synthetic-indexing$/)
  await expect(scan.getByLabel("Filename Template")).toHaveValue("frame_%d.h5")
  await expect(scan.getByLabel("Output Folder")).toHaveValue(output)
  await expect(scan.getByLabel("Scan Number")).toHaveValue("990001")
  await expect(
    page.getByRole("switch", { name: "Reconstruction" })
  ).not.toBeChecked()
  await expect(page.getByRole("switch", { name: "Indexing" })).toBeChecked()
  await expect(session.getByRole("button", { name: "Start" })).toBeEnabled()
  await expect(session.getByRole("button", { name: "Stop" })).toBeDisabled()
  await shots(page, "live-ended")
})

test("a Both session is Stopping, Stop now in Stop's place, from Stop until its draining follower has ended", async ({
  page,
  request,
}) => {
  test.setTimeout(300_000)
  await checkStackDatabase(request)
  await untilLocalRunsEnd(request)
  const root = configuredRunsRoot()
  const folder = `${root}/inputs/synthetic-reconstruction`

  await page.goto("/live")
  const session = page.getByRole("region", { name: "Session" })
  const scan = page.getByRole("region", { name: "Scan" })
  const folderField = scan.getByLabel("Folder", { exact: true })
  await expect(folderField).toBeEnabled()
  for (const name of ["Reconstruction", "Indexing"]) {
    const part = page.getByRole("switch", { name })
    if (!(await part.isChecked())) await part.click()
  }
  await folderField.fill(folder)
  await scan.getByLabel("Filename Template").fill("wire_%d.h5")
  await scan.getByLabel("Output Folder").fill(outputFolder("both"))
  await scan.getByLabel("Scan Number").fill("990001")
  await scan.getByLabel("Author").fill("e2e")
  await session.getByRole("button", { name: "Validate" }).click()
  await expect(page.getByRole("region", { name: "Validation" })).toContainText(
    "No issues",
    { timeout: 15_000 }
  )

  const started = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/live-runs" &&
      response.request().method() === "POST"
  )
  await session.getByRole("button", { name: "Start" }).click()
  const answer = await started
  expect(answer.status()).toBe(201)
  const created = (await answer.json()) as CreatedLiveRuns
  expect(created.runs.map((run) => run.run.kind)).toEqual([
    "reconstruction",
    "indexing",
  ])
  const [wire, follower] = created.runs.map((run) => run.job_id)
  const status = (job: number | undefined) =>
    session.locator(`[data-job="${String(job)}"] [data-status]`).first()
  const badge = session.locator('[data-slot="card-title"] [data-status]')
  const stop = session.getByRole("button", { name: "Stop", exact: true })
  const stopNow = session.getByRole("button", { name: "Stop now" })

  // The wire stack arrives, then Stop: the wire run reconstructs it and ends; its follower indexes the point's
  // depths after that, about 10 s here, so the page's 5 s read sees the pair in that state.
  notifyLive(wire ?? -1, `${folder}/wire_1.h5`)
  await expect(
    session.locator(`[data-job="${String(wire)}"] [data-count="arrived"]`)
  ).toHaveText("1", { timeout: 30_000 })
  await stop.click()
  await page
    .getByRole("alertdialog")
    .getByRole("button", { name: "Stop" })
    .click()
  await expect(badge).toHaveAttribute("data-status", "stopping")
  // Stopping: Stop is Stop now (not pressed here: the follower drains by itself).
  await expect(stopNow).toBeEnabled()

  // Ended: no session shown (a stopped one), Idle.
  const ended = /^(idle|finished|failed|cancelled)$/
  const samples: { state: string; stopNow: boolean; pair: string }[] = []
  await expect
    .poll(
      async () => {
        // The button first: Stopping only ends, so a "Stop now" read before a Stopping badge was there with it.
        const shown =
          (await stopNow.count()) === 1 && (await stopNow.isEnabled())
        const state = (await badge.getAttribute("data-status")) ?? ""
        if (ended.test(state)) return state
        samples.push({
          state,
          stopNow: shown,
          pair: `${String(await status(wire).getAttribute("data-status"))}/${String(
            await status(follower).getAttribute("data-status")
          )}`,
        })
        return state
      },
      { timeout: 180_000, intervals: [250] }
    )
    .toMatch(ended)
  // Stopping with Stop now on every look until the end, also once the wire run had ended with its follower open.
  expect(
    samples.filter((sample) => sample.state !== "stopping" || !sample.stopNow)
  ).toEqual([])
  expect(samples.map((sample) => sample.pair)).toContain("finished/running")
  await expect(badge).toHaveAttribute("data-status", "idle")
  expect((await latestSession(request))?.runs.map((run) => run.status)).toEqual(
    ["finished", "finished"]
  )
  await expect(session.getByRole("button", { name: "Start" })).toBeEnabled()
  await expect(stop).toBeDisabled()
})

test("Output Folder mirrors a data Folder: the session's runs go in it, made at Start, and a second session there starts unasked", async ({
  page,
  request,
}) => {
  test.setTimeout(300_000)
  await checkStackDatabase(request)
  await untilLocalRunsEnd(request)
  const root = configuredRunsRoot()
  const base = `${root}/inputs/mirror-${String(Date.now())}`
  const folder = `${base}/data/scan_12`
  const mirrored = `${base}/analysis/scan_12`
  mkdirSync(folder, { recursive: true })

  await page.goto("/live")
  const session = page.getByRole("region", { name: "Session" })
  const scan = page.getByRole("region", { name: "Scan" })
  const folderField = scan.getByLabel("Folder", { exact: true })
  const output = scan.getByLabel("Output Folder")
  const fromData = scan.getByRole("button", { name: "From data folder" })
  await expect(folderField).toBeEnabled()
  // The form fills from the last session (its switches too) once its clones have answered: wait for it.
  const last = (await latestSession(request))?.scan
  if (last) await expect(folderField).toHaveValue(last.folder)
  for (const [name, wanted] of [
    ["Reconstruction", false],
    ["Indexing", true],
  ] as const) {
    const part = page.getByRole("switch", { name })
    if ((await part.isChecked()) !== wanted) await part.click()
  }
  // A form filled from the last session keeps its Output Folder (an edit) until From data folder mirrors the Folder
  // (`…/data/scan_12` to `…/analysis/scan_12`); with none before, Output Folder follows the Folder at once.
  const before = await output.inputValue()
  await folderField.fill(folder)
  await expect(output).toHaveValue(last ? before : mirrored)
  await expect(fromData).toBeEnabled()
  await fromData.click()
  await expect(output).toHaveValue(mirrored)
  // No Filename Template: every .h5 file, its number the last in its name.
  await scan.getByLabel("Filename Template").fill("")
  await scan.getByLabel("Scan Number").fill("990001")
  await scan.getByLabel("Author").fill("e2e")
  await session.getByRole("button", { name: "Validate" }).click()
  await expect(page.getByRole("region", { name: "Validation" })).toContainText(
    "No issues",
    { timeout: 15_000 }
  )
  // The e2e root's path is long: the shot shows the Output Folder's end, its mirrored part.
  await output.evaluate((element) => {
    element.scrollLeft = element.scrollWidth
  })
  await cardShots(scan, "live-scan-mirrored")

  const started = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/live-runs" &&
      response.request().method() === "POST"
  )
  await session.getByRole("button", { name: "Start" }).click()
  const answer = await started
  expect(answer.status()).toBe(201)
  const [run] = ((await answer.json()) as CreatedLiveRuns).runs
  const id = run?.run.id ?? -1
  // Its folder in the mirrored Output Folder, which Start made.
  const detail = (await (
    await request.get(`/api/v1/indexing-runs/${String(id)}`)
  ).json()) as { output_path: string }
  expect(detail.output_path).toBe(`${mirrored}/index_${String(id)}`)
  expect(existsSync(detail.output_path)).toBe(true)
  const row = session.locator(`[data-job="${String(run?.job_id)}"]`)
  copyFileSync(
    `${root}/inputs/synthetic-indexing/frame_1.h5`,
    `${folder}/Si_00012.h5`
  )
  notifyLive(run?.job_id ?? -1, `${folder}/Si_00012.h5`)
  await expect(row.locator('[data-count="arrived"]')).toHaveText("1", {
    timeout: 30_000,
  })

  await session.getByRole("button", { name: "Stop" }).click()
  await page
    .getByRole("alertdialog")
    .getByRole("button", { name: "Stop" })
    .click()
  await expect(
    session.locator('[data-slot="card-title"] [data-status]')
  ).toHaveAttribute("data-status", "idle", { timeout: 120_000 })
  await expect(row).toHaveCount(0)
  // The next form: the session's values, checked again (its run's folder is now in the Output Folder: no issue).
  const validation = page.getByRole("region", { name: "Validation" })
  await expect(output).toHaveValue(mirrored)
  await expect(scan.getByLabel("Filename Template")).toHaveValue("")
  await expect(validation).toContainText("No issues", { timeout: 15_000 })
  await shots(page, "live-after-stop")

  // A second session on the same data folder and mirrored Output Folder: Start asks no confirmation.
  const again = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/live-runs" &&
      response.request().method() === "POST"
  )
  await session.getByRole("button", { name: "Start" }).click()
  const second = await again
  expect(second.status()).toBe(201)
  await expect(page.getByRole("alertdialog")).toHaveCount(0)
  const [next] = ((await second.json()) as CreatedLiveRuns).runs
  const nextDetail = (await (
    await request.get(`/api/v1/indexing-runs/${String(next?.run.id)}`)
  ).json()) as { output_path: string }
  expect(nextDetail.output_path).toBe(
    `${mirrored}/index_${String(next?.run.id)}`
  )
  const nextRow = session.locator(`[data-job="${String(next?.job_id)}"]`)
  await expect(nextRow).toBeVisible()
  await session.getByRole("button", { name: "Stop" }).click()
  await page
    .getByRole("alertdialog")
    .getByRole("button", { name: "Stop" })
    .click()
  await expect(
    session.locator('[data-slot="card-title"] [data-status]')
  ).toHaveAttribute("data-status", "idle", { timeout: 120_000 })

  // The form filled from it keeps following the Folder (its Output Folder is the Folder's mirror).
  await folderField.fill(`${base}/data/scan_13`)
  await expect(output).toHaveValue(`${base}/analysis/scan_13`)
})

/** Start a live session from the Live page (its parts switched as asked); its runs, the wire run first. */
async function startSession(
  page: Page,
  options: {
    folder: string
    template: string
    reconstruction: boolean
    indexing: boolean
  }
): Promise<CreatedLiveRuns["runs"]> {
  await page.goto("/live")
  const session = page.getByRole("region", { name: "Session" })
  const scan = page.getByRole("region", { name: "Scan" })
  const folderField = scan.getByLabel("Folder", { exact: true })
  await expect(folderField).toBeEnabled()
  for (const [name, wanted] of [
    ["Reconstruction", options.reconstruction],
    ["Indexing", options.indexing],
  ] as const) {
    const part = page.getByRole("switch", { name })
    if ((await part.isChecked()) !== wanted) await part.click()
  }
  await folderField.fill(options.folder)
  await scan.getByLabel("Filename Template").fill(options.template)
  await scan.getByLabel("Output Folder").fill(outputFolder("session"))
  await scan.getByLabel("Scan Number").fill("990001")
  await scan.getByLabel("Author").fill("e2e")
  await session.getByRole("button", { name: "Validate" }).click()
  await expect(page.getByRole("region", { name: "Validation" })).toContainText(
    "No issues",
    { timeout: 15_000 }
  )
  const started = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/live-runs" &&
      response.request().method() === "POST"
  )
  await session.getByRole("button", { name: "Start" }).click()
  const answer = await started
  expect(answer.status()).toBe(201)
  return ((await answer.json()) as CreatedLiveRuns).runs
}

const LIVE_STOP = "No new files are taken; the files taken are finished."

test("Follow newest point selects each new point of a live wire run, and the user's pick turns it off", async ({
  page,
  request,
}) => {
  test.setTimeout(300_000)
  await checkStackDatabase(request)
  await untilLocalRunsEnd(request)
  // Three wire stacks to arrive one by one: copies of the demo's.
  const root = configuredRunsRoot()
  const folder = `${root}/inputs/follow-${String(Date.now())}`
  mkdirSync(folder, { recursive: true })
  for (const point of [1, 2, 3]) {
    copyFileSync(
      `${root}/inputs/synthetic-reconstruction/wire_1.h5`,
      `${folder}/wire_${String(point)}.h5`
    )
  }
  const [wire] = await startSession(page, {
    folder,
    template: "wire_%d.h5",
    reconstruction: true,
    indexing: false,
  })
  const job = wire?.job_id ?? -1
  notifyLive(job, `${folder}/wire_1.h5`)

  await page.goto(`/reconstructions/${String(wire?.run.id)}?tab=points`)
  const bar = page.getByRole("region", { name: "Reconstruction" })
  const follow = page.getByRole("switch", { name: "Follow newest point" })
  await expect(bar).toContainText("Point wire_1", { timeout: 60_000 })
  await expect(follow).not.toBeChecked()
  await follow.click()
  await expect(follow).toBeChecked()

  for (const point of ["wire_2", "wire_3"]) {
    notifyLive(job, `${folder}/${point}.h5`)
    await expect(bar).toContainText(`Point ${point}`, { timeout: 60_000 })
    await expect(page).toHaveURL(new RegExp(`point=${point}`))
  }
  await expect(bar).toContainText("3 of 3 points complete")
  await shots(page, "live-wire-follow")

  // The user's pick: that point, and following is off.
  await page
    .getByRole("row")
    .filter({
      has: page.getByRole("gridcell", { name: "wire_1", exact: true }),
    })
    .getByRole("checkbox")
    .check()
  await expect(bar).toContainText("Point wire_1")
  await expect(follow).not.toBeChecked()
})

test("the indexing viewer of a live LaueGo run updates as it publishes, its detector step kept", async ({
  page,
  request,
}) => {
  test.setTimeout(300_000)
  await checkStackDatabase(request)
  await untilLocalRunsEnd(request)
  const folder = `${configuredRunsRoot()}/inputs/synthetic-indexing`
  const [run] = await startSession(page, {
    folder,
    template: "frame_%d.h5",
    reconstruction: false,
    indexing: true,
  })
  const job = run?.job_id ?? -1
  const artifacts: number[] = []
  page.on("response", (response) => {
    if (new URL(response.url()).pathname.endsWith("/artifact")) {
      artifacts.push(response.status())
    }
  })
  // The demo's empty frame first (no pattern), every step in scope ("Skip steps with peaks ≤ 0").
  notifyLive(job, `${folder}/frame_3.h5`)

  await page.goto(`/indexing/${String(run?.run.id)}?tab=detector`)
  const skip = page.getByLabel("Skip steps with peaks ≤")
  await skip.fill("0")
  await skip.press("Enter")
  const steps = page
    .getByRole("group", { name: "Data scope" })
    .getByRole("status")
  await expect(steps).toHaveText("1 step in scope", { timeout: 90_000 })
  const step = page.getByLabel("Step summary")
  await expect(step).toContainText("Step #0")
  notifyLive(job, `${folder}/frame_1.h5`)
  notifyLive(job, `${folder}/frame_2.h5`)
  // Published again: the page reads the results again by itself. Their default step is now Step #1 (the first
  // with a pattern), and the step shown, Step #0, is kept.
  await expect(steps).toHaveText("3 steps in scope", { timeout: 90_000 })
  await expect(step).toContainText("Step #0")
  await expect(page.getByText("Writing results")).toHaveCount(0)
  await page.getByRole("button", { name: "Next step" }).click()
  await expect(step).toContainText("Step #1")
  await shots(page, "live-indexing-viewer")
  // Polled every 5 s while open.
  expect(artifacts.length).toBeGreaterThan(2)
})

test("the job page of a live run shows its source, arrivals and arrival status, and its Stop's one line", async ({
  page,
  request,
}) => {
  test.setTimeout(300_000)
  await checkStackDatabase(request)
  await untilLocalRunsEnd(request)
  const root = configuredRunsRoot()
  const folder = `${root}/inputs/synthetic-reconstruction`
  const [wire, follower] = await startSession(page, {
    folder,
    template: "wire_%d.h5",
    reconstruction: true,
    indexing: true,
  })
  const wireJob = wire?.job_id ?? -1
  const followerJob = follower?.job_id ?? -1
  notifyLive(wireJob, `${folder}/wire_1.h5`)

  await page.goto(`/jobs/${String(wireJob)}`)
  const card = page.getByRole("region", { name: "Live Run" })
  const value = (label: string) =>
    card.locator("dt", { hasText: new RegExp(`^${label}$`) }).locator("+ dd")
  await expect(value("Source")).toHaveText("Notifications")
  await expect(value("Folder")).toHaveText(
    /\/inputs\/synthetic-reconstruction$/
  )
  await expect(value("Filename Template")).toHaveText("wire_%d.h5")
  await expect(value("Arrivals")).toHaveText("1", { timeout: 30_000 })
  // The run has taken it (its counters), the page read again every 2 s.
  await expect(
    page.getByRole("region", { name: "Run Progress" })
  ).not.toContainText("No inputs", { timeout: 30_000 })
  await shots(page, "live-job")

  // The follower: the wire run it follows, linked; while a point holds it, why it waits (its own
  // arrival-status.json, written by its thread). The hold: once wire_1 is admitted, a point the wire run
  // (idle: nothing more arrives) lists after it whose file is not here yet, its line appended here.
  const live = async (jobId: number) =>
    (
      (await (
        await request.get(`/api/v1/jobs/${String(jobId)}`)
      ).json()) as JobDetail
    ).live
  await expect
    .poll(async () => (await live(followerJob))?.arrivals, { timeout: 60_000 })
    .toBe(1)
  const wireDetail = (await (
    await request.get(`/api/v1/jobs/${String(wireJob)}`)
  ).json()) as JobDetail
  const wireDirectory = wireDetail.configuration.run_directory.path ?? ""
  const completed = `${wireDirectory}/completed.jsonl`
  const first = JSON.parse(
    readFileSync(completed, "utf8").split("\n")[0] ?? ""
  ) as { path: string }
  const later = first.path.replace(/wire_1\.h5$/, "wire_9.h5")
  appendFileSync(
    completed,
    `${JSON.stringify({
      ...first,
      n: 1,
      point_id: "wire_9",
      scan_point: 9,
      path: later,
      completed_at: new Date().toISOString().replace(/\.\d+Z$/, "+00:00"),
    })}\n`
  )
  await page.goto(`/jobs/${String(followerJob)}`)
  await expect(value("Source")).toContainText(
    `Reconstruction R${String(wire?.run.id)}`
  )
  await expect(
    card.getByRole("link", { name: `R${String(wire?.run.id)}` })
  ).toHaveAttribute("href", `/reconstructions/${String(wire?.run.id)}`)
  await expect(value("Waiting")).toContainText(
    "point 'wire_9' (line 2 of the followed run's completed.jsonl) not admitted yet: ",
    { timeout: 30_000 }
  )
  await expect(value("Waiting")).toContainText("/wire_9.h5 does not exist")
  await expect(value("Points Read")).toHaveText("2")
  await expect(value("Arrivals")).toHaveText("1")
  await shots(page, "live-job-follower-waiting")
  // Its file comes: admitted at the follower's next look, and the wait clears.
  copyFileSync(first.path, later)
  await expect(value("Arrivals")).toHaveText("2", { timeout: 90_000 })
  await expect(value("Waiting")).not.toContainText("not admitted")

  // Stop from the wire run's job page: its dialog and its report say the one line.
  await page.goto(`/jobs/${String(wireJob)}`)
  await page.getByRole("button", { name: "Stop Run" }).click()
  const dialog = page.getByRole("alertdialog")
  await expect(dialog).toContainText(LIVE_STOP)
  await dialog.getByRole("button", { name: "Stop Run" }).click()
  await expect(page.locator("[data-sonner-toast]")).toContainText(
    `stop requested. ${LIVE_STOP}`
  )
})

test.describe("a DAQ session", () => {
  // The stand-in DM back to failing every call, whatever the test did.
  test.afterEach(() => {
    setFakeDM()
  })

  test("the DAQ picked fills Folder, its catalogued files are admitted, and Stop's last catalog read takes the last", async ({
    page,
    request,
  }) => {
    test.setTimeout(300_000)
    await checkStackDatabase(request)
    await untilLocalRunsEnd(request)
    const inputs = `${configuredRunsRoot()}/inputs`
    const folder = `${inputs}/synthetic-indexing`
    // A running DAQ that writes the demo's synthetic frames, and DM's catalog of them as they come.
    const daq: FakeDAQ = {
      id: "e2e-daq",
      experimentName: "e2e",
      status: "Running",
      dataDirectory: "/detector/e2e/raw",
      storageDirectory: inputs,
      destDirectory: "synthetic-indexing",
    }
    const files: FakeCatalogRow[] = []
    const catalogue = (frame: number) => {
      const name = `frame_${String(frame)}.h5`
      files.push({
        experimentFilePath: `synthetic-indexing/${name}`,
        fileSize: statSync(`${folder}/${name}`).size,
        fileProcessingTime: Date.now() / 1000,
        daqId: daq.id,
        experimentName: daq.experimentName,
      })
      setFakeDM({ daqs: [daq], files })
    }
    catalogue(1)
    // The DAQ list asks DM again once its kept answer, failed until now, has expired (60 s at most).
    await expect
      .poll(
        async () => {
          const answer = await request.get("/api/v1/live/daqs")
          if (!answer.ok()) return []
          return ((await answer.json()) as LiveDaqList).daqs.map((d) => d.id)
        },
        { timeout: 90_000, intervals: [2_000] }
      )
      .toEqual([daq.id])

    await page.goto("/live")
    const session = page.getByRole("region", { name: "Session" })
    const scan = page.getByRole("region", { name: "Scan" })
    const folderField = scan.getByLabel("Folder", { exact: true })
    await expect(folderField).toBeEnabled()
    await expect(scan.getByRole("status")).toHaveCount(0)
    await scan.getByRole("combobox", { name: "Source" }).click()
    await page.getByRole("option", { name: "e2e · e2e/raw (Running)" }).click()
    await expect(folderField).toHaveValue(folder)
    await expect(scan.getByLabel("DAQ")).toContainText("/detector/e2e/raw")
    if (
      await page.getByRole("switch", { name: "Reconstruction" }).isChecked()
    ) {
      await page.getByRole("switch", { name: "Reconstruction" }).click()
    }
    await scan.getByLabel("Filename Template").fill("frame_%d.h5")
    await scan.getByLabel("Output Folder").fill(outputFolder("daq"))
    await scan.getByLabel("Scan Number").fill("990001")
    await scan.getByLabel("Author").fill("e2e")
    await session.getByRole("button", { name: "Validate" }).click()
    await expect(
      page.getByRole("region", { name: "Validation" })
    ).toContainText("No issues", { timeout: 15_000 })

    const started = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/api/v1/live-runs" &&
        response.request().method() === "POST"
    )
    await session.getByRole("button", { name: "Start" }).click()
    const answer = await started
    expect(answer.status()).toBe(201)
    const created = (await answer.json()) as CreatedLiveRuns
    expect(created.runs).toHaveLength(1)
    const row = session.locator(
      `[data-job="${String(created.runs[0]?.job_id)}"]`
    )
    const count = (name: string) => row.locator(`[data-count="${name}"]`)

    // The catalog's first read admits the file it lists; one catalogued later comes at a later cycle.
    await expect(count("arrived")).toHaveText("1", { timeout: 60_000 })
    await expect(row.locator("[data-arrival]")).toContainText("Checked")
    catalogue(2)
    await expect(count("arrived")).toHaveText("2", { timeout: 60_000 })
    await expect(count("done")).toHaveText("2", { timeout: 120_000 })
    await shots(page, "live-daq-open")

    // A file catalogued as Stop is pressed: the Stop's last catalog read takes it. Nothing new arriving, the
    // catalog's cycles come 1, 2, 4, 8 s apart and more (e2e.sh): Stop is pressed after a cycle's read once they are
    // 4 s apart, so the next regular cycle would come 8 s after it, and none comes once the run's stop is read
    // (within 2 s).
    await session.getByRole("button", { name: "Stop" }).click()
    const dialog = page.getByRole("alertdialog")
    await expect(dialog).toContainText(LIVE_STOP)
    await expect
      .poll(
        () => {
          const [before, last] = fakeDMCatalogCalls().slice(-2)
          if (before === undefined || last === undefined) return false
          return last - before >= 3.5 && Date.now() / 1000 - last < 1
        },
        { timeout: 90_000, intervals: [200] }
      )
      .toBe(true)
    await page.waitForTimeout(1_500)
    catalogue(4)
    const stoppedAt = Date.now() / 1000
    await dialog.getByRole("button", { name: "Stop" }).click()
    // Ended: no session shown; its run, Finished with every file, is the API's.
    await expect(
      session.locator('[data-slot="card-title"] [data-status]')
    ).toHaveAttribute("data-status", "idle", { timeout: 120_000 })
    const ended = await latestSession(request)
    expect(
      ended?.runs.map((run) => [
        run.status,
        run.n_inputs,
        run.n_succeeded,
        run.n_failed,
      ])
    ).toEqual([["finished", 3, 3, 0]])
    // DM's catalog was read after the Stop (the last read; no regular cycle follows the run's stop).
    expect(fakeDMCatalogCalls().some((at) => at > stoppedAt)).toBe(true)
  })
})
