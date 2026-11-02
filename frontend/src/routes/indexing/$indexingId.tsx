import { createFileRoute } from "@tanstack/react-router"

import { LoadingState } from "@/components/LoadingState"
import { IndexingNotFound } from "@/features/indexing/IndexingNotFound"
import { IndexingViewerPage } from "@/features/indexing/IndexingViewerPage"
import { useOpenViewLink } from "@/features/indexing/use-view-link"
import {
  HISTORY_KEYS,
  loadIndexingRun,
  parseIndexingId,
  validateIndexingSearch,
  type IndexingSearch,
} from "@/features/indexing/viewer-search"
import { titleHeadFor } from "@/lib/title"
import { useViewSearch } from "@/lib/view-search"

// The indexing viewer. The URL holds the coarse view (tab, step, colour mode, 3D, HKL, point) and,
// once, a copied link's packed view (`link`). An unknown run is the route's not-found page.
export const Route = createFileRoute("/indexing/$indexingId")({
  validateSearch: validateIndexingSearch,
  loader: ({ context, params }) =>
    loadIndexingRun(context.queryClient, params.indexingId),
  head: ({ match, params }) =>
    titleHeadFor(match, `Indexing I${params.indexingId}`),
  component: IndexingRoute,
  pendingComponent: () => <LoadingState label="Loading the indexing run" />,
  notFoundComponent: IndexingRouteNotFound,
})

function IndexingRouteNotFound() {
  const { indexingId } = Route.useParams()
  return <IndexingNotFound indexingId={indexingId} />
}

function IndexingRoute() {
  const { indexingId } = Route.useParams()
  const navigate = Route.useNavigate()
  const [search, setSearch] = useViewSearch<IndexingSearch>({
    search: Route.useSearch(),
    write: (next, replace) => {
      void navigate({ search: next, replace })
    },
    historyKeys: HISTORY_KEYS,
  })
  useOpenViewLink(search, (next) => {
    void navigate({ search: next, replace: true })
  })
  // The loader let only an indexing ID through.
  const id = parseIndexingId(indexingId) ?? 0
  return (
    <IndexingViewerPage
      key={id}
      indexingId={id}
      search={search}
      onSearchChange={setSearch}
    />
  )
}
