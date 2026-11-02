// Lasso and box selection in data space, on the CPU: a bounding-box test first, then even-odd
// point-in-polygon. Points with `mask[i] === 0` (hidden) are never selected.

/** Whether (x, y) lies inside the polygon given as flat [x0, y0, x1, y1, …] (even-odd rule). */
export function pointInPolygon(
  x: number,
  y: number,
  polygon: ArrayLike<number>
): boolean {
  let inside = false
  const n = polygon.length >> 1
  for (let j = 0, k = n - 1; j < n; k = j++) {
    const xa = polygon[2 * j] as number
    const ya = polygon[2 * j + 1] as number
    const xb = polygon[2 * k] as number
    const yb = polygon[2 * k + 1] as number
    if (ya > y !== yb > y && x < ((xb - xa) * (y - ya)) / (yb - ya) + xa)
      inside = !inside
  }
  return inside
}

/** The indices of the points inside a data-space polygon (flat [x0, y0, …]), ascending. */
export function lassoSelect(
  x: ArrayLike<number>,
  y: ArrayLike<number>,
  polygon: ArrayLike<number>,
  mask?: ArrayLike<number> | null
): Int32Array {
  if (polygon.length < 6) return new Int32Array(0)
  let bx0 = Infinity
  let by0 = Infinity
  let bx1 = -Infinity
  let by1 = -Infinity
  for (let k = 0; k + 1 < polygon.length; k += 2) {
    const px = polygon[k] as number
    const py = polygon[k + 1] as number
    bx0 = Math.min(bx0, px)
    bx1 = Math.max(bx1, px)
    by0 = Math.min(by0, py)
    by1 = Math.max(by1, py)
  }
  const out: number[] = []
  for (let i = 0; i < x.length; i++) {
    if (mask && mask[i] === 0) continue
    const xi = x[i] as number
    const yi = y[i] as number
    if (!(xi >= bx0 && xi <= bx1 && yi >= by0 && yi <= by1)) continue
    if (pointInPolygon(xi, yi, polygon)) out.push(i)
  }
  return Int32Array.from(out)
}

/** The indices of the points inside the data rectangle between two corners (edges included). */
export function boxSelect(
  x: ArrayLike<number>,
  y: ArrayLike<number>,
  corners: { x0: number; y0: number; x1: number; y1: number },
  mask?: ArrayLike<number> | null
): Int32Array {
  const xlo = Math.min(corners.x0, corners.x1)
  const xhi = Math.max(corners.x0, corners.x1)
  const ylo = Math.min(corners.y0, corners.y1)
  const yhi = Math.max(corners.y0, corners.y1)
  const out: number[] = []
  for (let i = 0; i < x.length; i++) {
    if (mask && mask[i] === 0) continue
    const xi = x[i] as number
    const yi = y[i] as number
    if (xi >= xlo && xi <= xhi && yi >= ylo && yi <= yhi) out.push(i)
  }
  return Int32Array.from(out)
}
