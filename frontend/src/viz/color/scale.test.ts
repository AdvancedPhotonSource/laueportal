import { describe, expect, it } from "vitest"

import { normalizer, type ColorScale } from "./scale"

const scale = (kind: ColorScale["kind"], gamma = 2): ColorScale => ({
  kind,
  gamma,
})

describe("colour scales", () => {
  it("map the range's ends to 0 and 1 and the inside along each curve, clamped", () => {
    const cases: [ColorScale, number, number, number, number][] = [
      // scale, lo, hi, value, t
      [scale("linear"), 10, 20, 15, 0.5],
      [scale("sqrt"), 10, 20, 12.5, 0.5],
      [scale("gamma", 2), 10, 20, 15, 0.25],
      [scale("gamma", 0.5), 0, 100, 25, 0.5],
      [scale("log"), 1, 10000, 100, 0.5],
    ]
    for (const [s, lo, hi, v, t] of cases) {
      const f = normalizer(lo, hi, s)
      expect(f(lo), s.kind).toBe(0)
      expect(f(hi), s.kind).toBe(1)
      expect(f(v), `${s.kind} ${String(s.gamma)}`).toBeCloseTo(t, 12)
      expect(f(lo - 5), s.kind).toBe(0)
      expect(f(hi * 2), s.kind).toBe(1)
      expect(f(NaN), s.kind).toBeNaN()
    }
    // Log: values at or below zero sit at the bottom; without a positive minimum it is linear.
    expect(normalizer(1, 100, scale("log"))(-3)).toBe(0)
    expect(normalizer(0, 100, scale("log"))(25)).toBe(0.25)
  })
})
