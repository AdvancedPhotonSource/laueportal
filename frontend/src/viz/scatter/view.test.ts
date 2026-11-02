import { describe, expect, it } from "vitest"

import {
  equalizeAspect,
  fitView,
  formatTick,
  gestureTransform,
  niceTicks,
  panView,
  resizeView,
  toData,
  toScreen,
  zoomLimits,
  zoomView,
} from "./view"

const size = { width: 200, height: 100 }
const view = { x0: 0, y0: 0, x1: 10, y1: 5 }

describe("view transforms", () => {
  it("maps data to screen with y up and back", () => {
    expect(toScreen(view, size, 0, 5)).toEqual([0, 0])
    expect(toScreen(view, size, 10, 0)).toEqual([200, 100])
    expect(toData(view, size, ...toScreen(view, size, 2.5, 1))).toEqual([
      2.5, 1,
    ])
  })

  it("pans so the content follows the pointer", () => {
    // Dragging 20 px right and 20 px down moves the view 1 unit left and 1 unit up.
    expect(panView(view, size, 20, 20)).toEqual({ x0: -1, y0: 1, x1: 9, y1: 6 })
  })

  it("zooms about the cursor, which keeps its data point", () => {
    const zoomed = zoomView(view, size, 50, 25, 2)
    expect(zoomed.x1 - zoomed.x0).toBe(5)
    expect(toData(zoomed, size, 50, 25)).toEqual(toData(view, size, 50, 25))
  })

  it("fits the bounds with padding, widening the narrow axis for an equal aspect", () => {
    const bounds = { x0: 0, y0: 0, x1: 10, y1: 10 }
    expect(fitView(bounds, size, { padding: 0 })).toEqual(bounds)
    const equal = fitView(bounds, size, { padding: 0, equalAspect: true })
    expect(equal).toEqual({ x0: -5, y0: 0, x1: 15, y1: 10 })
    expect((equal.x1 - equal.x0) / size.width).toBe(
      (equal.y1 - equal.y0) / size.height
    )
    expect(equalizeAspect(equal, size)).toEqual(equal)
    // Half a marker's pixels on each side, so the edge markers are whole: 10 px of 200 and of 100.
    const margin = fitView(bounds, size, { padding: 0, marginPx: 10 })
    const corners = [
      ...toScreen(margin, size, 0, 0),
      ...toScreen(margin, size, 10, 10),
    ]
    ;[10, 90, 190, 10].forEach((px, i) => {
      expect(corners[i]).toBeCloseTo(px, 9)
    })
    // One point (zero span) still gets a unit-wide view about it.
    expect(
      fitView({ x0: 3, y0: 3, x1: 3, y1: 3 }, size, { padding: 0 })
    ).toEqual({
      x0: 2.5,
      y0: 2.5,
      x1: 3.5,
      y1: 3.5,
    })
  })

  it("refits the automatic view on resize; the user's keeps its equal-aspect scale or stretches", () => {
    const equal = { x0: 0, y0: 0, x1: 20, y1: 10 }
    const to = { width: 100, height: 100 }
    const user = { fitted: false }
    expect(resizeView(equal, size, to, { equalAspect: true, ...user })).toEqual(
      {
        x0: 5,
        y0: 0,
        x1: 15,
        y1: 10,
      }
    )
    expect(resizeView(view, size, to, { equalAspect: false, ...user })).toBe(
      view
    )
    expect(
      resizeView(equal, size, to, { equalAspect: true, fitted: true })
    ).toBeNull()
  })

  it("clamps the zoom to its limits and the coordinates' precision, and keeps the ticks bounded", () => {
    const fitted = { x0: 0, y0: 0, x1: 150, y1: 75 }
    const limits = zoomLimits(fitted) // 1e6 times in, 1e3 times out
    let v = fitted
    for (let k = 0; k < 400; k++) v = zoomView(v, size, 37, 81, 1.5, limits)
    expect(v.x1 - v.x0).toBeCloseTo(150e-6, 12)
    expect(zoomView(v, size, 37, 81, 2, limits)).toBe(v) // no further, never the other way
    for (let k = 0; k < 400; k++) v = zoomView(v, size, 37, 81, 1 / 1.5, limits)
    expect(v.x1 - v.x0).toBeCloseTo(150e3, 3)
    // Far from the origin, the precision floor stops the zoom before the span loses its digits.
    let far = { x0: 1e9, y0: 1e9, x1: 1e9 + 1, y1: 1e9 + 0.5 }
    const farLimits = zoomLimits(far, { zoomIn: 1e12 })
    for (let k = 0; k < 60; k++)
      far = zoomView(far, size, 100, 50, 2, farLimits)
    expect(far.x1 - far.x0).toBeGreaterThan(1e9 * Number.EPSILON * 1000)
    expect(niceTicks(far.x0, far.x1).values.length).toBeGreaterThan(0)
    // niceTicks alone: a step below the coordinates' precision gives no ticks (no runaway loop).
    expect(niceTicks(1e12, 1e12 + 2.5e-4).values).toEqual([])
  })

  it("maps the cached bitmap's pixels onto the live view during a gesture", () => {
    const live = zoomView(panView(view, size, 30, -10), size, 80, 40, 1.5)
    const t = gestureTransform(view, live, size)
    const [rx, ry] = toScreen(view, size, 7, 2)
    const [lx, ly] = toScreen(live, size, 7, 2)
    expect(t.ax * rx + t.bx).toBeCloseTo(lx, 9)
    expect(t.ay * ry + t.by).toBeCloseTo(ly, 9)
  })

  it("picks round tick steps and labels them with the decimals the step needs", () => {
    expect(niceTicks(0, 10, 5)).toEqual({
      values: [0, 2, 4, 6, 8, 10],
      step: 2,
    })
    expect(
      niceTicks(-0.23, 0.31, 10).values.map((v) => formatTick(v, 0.1))
    ).toEqual(["-0.2", "-0.1", "0.0", "0.1", "0.2", "0.3"])
    // Deep in a zoom, neighbouring labels still differ.
    const deep = niceTicks(55.41, 55.410165, 4)
    const labels = deep.values.map((v) => formatTick(v, deep.step))
    expect(labels.length).toBeGreaterThanOrEqual(3)
    expect(new Set(labels).size).toBe(labels.length)
    expect(labels.every((l) => /^55\.410\d\d$/.test(l))).toBe(true)
    expect(formatTick(1.5e7, 1e6)).toBe("1.5e+7")
    expect(niceTicks(1, 1).values).toEqual([])
  })
})
