import { describe, expect, it } from "vitest"

import type { PeakRow } from "@/api"
import type { GridColumn } from "@/components/DataGrid"
import { toColumnDefs } from "@/components/DataGrid/columns"
import { PATTERN_COLUMNS, PEAK_COLUMNS } from "./columns"
import {
  groupChoices,
  PATTERN_COLUMN_GROUPS,
  PEAK_COLUMN_GROUPS,
  totalText,
  type ColumnGroup,
} from "./results-tables"

// The Peaks and Patterns tabs' columns and checklists (grid-contract.test.ts checks their sorts
// and filters against the API).

/** A tab's checklist (each group's headers), the grouped fields, and the columns shown at first. */
function table<TRow>(
  columns: readonly GridColumn<TRow>[],
  groups: readonly ColumnGroup<TRow>[]
) {
  return {
    checklist: Object.fromEntries(
      groupChoices(groups, columns).map(({ title, choices }) => [
        title,
        choices.map((c) => c.label),
      ])
    ),
    grouped: groups.flatMap(({ fields }): string[] => [...fields]).sort(),
    fields: columns.map((c) => c.field as string).sort(),
    shownAtFirst: columns.filter((c) => !c.hidden).map((c) => c.field),
    defaults: groups[0]?.fields,
  }
}

describe("the results tables' columns", () => {
  it.each([
    {
      tab: "Peaks",
      table: table(PEAK_COLUMNS, PEAK_COLUMN_GROUPS),
      expected: {
        Default: [
          "Step",
          "Pattern",
          "h",
          "k",
          "l",
          "Peak #",
          "X pixel",
          "Y pixel",
          "Intensity",
          "Integral",
          "Qx",
          "Qy",
          "Qz",
          "RMS Error",
          "Goodness",
        ],
        Geometry: [
          "Frame",
          "N Peaks",
          "Energy",
          "Error",
          "Pred. Intensity",
          "Input Image",
        ],
        "Fit Quality": ["hwhmX", "hwhmY", "Tilt", "ChiSq", "Background"],
        Indexing: ["Pattern N Indexed", "Pattern Indexed %"],
      },
    },
    {
      tab: "Patterns",
      table: table(PATTERN_COLUMNS, PATTERN_COLUMN_GROUPS),
      expected: {
        Default: [
          "Step",
          "Frame",
          "Scan",
          "Pattern",
          "N Indexed",
          "N Peaks",
          "Indexed %",
          "RMS Error",
          "Goodness",
          "N Patterns",
          "a*",
          "b*",
          "c*",
          "Structure",
          "Space Group",
        ],
        Position: ["X motor", "Y motor", "Z motor", "Depth"],
        Run: ["Energy", "Input Image"],
      },
    },
  ])(
    "$tab: the groups name every column once; only Default shows at first",
    ({ table, expected }) => {
      expect(table.checklist).toEqual(expected)
      expect(table.grouped).toEqual(table.fields)
      expect(table.shownAtFirst).toEqual(table.defaults)
    }
  )

  it("shows measured values to five significant digits and fractions as percentages", () => {
    const defs = toColumnDefs(PEAK_COLUMNS)
    const shown = (field: keyof PeakRow, value: number) => {
      const def = defs.find((d) => d.colId === field)
      const format = def?.valueFormatter
      if (typeof format !== "function") throw new Error(`${field}: no format`)
      return format({ value, data: {} } as never)
    }
    expect(shown("intensity", 12000)).toBe("12000")
    expect(shown("qx", -0.123456789)).toBe("-0.12346")
    expect(shown("pattern_indexed_fraction", 2 / 3)).toBe("66.667%")
  })
})

describe("totalText", () => {
  it("counts the scope's rows, and those matching the filters while one applies", () => {
    expect(totalText(undefined, undefined)).toBe("")
    expect(totalText(120, { total: 120, filtered: false })).toBe(" (120 total)")
    expect(totalText(550773, { total: 7, filtered: true })).toBe(
      " (550,773 total, 7 match the filters)"
    )
  })
})
