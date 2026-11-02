import { execFileSync } from "node:child_process"
import { existsSync, readFileSync, renameSync, writeFileSync } from "node:fs"
import { dirname } from "node:path"

import { expect, type APIRequestContext } from "@playwright/test"

import type { JobPage, JobSummary, SystemStatus } from "@/api"

// The backend's development commands, for the specs that need what no API does (`laueportal seed slow-runs` queues
// long demo runs). They run with the e2e stack's settings (scripts/env.sh, then deploy/supervisor/e2e.sh, as
// `make e2e` starts that stack), after checking that the stack the browser talks to serves the same database
// (checkStackDatabase); stopJobsAfter stops what a test queued.

/** The repository (this file is in frontend/e2e). */
const REPO = decodeURIComponent(new URL("../..", import.meta.url).pathname)

const SCRIPT =
  '. "$1/scripts/env.sh" && . "$1/deploy/supervisor/e2e.sh" && shift && exec laueportal "$@"'

function laueportal(args: readonly string[]): string {
  // Its stderr (config show's notes on where the settings came from) stays out of the test's output.
  return execFileSync("bash", ["-c", SCRIPT, "laueportal", REPO, ...args], {
    encoding: "utf8",
    timeout: 120_000,
    stdio: ["ignore", "pipe", "pipe"],
  })
}

/** The database `laueportal` uses with the e2e stack's settings (`config show`). */
function configuredDatabase(): string {
  const settings = JSON.parse(laueportal(["config", "show"])) as {
    database: { path: string }
  }
  return settings.database.path
}

export interface SlowRun {
  job_id: number
  indexing_id: number
}

/**
 * Check that `laueportal`, with the e2e stack's settings, uses the database of the stack the browser
 * talks to: runs queued in another database would reach this stack's worker through the shared queue.
 */
export async function checkStackDatabase(
  request: APIRequestContext
): Promise<void> {
  const response = await request.get("/api/v1/system/status")
  expect(response.ok()).toBe(true)
  const status = (await response.json()) as SystemStatus
  expect(
    configuredDatabase(),
    "laueportal must use the database of the stack under test (make e2e)"
  ).toBe(status.database.path)
}

/**
 * Queue `count` long demo indexing runs (`inputs` frames each, about 0.25 s a frame) on the stack's
 * database, in order (after checkStackDatabase): the stack's worker starts the first at once, the
 * others wait.
 */
export function queueSlowRuns(count: number, inputs: number): SlowRun[] {
  const output = laueportal([
    "seed",
    "slow-runs",
    "--count",
    String(count),
    "--inputs",
    String(inputs),
  ])
  const { runs } = JSON.parse(output) as { runs: SlowRun[] }
  return runs
}

/** The newest job's ID (0 when there is none): the jobs after it are those queued since. */
export async function lastJobId(request: APIRequestContext): Promise<number> {
  const response = await request.get("/api/v1/jobs?limit=1")
  expect(response.ok()).toBe(true)
  const page = (await response.json()) as JobPage
  return page.rows[0]?.job_id ?? 0
}

/**
 * The demo's run on CRUX (R4, which the e2e stack seeds with `laueportal seed demo --crux-run`, recorded as
 * running there with the golden observation `running`): the only job with a remote observation on a
 * freshly seeded stack.
 */
export async function seededCruxJob(
  request: APIRequestContext
): Promise<JobSummary> {
  const response = await request.get("/api/v1/jobs?limit=1000")
  expect(response.ok()).toBe(true)
  const page = (await response.json()) as JobPage
  const crux = page.rows.filter(
    (job) => job.target === "crux" && job.observation !== null
  )
  expect(crux, "the seeded stack's crux run").toHaveLength(1)
  return crux[0] as JobSummary
}

/**
 * Stop every job after `jobId` that is still queued or running, through the API (a test's cleanup):
 * found in the run monitor's list, so it also stops runs a command queued before it failed.
 * Returns their IDs.
 */
export async function stopJobsAfter(
  request: APIRequestContext,
  jobId: number
): Promise<number[]> {
  const response = await request.get("/api/v1/jobs?limit=1000")
  if (!response.ok()) return []
  const page = (await response.json()) as JobPage
  const active = page.rows.filter(
    (job) =>
      job.job_id > jobId &&
      (job.status === "queued" || job.status === "running")
  )
  for (const job of active) {
    // As the app's fetcher (src/api/fetcher.ts): a request that changes data needs the header, or it is a 403.
    const response = await request.post(
      `/api/v1/jobs/${String(job.job_id)}/cancel`,
      { headers: { "X-Laue-Portal": "1" } }
    )
    expect(response.ok(), await response.text()).toBe(true)
  }
  return active.map((job) => job.job_id)
}

