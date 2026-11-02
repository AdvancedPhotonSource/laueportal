import { Link } from "@tanstack/react-router"
import { useRef, useState } from "react"

import {
  listIndexingRuns,
  listReconstructionRuns,
  useListReconstructionRuns,
  type IndexingRunSummary,
  type ReconstructionRunSummary,
} from "@/api"
import { DataGrid, type DataGridHandle } from "@/components/DataGrid"
import { Button, buttonVariants } from "@/components/ui/button"
import type { CreatePrefill } from "@/lib/prefill"
import { isDepthScan } from "./scan-detail"
import {
  indexingCardLinks,
  reconstructionCardLinks,
  type SelectedIndexing,
  type SelectedReconstruction,
} from "./scan-run-actions"
import { ScanCard } from "./ScanCard"
import { SCAN_RECONSTRUCTION_COLUMNS, SCAN_INDEXING_COLUMNS } from "./columns"

const reconstructionRowId = (run: ReconstructionRunSummary) => String(run.id)
const indexingRowId = (run: IndexingRunSummary) => String(run.id)

/**
 * The scan's run cards: Reconstructions and Peak Indexing, each a grid of this scan's runs (one
 * checked row) with New Recon and New Index, which open the create forms with this scan and the
 * selected run (or this scan alone when none is selected). Reconstructions shows for a depth scan
 * and for a scan of another type that has reconstructions (counted by asking for one), or whose
 * count failed (its grid then says why).
 */
export function ScanRunCards({
  scanId,
  technique,
}: {
  scanId: number
  technique: string
}) {
  const depth = isDepthScan(technique)
  const count = useListReconstructionRuns(
    { scan_id: scanId, limit: 1 },
    { query: { enabled: !depth } }
  )
  const reconstructions = depth || (count.data?.total ?? 0) > 0 || count.isError
  return (
    <div className="flex flex-col gap-4">
      {reconstructions && <ReconstructionsCard scanId={scanId} />}
      <IndexingCard scanId={scanId} />
    </div>
  )
}

function ReconstructionsCard({ scanId }: { scanId: number }) {
  const grid = useRef<DataGridHandle>(null)
  const [selected, setSelected] = useState<SelectedReconstruction>()
  const links = reconstructionCardLinks(scanId, selected)
  return (
    <RunCard
      title="Reconstructions"
      selected={selected !== undefined}
      onClearSelection={() => grid.current?.clearSelection()}
      newRecon={links.newRecon}
      newIndex={links.newIndex}
    >
      <DataGrid
        ref={grid}
        gridId="scan-reconstructions"
        listKey={scanId}
        columns={SCAN_RECONSTRUCTION_COLUMNS}
        fetchRows={(query) =>
          listReconstructionRuns({ ...query, scan_id: scanId })
        }
        getRowId={reconstructionRowId}
        selection="singleRow"
        onSelectionChange={(rows) => {
          const [run] = rows
          setSelected(run && { id: run.id, method: run.method })
        }}
        live
        changedSince
        noun="reconstructions"
        className="h-80"
      />
    </RunCard>
  )
}

function IndexingCard({ scanId }: { scanId: number }) {
  const grid = useRef<DataGridHandle>(null)
  const [selected, setSelected] = useState<SelectedIndexing>()
  const links = indexingCardLinks(scanId, selected)
  return (
    <RunCard
      title="Peak Indexing"
      selected={selected !== undefined}
      onClearSelection={() => grid.current?.clearSelection()}
      newRecon={links.newRecon}
      newIndex={links.newIndex}
    >
      <DataGrid
        ref={grid}
        gridId="scan-indexations"
        listKey={scanId}
        columns={SCAN_INDEXING_COLUMNS}
        fetchRows={(query) => listIndexingRuns({ ...query, scan_id: scanId })}
        getRowId={indexingRowId}
        selection="singleRow"
        onSelectionChange={(rows) => {
          const [run] = rows
          setSelected(
            run && { id: run.id, reconstruction_id: run.reconstruction_id }
          )
        }}
        live
        changedSince
        noun="indexations"
        className="h-80"
      />
    </RunCard>
  )
}

/** A run card: its title, the selection, New Recon and New Index as links, and its grid. */
function RunCard({
  title,
  selected,
  onClearSelection,
  newRecon,
  newIndex,
  children,
}: {
  title: string
  selected: boolean
  onClearSelection: () => void
  newRecon: CreatePrefill
  newIndex: CreatePrefill
  children: React.ReactNode
}) {
  return (
    <ScanCard
      title={title}
      actions={
        <>
          {selected && (
            <>
              <span className="text-sm text-muted-foreground" role="status">
                1 selected
              </span>
              <Button variant="ghost" size="sm" onClick={onClearSelection}>
                Clear selection
              </Button>
            </>
          )}
          <Link
            to="/reconstructions/new"
            search={newRecon}
            className={buttonVariants({ size: "sm" })}
          >
            New Recon
          </Link>
          <Link
            to="/indexing/new"
            search={newIndex}
            className={buttonVariants({ size: "sm" })}
          >
            New Index
          </Link>
        </>
      }
    >
      {children}
    </ScanCard>
  )
}
