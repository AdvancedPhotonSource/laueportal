// Problem answers for mocked routes: `route.fulfill` options with the problem+json content type set
// through `contentType` (beside `json:`, a `headers` content type is replaced by application/json),
// and the backend's own titles and details, so a page is tested on what the API says. Keep the
// texts as the backend words them (the module named at each).

const PROBLEM_JSON = "application/problem+json"

export interface ProblemAnswer {
  status: number
  contentType: string
  json: {
    type: string
    status: number
    title: string
    detail?: string
    instance?: string
  }
}

/** A problem answer: `route.fulfill(problem(404, "Scan not found", "No scan 12"))`. */
export function problem(
  status: number,
  title: string,
  detail?: string,
  extra: { type?: string; instance?: string } = {}
): ProblemAnswer {
  return {
    status,
    contentType: PROBLEM_JSON,
    json: {
      type: extra.type ?? "about:blank",
      status,
      title,
      ...(detail === undefined ? {} : { detail }),
      ...(extra.instance === undefined ? {} : { instance: extra.instance }),
    },
  }
}

/** The API without its database file (core/db.py, DatabaseMissing). */
export function databaseUnavailable(
  path = "/srv/laueportal/laueportal.db"
): ProblemAnswer {
  return problem(
    503,
    "Database unavailable",
    `The database file ${path} does not exist; create it with \`laueportal db upgrade\`.`
  )
}

/** An unexpected error (core/errors.py, UnhandledErrorMiddleware): its detail names the instance. */
export function internalError(
  instance = "urn:uuid:5f0c2a7e-1b8e-4f3a-9a51-0c6d2e9b7f10"
): ProblemAnswer {
  return problem(
    500,
    "Internal Server Error",
    `Unexpected error. The server log has the details under ${instance}.`,
    { instance }
  )
}
