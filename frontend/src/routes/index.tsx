import { createFileRoute } from "@tanstack/react-router"

import { StatusPage } from "@/features/system/StatusPage"
import { titleHead } from "@/lib/title"

export const Route = createFileRoute("/")({
  head: titleHead("Status"),
  component: StatusPage,
})
