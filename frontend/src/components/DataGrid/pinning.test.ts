import { describe, expect, it } from "vitest"

import {
  columnsOverflow,
  pinnedSide,
  pinningUpdate,
  type ColumnWidth,
} from "./pinning"

const fixed = (
  width: number,
  pinned: ColumnWidth["pinned"] = null
): ColumnWidth => ({ pinned, width, minWidth: width, flex: false })

const flex = (width: number, minWidth: number): ColumnWidth => ({
  pinned: null,
  width,
  minWidth,
  flex: true,
})

describe("columnsOverflow", () => {
  it("says the columns fit when their widths (a flex column's minimum) fit the grid", () => {
    // 40 (checkboxes, pinned) + 100 + a flex column stretched to 660 (at least 80).
    expect(
      columnsOverflow([fixed(40, "left"), fixed(100), flex(660, 80)], 760)
    ).toBe(false)
    // Just room for every column at its narrowest.
    expect(
      columnsOverflow([fixed(40, "left"), fixed(100), flex(80, 80)], 180)
    ).toBe(false)
  })

  it("says they overflow when even the flex columns at their minimum do not fit", () => {
    expect(
      columnsOverflow([fixed(40, "left"), fixed(100), flex(80, 80)], 179)
    ).toBe(true)
  })

  it("counts the pinned columns' room as the grid's, so pinning does not change the answer", () => {
    const unpinned = [fixed(40, "left"), fixed(60), fixed(500), fixed(180)]
    const pinned = [
      fixed(40, "left"),
      fixed(60, "left"),
      fixed(500),
      fixed(180, "right"),
    ]
    // 780 px of columns in a 700 px grid: the center shows 700 - 40 unpinned, 700 - 280 pinned.
    expect(columnsOverflow(unpinned, 660)).toBe(true)
    expect(columnsOverflow(pinned, 420)).toBe(true)
    // In an 800 px grid they fit either way.
    expect(columnsOverflow(unpinned, 760)).toBe(false)
    expect(columnsOverflow(pinned, 520)).toBe(false)
  })
})

describe("pinningUpdate", () => {
  const wanted = [
    { colId: "id", side: "left" as const },
    { colId: "status", side: "right" as const },
  ]

  it("pins the columns to their sides while the columns overflow", () => {
    expect(pinningUpdate(wanted, () => null, true)).toEqual([
      { colId: "id", pinned: "left" },
      { colId: "status", pinned: "right" },
    ])
  })

  it("unpins them when every column fits", () => {
    const current = (colId: string) => (colId === "id" ? "left" : "right")
    expect(pinningUpdate(wanted, current, false)).toEqual([
      { colId: "id", pinned: null },
      { colId: "status", pinned: null },
    ])
  })

  it("changes nothing when they are already so", () => {
    expect(pinningUpdate(wanted, () => null, false)).toBeUndefined()
    const current = (colId: string) => (colId === "id" ? "left" : "right")
    expect(pinningUpdate(wanted, current, true)).toBeUndefined()
  })
})

describe("pinnedSide", () => {
  it("reads AG Grid's pinning (true is the left)", () => {
    expect(pinnedSide("left")).toBe("left")
    expect(pinnedSide(true)).toBe("left")
    expect(pinnedSide("right")).toBe("right")
    expect(pinnedSide(false)).toBeNull()
    expect(pinnedSide(null)).toBeNull()
    expect(pinnedSide(undefined)).toBeNull()
  })
})
