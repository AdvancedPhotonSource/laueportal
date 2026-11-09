import type { Page, Route } from "@playwright/test"

import type {
  ActiveRun,
  Health,
  IndexingRunSummary,
  ReconstructionRunSummary,
  SystemStatus,
} from "@/api"
import { makeWorker } from "../src/test/system"

// Stand-ins for the run lists (GET /api/v1/reconstruction-runs and /api/v1/indexing-runs) and the
// status (GET /api/v1/system/status), for the flows the seeded demo database cannot show: running
// runs whose counters move, many runs, a remote run, the database down. The lists answer the grid
// query protocol's paging, sort and `changed_since` (the active rows and the rows changed since, 2 s
// overlap); they ignore filters. A test changes the rows between requests.

/** A wire reconstruction run, finished unless `overrides` say otherwise. */
export function reconstructionRun(
  id: number,
  overrides: Partial<ReconstructionRunSummary> = {}
): ReconstructionRunSummary {
  const created = Date.UTC(2026, 9, 1, 14, 0) + id * 3_600_000
  return {
    id,
    method: "wire",
    scan_number: 990_000 + (id % 7),
    sample_name: "Si",
    n_points: 441,
    author: ["jdoe", "mdoe", "rdoe"][id % 3] ?? null,
    notes: id % 2 === 0 ? `Depth scan, part ${String(id)}` : null,
    created_at: new Date(created).toISOString(),
    start_time: new Date(created + 60_000).toISOString(),
    finish_time: new Date(created + 1_800_000).toISOString(),
    status: "finished",
    n_inputs: 441,
    n_succeeded: 441,
    n_failed: 0,
    n_not_run: 0,
    n_processed: 441,
    depth_start: -50,
    depth_end: 50,
    percent_brightest: 2,
    ...overrides,
  }
}

/** A LaueGo indexing run of a reconstruction, finished unless `overrides` say otherwise. */
export function indexingRun(
  id: number,
  overrides: Partial<IndexingRunSummary> = {}
): IndexingRunSummary {
  const created = Date.UTC(2026, 9, 1, 15, 0) + id * 3_600_000
  return {
    id,
    method: "lauego",
    method_label: "LaueGo",
    scan_number: 990_000 + (id % 7),
    sample_name: "Si",
    reconstruction_id: id % 3 === 0 ? null : id,
    n_points: 441,
    n_depths: id % 3 === 0 ? null : 101,
    author: ["jdoe", "mdoe", "rdoe"][id % 3] ?? null,
    notes: id % 2 === 0 ? `Grain map ${String(id)}` : null,
    box_size: 5,
    crystal_file: "Si.xtal",
    created_at: new Date(created).toISOString(),
    status: "finished",
    n_inputs: 441,
    n_succeeded: 441,
    n_failed: 0,
    n_not_run: 0,
    n_processed: 441,
    ...overrides,
  }
}

/** Counters `processed` of `total` in, for a running run. */
export function running(processed: number, total = 441) {
  return {
    status: "running" as const,
    finish_time: null,
    n_inputs: total,
    n_succeeded: processed,
    n_failed: 0,
    n_not_run: 0,
    n_processed: processed,
  }
}

type RunRow = ReconstructionRunSummary | IndexingRunSummary

function isActive(row: RunRow): boolean {
  return row.status === "queued" || row.status === "running"
}

// The run lists' fields are all strings, numbers or null.
type Value = string | number | null | undefined

function compare(a: Value, b: Value): number {
  if (a == null || b == null) return a == null ? (b == null ? 0 : -1) : 1
  if (typeof a === "number" && typeof b === "number") return a - b
  return String(a).toLowerCase().localeCompare(String(b).toLowerCase())
}

export interface RunListMock<TRow extends RunRow> {
  /** Every request's URL, oldest first. */
  requests: URL[]
  /** Replace the rows (a run progressed, finished or appeared): later answers see them. */
  update: (rows: TRow[]) => void
}

