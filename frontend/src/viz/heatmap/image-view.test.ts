import { describe, expect, it } from "vitest"

import { fitView, resizeView } from "../scatter/view"
import {
  frameBounds,
  imageBox,
  imageToScreen,
  imageZoomLimits,
  panImage,
  pixelAt,
  screenToImage,
  visibleRect,
  zoomImage,
} from "./image-view"

const size = { width: 400, height: 200 }

describe("heatmap view", () => {
  it("maps image pixels to the plot with y down, pans with the pointer and zooms at the cursor", () => {
    const view = { x0: -0.5, y0: -0.5, x1: 99.5, y1: 49.5 }
    expect(imageToScreen(view, size, -0.5, -0.5)).toEqual([0, 0])
    expect(imageToScreen(view, size, 99.5, 49.5)).toEqual([400, 200])
    expect(screenToImage(view, size, 200, 100)).toEqual([49.5, 24.5])
    // Dragging down and right moves the image down and right.
    const panned = panImage(view, size, 40, 20)
    expect(imageToScreen(panned, size, -0.5, -0.5)).toEqual([40, 20])
    // The pixel under the cursor stays under it.
    const zoomed = zoomImage(view, size, 300, 50, 4)
    const [x, y] = screenToImage(view, size, 300, 50)
    const [px, py] = imageToScreen(zoomed, size, x, y)
    expect(px).toBeCloseTo(300, 9)
    expect(py).toBeCloseTo(50, 9)
    expect(zoomed.x1 - zoomed.x0).toBeCloseTo(25, 9)
  })

  it("fits the whole frame with square pixels, limits the zoom and keeps the user's view on resize", () => {
    const fitted = fitView(frameBounds(2048, 1024), size, {
      equalAspect: true,
      padding: 0,
    })
    expect((fitted.x1 - fitted.x0) / size.width).toBeCloseTo(
      (fitted.y1 - fitted.y0) / size.height,
      12
    )
    expect([fitted.x0, fitted.x1]).toEqual([-0.5, 2047.5])
    // In to about 4 pixels across, out to 8 times the fit.
    const limits = imageZoomLimits(fitted)
    const deep = zoomImage(fitted, size, 200, 100, 1e9, limits)
    expect(deep.x1 - deep.x0).toBeCloseTo(4, 9)
    const far = zoomImage(fitted, size, 200, 100, 1e-9, limits)
    expect(far.x1 - far.x0).toBeCloseTo(8 * 2048, 6)
    // "Still fitted" refits on resize; a zoomed view keeps its scale.
    const to = { width: 800, height: 200 }
    expect(
      resizeView(fitted, size, to, { equalAspect: true, fitted: true })
    ).toBeNull()
    const kept = resizeView(deep, size, to, {
      equalAspect: true,
      fitted: false,
    })
    expect(kept && (kept.x1 - kept.x0) / to.width).toBeCloseTo(
      (deep.x1 - deep.x0) / size.width,
      12
    )
  })

  it("reads the pixel under the cursor and its value, none off the frame", () => {
    const frame = {
      data: Uint16Array.from([1, 2, 3, 4, 5, 6]),
      width: 3,
      height: 2,
    }
    const view = frameBounds(3, 2)
    const plot = { width: 300, height: 200 }
    expect(pixelAt(view, plot, 250, 150, frame)).toEqual({
      x: 2,
      y: 1,
      value: 6,
    })
    expect(pixelAt(view, plot, 10, 10, frame)).toEqual({ x: 0, y: 0, value: 1 })
    const away = panImage(view, plot, 290, 0)
    expect(pixelAt(away, plot, 5, 10, frame)).toBeNull()
  })

  it("draws only the frame's visible pixels, where the view puts them", () => {
    // The whole frame fills the plot.
    expect(visibleRect(frameBounds(100, 50), size, 100, 50)).toEqual({
      sx: 0,
      sy: 0,
      sw: 100,
      sh: 50,
      dx: 0,
      dy: 0,
      dw: 400,
      dh: 200,
    })
    // Zoomed in: whole source pixels that cover the view, starting just off the plot.
    const r = visibleRect({ x0: 10.2, y0: 5, x1: 20.2, y1: 10 }, size, 100, 50)
    expect(r && [r.sx, r.sy, r.sw, r.sh]).toEqual([10, 5, 11, 6])
    expect(
      r && [r.dx, r.dy, r.dw, r.dh].map((v) => Math.round(v * 1e6) / 1e6)
    ).toEqual([-28, -20, 440, 240])
    // Panned off the frame: nothing to draw.
    expect(
      visibleRect({ x0: 200, y0: 0, x1: 300, y1: 50 }, size, 100, 50)
    ).toBeNull()
  })
})

describe("the heatmap's plot box", () => {
  it("spans only the image, centred, so the axes stay over it", () => {
    const margin = { left: 56, top: 8, right: 12, bottom: 40 }
    // A square frame in a wide card: the box is square, centred across.
    expect(imageBox(868, 448, margin, frameBounds(2048, 2048))).toEqual({
      left: 56 + 200,
      top: 8,
      width: 400,
      height: 400,
    })
    // A wide frame in a tall card: centred down.
    expect(imageBox(468, 648, margin, frameBounds(200, 100))).toEqual({
      left: 56,
      top: 8 + 200,
      width: 400,
      height: 200,
    })
    // Without an image (nothing to fit), the whole area.
    expect(imageBox(468, 448, margin, null)).toEqual({
      left: 56,
      top: 8,
      width: 400,
      height: 400,
    })
  })
})
