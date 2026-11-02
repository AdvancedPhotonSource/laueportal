import type { ColDef, ValueFormatterParams } from "ag-grid-community"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { RunReference, RunStatus, ScanSummary } from "@/api"
import { LinkCell, ProgressCell, StatusCell } from "./cells"
import {
  filterKind,
  FILTERS,
  statusField,
  toColumnDefs,
  type GridColumn,
} from "./columns"

afterEach(() => {
  vi.unstubAllEnvs()
})

/** A run row, as the run lists and the run monitor have them. */
interface RunRow {
  id: number
  job_id: number
  scan_number: number | null
  reconstruction_id: number | null
  run: RunReference | null
  status: RunStatus
  n_inputs: number
  n_succeeded: number
  n_failed: number
  n_not_run: number
  n_processed: number
  duration_seconds: number | null
  created_at: string
  author: string | null
}

const SCAN_COLUMNS: GridColumn<ScanSummary>[] = [
  { field: "scan_number", header: "Scan ID", type: "link", link: "scan" },
  { field: "sample_name", header: "Sample Name", type: "text" },
  {
    field: "n_dimensions",
    header: "Scan Dim",
    type: "number",
    format: (n) => `${String(n)}D`,
  },
  { field: "time", header: "Date", type: "date", width: 180 },
  {
    field: "notes",
    header: "Notes",
    type: "text",
    flex: 2,
    filterable: false,
    sortable: false,
    hidden: true,
  },
]

const RUN_COLUMNS: GridColumn<RunRow>[] = [
  { field: "id", header: "ID", type: "link", link: "indexing" },
  { field: "job_id", header: "Job", type: "link", link: "job" },
  { field: "scan_number", header: "Source", type: "link", link: "source" },
  {
    field: "reconstruction_id",
    header: "Recon",
    type: "link",
    link: "reconstruction",
  },
  { field: "run", header: "Run", type: "link", link: "run", sortable: false },
  { field: "status", header: "Status", type: "status" },
  { field: "n_processed", header: "Run Progress", type: "progress" },
  { field: "duration_seconds", header: "Duration", type: "duration" },
  { field: "created_at", header: "Date", type: "date", initialSort: "desc" },
  { field: "author", header: "Author", type: "text" },
]

function format<TRow>(
  def: ColDef<TRow> | undefined,
  value: unknown,
  data: unknown
) {
  const formatter = def?.valueFormatter
  if (typeof formatter !== "function") throw new Error("no value formatter")
  return formatter({ value, data } as ValueFormatterParams<TRow>)
}

