// What the navbar's live indicator shows, derived from the GET /api/v1/health query. No React here:
// HealthIndicators.tsx renders it.
import { NetworkError, type Health } from "@/api"
import { describeError, type ErrorSummary } from "@/lib/errors"

/** The navbar polls the health check this often, and only while the tab is visible. */
export const HEALTH_POLL_MS = 10_000

export type Tone = "ok" | "warning" | "error" | "unknown"

export interface IndicatorView {
  tone: Tone
  /** The visible text. */
  label: string
  /** The state in words, for screen readers and the tooltip ("Worker: running"); the label may say it already. */
  state?: string
  /** A short line for the tooltip, worded as the Status page words it. */
  note?: string
}

export type ShellHealth =
  | { kind: "checking" }
  | { kind: "unreachable"; label: string; error: ErrorSummary }
  | { kind: "live"; worker: IndicatorView }

/** The parts of a TanStack Query result this needs. */
export interface HealthQuery {
  status: "pending" | "error" | "success"
  data: Health | undefined
  error: unknown
}

/**
 * The worker as /health reports it (its row's newest mark within 45 s: running, then late; not
 * running with no row, after its stop, or with its process gone on the API's host).
 */
export function workerView(health: Health): IndicatorView {
  switch (health.worker) {
    case "running":
      return { tone: "ok", label: "Worker", state: "running" }
    case "late":
      return {
        tone: "warning",
        label: "Worker",
        state: "late",
        note: "No sign of life for over 45 s",
      }
    case "not_running":
      return {
        tone: "error",
        label: "Worker",
        state: "not running",
        note: "Queued runs wait until it starts",
      }
    default:
      return {
        tone: "unknown",
        label: "Worker",
        state: "unknown",
        note: "The database does not answer",
      }
  }
}

/**
 * The indicator for the health query's state. A failed check means the API itself did not answer
 * properly, so the worker is unknown: one indicator says so instead, even when an older answer is
 * still cached.
 */
export function shellHealth(query: HealthQuery): ShellHealth {
  if (query.status === "error") {
    return {
      kind: "unreachable",
      label:
        query.error instanceof NetworkError ? "API unreachable" : "API error",
      error: describeError(query.error),
    }
  }
  if (query.status === "pending" || !query.data) return { kind: "checking" }
  return { kind: "live", worker: workerView(query.data) }
}
