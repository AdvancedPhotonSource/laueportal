// CPU picking for the 3D view, chosen over VTK.js's hardware selector (vtkOpenGLHardwareSelector,
// which renders the scene again and reads pixels back on every pick): the drawn points are
// projected to the screen once per camera or size change and binned in a grid, and a hover looks
// up only the cells around the cursor.

/** The drawn points on screen, binned in a grid of square cells. */
export interface ScreenPoints {
  /** The data index of each projected point. */
  ids: Int32Array
  /** CSS pixels from the view's top left, and the depth (NDC z: smaller is nearer). */
  sx: Float32Array
  sy: Float32Array
  sz: Float32Array
  count: number
  cell: number
  cols: number
  rows: number
  /** Per cell, its first slot in `order` (cols × rows + 1 entries). */
  start: Int32Array
  /** Projected points (slots into ids, sx, sy, sz) grouped by cell. */
  order: Int32Array
}

/** Off-screen points closer than this (CSS px) may still show a part of their marker. */
const EDGE_PX = 64

/**
 * Project the drawn points (`ids`, into the interleaved `xyz`) with the camera's world-to-clip
 * matrix (row-major, as VTK.js's `camera.getCompositeProjectionMatrix(aspect, -1, 1)`) onto a
 * width × height view. Points behind the camera, outside the depth range or far off-screen are
 * dropped.
 */
export function projectPoints(
  xyz: ArrayLike<number>,
  ids: Int32Array,
  m: ArrayLike<number>,
  width: number,
  height: number,
  cell = 32
): ScreenPoints {
  const n = ids.length
  const kept = new Int32Array(n)
  const sx = new Float32Array(n)
  const sy = new Float32Array(n)
  const sz = new Float32Array(n)
  const [
    m0 = 0,
    m1 = 0,
    m2 = 0,
    m3 = 0,
    m4 = 0,
    m5 = 0,
    m6 = 0,
    m7 = 0,
    m8 = 0,
    m9 = 0,
    m10 = 0,
    m11 = 0,
    m12 = 0,
    m13 = 0,
    m14 = 0,
    m15 = 0,
  ] = Array.from(m)
  let count = 0
  for (let k = 0; k < n; k++) {
    const i = ids[k] as number
    const x = xyz[3 * i] as number
    const y = xyz[3 * i + 1] as number
    const z = xyz[3 * i + 2] as number
    const w = m12 * x + m13 * y + m14 * z + m15
    if (!(w > 0)) continue
    const nz = (m8 * x + m9 * y + m10 * z + m11) / w
    if (!(nz >= -1 && nz <= 1)) continue
    const px = (((m0 * x + m1 * y + m2 * z + m3) / w + 1) / 2) * width
    const py = ((1 - (m4 * x + m5 * y + m6 * z + m7) / w) / 2) * height
    if (
      !(px >= -EDGE_PX && px <= width + EDGE_PX) ||
      !(py >= -EDGE_PX && py <= height + EDGE_PX)
    )
      continue
    kept[count] = i
    sx[count] = px
    sy[count] = py
    sz[count] = nz
    count++
  }
  const cols = Math.max(1, Math.ceil(width / cell))
  const rows = Math.max(1, Math.ceil(height / cell))
  const start = new Int32Array(cols * rows + 1)
  const cellOf = new Int32Array(count)
  for (let s = 0; s < count; s++) {
    const c =
      clamp(Math.floor((sy[s] as number) / cell), rows) * cols +
      clamp(Math.floor((sx[s] as number) / cell), cols)
    cellOf[s] = c
    start[c + 1] = (start[c + 1] as number) + 1
  }
  for (let c = 0; c < cols * rows; c++)
    start[c + 1] = (start[c + 1] as number) + (start[c] as number)
  const fill = start.slice(0, cols * rows)
  const order = new Int32Array(count)
  for (let s = 0; s < count; s++) {
    const c = cellOf[s] as number
    order[fill[c] as number] = s
    fill[c] = (fill[c] as number) + 1
  }
  return {
    ids: kept.subarray(0, count),
    sx: sx.subarray(0, count),
    sy: sy.subarray(0, count),
    sz: sz.subarray(0, count),
    count,
    cell,
    cols,
    rows,
    start,
    order,
  }
}

/**
 * The point under the cursor (CSS px): the front-most one whose square marker (`half` its side)
 * covers it, which is the one drawn there (a point marked in `top`, by data index, is drawn over
 * every other); otherwise the nearest centre within `radius`, the front-most on a tie. -1 when
 * there is none.
 */
export function pickPoint(
  screen: ScreenPoints,
  x: number,
  y: number,
  half: number,
  radius: number,
  top: Uint8Array | null = null
): number {
  const { sx, sy, sz, cell, cols, rows, start, order } = screen
  const reach = Math.max(half, radius)
  const c0 = clamp(Math.floor((x - reach) / cell), cols)
  const c1 = clamp(Math.floor((x + reach) / cell), cols)
  const r0 = clamp(Math.floor((y - reach) / cell), rows)
  const r1 = clamp(Math.floor((y + reach) / cell), rows)
  let covering = -1
  let coveringZ = Infinity
  let nearest = -1
  let nearestD = radius * radius
  let nearestZ = Infinity
  for (let r = r0; r <= r1; r++) {
    for (let c = c0; c <= c1; c++) {
      const cellIndex = r * cols + c
      const end = start[cellIndex + 1] as number
      for (let k = start[cellIndex] as number; k < end; k++) {
        const s = order[k] as number
        const dx = (sx[s] as number) - x
        const dy = (sy[s] as number) - y
        const z = sz[s] as number
        // NDC depth is within [-1, 1]: a point drawn on top ranks before every other.
        const rank = top?.[screen.ids[s] as number] ? z - 4 : z
        if (Math.abs(dx) <= half && Math.abs(dy) <= half && rank < coveringZ) {
          covering = s
          coveringZ = rank
        }
        const d = dx * dx + dy * dy
        if (d < nearestD || (d === nearestD && z < nearestZ)) {
          nearest = s
          nearestD = d
          nearestZ = z
        }
      }
    }
  }
  const slot = covering >= 0 ? covering : nearest
  return slot >= 0 ? (screen.ids[slot] as number) : -1
}

function clamp(index: number, size: number): number {
  return index < 0 ? 0 : index >= size ? size - 1 : index
}
