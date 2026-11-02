// The DataGrid's typed column definitions and their AG Grid column definitions: the filter each
// type takes (only the operators the grid query protocol supports), a floating filter on every
// filterable column, and the shared renderers.

import type { ColDef, ValueFormatterParams } from "ag-grid-community"
import type { ReactNode } from "react"

import type { RunReference, RunStatus } from "@/api"
import { formatDateTime, formatDuration } from "@/lib/format"
import { isActiveStatus } from "@/lib/run-status"
import { CustomCell, LinkCell, ProgressCell, StatusCell } from "./cells"
import { tabThroughLinks } from "./keyboard"
import type { PinnedColumn } from "./pinning"

export type ColumnType =
  | "text"
  | "number"
  | "date"
  | "link"
  | "status"
  | "progress"
  | "duration"
  | "custom"

/** The filter a column takes: the grid query protocol's three filter types. */
export type FilterKind = "text" | "number" | "date"

/**
 * What a link column links to: a scan (`ScanLink`, its number), a run's sources (`SN5 R3` or
 * "Unlinked", from the row's `scan_number` and `reconstruction_id`), an indexing run (`I12`), a
 * reconstruction run (`R3`, or "Direct"), a job, or a job's run (`I12`/`R3` from a RunReference).
 */
export type LinkKind =
  "scan" | "source" | "indexing" | "reconstruction" | "job" | "run"

type Field<TRow> = Extract<keyof TRow, string>
type Nullable<T> = T | null | undefined

/** A row of a run list (or the run monitor): its status and counters. */
interface RunRow {
  status: RunStatus
  n_inputs: number
  n_succeeded: number
  n_failed: number
  n_not_run: number
}

interface ColumnOptions<K extends string> {
  /** The row's field: the column's ID, and the API's sort and filter key. */
  field: K
  header: string
  /** A filter, with a floating filter under the header (default true). */
  filterable?: boolean
  /** Default true. */
  sortable?: boolean
  /** A fixed starting width in px. Without one, the column shares the free width (`flex`). */
  width?: number
  /** The column's share of the free width when it has no `width` (default 1). */
  flex?: number
  /** The narrowest the column gets (a default per type). */
  minWidth?: number
  /** Hidden at first (until the user's kept column state says otherwise). */
  hidden?: boolean
  /** The grid's sort when it opens. */
  initialSort?: "asc" | "desc"
  /**
   * Kept in view while the columns are wider than the grid and it scrolls sideways: pinned to this
   * side (an ID on the left, beside the checkboxes; a status on the right). Only then: where every
   * column fits, nothing is pinned (pinning.ts). Give such a column a fixed `width`.
   */
  pinned?: "left" | "right"
}

/** Each type, on the fields whose values it takes (checked against the row type). */
type TypeOptions<TRow, K extends Field<TRow>> =
  | (TRow[K] extends Nullable<string>
      ? {
          type: "text"
          /** The text shown for a value (not for a blank one). */
          format?: (value: NonNullable<TRow[K]>, row: TRow) => string
        }
      : never)
  | (TRow[K] extends Nullable<number>
      ? {
          type: "number"
          /** The text shown for a value, e.g. `(n) => \`${n}D\``. */
          format?: (value: NonNullable<TRow[K]>, row: TRow) => string
        }
      : never)
  | (TRow[K] extends Nullable<string> ? { type: "date" } : never)
  | (TRow[K] extends Nullable<string>
      ? {
          type: "custom"
          /** The cell (e.g. a badge); the column sorts and filters the field's text. */
          cell: (row: TRow) => ReactNode
        }
      : never)
  | (TRow[K] extends Nullable<number>
      ? { type: "link"; link: "scan" | "indexing" | "reconstruction" | "job" }
      : never)
  | (TRow extends { scan_number: Nullable<number> }
      ? TRow[K] extends Nullable<number>
        ? { type: "link"; link: "source" }
        : never
      : never)
  | (TRow[K] extends Nullable<RunReference>
      ? { type: "link"; link: "run" }
      : never)
  | (TRow extends RunRow
      ? TRow[K] extends RunStatus
        ? {
            type: "status"
            /**
             * The page's own cell for the row (the run monitor's: a crux run's remote phase); without it, the
             * run's status badge (`StatusCell`). The column still sorts and filters the status, and a live grid
             * polls while a visible row's status is active.
             */
            cell?: (row: TRow) => ReactNode
          }
        : TRow[K] extends Nullable<number>
          ? { type: "progress" } | { type: "duration" }
          : never
      : never)

