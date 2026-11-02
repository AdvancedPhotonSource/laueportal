import { createFileRoute } from "@tanstack/react-router"

import { IndexationsPage } from "@/features/indexing/IndexationsPage"
import { titleHead } from "@/lib/title"

export const Route = createFileRoute("/indexing/")({
  head: titleHead("Indexations"),
  component: IndexationsPage,
})
