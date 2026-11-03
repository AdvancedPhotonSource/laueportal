// The scans grid's columns, the scan page's run cards' and the import page's (apart from the components,
// which export only components; src/features/grid-contract.test.ts checks them against the API, all but
// the import's, a client-side grid).

import type {
  IndexingRunSummary,
  ReconstructionRunSummary,
  ScanSummary,
} from "@/api"
import type { GridColumn } from "@/components/DataGrid"
import { createElement } from "react"
import { ImportStatusBadge } from "./ImportStatusBadge"
import type { ImportRow } from "./scan-import"

// The scans page's columns, sorted by Scan ID descending. The columns fit the grid from 900 px up: the
// short ones have fixed widths (their headers and values, measured), the wide text ones share the rest
// (each from a minimum that fits its header); narrower than that, the grid scrolls sideways.
export const SCAN_COLUMNS: GridColumn<ScanSummary>[] = [
  {
    field: "scan_number",
    header: "Scan ID",
    type: "link",
    link: "scan",
    width: 90,
    initialSort: "desc",
  },
  {
    field: "sample_name",
    header: "Sample Name",
    type: "text",
    flex: 1.5,
    minWidth: 128,
  },
  { field: "aperture", header: "Aperture", type: "text", width: 97 },
  {
    field: "n_dimensions",
    header: "Scan Dim",
    type: "number",
    format: (dimensions) => `${String(dimensions)}D`,
    width: 99,
  },
  {
    field: "technique",
    header: "Technique",
    type: "text",
    flex: 1,
    minWidth: 104,
  },
  { field: "user_name", header: "User", type: "text", flex: 1, minWidth: 76 },
  { field: "time", header: "Date", type: "date", width: 150 },
  { field: "notes", header: "Notes", type: "text", flex: 2, minWidth: 80 },
]

// The run cards on the scan page: this scan's runs, newest first, live while one is queued or
// running. Their columns overflow the card below about 1300 px: the ID and the status are pinned
// then (GridColumn.pinned), so they stay in view while the middle scrolls.
export const SCAN_RECONSTRUCTION_COLUMNS: GridColumn<ReconstructionRunSummary>[] =
  [
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
    { field: "author", header: "Author", type: "text", flex: 1, minWidth: 96 },
    { field: "n_points", header: "Points", type: "number", width: 88 },
    { field: "percent_brightest", header: "Pixels", type: "number", width: 88 },
    { field: "depth_start", header: "Depth Start", type: "number", width: 120 },
    { field: "depth_end", header: "Depth End", type: "number", width: 104 },
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

// The indexing list sorts and filters every field but `method` (all LaueGo runs).
export const SCAN_INDEXING_COLUMNS: GridColumn<IndexingRunSummary>[] = [
  {
    field: "id",
    header: "ID",
    type: "link",
    link: "indexing",
    width: 72,
    initialSort: "desc",
    pinned: "left",
  },
  {
    field: "reconstruction_id",
    header: "Recon",
    type: "link",
    link: "reconstruction",
    width: 88,
  },
  {
    field: "method",
    header: "Method",
    type: "text",
    width: 88,
    filterable: false,
    sortable: false,
  },
  { field: "author", header: "Author", type: "text", flex: 1, minWidth: 96 },
  { field: "n_points", header: "Points", type: "number", width: 88 },
  { field: "n_depths", header: "Depth Points", type: "number", width: 124 },
  { field: "box_size", header: "Box", type: "number", width: 72 },
  {
    field: "crystal_file",
    header: "Structures File",
    type: "text",
    flex: 1,
    minWidth: 132,
  },
  { field: "created_at", header: "Date", type: "date", width: 150 },
  {
    field: "status",
    header: "Status",
    type: "status",
    width: 180,
    pinned: "right",
  },
  { field: "notes", header: "Notes", type: "text", flex: 1, minWidth: 88 },
]

// The import page's scan-log index: client-side, newest first. The columns fit the grid from 900 px
// up; the status is a badge (a failed import's error is its tooltip).
export const SCAN_LOG_COLUMNS: GridColumn<ImportRow>[] = [
  {
    field: "scan_number",
    header: "Scan ID",
    type: "number",
    width: 95,
  },
  {
    field: "time",
    header: "Date / Time",
    type: "date",
    width: 165,
    initialSort: "desc",
  },
  { field: "user_name", header: "User", type: "text", flex: 1, minWidth: 80 },
  {
    field: "energy",
    header: "Energy",
    type: "number",
    format: (energy, row) =>
      row.energy_unit ? `${String(energy)} ${row.energy_unit}` : String(energy),
    width: 105,
  },
  {
    field: "sample_xyz",
    header: "Sample XYZ",
    type: "text",
    flex: 1.5,
    minWidth: 120,
  },
  {
    field: "n_dimensions",
    header: "Dims",
    type: "number",
    format: (dimensions) => `${String(dimensions)}D`,
    width: 80,
  },
  {
    field: "status",
    header: "Status",
    type: "custom",
    cell: (row) =>
      createElement(ImportStatusBadge, {
        status: row.status,
        error: row.error,
      }),
    width: 120,
  },
]
