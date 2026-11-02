// The job page's logic: its route's job and loader, its polling, the notes on the status, the
// heartbeat and a stop, the run files' states, the configuration's rows and the failure report's
// pages. No React here: the cards render it.
import type { QueryClient } from "@tanstack/react-query"
import { notFound } from "@tanstack/react-router"

import {
  ApiError,
  FileState,
  getGetJobQueryOptions,
  RunStatus,
  type FailurePage,
  type JobDetail,
  type JobLive,
  type RunReference,
  type RunArtifact,
  type RunConfiguration,
  type RunExecution,
  type RunFile,
  type RunHistory,
  type SubmittedRequest,
} from "@/api"
import { formatAge, formatCount, formatDateTime } from "@/lib/format"
import { backoff, isGone } from "@/lib/polling"
import { isActiveStatus } from "@/lib/run-status"
import { remotePhaseView, type RemotePhaseView } from "./remote-status"
import { jobRef, type JobRef } from "./run-monitor"

/** A queued or running job's page asks for it this often, while the tab shows. */
export const JOB_POLL_MS = 2_000
/** A stale run's job page asks for it this often. */
export const JOB_STALE_POLL_MS = 30_000
/** Failed inputs per page. */
export const FAILURE_PAGE_SIZE = 50
/** Log lines shown. */
export const LOG_TAIL_LINES = 50

// ---------------------------------------------------------------------------
// The route's job

/** The job ID of the URL's `jobId`, or undefined when it is not one (then the page is a 404). */
export function parseJobId(text: string): number | undefined {
  if (!/^\d+$/.test(text)) return undefined
  const value = Number(text)
  return Number.isSafeInteger(value) ? value : undefined
}

/**
 * The route's loader: the job into the query cache (the page reads it with useGetJob), or notFound()
 * for a job ID that is not one or a job the API does not know (404). Other errors go to the route's
 * error boundary.
 */
export async function loadJob(
  queryClient: QueryClient,
  jobId: string
): Promise<void> {
  const job = parseJobId(jobId)
  if (job === undefined) throw notFound()
  const { queryKey, queryFn } = getGetJobQueryOptions(job)
  try {
    await queryClient.query({ queryKey, queryFn, staleTime: "static" })
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) throw notFound()
    throw error
  }
}

/**
 * The job page's polling: every 2 s while the job is queued or running, every 30 s while its run is
 * stale (the heartbeat is missing or old: the run process is probably gone), each doubled per
 * consecutive failed poll (src/lib/polling.ts); none once it has ended or is gone (a 404).
 */
export function jobRefetchInterval(
  job: Pick<JobDetail, "status" | "progress"> | undefined,
  error: unknown,
  failures: number
): number | false {
  if (isGone(error) || !job || !isActiveStatus(job.status)) return false
  return backoff(job.progress.stale ? JOB_STALE_POLL_MS : JOB_POLL_MS, failures)
}

/**
 * What the job's files depend on: when it changes (the run started, finished, failed), the log and
 * the failure report are read again (they are written when the run ends).
 */
export function jobRevision(job: Pick<JobDetail, "status" | "phase">): string {
  return `${job.status}/${job.phase ?? ""}`
}

// ---------------------------------------------------------------------------
// Job Details and Run Progress

/**
 * The note beside the status badge, when the finer phase says more than the status: an interrupted
 * run, one finalizing, or one created or published but not yet queued: the phase's name, or "not
 * yet queued" for a created or published run.
 */
export function phaseNote(
  status: RunStatus,
  phase: string | null
): string | undefined {
  if (!phase || phase === status) return undefined
  if (phase === "created" || phase === "published") return "not yet queued"
  return phase.replaceAll("_", " ")
}

export interface ProgressNote {
  text: string
  /** `alert`: the run is probably gone (a stale heartbeat); `warning`: a stop is on its way. */
  tone: "muted" | "warning" | "alert"
}

/**
 * The Run Progress card's notes: a running local run's heartbeat age against the stale limit (a
 * remote run has none: its card says when the poller last looked), a stop requested, and once the
 * run has ended what it processed.
 */
