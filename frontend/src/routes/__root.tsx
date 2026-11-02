import type { QueryClient } from "@tanstack/react-query"
import { createRootRouteWithContext } from "@tanstack/react-router"

import { AppShell } from "@/app/AppShell"
import { titleHead } from "@/lib/title"

export interface RouterContext {
  queryClient: QueryClient
}

// Titles come only from route heads (titleHead): this one is the fallback, and the deepest route
// that sets a title wins. Unknown URLs reach the catch-all route in routes/$.tsx.
export const Route = createRootRouteWithContext<RouterContext>()({
  head: titleHead(),
  component: AppShell,
})
