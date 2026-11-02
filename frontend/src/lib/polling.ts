// Polling robustness, shared by the live lists (DataGrid) and the job page: after consecutive
// failures a poll waits longer, and a 404 (the thing polled is gone) stops it.
import { ApiError } from "@/api"

/** The longest wait between polls after failures. */
export const MAX_BACKOFF_MS = 60_000

/**
 * The wait before the next poll: `base`, doubled for each of `failures` consecutive failed polls
 * (a list's 5 s → 10, 20, 40 s), at most a minute (never less than `base`).
 */
export function backoff(base: number, failures: number): number {
  if (failures <= 0) return base
  return Math.max(base, Math.min(MAX_BACKOFF_MS, base * 2 ** failures))
}

/** Whether a poll's error is the API's 404: what it polls is gone, so polling stops. */
export function isGone(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404
}

/** What `failureStreak` reads of a query's state (TanStack Query's `query.state`). */
export interface FetchOutcome {
  error: unknown
  errorUpdateCount: number
}

/**
 * Counts a query's consecutive failed fetches (its errors since its last success) for a
 * `refetchInterval`, which TanStack Query evaluates on every change of the query's state. One counter
 * per query observer (`useState(failureStreak)`).
 */
export function failureStreak(): (state: FetchOutcome) => number {
  let errorsAtSuccess = 0
  return ({ error, errorUpdateCount }) => {
    if (error === null || error === undefined) {
      errorsAtSuccess = errorUpdateCount
      return 0
    }
    return errorUpdateCount - errorsAtSuccess
  }
}
