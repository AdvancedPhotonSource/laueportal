import type { AnyRouteMatch } from "@tanstack/react-router"

export const APP_TITLE = "3DMN Portal"

export const NOT_FOUND_PAGE = "Page not found"
export const ERROR_PAGE = "Error"

/** Document title for a page: "<page> · 3DMN Portal". */
export function pageTitle(page?: string): string {
  return page ? `${page} · ${APP_TITLE}` : APP_TITLE
}

type MatchStatus = Pick<AnyRouteMatch, "status">

/**
 * A route `head` result with the title of the page that is shown: `page`, unless the route's loader
 * threw notFound() or an error. The router still runs the head then, with the match status set, so
 * the title follows the 404 or error page. Detail pages call it from their own head with a name
 * built from the params, e.g.
 * `head: ({ match, params }) => titleHeadFor(match, \`Indexing I${params.indexingId}\`)`.
 */
export function titleHeadFor(match: MatchStatus, page?: string) {
  const shown =
    match.status === "notFound"
      ? NOT_FOUND_PAGE
      : match.status === "error"
        ? ERROR_PAGE
        : page
  return { meta: [{ title: pageTitle(shown) }] }
}

/**
 * Route `head` option that sets a fixed page title. Titles come only from the router (HeadContent
 * in the app shell); see titleHeadFor.
 */
export function titleHead(page?: string) {
  return ({ match }: { match: MatchStatus }) => titleHeadFor(match, page)
}