export function progressNotes(job: JobDetail): ProgressNote[] {
  const { progress } = job
  const notes: ProgressNote[] = []
  const remote = job.remote !== null
  if (job.status === RunStatus.running && !remote) {
    const age = progress.heartbeat_age_seconds
    const limit = progress.stale_after_seconds
    const limitText = limit === null ? "" : formatAge(limit)
    if (progress.stale) {
      const silent =
        age === null
          ? "No heartbeat recorded"
          : `No heartbeat for ${formatAge(age)}${limit === null ? "" : ` (limit ${limitText})`}`
      notes.push({
        text: `${silent}: the run process is probably gone.`,
        tone: "alert",
      })
    } else if (age === null) {
      notes.push({ text: "No heartbeat recorded yet.", tone: "muted" })
    } else {
      notes.push({
        text: `Last heartbeat ${formatAge(age)} ago${limit === null ? "" : ` (stale after ${limitText})`}.`,
        tone: "muted",
      })
    }
  }
  if (progress.stop_requested) {
    const at = progress.stop_requested_at
      ? ` at ${formatDateTime(progress.stop_requested_at)}`
      : ""
    notes.push({ text: `Stop requested${at}.`, tone: "warning" })
  }
  if (!isActiveStatus(job.status) && progress.n_inputs > 0) {
    notes.push({
      text: `Processed ${String(progress.n_processed)} of ${plural(progress.n_inputs, "input")}: ${String(progress.n_succeeded)} succeeded, ${String(progress.n_failed)} failed, ${String(progress.n_not_run)} not run.`,
      tone: "muted",
    })
  }
  return notes
}

function plural(count: number, noun: string): string {
  return `${String(count)} ${noun}${count === 1 ? "" : "s"}`
}

/**
 * Run Progress without inputs: a batch run selected none; an open live run has none yet (its run
 * counts an arrival within a few seconds, so the Live Run card's Arrivals may be ahead), an ended one
 * none arrived.
 */
export function noInputsText(job: Pick<JobDetail, "live" | "status">): string {
  if (job.live === null) return "This job selected no inputs."
  return isActiveStatus(job.status) ? "No inputs yet." : "No inputs arrived."
}

/** The six counters, in the card's order. */
export function progressCounters(
  job: JobDetail
): { label: string; value: number }[] {
  const p = job.progress
  return [
    { label: "Inputs", value: p.n_inputs },
    { label: "Processed", value: p.n_processed },
    { label: "Succeeded", value: p.n_succeeded },
    { label: "Failed", value: p.n_failed },
    { label: "Not run", value: p.n_not_run },
    { label: "Pending", value: p.n_pending },
  ]
}

// ---------------------------------------------------------------------------
// The remote run and a crux run's remote phase

export interface RemoteView {
  /** "ALCF via DM": the target by the API's name, the run monitor's Execution. */
  target: string
  /** A `crux` run's phase while it is queued or running; null without an observation (DM's state says it). */
  phase: RemotePhaseView | null
  /** DM's state as recorded, or "pending" before there is one. */
  dmState: string
  /** The Globus run row, for a `crux` run only: its link, null until DM reports one. */
  globus: { url: string | null } | null
  /** When the remote poller last looked ("12 s ago"); undefined before it has. */
  polled: string | undefined
}

export function remoteView(job: JobDetail): RemoteView | undefined {
  const remote = job.remote
  if (!remote) return undefined
  const age = job.progress.heartbeat_age_seconds
  return {
    target: `${remote.execution} via DM`,
    phase: remote.observation
      ? remotePhaseView(remote.observation, job.as_of)
      : null,
    dmState: remote.dm_state ?? "pending",
    globus: remote.target === "crux" ? { url: remote.globus_url } : null,
    polled: age === null ? undefined : `${formatAge(age)} ago`,
  }
}

// ---------------------------------------------------------------------------
// Run files

export type FileTone = "ok" | "muted" | "warning" | "error"

export interface FileStateView {
  /**
   * "Available", "Not available on this host", "Not written yet", "Not recorded", "Unreadable", "Not
   * responding".
   */
  label: string
  tone: FileTone
  /** Why: an unreadable file's reason. */
  detail?: string
}

/** Files the run writes when it ends: while it is active, a missing one is still to come. */
const WRITTEN_AT_THE_END = new Set([
  "run.json",
  "failures.jsonl",
  "pbs-output.log",
])

