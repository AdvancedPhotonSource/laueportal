// View transforms for the 2D scatter. A view is the data rectangle shown in a plot area of `width` ×
// `height` pixels, with y up: x0 at the left edge, y1 at the top.

export interface View {
  x0: number
  y0: number
  x1: number
  y1: number
}

export interface Size {
  width: number
  height: number
}

/** Data → plot pixels (continuous; the origin is the plot's top-left corner). */
export function toScreen(
  view: View,
  size: Size,
  x: number,
  y: number
): [number, number] {
  return [
    ((x - view.x0) * size.width) / (view.x1 - view.x0),
    ((view.y1 - y) * size.height) / (view.y1 - view.y0),
  ]
}

/** Plot pixels → data. */
export function toData(
  view: View,
  size: Size,
  px: number,
  py: number
): [number, number] {
  return [
    view.x0 + (px / size.width) * (view.x1 - view.x0),
    view.y1 - (py / size.height) * (view.y1 - view.y0),
  ]
}

/** The view after the content followed the pointer by (dx, dy) pixels. */
export function panView(view: View, size: Size, dx: number, dy: number): View {
  const ux = (dx * (view.x1 - view.x0)) / size.width
  const uy = (dy * (view.y1 - view.y0)) / size.height
  return {
    x0: view.x0 - ux,
    y0: view.y0 + uy,
    x1: view.x1 - ux,
    y1: view.y1 + uy,
  }
}

/** The narrowest and widest spans a zoom may reach. */
export interface ZoomLimits {
  minWidth: number
  maxWidth: number
  minHeight: number
  maxHeight: number
}

/** Zoom limits about a fitted view: up to `zoomIn` times in and `zoomOut` times out. */
export function zoomLimits(
  fitted: View,
  { zoomIn = 1e6, zoomOut = 1e3 }: { zoomIn?: number; zoomOut?: number } = {}
): ZoomLimits {
  const w = fitted.x1 - fitted.x0
  const h = fitted.y1 - fitted.y0
  return {
    minWidth: w / zoomIn,
    maxWidth: w * zoomOut,
    minHeight: h / zoomIn,
    maxHeight: h * zoomOut,
  }
}

/**
 * Zoom by `factor` (> 1 zooms in) about the pixel (px, py), which keeps its data point. With
 * `limits`, the factor is clamped so the spans stay within them and wider than a few thousand
 * ulps of the coordinates (below that, the view's arithmetic loses its precision); a zoom never
 * moves the other way.
 */
export function zoomView(
  view: View,
  size: Size,
  px: number,
  py: number,
  factor: number,
  limits?: ZoomLimits
): View {
  if (limits) factor = clampZoom(view, factor, limits)
  if (factor === 1 || !(factor > 0)) return view
  const [cx, cy] = toData(view, size, px, py)
  const k = 1 / factor
  return {
    x0: cx + (view.x0 - cx) * k,
    y0: cy + (view.y0 - cy) * k,
    x1: cx + (view.x1 - cx) * k,
    y1: cy + (view.y1 - cy) * k,
  }
}

/** Widen the narrower axis about the centre so one data unit spans the same pixels on both axes. */
export function equalizeAspect(view: View, size: Size): View {
  const w = view.x1 - view.x0
  const h = view.y1 - view.y0
  const perPixel = Math.max(w / size.width, h / size.height)
  return around(view, (perPixel * size.width) / 2, (perPixel * size.height) / 2)
}

/** The finite points' bounding box, or null when there is none. */
export function dataBounds(
  x: ArrayLike<number>,
  y: ArrayLike<number>
): View | null {
  let x0 = Infinity
  let y0 = Infinity
  let x1 = -Infinity
  let y1 = -Infinity
  for (let i = 0; i < x.length; i++) {
    const xi = x[i] as number
    const yi = y[i] as number
    if (!Number.isFinite(xi) || !Number.isFinite(yi)) continue
    if (xi < x0) x0 = xi
    if (xi > x1) x1 = xi
    if (yi < y0) y0 = yi
    if (yi > y1) y1 = yi
  }
  return x0 <= x1 ? { x0, y0, x1, y1 } : null
}

/**
 * The view that shows `bounds` with `padding` (a fraction of the span) on each side, plus `marginPx`
 * screen pixels (half a marker, so the edge markers are whole; at most a quarter of the plot); a zero
 * span becomes one unit. With `equalAspect`, the narrower axis widens about the centre.
 */