/**
 * A DataGrid column. `type` picks the renderer and the filter: text, number and date show the value
 * (dates in local time); link, status, progress and duration render the shared cells; custom draws
 * the page's `cell`.
 */
export type GridColumn<TRow> = {
  [K in Field<TRow>]: ColumnOptions<K> & TypeOptions<TRow, K>
}[Field<TRow>]

// The operators the grid query protocol supports, by filter type; AG Grid's date presets
// ("today", "last 7 days") and "not equal" for dates are not among them.
const TEXT_OPTIONS = [
  "contains",
  "notContains",
  "equals",
  "notEqual",
  "startsWith",
  "endsWith",
  "blank",
  "notBlank",
]
const NUMBER_OPTIONS = [
  "equals",
  "notEqual",
  "lessThan",
  "lessThanOrEqual",
  "greaterThan",
  "greaterThanOrEqual",
  "inRange",
  "blank",
  "notBlank",
]
const DATE_OPTIONS = [
  "equals",
  "lessThan",
  "greaterThan",
  "inRange",
  "blank",
  "notBlank",
]

/** AG Grid's filter and its parameters for each filter type (two conditions at most, AND/OR). */
export const FILTERS: Record<
  FilterKind,
  { filter: string; filterParams: Record<string, unknown> }
> = {
  text: {
    filter: "agTextColumnFilter",
    filterParams: {
      filterOptions: TEXT_OPTIONS,
      maxNumConditions: 2,
      trimInput: true,
    },
  },
  number: {
    filter: "agNumberColumnFilter",
    // A range includes both ends, as on the server (only the client-side model reads this).
    filterParams: {
      filterOptions: NUMBER_OPTIONS,
      maxNumConditions: 2,
      inRangeInclusive: true,
    },
  },
  date: {
    filter: "agDateColumnFilter",
    filterParams: {
      filterOptions: DATE_OPTIONS,
      maxNumConditions: 2,
      inRangeInclusive: true,
      comparator: compareLocalDay,
    },
  },
}

/**
 * The client-side model's date filter: a cell's ISO time against the filter's local day (AG Grid's
 * comparator: negative when the cell is before that day, positive after, 0 on it). The server's
 * filter reads the same local day (datasource.ts, `localDayStart`).
 */
export function compareLocalDay(filterDay: Date, cellValue: unknown): number {
  if (typeof cellValue !== "string") return -1
  const time = new Date(cellValue)
  if (Number.isNaN(time.getTime())) return -1
  const day = new Date(time.getFullYear(), time.getMonth(), time.getDate())
  return Math.sign(day.getTime() - filterDay.getTime())
}

const MIN_WIDTHS: Record<ColumnType, number> = {
  text: 80,
  number: 70,
  // A date and its floating filter's date ("mm/dd/yyyy" when empty) show whole (DataGrid.css).
  date: 150,
  link: 80,
  status: 120,
  progress: 160,
  duration: 130,
  custom: 80,
}

/** The narrowest a column gets by default: its type's minimum, or room for its header and icons. */
function defaultMinWidth(type: ColumnType, header: string): number {
  // About 8 px a character, the sort icon and the padding (measured in Chromium with the system
  // font: "Sample Name" needs 129 px), with some room for wider system fonts.
  return Math.max(MIN_WIDTHS[type], Math.ceil(header.length * 8.5) + 36)
}

