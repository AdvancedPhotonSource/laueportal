import { GoodnessLabel, goodnessLabel } from "./goodness-label"
import { Link } from "@tanstack/react-router"
import { useState } from "react"

import {
  ArtifactState,
  useGetIndexingArtifact,
  useGetIndexingRun,
  useGetIndexingSummary,
  type IndexingRunDetail,
} from "@/api"
import { ErrorState } from "@/components/ErrorState"
import { LoadingState } from "@/components/LoadingState"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { surface } from "@/lib/styles"
import { cn } from "@/lib/utils"
import { createSelectionStore } from "@/viz"
import { ArtifactBar } from "./ArtifactBar"
import { ColorMapTab } from "./ColorMapTab"
import { DetectorTab } from "./DetectorTab"
import { ParametersTab } from "./ParametersTab"
import { PoleFigureTab } from "./PoleFigureTab"
import { PatternsTab, PeaksTab } from "./ResultsTableTabs"
import { CopyViewLinkButton } from "./CopyViewLinkButton"
import { ResultsGate } from "./ResultsGate"
import { ScopeBar } from "./ScopeBar"
import {
  INDEXING_TABS,
  isIndexingTab,
  searchPatch,
  TAB_LABELS,
  viewOf,
  type IndexingSearch,
} from "./viewer-search"
import { useScopeParams } from "./viewer-store"

const LINK_CLASS = "text-link underline-offset-4 hover:underline"

/**
 * The indexing viewer: the header, one region: the title with the run's job, reconstruction and
 * scan and "Copy link to this view"; the artifact bar; the scope bar (for every tab) as its
 * toolbar; and the tabs on its baseline. Then the tab's content. `search` is the URL's view
 * (src/lib/view-search.ts), `onSearchChange` changes it.
 */
export function IndexingViewerPage({
  indexingId,
  search,
  onSearchChange,
}: {
  indexingId: number
  search: IndexingSearch
  onSearchChange: (patch: Partial<IndexingSearch>) => void
}) {
  // The route's loader has read the run (its job and stored parameters).
  const runQuery = useGetIndexingRun(indexingId, {
    query: { staleTime: 10_000 },
  })
  const run = runQuery.data

  const artifact = useGetIndexingArtifact(indexingId)
  const readable =
    artifact.data?.state === ArtifactState.ready ||
    artifact.data?.state === ArtifactState.xml_only
  const summary = useGetIndexingSummary(indexingId, useScopeParams(), {
    query: { enabled: readable, placeholderData: (previous) => previous },
  })
  // Every results tab waits on the summary, and says so when it cannot have one (ResultsGate).
  const gate = (content: Parameters<typeof ResultsGate>[0]["children"]) => (
    <ResultsGate
      readable={readable}
      artifactSettled={artifact.data !== undefined || artifact.error !== null}
      summary={summary}
    >
      {content}
    </ResultsGate>
  )

  const view = viewOf(search)
  // The linked selection of the map and the pole figure: one per page, in memory only.
  const [selectionStore] = useState(createSelectionStore)

  return (
    <GoodnessLabel.Provider value={goodnessLabel(run?.method)}>
      <Tabs
        render={<section />}
        className="gap-4"
        value={view.tab}
        onValueChange={(value) => {
          if (isIndexingTab(value)) onSearchChange(searchPatch({ tab: value }))
        }}
      >
        <header className={cn(surface, "flex flex-col")}>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2.5">
            <h1 className="text-xl font-semibold">Indexing I{indexingId}</h1>
            {run && <RelatedLinks run={run} />}
            <CopyViewLinkButton search={search} />
          </div>
          {/* Empty (hidden) while the results file is ready. */}
          <div className="px-4 pb-2.5 empty:hidden">
            <ArtifactBar indexingId={indexingId} />
          </div>
          <ScopeBar
            stepsInScope={readable ? summary.data?.steps.length : undefined}
          />
          <TabsList variant="line" className="border-t border-b-0 px-2">
            {INDEXING_TABS.map((tab) => (
              <TabsTrigger key={tab} value={tab}>
                {TAB_LABELS[tab]}
              </TabsTrigger>
            ))}
          </TabsList>
        </header>
        <TabsContent value="params">
          {run ? (
            <ParametersTab run={run} />
          ) : runQuery.error ? (
            <ErrorState
              error={runQuery.error}
              onRetry={() => void runQuery.refetch()}
              refreshing={runQuery.isFetching}
            />
          ) : (
            <LoadingState label="Loading the parameters" />
          )}
        </TabsContent>
        <TabsContent value="map">
          {gate((results) => (
            <ColorMapTab
              key={indexingId}
              indexingId={indexingId}
              summary={results}
              view={view}
              onViewChange={(change) => {
                onSearchChange(searchPatch(change, results.default_step))
              }}
              selectionStore={selectionStore}
            />
          ))}
        </TabsContent>
        <TabsContent value="pole">
          {gate((results) => (
            <PoleFigureTab
              key={indexingId}
              indexingId={indexingId}
              view={view}
              onViewChange={(change) => {
                onSearchChange(searchPatch(change, results.default_step))
              }}
              selectionStore={selectionStore}
            />
          ))}
        </TabsContent>
        <TabsContent value="detector">
          {gate((results) => (
            <DetectorTab
              key={indexingId}
              indexingId={indexingId}
              summary={results}
              view={view}
              onViewChange={(change) => {
                onSearchChange(searchPatch(change, results.default_step))
              }}
            />
          ))}
        </TabsContent>
        <TabsContent value="peaks">
          {gate(() => (
            <PeaksTab key={indexingId} indexingId={indexingId} />
          ))}
        </TabsContent>
        <TabsContent value="patterns">
          {gate(() => (
            <PatternsTab key={indexingId} indexingId={indexingId} />
          ))}
        </TabsContent>
      </Tabs>
    </GoodnessLabel.Provider>
  )
}

/** The header links: the job, the reconstruction (none for a direct indexation) and the scan. */
function RelatedLinks({ run }: { run: IndexingRunDetail }) {
  return (
    <nav aria-label="Related" className="flex flex-wrap gap-x-4 text-sm">
      <Link
        to="/jobs/$jobId"
        params={{ jobId: String(run.job_id) }}
        className={LINK_CLASS}
      >
        Job {run.job_id}
      </Link>
      {run.reconstruction_id !== null && (
        <Link
          to="/reconstructions/$reconstructionId"
          params={{ reconstructionId: String(run.reconstruction_id) }}
          className={LINK_CLASS}
        >
          Reconstruction R{run.reconstruction_id}
        </Link>
      )}
      {run.scan_number !== null && (
        <Link
          to="/scans/$scanId"
          params={{ scanId: String(run.scan_number) }}
          className={LINK_CLASS}
        >
          Scan {run.scan_number}
        </Link>
      )}
    </nav>
  )
}
