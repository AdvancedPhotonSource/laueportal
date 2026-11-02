import { createFileRoute } from "@tanstack/react-router"

import { LoadingState } from "@/components/LoadingState"
import {
  loadScan,
  parseScanId,
  searchTab,
  tabSearch,
  validateScanSearch,
} from "@/features/scans/scan-detail"
import { ScanDetailPage } from "@/features/scans/ScanDetailPage"
import { ScanNotFound } from "@/features/scans/ScanNotFound"
import { titleHeadFor } from "@/lib/title"

// A scan. The tab is in the URL (`?tab=metadata`, `?tab=catalog`; Scan Info is the default). An
// unknown scan is the route's not-found page.
export const Route = createFileRoute("/scans/$scanId")({
  validateSearch: validateScanSearch,
  loader: ({ context, params }) => loadScan(context.queryClient, params.scanId),
  head: ({ match, params }) => titleHeadFor(match, `Scan ${params.scanId}`),
  component: ScanRoute,
  pendingComponent: () => <LoadingState label="Loading the scan" />,
  notFoundComponent: ScanRouteNotFound,
})

function ScanRouteNotFound() {
  const { scanId } = Route.useParams()
  return <ScanNotFound scanId={scanId} />
}

function ScanRoute() {
  const { scanId } = Route.useParams()
  const search = Route.useSearch()
  const navigate = Route.useNavigate()
  // The loader let only a scan ID through.
  const scan = parseScanId(scanId) ?? 0
  return (
    <ScanDetailPage
      scanId={scan}
      tab={searchTab(search)}
      onTabChange={(tab) => {
        void navigate({
          search: (previous) => ({ ...previous, ...tabSearch(tab) }),
          replace: true,
        })
      }}
    />
  )
}
