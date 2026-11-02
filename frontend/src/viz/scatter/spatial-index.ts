// A uniform grid over the points in data space (for hover): built once per data set, it finds the
// nearest point within a few pixels at any zoom by visiting only the cells near the cursor.
import { toData, type Size, type View } from "./view"

export interface GridIndex {
  x0: number
  y0: number
  cellW: number
  cellH: number
  nx: number
  ny: number
  /** Cell c holds items[start[c] .. start[c + 1]) (point indices, ascending). */
  start: Int32Array
  items: Int32Array
}

const MAX_CELLS_PER_SIDE = 2048

/**
 * Index the finite points, about `perCell` per cell. It depends on the coordinates only, so a
 * recolour (or hiding points) keeps it; `nearestPoint` skips the hidden points.
 */
export function buildGridIndex(
  x: ArrayLike<number>,
  y: ArrayLike<number>,
  perCell = 4
): GridIndex {
  const n = x.length
  let x0 = Infinity
  let y0 = Infinity
  let x1 = -Infinity
  let y1 = -Infinity
  let count = 0
  for (let i = 0; i < n; i++) {
    if (!finite(x, y, i)) continue
    const xi = x[i] as number
    const yi = y[i] as number
    if (xi < x0) x0 = xi
    if (xi > x1) x1 = xi
    if (yi < y0) y0 = yi
    if (yi > y1) y1 = yi
    count++
  }
  if (count === 0) {
    return {
      x0: 0,
      y0: 0,
      cellW: 1,
      cellH: 1,
      nx: 1,
      ny: 1,
      start: new Int32Array(2),
      items: new Int32Array(0),
    }
  }
  const spanX = x1 - x0 || 1
  const spanY = y1 - y0 || 1
  const cells = Math.max(1, count / perCell)
  const nx = clamp(
    Math.round(Math.sqrt((cells * spanX) / spanY)),
    1,
    MAX_CELLS_PER_SIDE
  )
  const ny = clamp(Math.round(cells / nx), 1, MAX_CELLS_PER_SIDE)
  const grid = { x0, y0, cellW: spanX / nx, cellH: spanY / ny, nx, ny }
  // A counting sort by cell keeps each cell's points in ascending order.
  const cellOf = new Int32Array(n).fill(-1)
  const start = new Int32Array(nx * ny + 1)
  for (let i = 0; i < n; i++) {
    if (!finite(x, y, i)) continue
    const c = cellIndex(grid, x[i] as number, y[i] as number)
    cellOf[i] = c
    start[c + 1] = (start[c + 1] as number) + 1
  }
  for (let c = 0; c < nx * ny; c++)
    start[c + 1] = (start[c + 1] as number) + (start[c] as number)
  const fill = start.slice(0, nx * ny)
  const items = new Int32Array(count)
  for (let i = 0; i < n; i++) {
    const c = cellOf[i] as number
    if (c < 0) continue
    items[fill[c] as number] = i
    fill[c] = (fill[c] as number) + 1
  }
  return { ...grid, start, items }
}

/**
 * The index of the point nearest the plot pixel (px, py) under `view`, within `maxPx` pixels, or
 * -1; points with `mask[i] === 0` (hidden) are skipped. Of equally near points, the last one wins
 * (it is drawn on top).
 */
export function nearestPoint(
  index: GridIndex,
  x: ArrayLike<number>,
  y: ArrayLike<number>,
  view: View,
  size: Size,
  px: number,
  py: number,
  maxPx: number,
  mask?: ArrayLike<number> | null
): number {
  const [cx, cy] = toData(view, size, px, py)
  const sx = size.width / (view.x1 - view.x0)
  const sy = size.height / (view.y1 - view.y0)
  const rx = maxPx / sx
  const ry = maxPx / sy
  const i0 = clamp(
    Math.floor((cx - rx - index.x0) / index.cellW),
    0,
    index.nx - 1
  )
  const i1 = clamp(
    Math.floor((cx + rx - index.x0) / index.cellW),
    0,
    index.nx - 1
  )
  const j0 = clamp(
    Math.floor((cy - ry - index.y0) / index.cellH),
    0,
    index.ny - 1
  )
  const j1 = clamp(
    Math.floor((cy + ry - index.y0) / index.cellH),
    0,
    index.ny - 1
  )
  let best = -1
  let bestD = maxPx * maxPx
  for (let j = j0; j <= j1; j++) {
    for (let i = i0; i <= i1; i++) {
      const c = j * index.nx + i
      for (
        let k = index.start[c] as number;
        k < (index.start[c + 1] as number);
        k++
      ) {
        const p = index.items[k] as number
        if (mask && mask[p] === 0) continue
        const dx = ((x[p] as number) - cx) * sx
        const dy = ((y[p] as number) - cy) * sy
        const d = dx * dx + dy * dy
        if (d < bestD || (d === bestD && p > best)) {
          bestD = d
          best = p
        }
      }
    }
  }
  return best
}

function cellIndex(
  grid: {
    x0: number
    y0: number
    cellW: number
    cellH: number
    nx: number
    ny: number
  },
  x: number,
  y: number
): number {
  const i = Math.min(grid.nx - 1, Math.floor((x - grid.x0) / grid.cellW))
  const j = Math.min(grid.ny - 1, Math.floor((y - grid.y0) / grid.cellH))
  return j * grid.nx + i
}

function finite(
  x: ArrayLike<number>,
  y: ArrayLike<number>,
  i: number
): boolean {
  return Number.isFinite(x[i]) && Number.isFinite(y[i])
}

function clamp(value: number, min: number, max: number): number {
  return value < min ? min : value > max ? max : value
}
