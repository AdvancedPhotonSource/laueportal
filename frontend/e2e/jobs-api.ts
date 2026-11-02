import type { Page, Route } from "@playwright/test"

import type { FailurePage, JobDetail, JobLog, JobSummary } from "@/api"
import { failurePage, makeJobLog } from "../src/test/jobs"

// Stand-ins for the jobs API, for the flows the seeded demo database cannot show: a remote run, a
// migrated run with its history, files not on this host, a failed run's report, Stop and Move to
// Front answering 409 or 503. The run monitor's list answers the grid query protocol's paging, sort
// and `changed_since` (the active rows and those changed since, 2 s overlap); it ignores filters.

type Value = string | number | null | undefined

function compare(a: Value, b: Value): number {
  if (a == null || b == null) return a == null ? (b == null ? 0 : -1) : 1
  if (typeof a === "number" && typeof b === "number") return a - b
  return String(a).toLowerCase().localeCompare(String(b).toLowerCase())
}

function isActive(job: JobSummary): boolean {
  return job.status === "queued" || job.status === "running"
}

/** What a POST to /api/v1/jobs/{id}/cancel or /move-to-front answers (default: 204 / requested). */
export type JobAction = (
  jobId: number,
  action: "cancel" | "move-to-front"
) => { status: number; json?: unknown; contentType?: string }

export interface JobListMock {
  /** Every request's method and URL, oldest first. */
  requests: { method: string; url: URL }[]
  /** Replace the rows: later answers see them. */
  update: (jobs: JobSummary[]) => void
  /** How Stop and Move to Front answer from now on. */
  answer: (action: JobAction) => void
}

/** Answer the run monitor: GET /api/v1/jobs from `jobs` (newest first by default), and its actions. */
export async function mockJobList(
  page: Page,
  jobs: JobSummary[],
  action?: JobAction
): Promise<JobListMock> {
  const requests: { method: string; url: URL }[] = []
  let table = jobs
  let respond: JobAction =
    action ??
    ((jobId, kind) =>
      kind === "cancel"
        ? {
            status: 200,
            json: {
              job_id: jobId,
              state: "requested",
              n_pending: 0,
            },
          }
        : { status: 204 })
  const changed = new Map<number, number>(jobs.map((job) => [job.job_id, 0]))
  await page.route("**/api/v1/jobs?*", (route: Route) => {
    const url = new URL(route.request().url())
    requests.push({ method: "GET", url })
    const params = url.searchParams
    const since = params.get("changed_since")
    const sort = (params.get("sort") ?? "-job_id").split(",")
    let selected = [...table].sort((a, b) => {
      for (const key of [...sort, "-job_id"]) {
        const field = key.replace(/^-/, "") as keyof JobSummary
        const order = compare(a[field] as Value, b[field] as Value)
        if (order !== 0) return key.startsWith("-") ? -order : order
      }
      return 0
    })
    if (since) {
      const from = Date.parse(since) - 2000
      selected = selected.filter(
        (job) => isActive(job) || (changed.get(job.job_id) ?? 0) >= from
      )
    }
    const offset = Number(params.get("offset") ?? 0)
    const limit = Number(params.get("limit") ?? 100)
    return route.fulfill({
      json: {
        rows: selected.slice(offset, offset + limit),
        total: selected.length,
        as_of: new Date().toISOString(),
      },
    })
  })
  await page.route(/\/api\/v1\/jobs\/\d+\/(cancel|move-to-front)$/, (route) => {
    const url = new URL(route.request().url())
    requests.push({ method: route.request().method(), url })
    const [, id, kind] =
      /\/jobs\/(\d+)\/(cancel|move-to-front)$/.exec(url.pathname) ?? []
    const answer = respond(
      Number(id),
      kind === "cancel" ? "cancel" : "move-to-front"
    )
    if (answer.status === 204) return route.fulfill({ status: 204 })
    // A problem (e2e/problems.ts) brings its own contentType.
    return route.fulfill(answer)
  })
  return {
    requests,
    update: (next) => {
      const now = Date.now()
      const before = new Map(
        table.map((job) => [job.job_id, JSON.stringify(job)])
      )
      for (const job of next) {
        if (before.get(job.job_id) !== JSON.stringify(job))
          changed.set(job.job_id, now)
      }
      table = next
    },
    answer: (next) => {
      respond = next
    },
  }
}

export interface JobMock {
  /** Every GET's URL, oldest first (the job, its failures, its log). */
  requests: URL[]
  /** Replace the job (it progressed or ended): later answers see it. */
  update: (job: JobDetail) => void
  updateLog: (log: JobLog) => void
}

/**
 * Answer the job page: GET /api/v1/jobs/{id} with `job`, its failures from `failures(offset, limit)`
 * (none by default) and its log (`log`, an empty run log by default).
 */
export async function mockJob(
  page: Page,
  job: JobDetail,
  options: {
    failures?: (offset: number, limit: number) => FailurePage
    log?: JobLog
  } = {}
): Promise<JobMock> {
  const requests: URL[] = []
  let current = job
  let log = options.log ?? makeJobLog(job.job_id)
  const failures =
    options.failures ?? ((offset, limit) => failurePage(0, offset, limit))
  const id = String(job.job_id)
  await page.route(
    new RegExp(`/api/v1/jobs/${id}(/failures|/log)?(\\?.*)?$`),
    (route: Route) => {
      const url = new URL(route.request().url())
      requests.push(url)
      if (url.pathname.endsWith("/failures")) {
        return route.fulfill({
          json: failures(
            Number(url.searchParams.get("offset") ?? 0),
            Number(url.searchParams.get("limit") ?? 50)
          ),
        })
      }
      if (url.pathname.endsWith("/log")) return route.fulfill({ json: log })
      return route.fulfill({ json: current })
    }
  )
  return {
    requests,
    update: (next) => {
      current = next
    },
    updateLog: (next) => {
      log = next
    },
  }
}
