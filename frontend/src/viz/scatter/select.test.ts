import { describe, expect, it } from "vitest"

import { boxSelect, lassoSelect, pointInPolygon } from "./select"

// A concave "C" shape: the notch (2..4, 1..3) is outside.
const cShape = [0, 0, 4, 0, 4, 1, 2, 1, 2, 3, 4, 3, 4, 4, 0, 4]

describe("lasso", () => {
  it("tests points against a concave polygon", () => {
    expect(pointInPolygon(1, 2, cShape)).toBe(true)
    expect(pointInPolygon(3, 2, cShape)).toBe(false) // in the notch
    expect(pointInPolygon(3, 3.5, cShape)).toBe(true)
    expect(pointInPolygon(5, 2, cShape)).toBe(false)
  })

  it("counts crossings (even-odd): both lobes of a figure 8, not a region circled twice", () => {
    const eight = [0, 0, 2, 2, 2, 0, 0, 2] // two triangles meeting at (1, 1)
    expect(pointInPolygon(0.3, 1, eight)).toBe(true)
    expect(pointInPolygon(1.7, 1, eight)).toBe(true)
    expect(pointInPolygon(1, 0.3, eight)).toBe(false) // between the lobes
    const twice = [0, 0, 4, 0, 4, 4, 0, 4, 0, 0, 4, 0, 4, 4, 0, 4]
    expect(pointInPolygon(2, 2, twice)).toBe(false)
  })

  it("selects the unmasked points inside, coincident ones included", () => {
    const x = [1, 1, 3, 3, 1]
    const y = [2, 2, 2, 3.5, 0.5]
    expect(Array.from(lassoSelect(x, y, cShape))).toEqual([0, 1, 3, 4])
    expect(
      Array.from(lassoSelect(x, y, cShape, Uint8Array.of(1, 0, 1, 1, 0)))
    ).toEqual([0, 3])
    expect(lassoSelect(x, y, [0, 0, 4, 4]).length).toBe(0) // not a polygon
  })
})

describe("box selection", () => {
  it("selects the unmasked points between two corners in any order, edges included", () => {
    const x = [0, 1, 2, 3]
    const y = [0, 1, 2, 3]
    expect(Array.from(boxSelect(x, y, { x0: 2, y0: 2, x1: 1, y1: 1 }))).toEqual(
      [1, 2]
    )
    expect(
      Array.from(
        boxSelect(
          x,
          y,
          { x0: 0, y0: 0, x1: 3, y1: 3 },
          Uint8Array.of(1, 1, 0, 1)
        )
      )
    ).toEqual([0, 1, 3])
  })
})
