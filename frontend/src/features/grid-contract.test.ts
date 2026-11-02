import { readdirSync, readFileSync } from "node:fs"

import { describe, expect, it } from "vitest"

import {
  FILTERS,
  filterKind,
  toColumnDefs,
  type GridColumn,
} from "@/components/DataGrid/columns"
import * as indexing from "./indexing/columns"
import * as jobs from "./jobs/columns"
import * as reconstruction from "./reconstruction/columns"
import * as scans from "./scans/columns"

// The grids' contract with the API: every column a grid lets the user sort sorts by a field its list sorts, and
// every column it lets the user filter filters a field its list filters, with the same filter type. The lists
// publish their fields in openapi.json (`x-grid-fields`, kept current by the contract-drift check in `make check`);
// the backend's API sweep checks the server takes each published sort and filter, and test_paging.py what it
// answers. Files are read at run time (not imported: the frontend's TypeScript does not import JSON, and Vitest
// caches a module's glob imports). The base is a variable because Vite rewrites `new URL("…", import.meta.url)`.
const here = import.meta.url
const openapi = JSON.parse(
  readFileSync(new URL("../../openapi.json", here), "utf8")
) as {
  paths: Record<string, Record<string, OpenApiOperation | undefined>>
}

interface GridField {
  filterType: string
  sortable: boolean
  filterable: boolean
}

interface OpenApiOperation {
  operationId?: string
  "x-grid-fields"?: Record<string, GridField>
}

/** The fields a list operation (by its operationId, the generated function's name) sorts and filters. */
function gridFields(operationId: string): Record<string, GridField> {
  for (const item of Object.values(openapi.paths)) {
    const operation = item.get
    if (operation?.operationId === operationId) {
      const fields = operation["x-grid-fields"]
      if (!fields) throw new Error(`${operationId} publishes no x-grid-fields`)
      return fields
    }
  }
  throw new Error(`no GET operation ${operationId} in openapi.json`)
}

/** What a grid's columns offer the user, from the AG Grid definitions the DataGrid builds. */
function offered<TRow>(columns: readonly GridColumn<TRow>[]) {
  const defs = toColumnDefs(columns)
  return columns.map((column, i) => {
    const def = defs[i]
    if (!def) throw new Error(`no column definition for ${column.field}`)
    const kind = filterKind(column)
    // The DataGrid's filter for the column's kind (a check that the kind is what the grid really sends).
    expect(def.filter === false || def.filter === FILTERS[kind].filter).toBe(
      true
    )
    return {
      field: column.field,
      sorts: def.sortable === true,
      filters: def.filter !== false ? kind : undefined,
    }
  })
}

/** The problems of one grid against its list: each sort or filter the API would refuse. */
function refused<TRow>(
  columns: readonly GridColumn<TRow>[],
  operationId: string
): string[] {
  const fields = gridFields(operationId)
  const problems: string[] = []
  for (const { field, sorts, filters } of offered(columns)) {
    const api = Object.hasOwn(fields, field) ? fields[field] : undefined
    if (sorts && !api?.sortable)
      problems.push(`${field}: sorts, the API does not`)
    if (filters && !api?.filterable) {
      problems.push(`${field}: filters, the API does not`)
    } else if (filters && api?.filterType !== filters) {
      problems.push(
        `${field}: a ${filters} filter, the API's is ${String(api?.filterType)}`
      )
    }
  }
  return problems
}

/** A grid of the app and the list its `fetchRows` calls. */
function grid<TRow>(
  title: string,
  columns: readonly GridColumn<TRow>[],
  operationId: string
) {
  return { title, columns, refused: () => refused(columns, operationId) }
}

const GRIDS = [
  grid("the scans", scans.SCAN_COLUMNS, "listScans"),
  grid(
    "the reconstructions",
    reconstruction.RECONSTRUCTION_COLUMNS,
    "listReconstructionRuns"
  ),
  grid("the indexations", indexing.INDEXING_COLUMNS, "listIndexingRuns"),
  grid("a run's Peaks tab", indexing.PEAK_COLUMNS, "listIndexingPeaks"),
  grid(
    "a run's Patterns tab",
    indexing.PATTERN_COLUMNS,
    "listIndexingPatterns"
  ),
  grid("the run monitor", jobs.JOB_COLUMNS, "listJobs"),
  grid(
    "a scan's Reconstructions card",
    scans.SCAN_RECONSTRUCTION_COLUMNS,
    "listReconstructionRuns"
  ),
  grid(
    "a scan's Peak Indexing card",
    scans.SCAN_INDEXING_COLUMNS,
    "listIndexingRuns"
  ),
]

// The client-side grids: the page holds their rows, and AG Grid sorts and filters them in the browser
// (DataGrid's `rows`), so no list of the API is involved.
const CLIENT_SIDE_GRIDS: readonly unknown[] = [
  scans.SCAN_LOG_COLUMNS,
  reconstruction.POINT_COLUMNS,
]

/** The features' column modules, by their path under src. */
const COLUMN_MODULES: Record<string, Record<string, unknown>> = {
  "features/indexing/columns.ts": indexing,
  "features/jobs/columns.ts": jobs,
  "features/reconstruction/columns.ts": reconstruction,
  "features/scans/columns.ts": scans,
}

describe("the grids' sorts and filters are the API's", () => {
  it.each(GRIDS)("$title", ({ refused }) => {
    expect(refused()).toEqual([])
  })

  it("covers every grid: column lists live only in the features' columns.ts, each checked here", () => {
    const src = new URL("../", here)
    const files = readdirSync(src, { recursive: true }).filter(
      (path) =>
        /\.tsx?$/.test(path) &&
        !/\.test\.tsx?$/.test(path) &&
        !path.startsWith("api/generated/")
    )
    const columnFiles = files.filter((path) =>
      /^features\/[^/]+\/columns\.ts$/.test(path)
    )
    expect(columnFiles.sort()).toEqual(Object.keys(COLUMN_MODULES).sort())
    const declaring = files.filter(
      (path) =>
        !columnFiles.includes(path) &&
        /:\s*GridColumn<\w+>\[\]\s*=/.test(
          readFileSync(new URL(path, src), "utf8")
        )
    )
    expect(declaring).toEqual([])
    const checked: unknown[] = [
      ...GRIDS.map(({ columns }) => columns),
      ...CLIENT_SIDE_GRIDS,
    ]
    const unchecked = Object.entries(COLUMN_MODULES).flatMap(([path, module]) =>
      Object.entries(module)
        .filter(([, list]) => !checked.includes(list))
        .map(([name]) => `${path}: ${name}`)
    )
    expect(unchecked).toEqual([])
  })

  it("fails for a sort, a filter or a filter type the API refuses", () => {
    const columns: GridColumn<{ scan_number: string; motor: string }>[] = [
      { field: "scan_number", header: "Scan", type: "date" },
      { field: "motor", header: "Motor", type: "text" },
    ]
    expect(refused(columns, "listScans")).toEqual([
      "scan_number: a date filter, the API's is number",
      "motor: sorts, the API does not",
      "motor: filters, the API does not",
    ])
  })
})