/**
 * Answer the page's `GET <path>` (`/api/v1/reconstruction-runs`, `/api/v1/indexing-runs`) from
 * `rows`, newest first by default.
 */
export async function mockRunList<TRow extends RunRow>(
  page: Page,
  path: string,
  rows: TRow[]
): Promise<RunListMock<TRow>> {
  const requests: URL[] = []
  let table = rows
  // When each row last changed (its ID), for changed_since.
  const changed = new Map<number, number>(rows.map((row) => [row.id, 0]))
  await page.route(`**${path}?*`, (route: Route) => {
    const url = new URL(route.request().url())
    requests.push(url)
    const params = url.searchParams
    const since = params.get("changed_since")
    const sort = (params.get("sort") ?? "-id").split(",")
    let selected = [...table].sort((a, b) => {
      for (const key of [...sort, "-id"]) {
        const field = key.replace(/^-/, "") as keyof TRow
        const order = compare(a[field] as Value, b[field] as Value)
        if (order !== 0) return key.startsWith("-") ? -order : order
      }
      return 0
    })
    if (since) {
      const from = Date.parse(since) - 2000
      selected = selected.filter(
        (row) => isActive(row) || (changed.get(row.id) ?? 0) >= from
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
  return {
    requests,
    update: (next) => {
      const now = Date.now()
      const before = new Map(table.map((row) => [row.id, JSON.stringify(row)]))
      for (const row of next) {
        if (before.get(row.id) !== JSON.stringify(row)) changed.set(row.id, now)
      }
      table = next
    },
  }
}

export const HEALTH_OK: Health = {
  db: "ok",
  worker: "running",
  version: "2.0.0.dev0",
}

/** A running local indexing run (job 41, I72) and a remote reconstruction (job 40, R12). */
export const ACTIVE_RUNS: ActiveRun[] = [
  {
    job_id: 41,
    run: { kind: "indexing", id: 72, label: "I72" },
    status: "running",
    phase: "running",
    n_inputs: 441,
    n_succeeded: 120,
    n_failed: 3,
    n_not_run: 0,
    heartbeat_age_seconds: 4,
    stale: false,
    stop_requested: false,
    remote: null,
  },
  {
    job_id: 40,
    run: { kind: "reconstruction", id: 12, label: "R12" },
    status: "running",
    phase: "running",
    n_inputs: 441,
    n_succeeded: 300,
    n_failed: 0,
    n_not_run: 0,
    heartbeat_age_seconds: 25,
    stale: false,
    stop_requested: false,
    remote: { target: "aps", dm_state: "running" },
  },
]

export const STATUS_OK: SystemStatus = {
  database: {
    status: "ok",
    path: "/home/user/portal/laueportal.sqlite",
  },
  worker: makeWorker(),
  queue: {
    queued_runs: 1,
    running: 1,
    queued_conversions: 1,
    live_slots_in_use: 1,
    live_slots: 2,
  },
  next_up: [
    {
      kind: "conversion",
      job_id: null,
      run: { kind: "indexing", id: 70, label: "I70" },
      moved_to_front: false,
    },
    {
      kind: "run",
      job_id: 42,
      run: { kind: "indexing", id: 73, label: "I73" },
      moved_to_front: false,
    },
  ],
  version: "2.0.0.dev0",
  active_runs: ACTIVE_RUNS,
}

/**
 * Answer GET /api/v1/system/status with `answer()` (read on every request, so a test can change
 * it), and the navbar's health check with the matching database and worker states.
 */
export async function mockStatus(page: Page, answer: () => SystemStatus) {
  await page.route("**/api/v1/system/status", (route: Route) =>
    route.fulfill({ json: answer() })
  )
  await page.route("**/api/v1/health", (route: Route) => {
    const status = answer()
    return route.fulfill({
      json: {
        ...HEALTH_OK,
        db: status.database.status,
        worker: status.worker?.state ?? null,
      },
    })
  })
}
