import { useCallback, useEffect, useMemo, useRef } from "react"

import type { ReconstructionPointList } from "@/api"
import { DataGrid, type DataGridHandle } from "@/components/DataGrid"
import { EmptyState } from "@/components/EmptyState"
import { ErrorState } from "@/components/ErrorState"
import { LoadingState } from "@/components/LoadingState"
import { formatCount } from "@/lib/format"
import { isComplete } from "./artifact-bar"
import { POINT_COLUMNS } from "./columns"
import { pointRowId, pointRows } from "./points"

/**
 * The Points tab: every point of the run in a client-side DataGrid, one selectable at a time
 * (`singleRow`), only the complete ones. Checking a point selects it on the page; the selected point
 * stays checked (unchecking it keeps it). The page reads the points again with the artifact while the
 * run is active.
 */
export function PointsTab({
  ready,
  unavailable,
  settled,
  artifactError,
  onArtifactRetry,
  points,
  selected,
  onSelect,
}: {
  /** The artifact is ready: the run has points to list. */
  ready: boolean
  /** Why the run has no reconstruction to show (`noReconstruction`); undefined when it has one. */
  unavailable: string | undefined
  /** The artifact has answered (or failed): not ready then means none yet. */
  settled: boolean
  /** Why the artifact cannot be read (a 409 unreadable, a 404), when it cannot. */
  artifactError: unknown
  onArtifactRetry: () => void
  points: {
    data: ReconstructionPointList | undefined
    error: unknown
    isFetching: boolean
    refetch: () => unknown
  }
  selected: string | undefined
  onSelect: (pointId: string) => void
}) {
  const grid = useRef<DataGridHandle>(null)
  const rows = useMemo(
    () => (points.data ? pointRows(points.data.rows) : undefined),
    [points.data]
  )
  // The grid shows the page's point checked, whichever way it was chosen (the URL, the default).
  const selectedRef = useRef(selected)
  const show = useCallback(() => {
    const point = selectedRef.current
    grid.current?.selectRows(point === undefined ? [] : [point])
  }, [])
  useEffect(() => {
    selectedRef.current = selected
    show()
  }, [selected, rows, show])

  const title = (
    <h2 className="text-lg font-semibold">
      Points
      {rows && ` (${formatCount(rows.length)} total)`}
    </h2>
  )
  let content
  if (points.error && !rows) {
    content = (
      <ErrorState
        error={points.error}
        onRetry={() => void points.refetch()}
        retrying={points.isFetching}
      />
    )
  } else if (!ready && artifactError) {
    content = <ErrorState error={artifactError} onRetry={onArtifactRetry} />
  } else if (!ready) {
    content = settled ? (
      <EmptyState title={unavailable ?? "No points yet"} />
    ) : (
      <LoadingState label="Loading the points" variant="table" />
    )
  } else if (!rows) {
    content = <LoadingState label="Loading the points" variant="table" />
  } else {
    content = (
      <DataGrid
        ref={grid}
        gridId="reconstruction-points"
        columns={POINT_COLUMNS}
        rows={rows}
        getRowId={pointRowId}
        selection="singleRow"
        isRowSelectable={isComplete}
        onSelectionChange={(chosen) => {
          const row = chosen[0]
          if (row === undefined) {
            // Unchecked (or between two rows of one change): the page's point stays checked.
            setTimeout(show, 0)
          } else if (row.point_id !== selectedRef.current) {
            selectedRef.current = row.point_id
            onSelect(row.point_id)
          }
        }}
        onRowCount={show}
        noun="points"
        className="h-[max(400px,calc(100dvh-17rem))]"
      />
    )
  }
  return (
    <div className="flex flex-col gap-2">
      {title}
      {points.error !== null && rows && (
        <ErrorState
          error={points.error}
          onRetry={() => void points.refetch()}
          refreshing={points.isFetching}
          stale
        />
      )}
      {content}
    </div>
  )
}
