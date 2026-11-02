import { readFileSync } from "node:fs"

import { describe, expect, it } from "vitest"

import type { ScanImportItem } from "@/api"
import {
  applyImportResults,
  catalogDefaultsOf,
  CATALOG_DEFAULT_VALUES,
  IMPORT_STATUSES,
  isImportable,
  localInstant,
  quickSelect,
  scanLogSizeProblem,
  type ImportRow,
  type ImportStatus,
} from "./scan-import"

// Quick select's cases, shared with pytest (tests/scans/test_scan_import_api.py, scans.service.quick_select):
// read at run time, as grid-contract.test.ts reads openapi.json (the base is a variable because Vite rewrites
// `new URL("…", import.meta.url)`).
const here = import.meta.url
const shared = JSON.parse(
  readFileSync(
    new URL("../../../../backend/tests/scan_logs/quick_select.json", here),
    "utf8"
  )
) as {
  rows: { scan_number: number; status: ImportStatus; time: string }[]
  cases: {
    criteria: { scan_id_greater_than?: number; created_at_or_after?: string }
    expected: number[]
  }[]
}

function row(
  scan_index: number,
  scan_number: number | null,
  status: ImportStatus
): ImportRow {
  return {
    scan_index,
    scan_number,
    status,
    time: "2022-04-20T04:34:02Z",
    user_name: "user",
    energy: 20,
    energy_unit: "keV",
    sample_xyz: "1 2 3",
    n_dimensions: 2,
    error: null,
  }
}

describe("quick select", () => {
  it.each(shared.cases)(
    "picks the New rows of the shared cases: $criteria",
    ({ criteria, expected }) => {
      const picked = quickSelect(shared.rows, {
        scanIdGreaterThan: criteria.scan_id_greater_than,
        createdAtOrAfter:
          criteria.created_at_or_after === undefined
            ? undefined
            : new Date(criteria.created_at_or_after),
      })
      expect(picked.map((picked) => picked.scan_number)).toEqual(expected)
    }
  )

  it("reads the entered date and time in the browser's zone", () => {
    expect(localInstant("2022-04-20T11:00")?.getTime()).toBe(
      new Date(2022, 3, 20, 11, 0).getTime()
    )
    expect(localInstant("2022-04-20T11:00:30")?.getTime()).toBe(
      new Date(2022, 3, 20, 11, 0, 30).getTime()
    )
    expect(localInstant("")).toBeUndefined()
    expect(localInstant("2022-04-20")).toBeUndefined()
  })
})

describe("the import page's rules", () => {
  it("lets only New rows be selected", () => {
    expect(
      IMPORT_STATUSES.filter((status) => isImportable({ status }))
    ).toEqual(["new"])
  })

  it("refuses an upload over the published limit, its size shown apart from the limit", () => {
    const limit = 7 * 1024 * 1024
    expect(scanLogSizeProblem(limit, limit)).toBeUndefined()
    expect(scanLogSizeProblem(limit + 1, limit)).toBe(
      "7.1 MiB, over the 7 MiB limit"
    )
    expect(scanLogSizeProblem(40 * 1024 * 1024, limit)).toBe(
      "40.0 MiB, over the 7 MiB limit"
    )
  })

  it("updates the statuses of the rows sent from the import's results", () => {
    const rows = [
      row(0, 10, "new"),
      row(1, 11, "new"),
      row(2, 12, "new"),
      row(3, 10, "duplicate"),
      row(4, 13, "new"),
      row(5, null, "invalid"),
    ]
    const results: ScanImportItem[] = [
      { scan_number: 10, outcome: "imported", error: null },
      { scan_number: 11, outcome: "exists", error: null },
      { scan_number: 12, outcome: "failed", error: "IntegrityError: x" },
    ]
    const after = applyImportResults(rows, new Set(["0", "1", "2"]), results)
    expect(after.map(({ status, error }) => [status, error])).toEqual([
      ["imported", null],
      ["exists", null],
      ["failed", "IntegrityError: x"],
      // Not sent: the duplicate of scan 10 and the New row left out keep their statuses.
      ["duplicate", null],
      ["new", null],
      ["invalid", null],
    ])
  })

  it("sends the catalog defaults with the prefixes split at commas", () => {
    expect(catalogDefaultsOf(CATALOG_DEFAULT_VALUES)).toEqual({
      aperture: "wire",
      sample_name: "",
      filefolder: "",
      filename_prefix: [],
      notes: "",
    })
    expect(
      catalogDefaultsOf({
        ...CATALOG_DEFAULT_VALUES,
        aperture: "mask",
        sample_name: " Si ",
        filename_prefix: "Si_%d.h5, ,Ni_%d.h5 ",
      })
    ).toMatchObject({
      aperture: "mask",
      sample_name: "Si",
      filename_prefix: ["Si_%d.h5", "Ni_%d.h5"],
    })
  })
})
