// The run monitor's actions: which selected jobs Stop and Move to Front act on, running them one
// job at a time, and what the toast says about each job. Stop also serves the job page's Stop Run.
// No React here.
import {
  ApiError,
  CancelState,
  NetworkError,
  RunStatus,
  type CancelResult,
  type JobSummary,
  type RemotePhase,
  type RunReference,
} from "@/api"
import { describeError } from "@/lib/errors"

/** A job an action applies to: its ID, its run (`I72`) and where it executes. */
export interface JobRef {
  jobId: number
  /** The run's label (`I72`, `R3`); null for a job without a run. */
  run: string | null
  /** `local`, or a remote target (`aps`, `crux`). */
  target: string
  status: RunStatus
  /** A live run: its stop seals it (`LIVE_STOP_EXPLANATION`). */
  live?: boolean
}

export function jobRef(job: {
  job_id: number
  run: RunReference | null
  target: string
  status: RunStatus
  live?: boolean
}): JobRef {
  return {
    jobId: job.job_id,
    run: job.run?.label ?? null,
    target: job.target,
    status: job.status,
    ...(job.live && { live: true }),
  }
}

/** "Job 7 (I73)", or "Job 7" for a job without a run. */
export function jobName(job: JobRef): string {
  const name = `Job ${String(job.jobId)}`
  return job.run ? `${name} (${job.run})` : name
}

export function isRemote(target: string): boolean {
  return target !== "local"
}

/** A remote target's name, in capitals: "APS", "CRUX". */
export function targetName(target: string): string {
  return target.toUpperCase()
}

/** A `crux` run's remote phase by its label (the backend's `PHASE_LABELS`). */
const REMOTE_PHASE_LABELS: Record<RemotePhase, string> = {
  uplink: "Uplink",
  queued: "Queued",
  running: "Running",
  downlink: "Downlink",
  login_required: "Login needed",
}

/** The remote phase badge (the run monitor's Status cell, the job page's Remote Run card): "Uplink", ... */
export function remotePhaseText(phase: RemotePhase): string {
  return REMOTE_PHASE_LABELS[phase]
}

export interface RunMonitorActions {
  /** The rows selected. */
  count: number
  /** The queued and running jobs, by job ID: Stop is enabled when there are some. */
  stoppable: JobRef[]
  /** The queued jobs, by job ID: Move to Front is enabled when there are some. */
  movable: JobRef[]
}

function byJobId(a: JobRef, b: JobRef): number {
  return a.jobId - b.jobId
}

/** The buttons' rules: Stop for queued or running jobs, Move to Front for queued ones. */
export function runMonitorActions(
  selected: readonly JobSummary[]
): RunMonitorActions {
  const jobs = selected.map(jobRef).sort(byJobId)
  return {
    count: selected.length,
    stoppable: jobs.filter(
      (job) =>
        job.status === RunStatus.queued || job.status === RunStatus.running
    ),
    movable: jobs.filter((job) => job.status === RunStatus.queued),
  }
}

/** What Stop does, in its confirm dialogs: one sentence. */
export const STOP_EXPLANATION =
  "A queued run leaves the queue; a running run finishes the inputs in flight, keeps its completed results and ends as Cancelled."

/** What Stop does to a live run, in its confirm dialogs and its report: one line. */
export const LIVE_STOP_EXPLANATION =
  "No new files are taken; the files taken are finished."

/** What the Live page's Stop now does, in its confirm dialog: one sentence. */
export const LIVE_STOP_NOW_EXPLANATION =
  "Every open run is stopped; a run already stopping is killed and ends Failed, keeping what it published."

/**
 * What Stop does to the runs a dialog stops: a live run's line when every one is live, else the batch
 * sentence, with the live line as a note when some are live (a live run is sealed, not cancelled).
 */
