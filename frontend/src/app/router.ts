import type { QueryClient } from "@tanstack/react-query"
import { createRouter, type RouterHistory } from "@tanstack/react-router"

import { parseSearch, stringifySearch } from "@/lib/search"
import { routeTree } from "@/routeTree.gen"
import { NotFound } from "./NotFound"
import { RouteError } from "./RouteError"

/** The app's router; `history` defaults to the browser's (tests pass a memory history). */
export function createAppRouter(
  queryClient: QueryClient,
  history?: RouterHistory
) {
  return createRouter({
    routeTree,
    context: { queryClient },
    history,
    // Search values are plain strings in the URL; each route's validateSearch converts them.
    parseSearch,
    stringifySearch,
    defaultPreload: "intent",
    // Server data lives in TanStack Query; the router should not cache it a second time.
    defaultPreloadStaleTime: 0,
    // Unknown URLs reach the catch-all route (routes/$.tsx); this covers loaders that throw notFound().
    defaultNotFoundComponent: NotFound,
    // Every route match gets its own error boundary, so a failing page keeps the navbar.
    defaultErrorComponent: RouteError,
    scrollRestoration: true,
  })
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof createAppRouter>
  }
}
