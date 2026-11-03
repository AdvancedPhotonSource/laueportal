// What the navbar's live indicator shows, derived from the GET /api/v1/health query, and how that
// query asks. No React here: HealthIndicators.tsx renders it.
import { getHealth, NetworkError, type Health } from "@/api"
import { describeError, type ErrorSummary } from "@/lib/errors"
import { shouldRetry } from "./query-client"

/** The navbar polls the health check this often, and only while the tab is visible. */
export const HEALTH_POLL_MS = 10_000

/** A dot alone, while all is well: the worker running (filled), or the first answer awaited (a ring). */
export interface DotView {
  form: "dot"
  tone: "ok" | "unknown"
  /** The state in words ("Worker: running"): the dot's accessible name and the tooltip's first line. */
  state: string
}

/** A tinted pill with words, when something is wrong. */
export interface PillView {
  form: "pill"
  tone: "warning" | "error"
  /** The state in words ("Worker: late"): the tooltip's first line. */
  state: string
  /** What the words are about ("Worker", "API"): on a narrow navbar it shortens first, down to nothing. */
  subject: string
  /** The state's own words ("late", "not running"): always whole. */
  words: string
  /** A short line for the tooltip, worded as the Status page words it. */
  note?: string
}

export type IndicatorView = DotView | PillView

/** The pill's words in full ("Worker late"): its accessible name. */
export function pillLabel(view: PillView): string {
  return `${view.subject} ${view.words}`
}

export type ShellHealth =
  | { kind: "checking"; view: DotView }
  | { kind: "unreachable"; view: PillView; error: ErrorSummary }
  | { kind: "live"; view: IndicatorView }

/** The parts of a TanStack Query result this needs. */
export interface HealthQuery {
  status: "pending" | "error" | "success"
  data: Health | undefined
  error: unknown
}

function workerPill(
  tone: PillView["tone"],
  words: string,
  note: string
): PillView {
  return {
    form: "pill",
    tone,
    state: `Worker: ${words}`,
    subject: "Worker",
    words,
    note,
  }
}

/**
 * The worker as /health reports it (its row's newest mark within 45 s: running, then late; not
 * running with no row, after its stop, or with its process gone on the API's host). Unknown when the
 * database does not answer: an error, as the Status page's alert for it, because no run can be read
 * or queued then.
 */
export function workerView(health: Health): IndicatorView {
  switch (health.worker) {
    case "running":
      return { form: "dot", tone: "ok", state: "Worker: running" }
    case "late":
      return workerPill("warning", "late", "No sign of life for over 45 s")
    case "not_running":
      return workerPill(
        "error",
        "not running",
        "Queued runs wait until it starts"
      )
    default:
      return workerPill("error", "unknown", "The database does not answer")
  }
}

/**
 * The indicator for the health query's state. A failed check means the API itself did not answer
 * properly, so the worker is unknown: one indicator says so instead, even when an older answer is
 * still cached.
 */
export function shellHealth(query: HealthQuery): ShellHealth {
  if (query.status === "error") {
    const words = query.error instanceof NetworkError ? "unreachable" : "error"
    return {
      kind: "unreachable",
      view: {
        form: "pill",
        tone: "error",
        state: `API ${words}`,
        subject: "API",
        words,
      },
      error: describeError(query.error),
    }
  }
  if (query.status === "pending" || !query.data) {
    return {
      kind: "checking",
      view: { form: "dot", tone: "unknown", state: "Worker: checking" },
    }
  }
  return { kind: "live", view: workerView(query.data) }
}

/** The health check had no answer within its bound: the API is hung, so the check counts as no answer at all. */
export class HealthTimeout extends NetworkError {
  constructor(cause: unknown) {
    super(cause)
    this.name = "HealthTimeout"
  }
}

/**
 * GET /health, given up after `ms` (one poll interval) without an answer, so a hung API shows as
 * unreachable instead of a check that never ends. A cancel through `signal` (TanStack Query's)
 * keeps its AbortError. The bound is a timer, which the tests' fake clocks control.
 */
export async function getHealthWithin(
  signal: AbortSignal,
  ms: number = HEALTH_POLL_MS
): Promise<Health> {
  const controller = new AbortController()
  const cancel = () => {
    controller.abort(signal.reason)
  }
  if (signal.aborted) cancel()
  signal.addEventListener("abort", cancel, { once: true })
  const timeout = new DOMException(
    `No answer within ${String(ms)} ms`,
    "TimeoutError"
  )
  const timer = setTimeout(() => {
    controller.abort(timeout)
  }, ms)
  try {
    return await getHealth({ signal: controller.signal })
  } catch (error) {
    throw controller.signal.reason === timeout
      ? new HealthTimeout(error)
      : error
  } finally {
    clearTimeout(timer)
    signal.removeEventListener("abort", cancel)
  }
}

/**
 * The app's retry rule (query-client.ts), but a timed-out check is not retried: it has waited a
 * whole poll interval already, and each retry would wait another before the indicator says so.
 */
export function retryHealth(failureCount: number, error: unknown): boolean {
  return !(error instanceof HealthTimeout) && shouldRetry(failureCount, error)
}
