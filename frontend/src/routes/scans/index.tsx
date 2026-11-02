import { createFileRoute } from "@tanstack/react-router"

import { ScansPage } from "@/features/scans/ScansPage"
import { titleHead } from "@/lib/title"

export const Route = createFileRoute("/scans/")({
  head: titleHead("Scans"),
  component: ScansPage,
})
