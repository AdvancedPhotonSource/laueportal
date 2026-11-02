// @vitest-environment jsdom
import { describe, expect, it } from "vitest"

import {
  addRoi,
  createRoiStore,
  MAX_ROIS,
  ROI_COLORS,
  roiBounds,
  roiCenter,
  roiStorageKey,
  type Roi,
} from "./rois"

// A 64-row, 48-column image, as the test points' frames.
const SHAPE = [64, 48] as const

/** Adds ROIs at these pixels (size 9), each click accepted. */
function added(...at: [number, number][]): Roi[] {
  let rois: Roi[] = []
  for (const [x, y] of at) {
    const next = addRoi(rois, { x, y }, 9, SHAPE)
    if (!("rois" in next)) throw new Error(`refused at ${String([x, y])}`)
    rois = next.rois
  }
  return rois
}

function memoryStorage() {
  const items = new Map<string, string>()
  return {
    getItem: (key: string) => items.get(key) ?? null,
    setItem: (key: string, value: string) => {
      items.set(key, value)
    },
  }
}

describe("an ROI's placement", () => {
  it("is lauelab's square_bounds: the legal centre nearest the click, never clipped or shifted, refused outside the image", () => {
    // An odd size centres on the clicked pixel; an even one on the corner below it (the lower half-integer).
    expect(roiBounds({ x: 10, y: 20, size: 9 }, SHAPE)).toEqual([16, 25, 6, 15])
    expect(roiBounds({ x: 10, y: 20, size: 4 }, SHAPE)).toEqual([18, 22, 8, 12])
    expect(roiCenter({ x: 10, y: 20, size: 4 })).toEqual({ x: 9.5, y: 19.5 })
    // The edges: the square may touch them, not cross them.
    expect(roiBounds({ x: 4, y: 4, size: 9 }, SHAPE)).toEqual([0, 9, 0, 9])
    expect(roiBounds({ x: 43, y: 59, size: 9 }, SHAPE)).toEqual([
      55, 64, 39, 48,
    ])
    for (const [x, y] of [
      [3, 30],
      [44, 30],
      [20, 3],
      [20, 60],
    ] as const) {
      expect(addRoi([], { x, y }, 9, SHAPE)).toEqual({ refused: "outside" })
    }
    expect(addRoi([], { x: 20, y: 30 }, 65, SHAPE)).toEqual({
      refused: "outside",
    })
  })

  it("is refused past the API's 32 ROIs", () => {
    const rois = added(
      ...Array.from(
        { length: MAX_ROIS },
        (_, i) => [5 + i, 30] as [number, number]
      )
    )
    expect(rois).toHaveLength(32)
    expect(addRoi(rois, { x: 20, y: 30 }, 9, SHAPE)).toEqual({
      refused: "full",
    })
    expect(addRoi(rois.slice(1), { x: 20, y: 30 }, 9, SHAPE)).toHaveProperty(
      "rois"
    )
  })
})

describe("an ROI's colour", () => {
  it("follows the fixed order, stays when another is deleted, and the deleted one's colour is the next given", () => {
    const rois = added([10, 10], [20, 20], [30, 30])
    expect(rois.map((roi) => roi.color)).toEqual(ROI_COLORS.slice(0, 3))
    const [first, second, third] = rois as [Roi, Roi, Roi]

    const left = rois.filter((roi) => roi !== second)
    expect(left.map((roi) => roi.color)).toEqual([first.color, third.color])
    const next = addRoi(left, { x: 25, y: 40 }, 9, SHAPE)
    expect("rois" in next && next.rois.map((roi) => roi.color)).toEqual([
      first.color,
      third.color,
      second.color,
    ])

    // Eight differ; the ninth repeats the first colour, the least used.
    const nine = added(
      ...Array.from(
        { length: 9 },
        (_, i) => [5 + i * 4, 30] as [number, number]
      )
    )
    expect(new Set(nine.slice(0, 8).map((roi) => roi.color)).size).toBe(8)
    expect(nine[8]?.color).toBe(ROI_COLORS[0])
  })
})

describe("the stored ROIs", () => {
  it("are kept for each run and point, and read back by another page", () => {
    const storage = memoryStorage()
    const rois = added([10, 10], [20, 20])
    const key = roiStorageKey(1, "wire 1&x")
    expect(key).toBe("laueportal:rois:reconstruction:1:wire 1&x")
    createRoiStore(storage).getState().setRois(key, rois)

    // A reload: a new store on the same storage.
    const reloaded = createRoiStore(storage).getState()
    expect(reloaded.roisOf(key)).toEqual(rois)
    expect(JSON.parse(storage.getItem(key) ?? "")).toEqual(
      rois.map(({ x, y, size, color }) => ({ x, y, size, color }))
    )
    // Another point, another run: their own (none).
    expect(reloaded.roisOf(roiStorageKey(1, "wire_3"))).toEqual([])
    expect(reloaded.roisOf(roiStorageKey(2, "wire 1&x"))).toEqual([])

    // A stored value that cannot be read keeps only its readable ROIs.
    storage.setItem(
      roiStorageKey(3, "p"),
      JSON.stringify([
        { x: 1, y: 2, size: 9, color: "#3987e5" },
        { x: "1", y: 2, size: 9, color: "#3987e5" },
        { x: 1, y: 2, size: 0, color: "#3987e5" },
        { x: 1, y: 2, size: 9, color: "red" },
      ])
    )
    storage.setItem(roiStorageKey(4, "p"), "{not json")
    const store = createRoiStore(storage).getState()
    expect(store.roisOf(roiStorageKey(3, "p"))).toEqual([
      { x: 1, y: 2, size: 9, color: "#3987e5" },
    ])
    expect(store.roisOf(roiStorageKey(4, "p"))).toEqual([])
  })
})