/** The stack's runs root (`config show`'s `files.runs_root`, e2e.sh's): the demo's inputs are under it. */
export function configuredRunsRoot(): string {
  const settings = JSON.parse(laueportal(["config", "show"])) as {
    files: { runs_root: string }
  }
  return settings.files.runs_root
}

/**
 * Admit `path` into the live run `jobId` whose source is `notify`, as an acquisition hook does (`laueportal
 * live notify --complete`), with the stack's settings (after checkStackDatabase).
 */
export function notifyLive(jobId: number, path: string): void {
  laueportal(["live", "notify", String(jobId), path, "--complete"])
}

/**
 * Wait until no local run is queued or running (the demo's R1 and I1 done, a spec's runs ended): a live
 * session would stop a running batch run. The demo's crux run (R4) stays running remotely.
 */
export async function untilLocalRunsEnd(
  request: APIRequestContext,
  timeout = 120_000
): Promise<void> {
  await expect
    .poll(
      async () => {
        const response = await request.get("/api/v1/system/status")
        const status = (await response.json()) as SystemStatus
        return (status.active_runs ?? []).filter((run) => run.remote === null)
          .length
      },
      { timeout, intervals: [1_000] }
    )
    .toBe(0)
}

let fakeDM: string | undefined

/**
 * The stand-in DM's folder (deploy/supervisor/e2e.sh's prepare_stack): `[live] dm_setup_file`'s, as configured
 * (read once).
 */
function fakeDMFolder(): string {
  if (fakeDM === undefined) {
    const settings = JSON.parse(laueportal(["config", "show"])) as {
      live: { dm_setup_file: string | null }
    }
    if (!settings.live.dm_setup_file) throw new Error("no stand-in DM")
    fakeDM = dirname(settings.live.dm_setup_file)
  }
  return fakeDM
}

/** A DAQ as DM's ExperimentDaqApi gives it, and a row of DM's file catalog (tests.remote_support's stand-in). */
export interface FakeDAQ {
  id: string
  experimentName: string
  status: string
  dataDirectory: string
  storageDirectory: string
  destDirectory: string
}
export interface FakeCatalogRow {
  experimentFilePath: string
  fileSize: number
  fileProcessingTime: number
  daqId: string
  experimentName: string
}

/**
 * Replace the e2e stack's stand-in DM's state (atomically, as tests.remote_support.FakeDMInstall.set_state):
 * its DAQs and its catalog's rows, in the order they were catalogued; none: the stack's own, not JSON, so every DM
 * call fails (the DAQ list unavailable).
 */
export function setFakeDM(state?: {
  daqs: FakeDAQ[]
  files: FakeCatalogRow[]
}): void {
  const folder = fakeDMFolder()
  const text = state
    ? JSON.stringify({
        jobs: {},
        failing: [],
        daqs: Object.fromEntries(state.daqs.map((daq) => [daq.id, daq])),
        files: state.files,
        delay: 0,
        start_delay: 0,
      })
    : "not JSON\n"
  writeFileSync(`${folder}/state.partial`, text)
  renameSync(`${folder}/state.partial`, `${folder}/state.json`)
}

/**
 * When each DM call that read the stand-in's catalog started (seconds since the epoch): its calls by pid and time
 * (`calls.log`), those whose pid made a catalog request (`requests.log`); the DAQ list's and a bind check's calls
 * are left out.
 */
export function fakeDMCatalogCalls(): number[] {
  const folder = fakeDMFolder()
  const lines = (name: string) =>
    existsSync(`${folder}/${name}`)
      ? readFileSync(`${folder}/${name}`, "utf8").split("\n").filter(Boolean)
      : []
  const catalog = new Set(
    lines("requests.log").map(
      (line) => (JSON.parse(line) as { pid: number }).pid
    )
  )
  return lines("calls.log")
    .map((line) => line.split(" ").map(Number))
    .filter(([pid]) => catalog.has(pid ?? -1))
    .map(([, at]) => at ?? 0)
}