export function fitView(
  bounds: View | null,
  size: Size,
  {
    equalAspect = false,
    padding = 0.05,
    marginPx = 0,
  }: { equalAspect?: boolean; padding?: number; marginPx?: number } = {}
): View {
  const b = bounds ?? { x0: 0, y0: 0, x1: 1, y1: 1 }
  const w = b.x1 - b.x0 || 1
  const h = b.y1 - b.y0 || 1
  // The padded span then fills the plot less the margins: span / (1 - 2 margin / pixels).
  const widen = (pixels: number) =>
    pixels > 0
      ? 1 / (1 - 2 * Math.min(Math.max(marginPx, 0) / pixels, 0.25))
      : 1
  const view = around(
    b,
    (w / 2) * (1 + 2 * padding) * widen(size.width),
    (h / 2) * (1 + 2 * padding) * widen(size.height)
  )
  return equalAspect ? equalizeAspect(view, size) : view
}

/**
 * The view after the plot area changed size; null asks for a new fit. The automatic fit (`fitted`:
 * no pan or zoom since the last fit) fits again. The user's view keeps its centre and data units
 * per pixel with an equal aspect (so it shows more or less), and its range otherwise (it stretches).
 */
export function resizeView(
  view: View | null,
  from: Size,
  to: Size,
  { equalAspect, fitted }: { equalAspect: boolean; fitted: boolean }
): View | null {
  if (fitted || !view) return null
  if (!equalAspect || from.width <= 0) return view
  const perPixel = (view.x1 - view.x0) / from.width
  return around(view, (perPixel * to.width) / 2, (perPixel * to.height) / 2)
}

/**
 * The affine map from the pixels of a bitmap drawn under `rendered` to the pixels of `live`: draw
 * the cached bitmap with `setTransform(ax, 0, 0, ay, bx, by)` while a gesture runs.
 */
export function gestureTransform(
  rendered: View,
  live: View,
  size: Size
): { ax: number; bx: number; ay: number; by: number } {
  const liveW = live.x1 - live.x0
  const liveH = live.y1 - live.y0
  return {
    ax: (rendered.x1 - rendered.x0) / liveW,
    bx: ((rendered.x0 - live.x0) * size.width) / liveW,
    ay: (rendered.y1 - rendered.y0) / liveH,
    by: ((live.y1 - rendered.y1) * size.height) / liveH,
  }
}

/**
 * Tick values at 1, 2 or 5 × 10^k, at most about `count` of them, within [min, max]. Bounded on
 * its own: none when the step is below the coordinates' precision, and never more than 3 × count.
 */
export function niceTicks(
  min: number,
  max: number,
  count = 5
): { values: number[]; step: number } {
  const none = { values: [], step: 0 }
  const span = max - min
  if (!(span > 0) || !Number.isFinite(span)) return none
  const raw = span / Math.max(1, count)
  const power = 10 ** Math.floor(Math.log10(raw))
  const step = ([1, 2, 5, 10].find((m) => m * power >= raw) ?? 10) * power
  if (Math.max(Math.abs(min), Math.abs(max)) / step > 2 ** 52) return none
  const values: number[] = []
  const limit = 3 * Math.max(1, count)
  for (
    let k = Math.ceil(min / step);
    k * step <= max + step * 1e-9 && values.length < limit;
    k++
  ) {
    values.push(k * step)
  }
  return { values, step }
}

/** A tick label with as many decimals as the step needs (exponential for extreme magnitudes). */
export function formatTick(value: number, step: number): string {
  const magnitude = Math.max(Math.abs(value), step)
  const stepPower = Math.floor(Math.log10(step) + 1e-9)
  if (magnitude >= 1e6 || magnitude < 1e-3) {
    // Enough digits to tell neighbouring ticks apart at any zoom.
    const digits = Math.floor(Math.log10(magnitude) + 1e-9) - stepPower
    return value.toExponential(Math.min(20, Math.max(1, digits)))
  }
  const decimals = Math.min(20, Math.max(0, -stepPower))
  const text = value.toFixed(decimals)
  return Number(text) === 0 ? (0).toFixed(decimals) : text
}

function clampZoom(view: View, factor: number, limits: ZoomLimits): number {
  const w = view.x1 - view.x0
  const h = view.y1 - view.y0
  // A few thousand ulps of the coordinates: narrower spans lose their precision.
  const ulps = (a: number, b: number) =>
    (Math.abs(a) + Math.abs(b)) * Number.EPSILON * 2048
  const minW = Math.max(limits.minWidth, ulps(view.x0, view.x1))
  const minH = Math.max(limits.minHeight, ulps(view.y0, view.y1))
  if (factor > 1) return Math.max(1, Math.min(factor, w / minW, h / minH))
  return Math.min(
    1,
    Math.max(factor, w / limits.maxWidth, h / limits.maxHeight)
  )
}

function around(view: View, halfWidth: number, halfHeight: number): View {
  const cx = (view.x0 + view.x1) / 2
  const cy = (view.y0 + view.y1) / 2
  return {
    x0: cx - halfWidth,
    y0: cy - halfHeight,
    x1: cx + halfWidth,
    y1: cy + halfHeight,
  }
}
