import { createFileRoute } from "@tanstack/react-router"

import { LivePage } from "@/features/live/LivePage"
import { titleHead } from "@/lib/title"

export const Route = createFileRoute("/live")({
  head: titleHead("Live"),
  component: LivePage,
})