describe("toColumnDefs", () => {
  it("makes each field the column's ID, with its header", () => {
    const defs = toColumnDefs(SCAN_COLUMNS)
    expect(defs.map((def) => [def.colId, def.field, def.headerName])).toEqual([
      ["scan_number", "scan_number", "Scan ID"],
      ["sample_name", "sample_name", "Sample Name"],
      ["n_dimensions", "n_dimensions", "Scan Dim"],
      ["time", "time", "Date"],
      ["notes", "notes", "Notes"],
    ])
  })

  it("gives each type its filter, with a floating filter, and only the protocol's operators", () => {
    const [scan, sample, dims, time] = toColumnDefs(SCAN_COLUMNS)
    expect([scan?.filter, sample?.filter, dims?.filter, time?.filter]).toEqual([
      "agNumberColumnFilter",
      "agTextColumnFilter",
      "agNumberColumnFilter",
      "agDateColumnFilter",
    ])
    for (const def of [scan, sample, dims, time]) {
      expect(def?.floatingFilter).toBe(true)
      expect(def?.filterParams).toMatchObject({ maxNumConditions: 2 })
    }
    expect(time?.filterParams).toEqual(FILTERS.date.filterParams)
    expect(FILTERS.date.filterParams.filterOptions).toEqual([
      "equals",
      "lessThan",
      "greaterThan",
      "inRange",
      "blank",
      "notBlank",
    ])
    expect(FILTERS.text.filterParams.filterOptions).toEqual([
      "contains",
      "notContains",
      "equals",
      "notEqual",
      "startsWith",
      "endsWith",
      "blank",
      "notBlank",
    ])
    expect(FILTERS.number.filterParams.filterOptions).toEqual([
      "equals",
      "notEqual",
      "lessThan",
      "lessThanOrEqual",
      "greaterThan",
      "greaterThanOrEqual",
      "inRange",
      "blank",
      "notBlank",
    ])
  })

  it("leaves filters, sorting and visibility as the column asks", () => {
    const notes = toColumnDefs(SCAN_COLUMNS).at(-1)
    expect(notes?.filter).toBe(false)
    expect(notes?.floatingFilter).toBe(false)
    expect(notes?.filterParams).toBeUndefined()
    expect(notes?.sortable).toBe(false)
    expect(notes?.initialHide).toBe(true)
    const [scan] = toColumnDefs(SCAN_COLUMNS)
    expect(scan?.sortable).toBe(true)
    expect(scan?.unSortIcon).toBe(true)
    expect(scan?.resizable).toBe(true)
    expect(scan?.initialHide).toBeUndefined()
  })

  it("starts with a fixed width or a share of the free width, never narrower than the header", () => {
    const [scan, sample, , time, notes] = toColumnDefs(SCAN_COLUMNS)
    expect(time?.initialWidth).toBe(180)
    expect(time?.initialFlex).toBeUndefined()
    expect(sample?.initialFlex).toBe(1)
    expect(notes?.initialFlex).toBe(2)
    // "Sample Name" needs more room than a text column's minimum.
    expect(sample?.minWidth).toBeGreaterThan(scan?.minWidth ?? 0)
    expect(
      toColumnDefs([
        { ...SCAN_COLUMNS[0], minWidth: 60 } as GridColumn<ScanSummary>,
      ])[0]?.minWidth
    ).toBe(60)
    // The time column's fixed 180 px is wider than its default minimum; a fixed width narrower
    // than the header's default minimum is kept (the page chose it), not widened.
    expect(time?.minWidth).toBeLessThan(180)
    const narrow = toColumnDefs([
      { field: "sample_name", header: "Sample Name", type: "text", width: 70 },
    ])[0]
    expect([narrow?.initialWidth, narrow?.minWidth]).toEqual([70, 70])
  })

  it("sets the grid's initial sort", () => {
    const defs = toColumnDefs(RUN_COLUMNS)
    expect(defs.find((def) => def.colId === "created_at")?.initialSort).toBe(
      "desc"
    )
    expect(defs.find((def) => def.colId === "id")?.initialSort).toBeUndefined()
  })

  it("formats numbers with the column's format and dates in local time", () => {
    vi.stubEnv("TZ", "America/Chicago")
    const [, sample, dims, time] = toColumnDefs(SCAN_COLUMNS)
    expect(format(dims, 2, {})).toBe("2D")
    expect(format(dims, null, {})).toBe("")
    expect(sample?.valueFormatter).toBeUndefined()
    expect(format(time, "2026-01-15T06:30:00Z", {})).toBe("2026-01-15 00:30:00")
    expect(format(time, null, {})).toBe("")
  })

  it("renders links, statuses and progress with the shared renderers", () => {
    const defs = toColumnDefs(RUN_COLUMNS)
    const byId = (id: string) => defs.find((def) => def.colId === id)
    for (const [id, link] of [
      ["id", "indexing"],
      ["job_id", "job"],
      ["scan_number", "source"],
      ["reconstruction_id", "reconstruction"],
      ["run", "run"],
    ] as const) {
      expect(byId(id)?.cellRenderer).toBe(LinkCell)
      expect(byId(id)?.cellRendererParams).toEqual({ link })
    }
    expect(byId("status")?.cellRenderer).toBe(StatusCell)
    expect(byId("n_processed")?.cellRenderer).toBe(ProgressCell)
  })

  it('shows durations as HH:MM:SS, "(running)" while the run is active', () => {
    const duration = toColumnDefs(RUN_COLUMNS).find(
      (def) => def.colId === "duration_seconds"
    )
    expect(format(duration, 3725, { status: "running" })).toBe(
      "01:02:05 (running)"
    )
    expect(format(duration, 3725, { status: "finished" })).toBe("01:02:05")
    expect(format(duration, null, { status: "queued" })).toBe("")
  })
})

describe("filterKind", () => {
  it("filters IDs as numbers, a job's run as text on its label, a status as text", () => {
    const kinds = RUN_COLUMNS.map((column) => [
      column.field,
      filterKind(column),
    ])
    expect(Object.fromEntries(kinds)).toEqual({
      id: "number",
      job_id: "number",
      scan_number: "number",
      reconstruction_id: "number",
      run: "text",
      status: "text",
      n_processed: "number",
      duration_seconds: "number",
      created_at: "date",
      author: "text",
    })
  })
})

describe("statusField", () => {
  it("finds the status column a live grid watches", () => {
    expect(statusField(RUN_COLUMNS)).toBe("status")
    expect(statusField(SCAN_COLUMNS)).toBeUndefined()
  })
})

describe("GridColumn types", () => {
  it("checks each type against the row's field", () => {
    const columns: GridColumn<ScanSummary>[] = [
      // @ts-expect-error: a number filter cannot take a text field
      { field: "sample_name", header: "x", type: "number" },
      // @ts-expect-error: dates are strings
      { field: "scan_number", header: "x", type: "date" },
      // @ts-expect-error: a scan has no status (no run counters either)
      { field: "technique", header: "x", type: "status" },
      // @ts-expect-error: the field must be the row's
      { field: "no_such_field", header: "x", type: "text" },
      // @ts-expect-error: a link names what it links to
      { field: "scan_number", header: "x", type: "link" },
    ]
    expect(columns).toHaveLength(5)
  })
})