/**
 * How a run file (or the run directory) shows: available; not recorded (the job has no path for it); missing,
 * which is "not available on this host" (a path recorded on another host need not exist here) unless the run is
 * still active and writes it at its end; unreadable, with the reason; or not responding (its file system did not
 * answer: a hung mount).
 */
export function fileStateView(
  file: Pick<RunFile, "state" | "detail"> & { name: string },
  active: boolean
): FileStateView {
  switch (file.state) {
    case FileState.available:
      return { label: "Available", tone: "ok" }
    case FileState.not_recorded:
      return { label: "Not recorded", tone: "muted" }
    case FileState.missing:
      return active && WRITTEN_AT_THE_END.has(file.name)
        ? { label: "Not written yet", tone: "muted" }
        : { label: "Not available on this host", tone: "warning" }
    case FileState.unreadable:
      return {
        label: "Unreadable",
        tone: "error",
        detail: file.detail ?? undefined,
      }
    case FileState.not_responding:
      return { label: "Not responding", tone: "warning" }
  }
}

/** The run directory and the files the API read, as one list (the directory first). */
export function runFiles(configuration: RunConfiguration): RunFile[] {
  const directory = configuration.run_directory
  return [
    {
      name: "run directory",
      path: directory.path,
      state: directory.state,
      detail: directory.detail,
    },
    ...configuration.files,
  ]
}

// ---------------------------------------------------------------------------
// Run Configuration's rows

export type Row = [label: string, value: string]

/**
 * A value as text: a dash for none, yes/no, lists joined, objects as JSON. The values come from
 * JSON files, so nothing else reaches it.
 */
export function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—"
  if (typeof value === "boolean") return value ? "yes" : "no"
  if (typeof value === "string") return value
  if (typeof value === "number") return String(value)
  if (Array.isArray(value)) {
    return value.length === 0
      ? "—"
      : value.map((item: unknown) => formatValue(item)).join(", ")
  }
  return JSON.stringify(value)
}

/** The labels of paths, file names, hashes and sources ("input path", "output results", "lauelab commit"). */
const CODE_LABEL =
  /^(output |manifest)|(path|file|prefixes|xml|sha256|commit|source|map)$/

/**
 * A row shown in monospace (as the viewer's Parameters): a path, a file name, a hash, a source
 * or a recorded object (JSON); not a dash, a number or a word.
 */
export function isCodeRow([label, value]: Row): boolean {
  return value !== "—" && (CODE_LABEL.test(label) || value.startsWith("{"))
}

function label(key: string): string {
  return key.replaceAll("_", " ")
}

/** The submitted request: its parameters, its output, its manifest. */
export function requestRows(request: SubmittedRequest): Row[] {
  const rows: Row[] = Object.entries(request.parameters).map(([key, value]) => [
    label(key),
    formatValue(value),
  ])
  for (const [key, value] of Object.entries(request.output)) {
    rows.push([`output ${label(key)}`, formatValue(value)])
  }
  const manifest = request.manifest
  if (manifest) {
    const inputs = manifest.n_inputs === null ? "?" : String(manifest.n_inputs)
    rows.push(["manifest", `${manifest.path ?? "—"} (${inputs} inputs)`])
    if (manifest.sha256) rows.push(["manifest sha256", manifest.sha256])
  }
  return rows
}

/** Execution and versions: provenance, policy, host, the execution window, frames indexed. */
export function executionRows(execution: RunExecution): Row[] {
  const rows: Row[] = [
    ...Object.entries(execution.provenance),
    ...Object.entries(execution.policy),
  ].map(([key, value]) => [label(key), formatValue(value)])
  if (execution.host) rows.push(["host", execution.host])
  if (execution.started_at || execution.finished_at) {
    rows.push([
      "executed",
      `${execution.started_at ?? "—"} to ${execution.finished_at ?? "—"}`,
    ])
  }
  if (execution.n_indexed !== null) {
    rows.push(["frames indexed", String(execution.n_indexed)])
  }
  return rows
}

/** What a migrated run's per-input records kept. */
export function historyRows(history: RunHistory): Row[] {
  const rows: Row[] = []
  if (history.original_status_name) {
    rows.push(["recorded status", history.original_status_name])
  }
  if (history.first_input_at || history.last_input_at) {
    rows.push([
      "per-input work",
      `${history.first_input_at ?? "—"} to ${history.last_input_at ?? "—"}`,
    ])
  }
  for (const { message, count } of history.message_classes) {
    rows.push([plural(count, "input"), message])
  }
  return rows
}

