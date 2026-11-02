// The capped routes' busy answers (a point's trace, its reference images, its ROI traces and its
// depth frames, and a step's detector image wait for a turn of the app's limiter; past its waiting
// room the answer is a 503 with `Retry-After`). Every query of those routes takes BUSY_RETRY and
// shows busyStatus over its plot: it is asked again after the delay the API names, and once it gives
// up the status offers Retry. A 503 of the not-responding type (the file system of the run directory
// or of an image did not answer) is not busy: the server answers the same until the mount answers
// again, so it is not asked again by itself, and its status offers Retry at once.
import { ApiError } from "@/api"
import { describeError } from "@/lib/errors"

/** How many busy answers a query waits out before it reports one. */
export const MAX_BUSY_RETRIES = 30

/** The problem type of a file check that did not answer (`files.probe.NotResponding`). */
export const NOT_RESPONDING = "urn:laueportal:problem:not-responding"

/** A file system that did not answer: a 503 of the not-responding type. */
export function isNotResponding(error: unknown): error is ApiError {
  return (
    error instanceof ApiError &&
    error.status === 503 &&
    error.problem?.type === NOT_RESPONDING
  )
}

/** A busy answer of a capped route: a 503 with `Retry-After` (seconds), not a hung file system. */
export function isBusy(error: unknown): error is ApiError & {
  retryAfter: number
} {
  return (
    error instanceof ApiError &&
    error.status === 503 &&
    error.retryAfter !== null &&
    !isNotResponding(error)
  )
}

/** A 503 a capped route's plot shows as a status: busy, or not responding. */
export function isUnavailable(error: unknown): boolean {
  return isBusy(error) || isNotResponding(error)
}

/**
 * Whether to ask again: after a busy answer (up to `MAX_BUSY_RETRIES` times), or twice after a
 * failure that may pass (no answer, a 5xx); never after another 4xx (a 409 or a 404 is the point's
 * state, a 422 the request's), nor after a not-responding answer (its status offers Retry).
 */
export function retryBusy(failureCount: number, error: unknown): boolean {
  if (isBusy(error)) return failureCount < MAX_BUSY_RETRIES
  if (isNotResponding(error)) return false
  if (error instanceof ApiError && error.status < 500) return false
  return failureCount < 2
}

/** The wait before asking again: the busy answer's `Retry-After`, else React Query's back-off. */
export function busyRetryDelay(failureCount: number, error: unknown): number {
  if (isBusy(error)) return error.retryAfter * 1000
  return Math.min(1000 * 2 ** failureCount, 30_000)
}

/** A capped route's query options (React Query's `retry` and `retryDelay`). */
export const BUSY_RETRY = { retry: retryBusy, retryDelay: busyRetryDelay }

/** A status over a plot: its text, its detail as the tooltip, and whether it offers Retry. */
export interface PlotStatus {
  text: string
  hint?: string
  retry?: boolean
}

/**
 * The status of a capped route's query while it waits out a busy answer ("Busy: retrying in n s"),
 * or once it gave up (the problem's title, with Retry); "Not responding", with Retry, for a file
 * system that did not answer; null otherwise.
 */
export function busyStatus(query: {
  isFetching: boolean
  failureReason: unknown
  error: unknown
}): PlotStatus | null {
  if (query.isFetching && isBusy(query.failureReason)) {
    return {
      text: `Busy: retrying in ${String(query.failureReason.retryAfter)} s`,
      hint: query.failureReason.problem?.detail,
    }
  }
  if (!query.isFetching && isUnavailable(query.error)) {
    const { title, detail } = describeError(query.error)
    return { text: title, hint: detail, retry: true }
  }
  return null
}
