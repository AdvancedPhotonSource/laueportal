import { describe, expect, it } from "vitest"

import type { DetectorOverlays, DetectorPattern } from "@/api"

import { overlayLayers } from "./overlays"

function pattern(
  index: number,
  peaks: [number, number | null][]
): DetectorPattern {
  return {
    pattern_index: index,
    goodness: null,
    in_scope: true,
    indexed: true,
    n_indexed: peaks.length,
    rms_error_deg: null,
    peaks: peaks.map(([x, y], i) => ({
      h: index,
      k: -i,
      l: 2,
      peak_index: i,
      x,
      y,
    })),
    simulated: [
      { energy_kev: 9, h: 1, k: 1, l: 1, relative_intensity: 1, x: 5, y: 6 },
    ],
  }
}

const overlays: DetectorOverlays = {
  width: 100,
  height: 50,
  energy_range_kev: [6, 30],
  simulation_error: null,
  summary: {} as DetectorOverlays["summary"],
  detected: [
    { peak_index: 0, x: 10, y: 20, indexed: true, intensity: 5 },
    { peak_index: 1, x: null, y: null, indexed: false, intensity: null },
  ],
  patterns: [
    // In the frame, outside it (lauelab drops those), and not placed.
    pattern(0, [
      [1, 2],
      [100.5, 2],
      [3, null],
    ]),
    pattern(1, [[7, 8]]),
  ],
}

describe("overlay layer", () => {
  it("builds the toggled layers, with hkl labels, for the chosen patterns", () => {
    const all = overlayLayers(overlays, {
      detected: true,
      indexed: true,
      simulated: true,
      labels: true,
    })
    expect(
      all.map((l) => [l.kind, Array.from(l.x), Array.from(l.y), l.labels])
    ).toEqual([
      ["detected", [10], [20], null],
      ["indexed", [1, 7], [2, 8], ["(0 0 2)", "(1 0 2)"]],
      ["simulated", [5, 5], [6, 6], ["(1 1 1)", "(1 1 1)"]],
    ])
    const some = overlayLayers(overlays, {
      detected: false,
      indexed: true,
      simulated: false,
      labels: false,
      patterns: new Set([1]),
    })
    expect(some.map((l) => [l.kind, Array.from(l.x), l.labels])).toEqual([
      ["indexed", [7], null],
    ])
  })
})