/** The filter a column takes: IDs are numbers, a job's run filters as text on its label. */
export function filterKind<TRow>(column: GridColumn<TRow>): FilterKind {
  switch (column.type) {
    case "text":
    case "status":
    case "custom":
      return "text"
    case "date":
      return "date"
    case "link":
      return column.link === "run" ? "text" : "number"
    case "number":
    case "progress":
    case "duration":
      return "number"
  }
}

type Formatter<TRow> = (params: ValueFormatterParams<TRow>) => string

function formatWith<TRow>(
  format: ((value: never, row: TRow) => string) | undefined
): Formatter<TRow> | undefined {
  if (!format) return undefined
  return ({ value, data }) =>
    value == null || data === undefined ? "" : format(value as never, data)
}

function renderer<TRow>(column: GridColumn<TRow>): ColDef<TRow> {
  switch (column.type) {
    case "text":
    case "number":
      return { valueFormatter: formatWith<TRow>(column.format) }
    case "date":
      return {
        valueFormatter: ({ value }: ValueFormatterParams<TRow>) =>
          formatDateTime(typeof value === "string" ? value : null),
      }
    case "duration":
      return {
        valueFormatter: ({ value, data }: ValueFormatterParams<TRow>) =>
          formatDuration(
            typeof value === "number" ? value : null,
            isActiveStatus((data as { status?: unknown } | undefined)?.status)
          ),
      }
    case "link":
      return {
        cellRenderer: LinkCell,
        cellRendererParams: { link: column.link },
        // Tab goes through the cell's links before AG Grid moves to the next cell.
        suppressKeyboardEvent: ({ event }) => tabThroughLinks(event),
      }
    case "status":
      return column.cell
        ? {
            cellRenderer: CustomCell,
            cellRendererParams: { cell: column.cell },
          }
        : { cellRenderer: StatusCell }
    case "progress":
      return { cellRenderer: ProgressCell }
    case "custom":
      return {
        cellRenderer: CustomCell,
        cellRendererParams: { cell: column.cell },
      }
  }
}

/** The AG Grid column definitions of a DataGrid's columns. */
export function toColumnDefs<TRow>(
  columns: readonly GridColumn<TRow>[]
): ColDef<TRow>[] {
  return columns.map((column) => {
    const filterable = column.filterable ?? true
    const filter = FILTERS[filterKind(column)]
    const def: ColDef<TRow> = {
      colId: column.field,
      // A top-level field of the row (AG Grid's type also allows dotted paths, which TS cannot relate).
      field: column.field as unknown as ColDef<TRow>["field"],
      headerName: column.header,
      // No cell data type inference (AG Grid infers only on the client-side model, `rows`): a date column
      // keeps its date filter on the local day, as on the server, not a date-and-time one.
      cellDataType: false,
      sortable: column.sortable ?? true,
      unSortIcon: true,
      resizable: true,
      // Columns are pinned only by the grid (GridColumn.pinned), not by dragging them to an edge.
      lockPinned: true,
      filter: filterable ? filter.filter : false,
      floatingFilter: filterable,
      // A fixed width is the page's choice: the default minimum does not widen it.
      minWidth:
        column.minWidth ??
        Math.min(
          column.width ?? Number.POSITIVE_INFINITY,
          defaultMinWidth(column.type, column.header)
        ),
      ...renderer(column),
    }
    if (filterable) def.filterParams = filter.filterParams
    if (column.width !== undefined) def.initialWidth = column.width
    else def.initialFlex = column.flex ?? 1
    if (column.hidden) def.initialHide = true
    if (column.initialSort) def.initialSort = column.initialSort
    return def
  })
}

/** The columns kept in view while the grid scrolls sideways, and their sides. */
export function pinnedColumns<TRow>(
  columns: readonly GridColumn<TRow>[]
): PinnedColumn[] {
  return columns.flatMap((column) =>
    column.pinned ? [{ colId: column.field, side: column.pinned }] : []
  )
}

/** The field of the first status column: live grids poll while a visible row's status is active. */
export function statusField<TRow>(
  columns: readonly GridColumn<TRow>[]
): string | undefined {
  return columns.find((column) => column.type === "status")?.field
}
