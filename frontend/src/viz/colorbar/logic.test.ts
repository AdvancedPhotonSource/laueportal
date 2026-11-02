import { describe, expect, it } from "vitest"

import { countBelow, distributionOf, quantile } from "../color/distribution"
import { normalizer } from "../color/scale"
import {
  autoRange,
  axisDomain,
  axisTicks,
  colorbarSettings,
  dataDomain,
  effectiveScale,
  histogramBars,
  labelPositions,
  moveHandle,
  positionToValue,
  resolveRange,
  valueToPosition,
} from "./logic"

// 0 (masked), a background of 100–199, a tail up to 5000.
const frame = Uint16Array.from({ length: 10_000 }, (_, i) =>
  i < 50 ? 0 : i < 9_950 ? 100 + (i % 100) : 5000 - (i % 50) * 10
)
const counts = distributionOf(frame)
const floats = distributionOf(Float64Array.from(frame, (v) => v / 8 - 3))

describe("colorbar", () => {
  it("maps values to positions along the bar and back, linearly or in log space", () => {
    expect(valueToPosition(150, [100, 200], "linear", 300)).toBe(150)
    expect(positionToValue(150, [100, 200], "linear", 300)).toBe(150)
    expect(valueToPosition(100, [1, 10_000], "log", 400)).toBeCloseTo(200, 9)
    expect(positionToValue(300, [1, 10_000], "log", 400)).toBeCloseTo(1000, 6)
    for (const v of [1.5, 37, 9999]) {
      const x = valueToPosition(v, [1, 10_000], "log", 400)
      expect(positionToValue(x, [1, 10_000], "log", 400)).toBeCloseTo(v, 9)
    }
  })

  it("bounds a drag to what the bar shows and takes a typed value as given, the minimum below the maximum", () => {
    const shown = [0, 5000] as const
    const range = [100, 200] as const
    expect(moveHandle("min", 150.4, range, counts, shown)).toEqual([150, 200])
    expect(moveHandle("min", 250, range, counts, shown)).toEqual([199, 200])
    expect(moveHandle("min", -40, range, counts, shown)).toEqual([0, 200])
    expect(moveHandle("max", 50, range, counts, shown)).toEqual([100, 101])
    expect(moveHandle("max", 9e9, range, counts, shown)).toEqual([100, 5000])
    expect(moveHandle("max", 4000.6, range, counts, [90, 4000.5])).toEqual([
      100, 4000,
    ])
    expect(moveHandle("max", NaN, range, counts, shown)).toBe(range)
    // Typed: beyond the data and unsnapped, still below the other handle.
    expect(moveHandle("max", 9000, range, counts, null)).toEqual([100, 9000])
    expect(moveHandle("min", -40.5, range, counts, null)).toEqual([-40.5, 200])
    expect(moveHandle("min", 300, range, counts, null)).toEqual([199, 200])
    // Float data: no snapping, a millionth of the bar apart; the log scale starts above zero.
    const fd = dataDomain(floats, "linear")
    if (!fd) return
    const [lo, hi] = moveHandle("max", -100, [0, 10], floats, fd)
    expect([lo, hi - lo]).toEqual([0, (fd[1] - fd[0]) * 1e-6])
    expect(dataDomain(counts, "linear")).toEqual([0, 5000])
    expect(dataDomain(counts, "log")).toEqual([100, 5000])
    expect(dataDomain(distributionOf([3, 3]), "linear")).toEqual([3, 4])
    expect(dataDomain(distributionOf([]), "linear")).toBeNull()
  })

  it("resolves the automatic ranges, and the user's as given", () => {
    expect(autoRange(counts, "minmax", "linear")).toEqual([0, 5000])
    expect(autoRange(counts, "p1-99.9", "linear")).toEqual([
      quantile(counts, 0.01),
      quantile(counts, 0.999),
    ])
    // Log: a percentile at zero is lifted to the smallest positive value.
    expect(autoRange(counts, "p0.1-99.99", "log")?.[0]).toBe(100)
    // Percentiles that meet fall back to min–max.
    const flat = distributionOf(
      Uint16Array.from([0, ...Array<number>(500).fill(7), 9])
    )
    expect(autoRange(flat, "p5-95", "linear")).toEqual([0, 9])
    // A kept range is not clamped to each frame's data: beyond it, around it or outside it.
    const manual = { ...colorbarSettings("minmax"), auto: null }
    for (const [min, max] of [
      [10, 20],
      [-50, 300],
      [0, 9500],
      [9000, 9500],
    ] as const) {
      expect(resolveRange({ ...manual, min, max }, counts)).toEqual([min, max])
    }
    expect(resolveRange({ ...manual, min: 20, max: 10 }, counts)).toEqual([
      0, 5000,
    ])
    expect(resolveRange({ ...manual, min: NaN, max: 10 }, counts)).toEqual([
      0, 5000,
    ])
    // Log: a minimum at or below zero starts at the smallest positive value.
    const log = { ...manual, scale: "log" as const }
    expect(resolveRange({ ...log, min: 0, max: 300 }, counts)).toEqual([
      100, 300,
    ])
    expect(resolveRange({ ...log, min: 5, max: 300 }, counts)).toEqual([5, 300])
  })

  it("fits the bar's axis around the range, within the data and the range, or shows both", () => {
    expect(axisDomain([1000, 2000], [0, 5000], "linear", "fit")).toEqual([
      500, 2500,
    ])
    expect(axisDomain([100, 2000], [0, 5000], "linear", "fit")).toEqual([
      0, 2950,
    ])
    expect(axisDomain([100, 2000], [0, 5000], "linear", "data")).toEqual([
      0, 5000,
    ])
    const [lo, hi] = axisDomain([10, 1000], [1, 1e6], "log", "fit")
    expect([lo, hi].map((v) => Math.log10(v))).toEqual([0, 4])
    // A range beyond the data: the axis covers it.
    expect(axisDomain([1000, 9000], [0, 5000], "linear", "fit")).toEqual([
      0, 9000,
    ])
    expect(axisDomain([-50, 300], [0, 5000], "linear", "data")).toEqual([
      -50, 5000,
    ])
    expect(axisDomain([10, 200], [0, 5], "linear", "fit")).toEqual([0, 200])
  })

  it("draws log on data without positive values as linear, as the colours are", () => {
    const negative = distributionOf(
      Float64Array.from({ length: 500 }, (_, i) => -5 + (i % 50) * 0.1)
    )
    const log = { ...colorbarSettings("minmax"), scale: "log" as const }
    const range = resolveRange(log, negative)
    if (!range) throw new Error("no range")
    expect(range[0]).toBe(-5)
    const kind = effectiveScale("log", range[0])
    expect(kind).toBe("linear")
    // The colours agree: the normalizer is linear for a range starting at or below zero.
    expect(normalizer(range[0], range[1], { kind: "log", gamma: 2 })(-5)).toBe(
      0
    )
    const axis = axisDomain(range, dataDomain(negative, kind), kind, "fit")
    expect(axis).toEqual(range)
    expect(valueToPosition(range[1], axis, kind, 300)).toBe(300)
    const { heights } = histogramBars(negative, axis, kind, 64)
    expect(Math.max(...heights)).toBe(1)
    expect(heights.every(Number.isFinite)).toBe(true)
    // A typed positive range on it stays log; its axis is the range alone.
    expect(effectiveScale("log", 1)).toBe("log")
    expect(
      axisDomain([1, 10], dataDomain(negative, "log"), "log", "data")
    ).toEqual([1, 10])
  })

  it("bins the histogram along the axis, whole integers per bin for integer data", () => {
    for (const [dist, axis] of [
      [counts, [90, 260]],
      [floats, [-3, 622]],
    ] as const) {
      const { edges, heights } = histogramBars(dist, axis, "linear", 64)
      expect(heights.length).toBe(edges.length - 1)
      expect(Math.max(...heights)).toBe(1)
      // The bins cover the axis and hold every value on it.
      const total = Array.from(heights.keys()).reduce(
        (sum, i) =>
          sum +
          countBelow(dist, edges[i + 1] as number, i === heights.length - 1) -
          countBelow(dist, edges[i] as number),
        0
      )
      expect(total).toBe(
        countBelow(dist, axis[1], true) - countBelow(dist, axis[0])
      )
    }
    // 1 to 9 at most 64 bins: one integer each, edges at half-integers.
    const { edges } = histogramBars(counts, [100, 109], "linear", 64)
    expect(Array.from(edges)).toEqual([
      99.5, 100.5, 101.5, 102.5, 103.5, 104.5, 105.5, 106.5, 107.5, 108.5,
      109.5,
    ])
    // The last bin holds no integer beyond the axis.
    for (const axis of [
      [16.5, 18.5],
      [16.4, 18.6],
    ] as const) {
      expect(
        Array.from(histogramBars(counts, axis, "linear", 64).edges)
      ).toEqual([16.5, 17.5, 18.5])
    }
    // Log axis: evenly spaced in log space.
    const log = histogramBars(floats, [1, 1000], "log", 3).edges
    expect(
      Array.from(log, (e) => Math.round(Math.log10(e) * 1e9) / 1e9)
    ).toEqual([0, 1, 2, 3])
    // Counts ∝ 1/v are flat per log unit, as uint16 and as floats alike.
    const values: number[] = []
    for (let v = 100; v <= 10_000; v++) {
      for (let k = Math.round(1e5 / v); k > 0; k--) values.push(v)
    }
    const asUint16 = histogramBars(
      distributionOf(Uint16Array.from(values)),
      [100, 10_000],
      "log",
      64
    ).heights
    const asFloat = histogramBars(
      distributionOf(Float64Array.from([...values, 150.5])),
      [100, 10_000],
      "log",
      64
    ).heights
    expect(asUint16.length).toBe(asFloat.length)
    for (const i of [0, 32, 63]) {
      expect(asUint16[i]).toBeGreaterThan(0.97)
      expect(asUint16[i]).toBeCloseTo(asFloat[i] as number, 1)
    }
  })

  it("labels each decade of a log axis with its own magnitude", () => {
    expect(axisTicks([1e-4, 10], "log", 8).labels).toEqual([
      "1e-4",
      "0.001",
      "0.01",
      "0.1",
      "1",
      "10",
    ])
    expect(axisTicks([1, 1e7], "log", 4).labels).toEqual([
      "1",
      "100",
      "10000",
      "1e6",
    ])
    expect(axisTicks([0, 10], "linear", 3).labels).toEqual(["0", "5", "10"])
  })

  it("places the value labels on their handles without overlap, inside the bar", () => {
    expect(labelPositions(100, 300, 40, 40, 400)).toEqual([80, 280])
    expect(labelPositions(5, 300, 40, 40, 400)).toEqual([0, 280])
    // Too close: pushed apart about their midpoint; at an edge, both stay inside.
    expect(labelPositions(200, 210, 40, 40, 400)).toEqual([162, 208])
    expect(labelPositions(0, 2, 40, 40, 400)).toEqual([0, 46])
    expect(labelPositions(398, 400, 40, 40, 400)).toEqual([314, 360])
  })
})
