import { ApiError, NetworkError } from "@/api"

/** What the UI shows for an error: the error states, the route error boundary and error toasts. */
export interface ErrorSummary {
  title: string
  detail?: string
  /** The HTTP status of an API error. */
  status?: number
  /**
   * The server's reference for an unexpected error (logged with the traceback), when the detail
   * does not name it already.
   */
  instance?: string
  /** A validation problem's per-field issues, as "<location>: <message>". */
  issues?: string[]
}

const GENERIC_TITLE = "Something went wrong"

export const UNREACHABLE_TITLE = "The server cannot be reached"

/**
 * Describe any thrown value. A problem+json error shows its title and detail; an HTTP error without
 * one shows its status; a request that got no response says the server cannot be reached; anything
 * else shows its message.
 */
export function describeError(error: unknown): ErrorSummary {
  if (error instanceof ApiError) {
    const problem = error.problem
    const title = problem?.title || error.message
    const detail =
      problem?.detail && problem.detail !== title ? problem.detail : undefined
    const summary: ErrorSummary = { title, status: error.status }
    if (detail) summary.detail = detail
    // The API's 500 names its instance in the detail already (core.errors): not repeated.
    if (problem?.instance && !detail?.includes(problem.instance)) {
      summary.instance = problem.instance
    }
    if (problem?.errors?.length) {
      summary.issues = problem.errors.map(
        (issue) => `${issue.loc.join(".")}: ${issue.message}`
      )
    }
    return summary
  }
  if (error instanceof NetworkError) {
    return {
      title: UNREACHABLE_TITLE,
      detail:
        "The portal did not answer: it may be stopped or restarting, or the network is down.",
    }
  }
  if (error instanceof Error) {
    return error.message
      ? { title: GENERIC_TITLE, detail: error.message }
      : { title: GENERIC_TITLE }
  }
  return typeof error === "string" && error
    ? { title: GENERIC_TITLE, detail: error }
    : { title: GENERIC_TITLE }
}
