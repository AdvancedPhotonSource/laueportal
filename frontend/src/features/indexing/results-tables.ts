// The Peaks and Patterns tabs: the column checklists. Each group names columns of the tab's grid
// (columns.ts); "Default" is shown at first, the optional groups are hidden at first. What is shown
// is the grid's own column state, kept per gridId (the DataGrid's convention).
import type { PatternRow, PeakRow } from "@/api"
import type { GridColumn } from "@/components/DataGrid"
import { formatCount } from "@/lib/format"

export interface ColumnGroup<TRow> {
  title: string
  fields: readonly Extract<keyof TRow, string>[]
}

export const PEAKS_GRID_ID = "indexing-peaks"
export const PATTERNS_GRID_ID = "indexing-patterns"

export const PEAK_COLUMN_GROUPS: readonly ColumnGroup<PeakRow>[] = [
  {
    title: "Default",
    fields: [
      "step",
      "pattern_index",
      "h",
      "k",
      "l",
      "peak_index",
      "x_pixel",
      "y_pixel",
      "intensity",
      "integral",
      "qx",
      "qy",
      "qz",
      "rms_error_deg",
      "goodness",
    ],
  },
  {
    title: "Geometry",
    fields: [
      "frame_id",
      "n_peaks",
      "energy_kev",
      "error_deg",
      "predicted_intensity",
      "input_image",
    ],
  },
  {
    title: "Fit Quality",
    fields: ["hwhm_x", "hwhm_y", "tilt", "chisq", "background"],
  },
  {
    title: "Indexing",
    fields: ["pattern_n_indexed", "pattern_indexed_fraction"],
  },
]

export const PATTERN_COLUMN_GROUPS: readonly ColumnGroup<PatternRow>[] = [
  {
    title: "Default",
    fields: [
      "step",
      "frame_id",
      "scan_number",
      "pattern_index",
      "n_indexed",
      "n_peaks",
      "indexed_fraction",
      "rms_error_deg",
      "goodness",
      "n_patterns",
      "astar",
      "bstar",
      "cstar",
      "structure",
      "space_group",
    ],
  },
  { title: "Position", fields: ["x_um", "y_um", "z_um", "depth_um"] },
  { title: "Run", fields: ["frame_energy_kev", "input_image"] },
]

/** A checklist entry: a column's field and its header. */
export interface ColumnChoice {
  field: string
  label: string
}

/** Each group's columns with their headers, in the group's order. */
export function groupChoices<TRow>(
  groups: readonly ColumnGroup<TRow>[],
  columns: readonly GridColumn<TRow>[]
): { title: string; choices: ColumnChoice[] }[] {
  const headers = new Map(columns.map((c) => [c.field as string, c.header]))
  return groups.map(({ title, fields }) => ({
    title,
    choices: fields.map((field) => ({
      field,
      label: headers.get(field) ?? field,
    })),
  }))
}

/** The heading's count: "N total", and how many match the filters while any applies. */
export function totalText(
  total: number | undefined,
  count: { total: number; filtered: boolean } | undefined
): string {
  const parts: string[] = []
  if (total !== undefined) parts.push(`${formatCount(total)} total`)
  if (count?.filtered)
    parts.push(`${formatCount(count.total)} match the filters`)
  return parts.length > 0 ? ` (${parts.join(", ")})` : ""
}
