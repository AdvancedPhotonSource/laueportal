// The reconstructions grid's columns (src/features/grid-contract.test.ts checks them against the API),
// and the wire viewer's Points grid's (a client-side grid).
import { createElement } from "react"

import type { ReconstructionRunSummary } from "@/api"
import type { GridColumn } from "@/components/DataGrid"
import { PointSource, PointStatusBadge } from "./PointCells"
import { formatMicrons, type PointRow } from "./points"

// The reconstructions page's columns, newest first, the run's progress in its status
// ("Running 3/10"; wide enough for "Running 12345/40401"). The columns fit the grid from about
// 1200 px up (the three dates take most of it); narrower, the ID and the status are pinned
// (GridColumn.pinned), so they stay in view while the middle scrolls.
export const RECONSTRUCTION_COLUMNS: GridColumn<ReconstructionRunSummary>[] = [
  {
    field: "id",
    header: "ID",
    type: "link",
    link: "reconstruction",
    width: 72,
    initialSort: "desc",
    pinned: "left",
  },
  { field: "method", header: "Method", type: "text", width: 96 },
  {
    field: "scan_number",
    header: "Source",
    type: "link",
    link: "source",
    width: 96,
  },
  { field: "n_points", header: "Points", type: "number", width: 80 },
  { field: "author", header: "Author", type: "text", flex: 1, minWidth: 84 },
  { field: "notes", header: "Notes", type: "text", flex: 2, minWidth: 76 },
  { field: "created_at", header: "Date", type: "date", width: 150 },
  { field: "start_time", header: "Start Time", type: "date", width: 150 },
  { field: "finish_time", header: "Finish Time", type: "date", width: 150 },
  {
    field: "status",
    header: "Status",
    type: "status",
    width: 180,
    pinned: "right",
  },
]

// The wire viewer's Points tab: client-side, in the catalog's order. Wider than the grid below
// about 1450 px, so the point is pinned on the left and its status on the right while the middle
// scrolls. A point that is not complete has the API's explanation as its status's tooltip.
export const POINT_COLUMNS: GridColumn<PointRow>[] = [
  {
    field: "point_id",
    header: "Point",
    type: "text",
    width: 140,
    pinned: "left",
  },
  { field: "n_depths", header: "Depths", type: "number", width: 90 },
  {
    field: "depth_first_um",
    header: "First depth (µm)",
    type: "number",
    format: formatMicrons,
    width: 150,
  },
  {
    field: "depth_last_um",
    header: "Last depth (µm)",
    type: "number",
    format: formatMicrons,
    width: 150,
  },
  { field: "frame", header: "Frame (y × x)", type: "text", width: 130 },
  { field: "dtype", header: "Stored type", type: "text", width: 115 },
  {
    field: "sample",
    header: "Sample x, y, z (µm)",
    type: "text",
    width: 170,
  },
  {
    field: "source",
    header: "Source",
    type: "custom",
    cell: (row) => createElement(PointSource, { point: row }),
    flex: 2,
    minWidth: 260,
  },
  { field: "error", header: "Error", type: "text", flex: 1, minWidth: 140 },
  {
    field: "status",
    header: "Status",
    type: "custom",
    cell: (row) => createElement(PointStatusBadge, { point: row }),
    width: 135,
    pinned: "right",
  },
]
