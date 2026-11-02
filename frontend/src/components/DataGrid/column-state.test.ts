import type { GridState } from "ag-grid-community"
import { describe, expect, it } from "vitest"

import {
  columnStateKey,
  initialGridState,
  parseColumnState,
  pickColumnState,
  readColumnState,
  sameColumnState,
  writeColumnState,
  type StoredColumnState,
} from "./column-state"

function memoryStorage(initial: Record<string, string> = {}) {
  const data = new Map(Object.entries(initial))
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value)
    },
    removeItem: (key: string) => {
      data.delete(key)
    },
    data,
  }
}

const brokenStorage = {
  getItem: () => {
    throw new Error("SecurityError")
  },
  setItem: () => {
    throw new Error("QuotaExceededError")
  },
  removeItem: () => {
    throw new Error("SecurityError")
  },
}

const STATE: StoredColumnState = {
  columnOrder: { orderedColIds: ["time", "scan_number", "notes"] },
  columnSizing: {
    columnSizingModel: [
      { colId: "time", width: 220 },
      { colId: "notes", flex: 2 },
    ],
  },
  columnVisibility: { hiddenColIds: ["notes"] },
}

describe("column state per grid", () => {
  it("is kept under laueportal:grid:<gridId>:v1", () => {
    expect(columnStateKey("scans")).toBe("laueportal:grid:scans:v1")
  })

  it("round-trips the order, widths and visibility", () => {
    const storage = memoryStorage()
    writeColumnState(storage, "scans", STATE)
    expect(
      JSON.parse(storage.data.get("laueportal:grid:scans:v1") ?? "")
    ).toEqual(STATE)
    expect(readColumnState(storage, "scans")).toEqual(STATE)
  })

  it("keeps each grid's state apart", () => {
    const storage = memoryStorage()
    writeColumnState(storage, "scans", STATE)
    expect(readColumnState(storage, "runs")).toBeUndefined()
  })

  it("keeps only the column parts of AG Grid's state (not sort, filter, scroll or pinning)", () => {
    const state: GridState = {
      ...STATE,
      sort: { sortModel: [{ colId: "time", sort: "asc" }] },
      filter: { filterModel: { notes: { type: "contains", filter: "x" } } },
      scroll: { top: 300, left: 0 },
      columnPinning: { leftColIds: ["scan_number"], rightColIds: ["notes"] },
    }
    expect(pickColumnState(state)).toEqual(STATE)
  })

  it("compares two states by their order, widths and visibility, not by how AG Grid writes them", () => {
    // AG Grid's sizes list flex before width and may hold undefined values.
    const fromGrid = {
      columnVisibility: STATE.columnVisibility,
      columnSizing: {
        columnSizingModel: [
          { colId: "time", flex: undefined, width: 220 },
          { colId: "notes", flex: 2, width: undefined },
        ],
      },
      columnOrder: STATE.columnOrder,
    }
    expect(sameColumnState(fromGrid, STATE)).toBe(true)
    expect(
      sameColumnState(
        { ...STATE, columnVisibility: { hiddenColIds: [] } },
        STATE
      )
    ).toBe(false)
    expect(sameColumnState(STATE, undefined)).toBe(false)
    expect(sameColumnState(undefined, undefined)).toBe(true)
  })

  it("gives the kept state back with the checkbox column pinned (AG Grid would unpin it)", () => {
    expect(initialGridState(STATE, ["ag-Grid-SelectionColumn"])).toEqual({
      ...STATE,
      columnPinning: {
        leftColIds: ["ag-Grid-SelectionColumn"],
        rightColIds: [],
      },
    })
    // Without a kept state, the column definitions apply.
    expect(
      initialGridState(undefined, ["ag-Grid-SelectionColumn"])
    ).toBeUndefined()
  })

  it("discards and removes a value of another shape (no migrations)", () => {
    for (const value of [
      "not json",
      "[]",
      '{"columnOrder": {"orderedColIds": [1, 2]}}',
      '{"columnSizing": {"columnSizingModel": [{"colId": "a", "width": "wide"}]}}',
      '{"columnVisibility": {"hidden": ["a"]}}',
    ]) {
      const storage = memoryStorage({ "laueportal:grid:scans:v1": value })
      expect(readColumnState(storage, "scans"), value).toBeUndefined()
      expect(storage.data.has("laueportal:grid:scans:v1")).toBe(false)
    }
  })

  it("ignores members it does not know", () => {
    expect(
      parseColumnState({
        columnOrder: { orderedColIds: ["a"], extra: true },
        columnSizing: { columnSizingModel: [{ colId: "a", width: 5, x: 1 }] },
        sort: { sortModel: [] },
      })
    ).toEqual({
      columnOrder: { orderedColIds: ["a"] },
      columnSizing: { columnSizingModel: [{ colId: "a", width: 5 }] },
    })
  })

  it("works without storage, or with storage that fails", () => {
    expect(readColumnState(undefined, "scans")).toBeUndefined()
    expect(readColumnState(brokenStorage, "scans")).toBeUndefined()
    expect(() => {
      writeColumnState(brokenStorage, "scans", STATE)
    }).not.toThrow()
  })
})
