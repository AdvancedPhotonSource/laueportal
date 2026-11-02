import { describe, expect, it } from "vitest"

import { ApiError, MapColor } from "@/api"
import { linkedSelection } from "@/viz"
import {
  clampToCircle,
  clickPole,
  hklUpdate,
  isTooManyPatterns,
  MAX_STATS_PATTERNS,
  poleFigureRequest,
  resetPole,
  setCenter,
  statsRequest,
} from "./pole-figure"
import { DEFAULT_POLE, readPole } from "./viewer-slices"

const SCOPE = { pattern0_only: true, min_peaks: 3 }

describe("hklUpdate", () => {
  it("enables Update only for three integers, not all zero, other than the applied HKL", () => {
    const applied = [1, 0, 0] as const
    expect(hklUpdate(["1", "1", "0"], applied)).toEqual([1, 1, 0])
    expect(hklUpdate([" -1", "+2", "10"], applied)).toEqual([-1, 2, 10])
    // Unchanged, all zero, empty, not an integer.
    expect(hklUpdate(["1", "0", "0"], applied)).toBeNull()
    expect(hklUpdate(["01", "0", "0"], applied)).toBeNull()
    expect(hklUpdate(["0", "0", "0"], applied)).toBeNull()
    expect(hklUpdate(["1", "", "0"], applied)).toBeNull()
    expect(hklUpdate(["1.5", "0", "0"], applied)).toBeNull()
    expect(hklUpdate(["1e1", "0", "0"], applied)).toBeNull()
  })
})

describe("poleFigureRequest", () => {
  it("sends the radius and the clicked centre with the HSV scheme only", () => {
    const reference = {
      indexingId: 1,
      pattern: { step: 2, patternIndex: 0 },
      center: [0.1, -0.2] as const,
      previousColor: MapColor.cubic_ipf,
    }
    expect(
      poleFigureRequest(DEFAULT_POLE, reference, [1, 1, 0], SCOPE)
    ).toEqual({
      ...SCOPE,
      hkl: "1,1,0",
      surface: "normal",
      scheme: "hsv",
      radius: 22.5,
      center: "0.1,-0.2",
    })
    expect(
      poleFigureRequest(
        { ...DEFAULT_POLE, scheme: "ipf", surface: "X" },
        reference,
        [1, 0, 0],
        SCOPE
      )
    ).toEqual({ ...SCOPE, hkl: "1,0,0", surface: "X", scheme: "ipf" })
  })
})

describe("clickPole and resetPole", () => {
  const first = { step: 4, patternIndex: 1, center: [0.3, 0.4] as const }
  const second = { step: 9, patternIndex: 0, center: [-0.5, 0] as const }

  it("centres on a clicked pole and switches the map to Pole HSV; the same pattern again or Reset restores the mode", () => {
    const set = clickPole(7, null, first, MapColor.goodness)
    expect(set).toEqual({
      reference: {
        indexingId: 7,
        pattern: { step: 4, patternIndex: 1 },
        center: [0.3, 0.4],
        previousColor: MapColor.goodness,
      },
      color: MapColor.pole_hsv,
    })
    const reference = set.reference
    if (!reference) throw new Error("no reference")

    // Another pole moves the centre and keeps the mode to restore.
    const moved = clickPole(7, reference, second, MapColor.pole_hsv)
    expect(moved.reference).toMatchObject({
      pattern: { step: 9, patternIndex: 0 },
      center: [-0.5, 0],
      previousColor: MapColor.goodness,
    })
    expect(moved.color).toBe(MapColor.pole_hsv)
    // A mode chosen on the map since the first click is the one restored.
    expect(
      clickPole(7, reference, second, MapColor.n_patterns).reference
        ?.previousColor
    ).toBe(MapColor.n_patterns)

    // Another pole of the reference pattern clears it (the pattern is compared, not the pole).
    expect(
      clickPole(7, reference, { ...first, center: [0, 0.9] }, MapColor.pole_hsv)
    ).toEqual({ reference: null, color: MapColor.goodness })
    expect(resetPole(reference)).toEqual({
      reference: null,
      color: MapColor.goodness,
    })

    // From Pole HSV, and for a reference stored without a mode, cubic IPF returns. A reference
    // stored in the flat shape (its pattern's step and index on the reference itself) still reads.
    expect(
      clickPole(7, null, first, MapColor.pole_hsv).reference?.previousColor
    ).toBe(MapColor.cubic_ipf)
    const stored = readPole({
      reference: { indexingId: 7, step: 4, patternIndex: 1, center: [0, 0] },
    }).reference
    if (!stored) throw new Error("no stored reference")
    expect(stored.pattern).toEqual({ step: 4, patternIndex: 1 })
    expect(resetPole(stored).color).toBe(MapColor.cubic_ipf)
  })

  it("keeps the map in misorientation, whose hint asks for the reference", () => {
    expect(clickPole(7, null, first, MapColor.misorientation)).toMatchObject({
      reference: { pattern: { step: 4 }, previousColor: MapColor.cubic_ipf },
      color: MapColor.misorientation,
    })
  })
})

