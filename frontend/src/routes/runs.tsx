import { createFileRoute } from "@tanstack/react-router"

import { RunMonitorPage } from "@/features/jobs/RunMonitorPage"
import { titleHead } from "@/lib/title"

export const Route = createFileRoute("/runs")({
  head: titleHead("Run Monitor"),
  component: RunMonitorPage,
})
