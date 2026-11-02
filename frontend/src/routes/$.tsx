import { createFileRoute } from "@tanstack/react-router"

import { NotFound } from "@/app/NotFound"
import { NOT_FOUND_PAGE, titleHead } from "@/lib/title"

// Catch-all: every URL no other route matches is the 404 page, titled through the router like any
// page. (A loader that throws notFound() gets the same NotFound page via defaultNotFoundComponent.)
export const Route = createFileRoute("/$")({
  head: titleHead(NOT_FOUND_PAGE),
  component: NotFound,
})
