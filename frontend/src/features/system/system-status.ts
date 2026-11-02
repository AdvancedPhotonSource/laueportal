// What the status page shows, derived from GET /api/v1/system/status: the Connection, Worker, Queue and
// Active Runs cards, and an alert for each part of the system that is down. No React here:
// StatusPage.tsx renders it.
import type {
  ActiveRun,
  QueuedItem,
  RunReference,
  RunStatus,
  SystemStatus,
  WorkerStatus,
  WorkerThread,
} from "@/api"
import { formatAge, formatDateTime } from "@/lib/format"
import type { RunCounts } from "@/lib/run-status"

/** The page asks for the status this often, only while the tab is visible. */
export const STATUS_POLL_MS = 5_000

export type Tone = "ok" | "warning" | "error" | "unknown"

/** A line of the Connection card. */
export interface ServiceView {
  name: "Database" | "API"
  tone: Tone
  /** The badge: "OK" or "Error". */
  state: string
  /** Under it: the database's file, the API's version. */
  detail: string
}

/** The database (and its file, shown to the user) and the API that answered. */
export function connectionView(status: SystemStatus): ServiceView[] {
  const database = status.database.status === "ok"
  return [
    {
      name: "Database",
      tone: database ? "ok" : "error",
      state: database ? "OK" : "Error",
      detail: status.database.path,
    },
    {
      name: "API",
      tone: "ok",
      state: "OK",
      detail: `Version ${status.version}`,
    },
  ]
}

const WORKER_STATE: Record<
  WorkerStatus["state"],
  { tone: Tone; label: string }
> = {
  running: { tone: "ok", label: "Running" },
  late: { tone: "warning", label: "Late" },
  not_running: { tone: "error", label: "Not running" },
}

const THREAD_NAME: Record<WorkerThread["name"], string> = {
  dispatcher: "Dispatcher",
  reconciler: "Reconciler",
  poller: "Remote poller",
}

const THREAD_STATE: Record<
  WorkerThread["state"],
  { tone: Tone; label: string }
> = {
  ok: { tone: "ok", label: "OK" },
  late: { tone: "warning", label: "Late" },
  off: { tone: "unknown", label: "Off" },
}

/** A thread of the Worker card. */
export interface ThreadView {
  name: string
  tone: Tone
  /** The badge: "OK", "Late", "Off". */
  state: string
  /** Its last mark's age ("4 s ago"), or "None". */
  mark: string
  /** The badge's tooltip: "Late after 45 s"; none for an Off thread, which cannot be late. */
  hint?: string
}

export type WorkerView =
  | { kind: "unknown" }
  | {
      kind: "worker"
      tone: Tone
      state: string
      /** Host, PID, Started and, once recorded, Stopped; empty for a worker that never started. */
      rows: { label: string; value: string; mono?: boolean }[]
      threads: ThreadView[]
    }

export function threadView(thread: WorkerThread): ThreadView {
  const age = thread.last_mark_age_seconds
  const state = THREAD_STATE[thread.state]
  return {
    name: THREAD_NAME[thread.name],
    tone: state.tone,
    state: state.label,
    mark: age === null ? "None" : `${formatAge(age)} ago`,
    ...(thread.late_after_seconds !== null && {
      hint: `Late after ${formatAge(thread.late_after_seconds)}`,
    }),
  }
}

/** The Worker card: its state, where and since when it runs, and its threads; unknown when the database is down. */
export function workerView(status: SystemStatus): WorkerView {
  const worker = status.worker
  if (worker === null) return { kind: "unknown" }
  const rows: { label: string; value: string; mono?: boolean }[] = []
  if (worker.host !== null) {
    rows.push(
      { label: "Host", value: worker.host, mono: true },
      { label: "PID", value: String(worker.pid ?? "") },
      { label: "Started", value: formatDateTime(worker.started_at) }
    )
  }
  if (worker.stopped_at !== null) {
    rows.push({ label: "Stopped", value: formatDateTime(worker.stopped_at) })
  }
  const state = WORKER_STATE[worker.state]
  return {
    kind: "worker",
    tone: state.tone,
    state: state.label,
    rows,
    threads: worker.threads.map(threadView),
  }
}

