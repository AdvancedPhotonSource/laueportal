import { createFileRoute } from "@tanstack/react-router"

import { ReconstructionsPage } from "@/features/reconstruction/ReconstructionsPage"
import { titleHead } from "@/lib/title"

export const Route = createFileRoute("/reconstructions/")({
  head: titleHead("Reconstructions"),
  component: ReconstructionsPage,
})