describe("setCenter (the Set color center tool)", () => {
  const pole = { step: 4, patternIndex: 1, center: [0.3, 0.4] as const }

  it("snaps to the pole under the click, as a click on it, and a second click keeps it", () => {
    const snapped = setCenter(
      7,
      null,
      { pole, point: [0.31, 0.39] },
      MapColor.goodness
    )
    expect(snapped).toEqual(clickPole(7, null, pole, MapColor.goodness))
    expect(snapped.reference).toMatchObject({
      pattern: { step: 4, patternIndex: 1 },
      center: [0.3, 0.4],
    })
    // Unlike a plain click on the reference's pole, which clears it.
    expect(
      setCenter(
        7,
        snapped.reference,
        { pole, point: [0.3, 0.4] },
        MapColor.pole_hsv
      )
    ).toEqual(snapped)
  })

  it("places the centre at a point off the poles, clamped to the unit circle, and keeps the reference", () => {
    const placed = setCenter(
      7,
      null,
      { pole: null, point: [0.2, -0.1] },
      MapColor.goodness
    )
    expect(placed).toEqual({
      reference: {
        indexingId: 7,
        pattern: null,
        center: [0.2, -0.1],
        previousColor: MapColor.goodness,
      },
      color: MapColor.pole_hsv,
    })
    const reference = clickPole(7, null, pole, MapColor.goodness).reference
    const moved = setCenter(
      7,
      reference,
      { pole: null, point: [3, 4] },
      MapColor.pole_hsv
    )
    expect(moved.reference).toEqual({
      indexingId: 7,
      pattern: { step: 4, patternIndex: 1 },
      center: [0.6, 0.8],
      previousColor: MapColor.goodness,
    })
    expect(clampToCircle([0.5, 0.5])).toEqual([0.5, 0.5])
  })
})

describe("statsRequest", () => {
  /** Poles as the product's columns. */
  function poles(pairs: [number, number][]) {
    return {
      length: pairs.length,
      step: Int32Array.from(pairs, ([step]) => step),
      patternIndex: Int32Array.from(pairs, ([, index]) => index),
    }
  }

  it("sends each selected pattern with a pole once, sorted, never a frame-only record", () => {
    // Three poles of (2, 0), one of (1, 1); a frame-only record of step 3; (5, 0) not selected.
    const figure = poles([
      [2, 0],
      [1, 1],
      [2, 0],
      [3, -1],
      [2, 0],
      [5, 0],
    ])
    const selection = linkedSelection([
      [2, 0],
      [3, -1],
      [1, 1],
      // Selected on the map, but no pole (not indexed).
      [6, 0],
    ])
    expect(statsRequest(selection, figure)).toEqual({
      kind: "summary",
      poles: 4,
      patterns: 2,
      request: [
        [1, 1],
        [2, 0],
      ],
    })
    // Only frame-only records and patterns without a pole: nothing to send.
    expect(
      statsRequest(
        linkedSelection([
          [3, -1],
          [6, 0],
        ]),
        figure
      )
    ).toEqual({ kind: "summary", poles: 0, patterns: 0, request: null })
    expect(statsRequest(linkedSelection([]), figure)).toEqual({
      kind: "empty",
    })
  })

  it(`skips the request above ${String(MAX_STATS_PATTERNS)} distinct patterns (the skip notice) and below two`, () => {
    // Two poles per pattern: the bound counts patterns, not poles.
    const pairs = Array.from(
      { length: MAX_STATS_PATTERNS + 1 },
      (_, i) => [i, 0] as [number, number]
    )
    const figure = poles([...pairs, ...pairs])
    const all = statsRequest(linkedSelection(pairs), figure)
    expect(all).toMatchObject({
      poles: 2 * (MAX_STATS_PATTERNS + 1),
      patterns: MAX_STATS_PATTERNS + 1,
      request: null,
    })
    const atBound = statsRequest(
      linkedSelection(pairs.slice(0, MAX_STATS_PATTERNS)),
      figure
    )
    expect(atBound.kind === "summary" && atBound.request?.length).toBe(
      MAX_STATS_PATTERNS
    )
    expect(
      statsRequest(linkedSelection(pairs.slice(0, 1)), figure)
    ).toMatchObject({ patterns: 1, request: null })
  })
})

describe("isTooManyPatterns", () => {
  it("reads the API's 422 too_many_patterns as the skip notice, and no other error", () => {
    const issue = (code: string) => ({
      loc: ["patterns"],
      message: "Too many patterns",
      code,
      severity: "error" as const,
    })
    expect(
      isTooManyPatterns(
        new ApiError(422, "Unprocessable Content", {
          status: 422,
          errors: [issue("too_many_patterns")],
        })
      )
    ).toBe(true)
    expect(
      isTooManyPatterns(
        new ApiError(422, "Unprocessable Content", {
          status: 422,
          errors: [issue("unknown_pattern")],
        })
      )
    ).toBe(false)
    expect(isTooManyPatterns(new Error("network"))).toBe(false)
  })
})
