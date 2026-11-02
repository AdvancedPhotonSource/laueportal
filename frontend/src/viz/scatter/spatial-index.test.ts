import { describe, expect, it } from "vitest"

import { buildGridIndex, nearestPoint } from "./spatial-index"

const size = { width: 100, height: 100 }
const view = { x0: 0, y0: 0, x1: 100, y1: 100 } // one data unit per pixel

describe("grid spatial index picking", () => {
  it("finds the same nearest point as a full scan, within the pixel radius only", () => {
    let seed = 7
    const random = () =>
      ((seed = (seed * 16807) % 2147483647) / 2147483647) * 100
    const x = Float64Array.from({ length: 2000 }, random)
    const y = Float64Array.from({ length: 2000 }, random)
    const index = buildGridIndex(x, y)
    for (const [px, py] of [
      [10, 10],
      [50.5, 49.5],
      [99, 1],
      [0, 100],
    ] as const) {
      let best = -1
      let bestD = 6 * 6
      for (let i = 0; i < x.length; i++) {
        const d =
          ((x[i] as number) - px) ** 2 + (100 - (y[i] as number) - py) ** 2
        if (d <= bestD) [best, bestD] = [i, d]
      }
      expect(nearestPoint(index, x, y, view, size, px, py, 6)).toBe(best)
    }
    expect(
      nearestPoint(buildGridIndex([0], [0]), [0], [0], view, size, 50, 50, 6)
    ).toBe(-1)
  })

  it("follows the view's scale and skips masked and non-finite points", () => {
    const x = [10, 10.5, 20, NaN]
    const y = [10, 10, 10, 10]
    const index = buildGridIndex(x, y)
    const mask = Uint8Array.of(1, 0, 1, 1)
    // Zoomed in ten times about (10, 10): 0.4 units right of point 0 is 4 pixels away.
    const zoomed = { x0: 5, y0: 5, x1: 15, y1: 15 }
    expect(nearestPoint(index, x, y, zoomed, size, 54, 50, 6, mask)).toBe(0)
    expect(nearestPoint(index, x, y, zoomed, size, 60, 50, 6)).toBe(1)
    expect(nearestPoint(index, x, y, zoomed, size, 60, 50, 6, mask)).toBe(-1) // point 1 is masked
  })

  it("prefers the later of coincident points, which is drawn on top", () => {
    const index = buildGridIndex([5, 5, 5], [5, 5, 5])
    expect(
      nearestPoint(index, [5, 5, 5], [5, 5, 5], view, size, 5, 95, 3)
    ).toBe(2)
  })
})
