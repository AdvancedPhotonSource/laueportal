import { describe, expect, it } from "vitest"

import { linkedSelection, selectionMask } from "../linked-selection"
import { packColors } from "../scatter/stamp"
import {
  boundsOf,
  drawnPoints,
  interleave,
  pointRgb,
  ringedPoints,
  vertexCells,
} from "./points"

describe("drawnPoints", () => {
  it("draws only the points with a visible colour and finite coordinates", () => {
    const x = [0, 1, 2, Number.NaN, 4]
    const y = [0, -1, 2, 3, 4]
    const z = [5, 6, 7, 8, Number.POSITIVE_INFINITY]
    // Point 2 is a non-indexed point hidden with the transparent colour.
    const packed = packColors(
      {
        kind: "rgb",
        r: [9, 9, 9, 9, 9],
        g: [0, 0, 0, 0, 0],
        b: [1, 1, 1, 1, 1],
      },
      5,
      [1, 1, 0, 1, 1],
      "transparent"
    )
    const drawn = drawnPoints(packed, x, y, z)
    expect(Array.from(drawn)).toEqual([0, 1])
    expect(Array.from(vertexCells(drawn))).toEqual([1, 0, 1, 1])
    expect(boundsOf(interleave(x, y, z), drawn)).toEqual([0, 1, -1, 0, 5, 6])
    expect(boundsOf(interleave(x, y, z), new Int32Array(0))).toBeNull()
  })
})

describe("the linked selection in 3D", () => {
  it("dims the unselected points over the background and rings the selected drawn ones", () => {
    const step = [10, 10, 11, 12]
    const patternIndex = [0, 1, 0, -1]
    const packed = packColors(
      {
        kind: "rgb",
        r: [255, 100, 0, 50],
        g: [0, 100, 255, 50],
        b: [0, 100, 0, 50],
      },
      4
    )
    const white: [number, number, number] = [255, 255, 255]
    // No selection: the colours as they are, no rings.
    expect(Array.from(pointRgb(packed, null, white))).toEqual([
      255, 0, 0, 100, 100, 100, 0, 255, 0, 50, 50, 50,
    ])
    expect(ringedPoints(Int32Array.of(0, 1, 2, 3), null)).toHaveLength(0)

    const mask = selectionMask(
      linkedSelection([
        [10, 1],
        [12, -1],
      ]),
      step,
      patternIndex
    )
    // Unselected: 0.2 × colour + 0.8 × background.
    expect(Array.from(pointRgb(packed, mask, white))).toEqual([
      255, 204, 204, 100, 100, 100, 204, 255, 204, 50, 50, 50,
    ])
    // Point 3 (a frame-only record) is selected but not drawn here.
    expect(Array.from(ringedPoints(Int32Array.of(0, 1, 2), mask))).toEqual([1])
  })
})
