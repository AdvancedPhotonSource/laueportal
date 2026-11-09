// The indexations grid's columns and the results viewer's Peaks and Patterns grids' columns
// (src/features/grid-contract.test.ts checks them against the API).

import type { IndexingRunSummary, PatternRow, PeakRow } from "@/api"
import type { GridColumn } from "@/components/DataGrid"
import { formatSignificant } from "@/lib/format"

// The indexations page's columns, newest first, the scan and the reconstruction as one Source column of
// links, the run's progress in its status ("Running 3/10"; wide enough for "Running 12345/40401"). Like
// the scans, the columns fit the grid from 900 px up; narrower, the ID and the status stay in view
// (GridColumn.pinned).
export const INDEXING_COLUMNS: GridColumn<IndexingRunSummary>[] = [
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
    field: "scan_number",
    header: "Source",
    type: "link",
    link: "source",
    width: 120,
  },
  { field: "n_points", header: "Points", type: "number", width: 80 },
  { field: "author", header: "Author", type: "text", flex: 1, minWidth: 84 },
  { field: "notes", header: "Notes", type: "text", flex: 2, minWidth: 76 },
  { field: "method_label", header: "Method", type: "text", width: 120 },
  { field: "created_at", header: "Date", type: "date", width: 150 },
  {
    field: "status",
    header: "Status",
    type: "status",
    width: 180,
    pinned: "right",
  },
]

// The viewer's measured values to five significant digits (formatSignificant, as its cards), and a
// fraction as a percentage.
const percent = (fraction: number) => `${formatSignificant(fraction * 100)}%`

// The peak table's columns, the optional groups' columns hidden at first (results-tables.ts names the
// groups; the tab's checklist shows them). The defaults fit beside the checklist from 1600 px (Step at
// 70 px, h, k, l at 50); narrower, the grid scrolls sideways with the step in view.
export const PEAK_COLUMNS: GridColumn<PeakRow>[] = [
  { field: "step", header: "Step", type: "number", width: 70, pinned: "left" },
  { field: "frame_id", header: "Frame", type: "text", flex: 2, hidden: true },
  { field: "pattern_index", header: "Pattern", type: "number" },
  { field: "h", header: "h", type: "number", flex: 0.6, minWidth: 50 },
  { field: "k", header: "k", type: "number", flex: 0.6, minWidth: 50 },
  { field: "l", header: "l", type: "number", flex: 0.6, minWidth: 50 },
  { field: "peak_index", header: "Peak #", type: "number" },
  {
    field: "x_pixel",
    header: "X pixel",
    type: "number",
    format: formatSignificant,
  },
  {
    field: "y_pixel",
    header: "Y pixel",
    type: "number",
    format: formatSignificant,
  },
  {
    field: "intensity",
    header: "Intensity",
    type: "number",
    format: formatSignificant,
  },
  {
    field: "integral",
    header: "Integral",
    type: "number",
    format: formatSignificant,
  },
  {
    field: "qx",
    header: "Qx",
    type: "number",
    format: formatSignificant,
    minWidth: 100,
  },
  {
    field: "qy",
    header: "Qy",
    type: "number",
    format: formatSignificant,
    minWidth: 100,
  },
  {
    field: "qz",
    header: "Qz",
    type: "number",
    format: formatSignificant,
    minWidth: 100,
  },
  {
    field: "rms_error_deg",
    header: "RMS Error",
    type: "number",
    format: formatSignificant,
  },
  {
    field: "goodness",
    header: "Goodness",
    type: "number",
    format: formatSignificant,
  },
  { field: "n_peaks", header: "N Peaks", type: "number", hidden: true },
  {
    field: "energy_kev",
    header: "Energy",
    type: "number",
    format: formatSignificant,
    hidden: true,
  },
  {
    field: "error_deg",
    header: "Error",
    type: "number",
    format: formatSignificant,
    hidden: true,
  },
  {
    field: "predicted_intensity",
    header: "Pred. Intensity",
    type: "number",
    format: formatSignificant,
    hidden: true,
  },
  {
    field: "input_image",
    header: "Input Image",
    type: "text",
    flex: 3,
    minWidth: 200,
    hidden: true,
  },
  {
    field: "hwhm_x",
    header: "hwhmX",
    type: "number",
    format: formatSignificant,
    hidden: true,
  },
  {
    field: "hwhm_y",
    header: "hwhmY",
    type: "number",
    format: formatSignificant,
    hidden: true,
  },
  {
    field: "tilt",
    header: "Tilt",
    type: "number",
    format: formatSignificant,
    hidden: true,
  },
  {
    field: "chisq",
    header: "ChiSq",
    type: "number",
    format: formatSignificant,
    hidden: true,
  },
  {
    field: "background",
    header: "Background",
    type: "number",
    format: formatSignificant,
    hidden: true,
  },
  {
    field: "pattern_n_indexed",
    header: "Pattern N Indexed",
    type: "number",
    hidden: true,
  },
  {
    field: "pattern_indexed_fraction",
    header: "Pattern Indexed %",
    type: "number",
    format: percent,
    hidden: true,
  },
]

// The pattern table's columns, as the peaks: the Position and Run groups hidden at first.
export const PATTERN_COLUMNS: GridColumn<PatternRow>[] = [
  { field: "step", header: "Step", type: "number", width: 80, pinned: "left" },
  { field: "frame_id", header: "Frame", type: "text", flex: 2 },
  { field: "scan_number", header: "Scan", type: "number" },
  { field: "pattern_index", header: "Pattern", type: "number" },
  { field: "n_indexed", header: "N Indexed", type: "number" },
  { field: "n_peaks", header: "N Peaks", type: "number" },
  {
    field: "indexed_fraction",
    header: "Indexed %",
    type: "number",
    format: percent,
  },
  {
    field: "rms_error_deg",
    header: "RMS Error",
    type: "number",
    format: formatSignificant,
  },
  {
    field: "goodness",
    header: "Goodness",
    type: "number",
    format: formatSignificant,
  },
  { field: "n_patterns", header: "N Patterns", type: "number" },
  { field: "astar", header: "a*", type: "text", flex: 2, minWidth: 140 },
  { field: "bstar", header: "b*", type: "text", flex: 2, minWidth: 140 },
  { field: "cstar", header: "c*", type: "text", flex: 2, minWidth: 140 },
  { field: "structure", header: "Structure", type: "text" },
  { field: "space_group", header: "Space Group", type: "number" },
  {
    field: "x_um",
    header: "X motor",
    type: "number",
    format: formatSignificant,
    hidden: true,
  },
  {
    field: "y_um",
    header: "Y motor",
    type: "number",
    format: formatSignificant,
    hidden: true,
  },
  {
    field: "z_um",
    header: "Z motor",
    type: "number",
    format: formatSignificant,
    hidden: true,
  },
  {
    field: "depth_um",
    header: "Depth",
    type: "number",
    format: formatSignificant,
    hidden: true,
  },
  {
    field: "frame_energy_kev",
    header: "Energy",
    type: "number",
    format: formatSignificant,
    hidden: true,
  },
  {
    field: "input_image",
    header: "Input Image",
    type: "text",
    flex: 3,
    minWidth: 200,
    hidden: true,
  },
]
