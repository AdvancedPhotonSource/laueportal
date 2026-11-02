import { useNavigate } from "@tanstack/react-router"
import { useRef, useState } from "react"

import { listReconstructionRuns, type ReconstructionRunSummary } from "@/api"
import {
  DataGrid,
  type DataGridHandle,
  type FetchRows,
} from "@/components/DataGrid"
import { GridPage } from "@/components/GridPage"
import { Button } from "@/components/ui/button"
import {
  reconstructionActions,
  type ReconstructionActions,
} from "./reconstruction-actions"
import { RECONSTRUCTION_COLUMNS } from "./columns"

const fetchReconstructions: FetchRows<ReconstructionRunSummary> = (query) =>
  listReconstructionRuns(query)

const reconstructionRowId = (run: ReconstructionRunSummary) => String(run.id)

/**
 * The reconstructions: every reconstruction run, live. One checked row: New Wire Recon clones the
 * selected wire run, New Index indexes the selected run.
 */
export function ReconstructionsPage() {
  const navigate = useNavigate()
  const grid = useRef<DataGridHandle>(null)
  const [actions, setActions] = useState<ReconstructionActions>({ count: 0 })
  const { newWireRecon, newIndex } = actions

  return (
    <GridPage
      title="Reconstructions"
      selected={actions.count}
      onClearSelection={() => grid.current?.clearSelection()}
      actions={
        <>
          <Button
            disabled={!newWireRecon}
            onClick={() => {
              if (newWireRecon) {
                void navigate({
                  to: "/reconstructions/new",
                  search: newWireRecon,
                })
              }
            }}
          >
            New Wire Recon
          </Button>
          <Button
            disabled={!newIndex}
            onClick={() => {
              if (newIndex) {
                void navigate({ to: "/indexing/new", search: newIndex })
              }
            }}
          >
            New Index
          </Button>
        </>
      }
    >
      <DataGrid
        ref={grid}
        gridId="reconstructions"
        columns={RECONSTRUCTION_COLUMNS}
        fetchRows={fetchReconstructions}
        getRowId={reconstructionRowId}
        selection="singleRow"
        onSelectionChange={(rows) => {
          setActions(reconstructionActions(rows))
        }}
        live
        changedSince
        noun="reconstructions"
        className="flex-1"
      />
    </GridPage>
  )
}
