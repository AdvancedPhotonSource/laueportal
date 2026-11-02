// The run monitor's columns (src/features/grid-contract.test.ts checks them against the API).
import { createElement } from "react"

import type { GridColumn } from "@/components/DataGrid"
import { JobStatusCell } from "./JobStatusCell"
import type { RunMonitorRow } from "./remote-status"

// The run monitor's columns (an Execution column, where the run computes, with DM's state beside it; a
// crux run's phase in the Status cell), newest job first, live while a queued or running job shows. The
// Job ID comes first, pinned as in every list. The columns need about 1410 px: narrower, the job ID and
// the status stay in view (GridColumn.pinned) while the middle scrolls. Not shown: Recon Method,
// Aperture, queue position and priority, or a Remote Phase column.
export const JOB_COLUMNS: GridColumn<RunMonitorRow>[] = [
  {
    field: "job_id",
    header: "Job ID",
    type: "link",
    link: "job",
    width: 88,
    initialSort: "desc",
    pinned: "left",
  },
  // Filters on the run's label (`I12`); the list does not sort by it.
  {
    field: "run",
    header: "Run",
    type: "link",
    link: "run",
    width: 80,
    sortable: false,
  },
  { field: "author", header: "Author", type: "text", flex: 1, minWidth: 90 },
  // A queued or running crux run's remote phase, else the run's status; it sorts and filters the
  // status.
  {
    field: "status",
    header: "Status",
    type: "status",
    cell: (row) => createElement(JobStatusCell, { row }),
    width: 180,
    pinned: "right",
  },
  {
    field: "n_processed",
    header: "Run Progress",
    type: "progress",
    width: 200,
  },
  { field: "start_time", header: "Start Time", type: "date", width: 150 },
  // The last line of the job's messages; it neither sorts nor filters.
  {
    field: "message",
    header: "Messages",
    type: "text",
    flex: 2,
    minWidth: 120,
    sortable: false,
    filterable: false,
  },
  {
    field: "duration_seconds",
    header: "Duration",
    type: "duration",
    width: 150,
  },
  {
    field: "scan_number",
    header: "Scan ID",
    type: "link",
    link: "scan",
    width: 100,
  },
  // Where the run computes, by the API's name ("APS", "ALCF"), which it sorts and filters.
  { field: "execution", header: "Execution", type: "text", width: 120 },
  { field: "dm_state", header: "DM State", type: "text", width: 112 },
]
