import { useNavigate } from "@tanstack/react-router"
import { useRef, useState } from "react"

import { listScans, type ScanSummary } from "@/api"
import {
  DataGrid,
  type DataGridHandle,
  type FetchRows,
} from "@/components/DataGrid"
import { GridPage } from "@/components/GridPage"
import { Button } from "@/components/ui/button"
import { sourcePrefill, type CreatePrefill } from "@/lib/prefill"
import { SCAN_COLUMNS } from "./columns"

const fetchScans: FetchRows<ScanSummary> = (query) => listScans(query)

const scanRowId = (scan: ScanSummary) => String(scan.scan_number)

/**
 * The scans: the grid of every scan (one checked row), and New Recon / New Index, which open the
 * create forms with the selected scan (`?scan=N`).
 */
export function ScansPage() {
  const navigate = useNavigate()
  const grid = useRef<DataGridHandle>(null)
  const [selected, setSelected] = useState<number[]>([])
  const [scan] = selected
  const prefill: CreatePrefill | undefined =
    selected.length === 1 ? sourcePrefill({ scan }) : undefined

  return (
    <GridPage
      title="Scans"
      selected={selected.length}
      onClearSelection={() => grid.current?.clearSelection()}
      actions={
        <>
          <Button
            disabled={!prefill}
            onClick={() => {
              if (prefill) {
                void navigate({ to: "/reconstructions/new", search: prefill })
              }
            }}
          >
            New Recon
          </Button>
          <Button
            disabled={!prefill}
            onClick={() => {
              if (prefill) {
                void navigate({ to: "/indexing/new", search: prefill })
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
        gridId="scans"
        columns={SCAN_COLUMNS}
        fetchRows={fetchScans}
        getRowId={scanRowId}
        selection="singleRow"
        onSelectionChange={(rows) => {
          setSelected(rows.map((scan) => scan.scan_number))
        }}
        noun="scans"
        className="flex-1"
      />
    </GridPage>
  )
}
