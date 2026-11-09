import { useNavigate } from "@tanstack/react-router"
import { useRef, useState } from "react"

import { listIndexingRuns, type IndexingRunSummary } from "@/api"
import {
  DataGrid,
  type DataGridHandle,
  type FetchRows,
} from "@/components/DataGrid"
import { GridPage } from "@/components/GridPage"
import { Button } from "@/components/ui/button"
import { indexingActions, type IndexingActions } from "./indexing-actions"
import { INDEXING_COLUMNS } from "./columns"

const fetchIndexations: FetchRows<IndexingRunSummary> = (query) =>
  listIndexingRuns(query)

const indexingRowId = (run: IndexingRunSummary) => String(run.id)

/**
 * The indexations: every indexing run, live. One checked row: New Recon reconstructs the
 * selected run's source, New Index clones the selected run.
 */
export function IndexationsPage() {
  const navigate = useNavigate()
  const grid = useRef<DataGridHandle>(null)
  const [actions, setActions] = useState<IndexingActions>({ count: 0 })
  const { newRecon, newIndex } = actions

  return (
    <GridPage
      title="Indexations"
      selected={actions.count}
      onClearSelection={() => grid.current?.clearSelection()}
      actions={
        <>
          <Button
            disabled={!newRecon}
            onClick={() => {
              if (newRecon) {
                void navigate({ to: "/reconstructions/new", search: newRecon })
              }
            }}
          >
            New Recon
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
        gridId="indexations"
        columns={INDEXING_COLUMNS}
        fetchRows={fetchIndexations}
        getRowId={indexingRowId}
        selection="singleRow"
        onSelectionChange={(rows) => {
          setActions(indexingActions(rows))
        }}
        live
        changedSince
        noun="indexations"
        className="flex-1"
      />
    </GridPage>
  )
}
