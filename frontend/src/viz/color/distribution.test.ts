import { describe, expect, it } from "vitest"

import { countBelow, distributionOf, quantile } from "./distribution"

/** numpy.percentile's default (linear) on a sorted copy: the reference. */
function reference(values: number[], q: number): number {
  const s = values.filter(Number.isFinite).sort((a, b) => a - b)
  const pos = q * (s.length - 1)
  const i = Math.floor(pos)
  const lo = s[i] as number
  return i + 1 < s.length ? lo + ((s[i + 1] as number) - lo) * (pos - i) : lo
}

function random(seed: number) {
  let s = seed
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0
    return s / 2 ** 32
  }
}

describe("distribution", () => {
  it("gives a uint16 frame's and float data's summary and numpy's percentiles", () => {
    const next = random(7)
    // A detector-like frame: background near 1000, a few bright peaks, some zeros.
    const frame = Uint16Array.from({ length: 50_000 }, (_, i) =>
      i % 997 === 0
        ? 0
        : i % 211 === 0
          ? 30_000 + next() * 35_535
          : 900 + next() * 200
    )
    const numbers = Array.from(frame)
    const signed = Float32Array.from(numbers, (v, i) =>
      i % 3 ? v - 1500.25 : v
    )
    const withGaps = [...Array.from(signed), NaN, Infinity, -Infinity]
    const cases = [
      [distributionOf(frame), numbers],
      [distributionOf(Float32Array.from(withGaps)), Array.from(signed)],
    ] as const
    for (const [dist, values] of cases) {
      const finite = values.filter(Number.isFinite)
      expect(dist.n).toBe(finite.length)
      expect(dist.min).toBe(Math.min(...finite))
      expect(dist.max).toBe(Math.max(...finite))
      expect(dist.minPositive).toBe(Math.min(...finite.filter((v) => v > 0)))
      for (const q of [0, 0.001, 0.01, 0.5, 0.95, 0.999, 1]) {
        expect(quantile(dist, q), `${dist.kind} q=${String(q)}`).toBeCloseTo(
          reference(finite, q),
          2
        )
      }
    }
    expect(cases[0][0].integer).toBe(true)
    expect(cases[1][0].integer).toBe(false)
    expect(quantile(distributionOf([1, 2, 3, 4]), 0.5)).toBe(2.5)
    expect(quantile(distributionOf(new Uint16Array(0)), 0.5)).toBeNaN()
  })

  it("counts the values below a bound, strictly or not", () => {
    const values = [0, 1, 1, 2, 5, 5, 5, 9]
    for (const dist of [
      distributionOf(Uint16Array.from(values)),
      distributionOf(values),
    ]) {
      for (const x of [-1, 0, 0.5, 1, 4.9, 5, 5.1, 9, 10]) {
        expect(countBelow(dist, x), `${dist.kind} < ${String(x)}`).toBe(
          values.filter((v) => v < x).length
        )
        expect(countBelow(dist, x, true), `${dist.kind} <= ${String(x)}`).toBe(
          values.filter((v) => v <= x).length
        )
      }
    }
  })
})
