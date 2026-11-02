// View transforms for the heatmap. Image coordinates follow the detector convention: pixel
// centres at integers, (0, 0) at the top left and y down, so a view's y0 is its top edge. Pan,
// zoom, fit and resize reuse the scatter's view.ts (its limits and its "still fitted" rule); only
// the y direction is flipped here.
import {
  panView,
  zoomLimits,
  zoomView,
  type Size,
  type View,
  type ZoomLimits,
} from "../scatter/view"
import type { PlotBox } from "../axes"
import type { Frame } from "./frame"

/** The frame's extent: every pixel whole, [-0.5, width - 0.5] × [-0.5, height - 0.5]. */
export function frameBounds(width: number, height: number): View {
  return { x0: -0.5, y0: -0.5, x1: width - 0.5, y1: height - 0.5 }
}

/**
 * The plot box in a canvas of `width` × `height` less the margins, shrunk to the image's aspect and
 * centred, so the axes span only the image; the whole area without an image.
 */
export function imageBox(
  width: number,
  height: number,
  margin: { left: number; top: number; right: number; bottom: number },
  bounds: View | null
): PlotBox {
  const areaWidth = Math.max(1, width - margin.left - margin.right)
  const areaHeight = Math.max(1, height - margin.top - margin.bottom)
  const aspect = bounds ? (bounds.x1 - bounds.x0) / (bounds.y1 - bounds.y0) : 0
  if (!(aspect > 0 && Number.isFinite(aspect))) {
    return {
      left: margin.left,
      top: margin.top,
      width: areaWidth,
      height: areaHeight,
    }
  }
  const boxWidth = Math.max(
    1,
    Math.min(areaWidth, Math.round(areaHeight * aspect))
  )
  const boxHeight = Math.max(
    1,
    Math.min(areaHeight, Math.round(areaWidth / aspect))
  )
  return {
    left: margin.left + Math.floor((areaWidth - boxWidth) / 2),
    top: margin.top + Math.floor((areaHeight - boxHeight) / 2),
    width: boxWidth,
    height: boxHeight,
  }
}

/** Image → plot pixels. */
export function imageToScreen(
  view: View,
  size: Size,
  x: number,
  y: number
): [number, number] {
  return [
    ((x - view.x0) * size.width) / (view.x1 - view.x0),
    ((y - view.y0) * size.height) / (view.y1 - view.y0),
  ]
}

/** Plot pixels → image. */
export function screenToImage(
  view: View,
  size: Size,
  px: number,
  py: number
): [number, number] {
  return [
    view.x0 + (px / size.width) * (view.x1 - view.x0),
    view.y0 + (py / size.height) * (view.y1 - view.y0),
  ]
}

/** The view after the image followed the pointer by (dx, dy) pixels. */
export function panImage(view: View, size: Size, dx: number, dy: number): View {
  return panView(view, size, dx, -dy)
}

/** Zoom by `factor` about the plot pixel (px, py), which keeps its image point. */
export function zoomImage(
  view: View,
  size: Size,
  px: number,
  py: number,
  factor: number,
  limits?: ZoomLimits
): View {
  return zoomView(view, size, px, size.height - py, factor, limits)
}

/** The image's zoom limits: in until about 4 pixels fill the plot, out to 8 times the fit. */
export function imageZoomLimits(fitted: View): ZoomLimits {
  const span = Math.max(fitted.x1 - fitted.x0, fitted.y1 - fitted.y0)
  return zoomLimits(fitted, { zoomIn: Math.max(1, span / 4), zoomOut: 8 })
}

/** The pixel under the plot pixel (px, py) and its value; null off the frame. */
export function pixelAt(
  view: View,
  size: Size,
  px: number,
  py: number,
  frame: Frame
): { x: number; y: number; value: number } | null {
  const [ix, iy] = screenToImage(view, size, px, py)
  const x = Math.round(ix) || 0
  const y = Math.round(iy) || 0
  if (x < 0 || y < 0 || x >= frame.width || y >= frame.height) return null
  return { x, y, value: frame.data[y * frame.width + x] as number }
}

/**
 * The part of a `width` × `height` image the view shows, in whole source pixels (`s*`, for
 * drawImage), and where it lands in the plot (`d*`, plot pixels); null when none of it shows.
 */
export function visibleRect(
  view: View,
  size: Size,
  width: number,
  height: number
): {
  sx: number
  sy: number
  sw: number
  sh: number
  dx: number
  dy: number
  dw: number
  dh: number
} | null {
  // Source pixel i covers image coordinates [i - 0.5, i + 0.5).
  const sx = Math.max(0, Math.floor(view.x0 + 0.5))
  const sy = Math.max(0, Math.floor(view.y0 + 0.5))
  const ex = Math.min(width, Math.ceil(view.x1 + 0.5))
  const ey = Math.min(height, Math.ceil(view.y1 + 0.5))
  if (ex <= sx || ey <= sy) return null
  const kx = size.width / (view.x1 - view.x0)
  const ky = size.height / (view.y1 - view.y0)
  return {
    sx,
    sy,
    sw: ex - sx,
    sh: ey - sy,
    dx: (sx - 0.5 - view.x0) * kx,
    dy: (sy - 0.5 - view.y0) * ky,
    dw: (ex - sx) * kx,
    dh: (ey - sy) * ky,
  }
}
