import { useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { useEffect, useRef, useState } from "react"

import {
  getListReconstructionPointsQueryKey,
  ReconstructionArtifactState,
  useGetReconstructionArtifact,
  useGetReconstructionRun,
  useListReconstructionPoints,
  type ReconstructionRunDetail,
} from "@/api"
import { ErrorState } from "@/components/ErrorState"
import { LoadingState } from "@/components/LoadingState"
import { ViewLinkButton } from "@/components/ViewLinkButton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { failureStreak } from "@/lib/polling"
import { surface } from "@/lib/styles"
import { cn } from "@/lib/utils"
import { ArtifactBar } from "./ArtifactBar"
import { DepthViewTab } from "./DepthViewTab"
import {
  artifactRefetchInterval,
  canFollow,
  firstComplete,
  isComplete,
  newestComplete,
  noReconstruction,
  selectedPoint,
} from "./artifact-bar"
import { ParametersTab } from "./ParametersTab"
import { PointsTab } from "./PointsTab"
import { RoiInspectorTab } from "./RoiInspectorTab"
import { usePackedReconstructionLink } from "./viewer-link"
import {
  isReconstructionTab,
  RECONSTRUCTION_TABS,
  searchPatch,
  TAB_LABELS,
  viewOf,
  type ReconstructionSearch,
} from "./viewer-search"

const LINK_CLASS = "text-link underline-offset-4 hover:underline"

/**
 * The wire reconstruction viewer: the header, one region: the title with the run's job and scan and
 * "Copy link to this view"; the artifact bar; the tabs on its baseline. Then the tab's content. `search`
 * is the URL's view (src/lib/view-search.ts), `onSearchChange` changes it. The page reads the artifact
 * every 5 s while the run is active, and the points again whenever the artifact changes. For an open
 * live run, "Follow newest point" (off by default) selects each newest complete point
 * (`newestComplete`), written with replace; the user's own pick turns it off.
 */
export function ReconstructionViewerPage({
  reconstructionId,
  search,
  onSearchChange,
  onSearchReplace,
}: {
  reconstructionId: number
  search: ReconstructionSearch
  onSearchChange: (patch: Partial<ReconstructionSearch>) => void
  /** Write a change at once, replacing the history entry (the shown point, when it is not the URL's default). */
  onSearchReplace: (patch: Partial<ReconstructionSearch>) => void
}) {
  const queryClient = useQueryClient()
  // The route's loader has read the run (its job and stored parameters).
  const runQuery = useGetReconstructionRun(reconstructionId, {
    query: { staleTime: 10_000 },
  })
  const run = runQuery.data

  // The artifact's consecutive failed reads, for the back-off.
  const [streak] = useState(failureStreak)
  const artifact = useGetReconstructionArtifact(reconstructionId, {
    query: {
      refetchInterval: (query) =>
        artifactRefetchInterval(
          query.state.data,
          query.state.error,
          streak(query.state)
        ),
    },
  })
  const ready = artifact.data?.state === ReconstructionArtifactState.ready
  const unavailable = noReconstruction(artifact.data)
  const points = useListReconstructionPoints(reconstructionId, {
    query: { enabled: ready, placeholderData: (previous) => previous },
  })

  // A new artifact (a point completed, the run finished): read the points again. An unchanged one
  // (a 304) is the same object, so polling an idle run reads nothing more.
  const previous = useRef(artifact.data)
  useEffect(() => {
    const before = previous.current
    previous.current = artifact.data
    if (before !== undefined && artifact.data !== before) {
      void queryClient.invalidateQueries({
        queryKey: getListReconstructionPointsQueryKey(reconstructionId),
      })
    }
  }, [artifact.data, queryClient, reconstructionId])

  // The default point: the first complete one, kept while it stays complete, so a point completing
  // before it (a running run, its points out of order) does not move the page. The URL's default is
  // what a fresh load picks (`urlDefault`); when the kept point is no longer that, the page writes it
  // to the URL (with replace), so a reload and a copied link open the point shown.
  const rows = ready ? points.data?.rows : undefined
  const [firstPoint, setFirstPoint] = useState<string | undefined>(undefined)
  const kept =
    firstPoint !== undefined &&
    rows?.some((point) => point.point_id === firstPoint && isComplete(point))
  const fallback = rows === undefined || kept ? firstPoint : firstComplete(rows)
  if (fallback !== firstPoint) setFirstPoint(fallback)

  const urlDefault = rows && firstComplete(rows)

  const view = viewOf(search)
  // "Follow newest point" (an open live run only): the newest complete point is the one asked for.
  const [follow, setFollow] = useState(false)
  const following = follow && canFollow(artifact.data)
  const newest = following && rows ? newestComplete(rows) : undefined
  // The URL's point while the points load (the artifact is ready); none when they cannot load.
  const point = selectedPoint(newest ?? view.point, rows, fallback, ready)
  const replaceRef = useRef(onSearchReplace)
  useEffect(() => {
    replaceRef.current = onSearchReplace
  })
  useEffect(() => {
    if (rows === undefined || point === undefined) return
    if (newest !== undefined) {
      // Followed: written with replace (the history keeps the user's own picks), at its brightest depth.
      if (newest !== (view.point ?? urlDefault)) {
        replaceRef.current(searchPatch({ point: newest }, urlDefault))
      }
    } else if (view.point === undefined && point !== urlDefault) {
      replaceRef.current({ point })
    }
  }, [rows, point, urlDefault, view.point, newest])

  const pointsLoading = ready
    ? points.isPending
    : artifact.data === undefined && artifact.error === null
  const shownPoint = rows?.find((row) => row.point_id === point)

  const refresh = async () => {
    const before = artifact.data
    const read = await artifact.refetch()
    // A changed artifact re-reads the points itself (the effect above).
    if (ready && read.data === before) void points.refetch()
  }

  return (
    <Tabs
      render={<section />}
      className="gap-4"
      value={view.tab}
      onValueChange={(value) => {
        if (isReconstructionTab(value)) {
          onSearchChange(searchPatch({ tab: value }))
        }
      }}
    >
      <header className={cn(surface, "flex flex-col")}>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2.5">
          <h1 className="text-xl font-semibold">
            Reconstruction R{reconstructionId}
          </h1>
          {run && <RelatedLinks run={run} />}
          <CopyLink search={search} />
        </div>
        <div className="px-4 pb-2.5">
          <ArtifactBar
            point={point}
            artifact={artifact.data}
            error={artifact.error}
            refreshing={artifact.isFetching || points.isFetching}
            onRefresh={() => {
              void refresh()
            }}
            follow={
              canFollow(artifact.data)
                ? { checked: follow, onChange: setFollow }
                : undefined
            }
          />
        </div>
        <TabsList variant="line" className="border-t border-b-0 px-2">
          {RECONSTRUCTION_TABS.map((tab) => (
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
      <TabsContent value="points">
        <PointsTab
          ready={ready}
          unavailable={unavailable}
          settled={artifact.data !== undefined || artifact.error !== null}
          artifactError={artifact.error}
          onArtifactRetry={() => void artifact.refetch()}
          points={points}
          selected={point}
          onSelect={(chosen) => {
            // The user's own pick ends "Follow newest point".
            setFollow(false)
            // The URL's default chosen: it is the page's default again.
            if (chosen === urlDefault) setFirstPoint(chosen)
            onSearchChange(searchPatch({ point: chosen }, urlDefault))
          }}
        />
      </TabsContent>
      <TabsContent value="depth">
        <DepthViewTab
          reconstructionId={reconstructionId}
          loading={pointsLoading}
          unavailable={unavailable}
          point={shownPoint}
          depth={view.depth}
          onDepthChange={(depth, brightest) => {
            onSearchChange(searchPatch({ depth }, urlDefault, brightest))
          }}
        />
      </TabsContent>
      <TabsContent value="roi">
        <RoiInspectorTab
          reconstructionId={reconstructionId}
          loading={pointsLoading}
          unavailable={unavailable}
          point={shownPoint}
        />
      </TabsContent>
    </Tabs>
  )
}

function CopyLink({ search }: { search: ReconstructionSearch }) {
  return <ViewLinkButton link={usePackedReconstructionLink(search)} />
}

/** The header links: the job and the scan (none for an unlinked run). */
function RelatedLinks({ run }: { run: ReconstructionRunDetail }) {
  return (
    <nav aria-label="Related" className="flex flex-wrap gap-x-4 text-sm">
      <Link
        to="/jobs/$jobId"
        params={{ jobId: String(run.job_id) }}
        className={LINK_CLASS}
      >
        Job {run.job_id}
      </Link>
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