/** The note on the results a run that did not finish still published. */
export function partialResultsNote(
  configuration: RunConfiguration
): string | undefined {
  if (!configuration.partial_results) return undefined
  const names = new Set(
    configuration.artifacts.map((artifact) => artifact.name)
  )
  if (names.has("results")) {
    return "Partial results: the inputs that succeeded."
  }
  if (names.has("reconstruction")) {
    return "Partial results: the completed point files."
  }
  return undefined
}

/** The kind of run, as words ("lauego indexing"). */
export function kindText(kind: string | null): string | undefined {
  return kind ? kind.replaceAll("_", " ") : undefined
}

/** Where an artifact's viewer is: the job's own run, when the viewer is that run's page. */
export function artifactLink(
  artifact: RunArtifact,
  run: JobDetail["run"]
):
  | { to: "/indexing/$indexingId"; params: { indexingId: string } }
  | {
      to: "/reconstructions/$reconstructionId"
      params: { reconstructionId: string }
    }
  | undefined {
  if (!run) return undefined
  if (artifact.viewer === "indexing_results" && run.kind === "indexing") {
    return {
      to: "/indexing/$indexingId",
      params: { indexingId: String(run.id) },
    }
  }
  if (artifact.viewer === "reconstruction" && run.kind === "reconstruction") {
    return {
      to: "/reconstructions/$reconstructionId",
      params: { reconstructionId: String(run.id) },
    }
  }
  return undefined
}

// ---------------------------------------------------------------------------
// The logs

/** "Run log", "Run log (last 50 of 120 lines)". */
export function logTitle(
  name: string,
  shown: number,
  total: number | null,
  truncated: boolean
): string {
  if (!truncated) return name
  return total === null
    ? `${name} (last ${String(shown)} lines)`
    : `${name} (last ${String(shown)} of ${String(total)} lines)`
}

// ---------------------------------------------------------------------------
// Failed Inputs

export interface FailurePaging {
  /** "Showing 51–100 of 263". */
  text: string
  /** The offsets of the previous and next pages; null where there is none. */
  previous: number | null
  next: number | null
}

/** Where a page of the failure report is (its range text) and the offsets around it. */
export function failurePaging(
  page: FailurePage,
  offset: number
): FailurePaging {
  const previous = offset > 0 ? Math.max(offset - FAILURE_PAGE_SIZE, 0) : null
  const next = page.has_more ? offset + FAILURE_PAGE_SIZE : null
  if (page.rows.length === 0) return { text: "", previous, next }
  const first = offset + 1
  const last = offset + page.rows.length
  const text =
    page.total >= last
      ? `Showing ${String(first)}–${String(last)} of ${String(page.total)}`
      : `Showing ${String(first)}–${String(last)}`
  return { text, previous, next }
}

/**
 * Why a page of the failure report has no rows: none failed, the report is still to come, or it is
 * not on this host or not readable. Undefined when it has rows. `failedSoFar`: an active run's
 * failed inputs (its polled counter), which its report does not hold yet; undefined once the run
 * has ended.
 */
export function failuresEmptyText(
  page: FailurePage,
  offset: number,
  failedSoFar?: number
): string | undefined {
  if (page.rows.length > 0) return undefined
  if (offset > 0) return "No more records."
  const report = page.report
  const unreadable = (count: number) =>
    `${plural(count, "input")} did not succeed, but the failure report cannot be read${report.detail ? `: ${report.detail}` : ""}.`
  if (failedSoFar !== undefined) {
    if (report.state === FileState.unreadable && failedSoFar > 0) {
      return unreadable(failedSoFar)
    }
    return failedSoFar === 0
      ? "No failed inputs so far."
      : `${plural(failedSoFar, "input")} failed so far: the failure report is written when the run ends.`
  }
  const total = page.total
  if (total === 0) return "No failed or unattempted inputs."
  const inputs = plural(total, "input")
  switch (report.state) {
    case FileState.not_recorded:
      return `${inputs} did not succeed, but the run recorded no failure report.`
    case FileState.missing:
      return `${inputs} did not succeed, but the failure report is not available on this host.`
    case FileState.not_responding:
      return `${inputs} did not succeed, but the failure report's file system is not responding.`
    case FileState.unreadable:
      return unreadable(total)
    case FileState.available:
      return `${inputs} did not succeed, but the failure report holds no records.`
  }
}

