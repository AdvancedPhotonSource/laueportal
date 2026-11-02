import { createFileRoute } from "@tanstack/react-router"

import { ScanImportPage } from "@/features/scans/ScanImportPage"
import { titleHead } from "@/lib/title"

export const Route = createFileRoute("/scans/import")({
  head: titleHead("Import Scans"),
  component: ScanImportPage,
})