/** A row of the Queue card's next items. */
export interface NextItemView {
  key: string
  /** The run, or the indexing run a conversion converts. */
  run: RunReference | null
  jobId: number | null
  conversion: boolean
  movedToFront: boolean
}

export type QueueView =
  | { kind: "unknown" }
  | {
      kind: "queue"
      counts: { label: string; value: number | string }[]
      next: NextItemView[]
    }

export function nextItemView(item: QueuedItem): NextItemView {
  return {
    key:
      item.kind === "conversion"
        ? `conversion-${String(item.run?.id)}`
        : `run-${String(item.job_id)}`,
    run: item.run,
    jobId: item.job_id,
    conversion: item.kind === "conversion",
    movedToFront: item.moved_to_front,
  }
}

/**
 * The Queue card: the job table's counts, the live slots in use (of the worker's two) and the next items in
 * dispatch order; unknown when the database is down.
 */
export function queueView(status: SystemStatus): QueueView {
  if (status.queue === null) return { kind: "unknown" }
  return {
    kind: "queue",
    counts: [
      { label: "Queued runs", value: status.queue.queued_runs },
      { label: "Running", value: status.queue.running },
      { label: "Queued conversions", value: status.queue.queued_conversions },
      {
        label: "Live slots",
        value: `${String(status.queue.live_slots_in_use)}/${String(status.queue.live_slots)}`,
      },
    ],
    next: (status.next_up ?? []).map(nextItemView),
  }
}

/** An alert above the cards for a part of the system that is down: what is missing, in one line. */
export interface SystemAlertView {
  title: string
  /** The database's file. */
  detail?: string
}

export function systemAlerts(status: SystemStatus): SystemAlertView[] {
  const alerts: SystemAlertView[] = []
  if (status.database.status !== "ok") {
    alerts.push({
      title: "The database does not answer",
      detail: `${status.database.path} cannot be read`,
    })
  }
  if (status.worker?.state === "not_running") {
    alerts.push({ title: "The worker is not running" })
  }
  return alerts
}

/** A row of the Active Runs card. */
export interface ActiveRunView {
  jobId: number
  /** The indexing or reconstruction run the job executes (`I12`, `R3`); null for neither. */
  run: RunReference | null
  status: RunStatus
  counts: RunCounts
  /** Inputs processed, with or without an error. */
  processed: number
  /** "12 s ago", "Not started"; a remote run's is the poller's: "Polled 12 s ago". */
  heartbeat: string
  /** A running local run without a recent heartbeat (the API's `stale`). */
  stale: boolean
  stopRequested: boolean
  /** A remote run's target and DM's state; null for a local run. */
  remote: { target: string; dmState: string } | null
}

export type ActiveRunsView =
  | { kind: "unknown" }
  | { kind: "none" }
  | { kind: "runs"; runs: ActiveRunView[]; stale: boolean }

/** The active runs, newest first (as the API lists them); unknown when the database is down. */
export function activeRunsView(status: SystemStatus): ActiveRunsView {
  const runs = status.active_runs
  if (runs === null) return { kind: "unknown" }
  if (runs.length === 0) return { kind: "none" }
  const views = runs.map(activeRunView)
  return { kind: "runs", runs: views, stale: views.some((run) => run.stale) }
}

/** The heartbeat's age in words. */
export function heartbeatText(run: ActiveRun): string {
  const age = run.heartbeat_age_seconds
  if (age === null) return run.status === "queued" ? "Not started" : "None"
  const ago = `${formatAge(age)} ago`
  return run.remote ? `Polled ${ago}` : ago
}

export function activeRunView(run: ActiveRun): ActiveRunView {
  return {
    jobId: run.job_id,
    run: run.run,
    status: run.status,
    counts: {
      n_inputs: run.n_inputs,
      n_succeeded: run.n_succeeded,
      n_failed: run.n_failed,
      n_not_run: run.n_not_run,
    },
    processed: run.n_succeeded + run.n_failed,
    heartbeat: heartbeatText(run),
    stale: run.stale,
    stopRequested: run.stop_requested,
    remote: run.remote
      ? {
          target: run.remote.target,
          dmState: run.remote.dm_state ?? "not submitted",
        }
      : null,
  }
}