export function stopExplanation(jobs: readonly JobRef[]): {
  description: string
  note?: string
} {
  const live = jobs.filter((job) => job.live).length
  if (live > 0 && live === jobs.length) {
    return { description: LIVE_STOP_EXPLANATION }
  }
  return live === 0
    ? { description: STOP_EXPLANATION }
    : {
        description: STOP_EXPLANATION,
        note: `Live runs: ${LIVE_STOP_EXPLANATION}`,
      }
}

/** What Move to Front does, in its confirm dialog: one sentence. */
export const MOVE_EXPLANATION =
  "The selected runs start before the other queued runs, oldest first, after any run already running."

/** A confirm dialog's note on the selected jobs an action leaves alone ("2 selected runs have ended"). */
export function leftAlone(
  count: number,
  why: "ended" | "not queued"
): string | undefined {
  if (count === 0) return undefined
  const runs = count === 1 ? "1 selected run" : `${String(count)} selected runs`
  const state =
    why === "ended"
      ? count === 1
        ? "has ended"
        : "have ended"
      : count === 1
        ? "is not queued"
        : "are not queued"
  return `${runs} ${state} and ${count === 1 ? "is" : "are"} left alone.`
}

/** "Stop 1 run?", "Stop 3 runs?" */
export function stopTitle(count: number): string {
  return count === 1 ? "Stop 1 run?" : `Stop ${String(count)} runs?`
}

/** "Move 1 queued run to the front?" */
export function moveTitle(count: number): string {
  return count === 1
    ? "Move 1 queued run to the front?"
    : `Move ${String(count)} queued runs to the front?`
}

/**
 * The order Move to Front moves the jobs in: newest first, each to the front, so that they end up
 * ahead of the other queued runs in their own order (oldest first).
 */
export function moveOrder(jobs: readonly JobRef[]): JobRef[] {
  return [...jobs].sort((a, b) => b.jobId - a.jobId)
}

// ---------------------------------------------------------------------------
// Results, one job at a time

export type Outcome<T> =
  | { job: JobRef; ok: true; value: T }
  | { job: JobRef; ok: false; error: unknown }

/** Run `action` on each job in turn (never two requests at once); a failure does not stop the rest. */
export async function runEach<T>(
  jobs: readonly JobRef[],
  action: (jobId: number) => Promise<T>
): Promise<Outcome<T>[]> {
  const outcomes: Outcome<T>[] = []
  for (const job of jobs) {
    try {
      outcomes.push({ job, ok: true, value: await action(job.jobId) })
    } catch (error) {
      outcomes.push({ job, ok: false, error })
    }
  }
  return outcomes
}

export type Tone = "success" | "info" | "warning" | "error"

/** A toast line about one job. */
export interface OutcomeLine {
  jobId: number
  text: string
  tone: Tone
}

/** A toast: its title and tone, and a line per job (by job ID). */
export interface ActionReport {
  title: string
  tone: Tone
  lines: OutcomeLine[]
}

function plural(count: number, noun: string): string {
  return `${String(count)} ${noun}${count === 1 ? "" : "s"}`
}

function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404
}

/** What the server said, as "title: detail" (or that it could not be reached). */
function reason(error: unknown): string {
  if (error instanceof NetworkError) return "the server cannot be reached"
  const { title, detail } = describeError(error)
  return detail ? `${title}: ${detail}` : title
}