export type CategoryTone = "warning" | "error" | "muted"

/** The colours for the failure categories: input problems amber, errors red, inputs not run grey. */
export function categoryTone(category: string): CategoryTone {
  switch (category) {
    case "input":
      return "warning"
    case "numerical":
    case "memory":
    case "error":
      return "error"
    default:
      return "muted"
  }
}

/** A long message shortened (the whole one stays in its tooltip). */
export function shortMessage(message: string, limit = 300): string {
  return message.length <= limit ? message : `${message.slice(0, limit - 1)}…`
}

/** The job as Stop Run acts on it (a local run's target is `local`). */
export function detailRef(job: JobDetail): JobRef {
  return jobRef({
    job_id: job.job_id,
    run: job.run,
    status: job.status,
    target: job.remote?.target ?? "local",
    live: job.live !== null,
  })
}

// ---------------------------------------------------------------------------
// A live run: its source, arrivals and arrival status

/** A row of the Live Run card: a label and its value's text. */
export interface LiveRow {
  label: string
  text: string
  /** A state or an error, shown as a warning. */
  warning?: boolean
  /** The value's tooltip. */
  hint?: string
  mono?: boolean
  /** The Source row of a follower: the wire run it follows and that run's job, as links. */
  follows?: { run: RunReference; jobId: number | null }
}

/** A live file's state when it was not read, as a warning value: "Not responding", "Unreadable"... */
function liveFileText(
  state: FileState,
  name: string
): Pick<LiveRow, "text" | "warning"> {
  return {
    text: fileStateView({ name, state, detail: null }, false).label,
    warning: true,
  }
}

/**
 * The Live Run card's rows, labels and values: the source (`live.json`: a DAQ, Notifications, or the
 * wire run it follows), its folder and file name, the arrivals (`arrivals.jsonl`'s lines), then
 * `arrival-status.json`'s keys when the run has one (a DAQ's catalog: its rows seen and error; a
 * follower's: the points read and why it waits).
 */
export function liveRows(live: JobLive): LiveRow[] {
  const rows: LiveRow[] = []
  if (live.source === null) {
    rows.push({
      label: "Source",
      ...liveFileText(live.source_state, "live.json"),
    })
  } else if (live.source === "run" && live.follows) {
    rows.push({
      label: "Source",
      text: `Reconstruction ${live.follows.label}`,
      follows: { run: live.follows, jobId: live.follows_job_id },
    })
  } else {
    rows.push({
      label: "Source",
      text:
        live.source === "daq"
          ? `DAQ ${live.daq_id ?? ""}`.trim()
          : live.source === "notify"
            ? "Notifications"
            : "Reconstruction",
    })
  }
  if (live.folder !== null) {
    rows.push({ label: "Folder", text: live.folder, mono: true })
  }
  if (live.filename_template !== null) {
    rows.push({
      label: "Filename Template",
      text: live.filename_template,
      mono: true,
    })
  }
  rows.push(
    live.arrivals === null
      ? {
          label: "Arrivals",
          ...liveFileText(live.arrivals_state, "arrivals.jsonl"),
        }
      : { label: "Arrivals", text: formatCount(live.arrivals) }
  )
  const arrival = live.arrival
  if (arrival === null) return rows
  if (arrival.state !== FileState.available) {
    rows.push({
      label: "Arrival Status",
      ...liveFileText(arrival.state, "arrival-status.json"),
    })
    return rows
  }
  const follower = live.source === "run"
  rows.push(
    {
      label: "Last Check",
      text: arrival.checked_at ? formatDateTime(arrival.checked_at) : "",
    },
    {
      label: follower ? "Points Read" : "Rows Seen",
      text: arrival.rows_seen === null ? "" : formatCount(arrival.rows_seen),
    },
    {
      label: follower ? "Waiting" : "Error",
      text: arrival.error ?? "",
      warning: arrival.error !== null,
    },
    {
      label: follower ? "Waiting Since" : "Error Since",
      text: arrival.error_at ? formatDateTime(arrival.error_at) : "",
    }
  )
  return rows
}
