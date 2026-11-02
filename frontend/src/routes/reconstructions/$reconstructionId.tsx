import { createFileRoute } from "@tanstack/react-router"

import { LoadingState } from "@/components/LoadingState"
import { ReconstructionNotFound } from "@/features/reconstruction/ReconstructionNotFound"
import { ReconstructionViewerPage } from "@/features/reconstruction/ReconstructionViewerPage"
import { useOpenReconstructionLink } from "@/features/reconstruction/viewer-link"
import {
  HISTORY_KEYS,
  loadReconstructionRun,
  parseReconstructionId,
  validateReconstructionSearch,
  type ReconstructionSearch,
} from "@/features/reconstruction/viewer-search"
import { titleHeadFor } from "@/lib/title"
import { compactSearch, useViewSearch } from "@/lib/view-search"

// The wire reconstruction viewer. The URL holds the coarse view (tab, point) and, once, a copied
// link's packed view (`link`). An unknown run is the route's not-found page.
export const Route = createFileRoute("/reconstructions/$reconstructionId")({
  validateSearch: validateReconstructionSearch,
  loader: ({ context, params }) =>
    loadReconstructionRun(context.queryClient, params.reconstructionId),
  head: ({ match, params }) =>
    titleHeadFor(match, `Reconstruction R${params.reconstructionId}`),
  component: ReconstructionRoute,
  pendingComponent: () => <LoadingState label="Loading the reconstruction" />,
  notFoundComponent: ReconstructionRouteNotFound,
})

function ReconstructionRouteNotFound() {
  const { reconstructionId } = Route.useParams()
  return <ReconstructionNotFound reconstructionId={reconstructionId} />
}

function ReconstructionRoute() {
  const { reconstructionId } = Route.useParams()
  const navigate = Route.useNavigate()
  const [search, setSearch] = useViewSearch<ReconstructionSearch>({
    search: Route.useSearch(),
    write: (next, replace) => {
      void navigate({ search: next, replace })
    },
    historyKeys: HISTORY_KEYS,
  })
  useOpenReconstructionLink(search, (next) => {
    void navigate({ search: next, replace: true })
  })
  // The loader let only a reconstruction ID through.
  const id = parseReconstructionId(reconstructionId) ?? 0
  return (
    <ReconstructionViewerPage
      key={id}
      reconstructionId={id}
      search={search}
      onSearchChange={setSearch}
      onSearchReplace={(patch) => {
        void navigate({
          search: (previous) => compactSearch({ ...previous, ...patch }),
          replace: true,
        })
      }}
    />
  )
}