/** What a stop did to one job (`POST /api/v1/jobs/{id}/cancel`'s CancelResult). */
export function stopLine(job: JobRef, result: CancelResult): OutcomeLine {
  const name = jobName(job)
  const line = (text: string, tone: Tone): OutcomeLine => ({
    jobId: job.jobId,
    text,
    tone,
  })
  const pending = result.n_pending ?? 0
  switch (result.state) {
    case CancelState.cancelled:
      return line(
        `${name} is cancelled; none of its inputs were processed.`,
        "success"
      )
    case CancelState.requested: {
      // n_pending counts the inputs not processed by the stored counters, which lag behind the run (by up to
      // execution.progress_seconds), and the inputs in flight still finish: fewer may end up not run.
      const notRun =
        pending > 0
          ? `up to ${plural(pending, "pending input")} will be recorded as not run.`
          : "no pending input is left to record as not run."
      if (job.live) {
        return line(
          `${name}: stop requested. ${LIVE_STOP_EXPLANATION}`,
          "success"
        )
      }
      return isRemote(job.target)
        ? line(
            `${name}: stop requested; the remote poller delivers it to ${targetName(job.target)}. Completed results are kept; ${notRun}`,
            "success"
          )
        : line(
            `${name}: stop requested. The inputs in flight finish and completed results are kept; ${notRun}`,
            "success"
          )
    }
    case CancelState.already_terminal:
      return line(`${name} had already ended; nothing changed.`, "info")
    case CancelState.not_found:
      return line(`${name} no longer exists.`, "error")
    case CancelState.stopping_now:
      return line(
        `${name}: stopping now; its job process is killed and it ends Failed, keeping what it published.`,
        "success"
      )
    case CancelState.not_live:
      return line(`${name} is not a live run; nothing changed.`, "error")
  }
}

/** A stop that failed (the request itself). */
export function stopErrorLine(job: JobRef, error: unknown): OutcomeLine {
  const text = isNotFound(error)
    ? `${jobName(job)} no longer exists.`
    : `${jobName(job)} was not stopped: ${reason(error)}.`
  return { jobId: job.jobId, text, tone: "error" }
}

// The 409 problems of POST /api/v1/jobs/{id}/move-to-front, by title (jobs/service.py).
export const MOVE_CONFLICTS = {
  notQueued: "Job not queued",
  remote: "Job runs remotely",
} as const

/** A move to the front that the server refused or that failed. */
export function moveErrorLine(job: JobRef, error: unknown): OutcomeLine {
  const name = jobName(job)
  const notMoved = (why: string): OutcomeLine => ({
    jobId: job.jobId,
    text: `${name} was not moved: ${why}`,
    tone: "error",
  })
  if (isNotFound(error)) {
    return {
      jobId: job.jobId,
      text: `${name} no longer exists.`,
      tone: "error",
    }
  }
  if (error instanceof ApiError && error.status === 409) {
    switch (error.problem?.title) {
      case MOVE_CONFLICTS.notQueued:
        return notMoved("it is no longer waiting in the queue.")
      case MOVE_CONFLICTS.remote:
        return notMoved(
          `it runs on ${targetName(job.target)}, whose scheduler orders it.`
        )
    }
  }
  return notMoved(`${reason(error)}.`)
}

function reportTone(lines: readonly OutcomeLine[]): Tone {
  const errors = lines.filter((line) => line.tone === "error").length
  if (errors > 0) return errors === lines.length ? "error" : "warning"
  if (lines.some((line) => line.tone === "warning")) return "warning"
  if (lines.every((line) => line.tone === "info")) return "info"
  return "success"
}

function byLineJob(a: OutcomeLine, b: OutcomeLine): number {
  return a.jobId - b.jobId
}

/** The toast after Stop: a line per job. */
export function stopReport(
  outcomes: readonly Outcome<CancelResult>[],
  title = "Stop Runs"
): ActionReport {
  const lines = outcomes
    .map((outcome) =>
      outcome.ok
        ? stopLine(outcome.job, outcome.value)
        : stopErrorLine(outcome.job, outcome.error)
    )
    .sort(byLineJob)
  return { title, tone: reportTone(lines), lines }
}

/** The toast after Move to Front: a line per job. */
export function moveReport(
  outcomes: readonly Outcome<unknown>[]
): ActionReport {
  const lines = outcomes
    .map((outcome) =>
      outcome.ok
        ? {
            jobId: outcome.job.jobId,
            text: `${jobName(outcome.job)} moved to the front of the queue.`,
            tone: "success" as const,
          }
        : moveErrorLine(outcome.job, outcome.error)
    )
    .sort(byLineJob)
  return { title: "Move to Front", tone: reportTone(lines), lines }
}
