// A run's status and progress counters as text, on the API's values: statuses are lower-case strings.

import { RunStatus } from "@/api"

/** The label for each status (Queued, Running, Finished, Failed, Cancelled). */
export const RUN_STATUS_LABELS: Record<RunStatus, string> = {
  [RunStatus.queued]: "Queued",
  [RunStatus.running]: "Running",
  [RunStatus.finished]: "Finished",
  [RunStatus.failed]: "Failed",
  [RunStatus.cancelled]: "Cancelled",
}

/** A queued batch run held by an open live session (`held`): the tooltip of its "Held". */
export const HELD_HINT = "No batch run starts until the live session has ended"

export function isRunStatus(value: unknown): value is RunStatus {
  return typeof value === "string" && Object.hasOwn(RUN_STATUS_LABELS, value)
}

/** Queued or running: the run may still change, so live views keep polling it. */
export function isActiveStatus(status: unknown): boolean {
  return status === RunStatus.queued || status === RunStatus.running
}

/**
 * The status as text: its label, and for a running run its progress (`Running 3/10`) when it
 * has inputs. An unknown value reads `Unknown (<value>)`.
 */
export function runStatusText(
  status: unknown,
  progress?: { processed: number; total: number }
): string {
  if (!isRunStatus(status)) return `Unknown (${String(status)})`
  const label = RUN_STATUS_LABELS[status]
  if (status === RunStatus.running && progress && progress.total > 0) {
    return `${label} ${String(progress.processed)}/${String(progress.total)}`
  }
  return label
}

/** A run's stored counters (every run row and job carries them). */
export interface RunCounts {
  n_inputs: number
  n_succeeded: number
  n_failed: number
  n_not_run: number
}

export type ProgressSegmentKind = "succeeded" | "failed" | "notRun" | "pending"

export interface ProgressSegment {
  kind: ProgressSegmentKind
  count: number
  /** Share of the inputs, in percent. */
  percent: number
  /** "3 succeeded", the segment's tooltip. */
  title: string
  /** Pending inputs of a running run: striped and moving. */
  animated: boolean
}

export interface RunProgress {
  total: number
  /** Succeeded plus failed. */
  processed: number
  /** `3/10 processed, 1 failed, 2 not run`; "No inputs" without inputs. */
  text: string
  /** The non-empty segments, in order: succeeded, failed, not run, pending. */
  segments: ProgressSegment[]
}

function count(value: number | null | undefined): number {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.max(0, value)
    : 0
}

/**
 * The segmented progress bar's parts. Pending inputs are those neither processed nor recorded as not
 * run; they are "pending" while the run is active and "unaccounted" after it ended.
 */
export function runProgress(counts: RunCounts, status?: unknown): RunProgress {
  const total = count(counts.n_inputs)
  const succeeded = count(counts.n_succeeded)
  const failed = count(counts.n_failed)
  const notRun = count(counts.n_not_run)
  const processed = succeeded + failed
  if (total === 0) return { total, processed, text: "No inputs", segments: [] }

  const pending = Math.max(total - succeeded - failed - notRun, 0)
  const active = isActiveStatus(status)
  const parts: [ProgressSegmentKind, number, string][] = [
    ["succeeded", succeeded, "succeeded"],
    ["failed", failed, "failed"],
    ["notRun", notRun, "not run"],
    ["pending", pending, active ? "pending" : "unaccounted"],
  ]
  const segments = parts
    .filter(([, n]) => n > 0)
    .map(([kind, n, label]) => ({
      kind,
      count: n,
      percent: (n / total) * 100,
      title: `${String(n)} ${label}`,
      animated: kind === "pending" && status === RunStatus.running,
    }))

  let text = `${String(processed)}/${String(total)} processed`
  if (failed > 0) text += `, ${String(failed)} failed`
  if (notRun > 0) text += `, ${String(notRun)} not run`
  return { total, processed, text, segments }
}
