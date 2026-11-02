// The 3D view's per-point data, as plain arrays for VTK.js: which points are drawn, their
// positions and colours, and the linked selection mapped onto them (the selected ringed, the
// others dimmed, as the 2D views do).

/** A dimmed point shows its colour at this alpha over the background (opacity 0.2). */
export const DIM_ALPHA = 0.2

/** The points to draw: a visible colour (alpha > 0 in the packed colour) and finite x, y, z. */
export function drawnPoints(
  packed: Uint32Array,
  x: ArrayLike<number>,
  y: ArrayLike<number>,
  z: ArrayLike<number>
): Int32Array {
  const out = new Int32Array(packed.length)
  let n = 0
  for (let i = 0; i < packed.length; i++) {
    if (
      (packed[i] as number) >>> 24 !== 0 &&
      Number.isFinite(x[i]) &&
      Number.isFinite(y[i]) &&
      Number.isFinite(z[i])
    )
      out[n++] = i
  }
  return out.slice(0, n)
}

/** x, y, z interleaved (VTK.js's points). */
export function interleave(
  x: ArrayLike<number>,
  y: ArrayLike<number>,
  z: ArrayLike<number>
): Float32Array {
  const out = new Float32Array(3 * x.length)
  for (let i = 0; i < x.length; i++) {
    out[3 * i] = x[i] as number
    out[3 * i + 1] = y[i] as number
    out[3 * i + 2] = z[i] as number
  }
  return out
}

/** [xmin, xmax, ymin, ymax, zmin, zmax] */
export type Bounds = [number, number, number, number, number, number]

/** The bounds of the drawn points; null without points. */
export function boundsOf(xyz: Float32Array, ids: Int32Array): Bounds | null {
  if (ids.length === 0) return null
  const b: Bounds = [
    Infinity,
    -Infinity,
    Infinity,
    -Infinity,
    Infinity,
    -Infinity,
  ]
  for (const i of ids) {
    for (let k = 0; k < 3; k++) {
      const v = xyz[3 * i + k] as number
      if (v < (b[2 * k] as number)) b[2 * k] = v
      if (v > (b[2 * k + 1] as number)) b[2 * k + 1] = v
    }
  }
  return b
}

/** VTK.js vertex cells for these points: [1, id, 1, id, …]. */
export function vertexCells(ids: Int32Array): Uint32Array {
  const out = new Uint32Array(2 * ids.length)
  for (let k = 0; k < ids.length; k++) {
    out[2 * k] = 1
    out[2 * k + 1] = ids[k] as number
  }
  return out
}

/**
 * Per point r, g, b bytes from the packed colours (0xAABBGGRR, as the scatter packs them). With a
 * selection (1 = selected), the other points are dimmed: their colour at DIM_ALPHA over the
 * background (opaque, so depth sorting stays exact).
 */
export function pointRgb(
  packed: Uint32Array,
  selected: Uint8Array | null,
  background: readonly [number, number, number]
): Uint8Array {
  const out = new Uint8Array(3 * packed.length)
  const [br, bg, bb] = background
  for (let i = 0; i < packed.length; i++) {
    const c = packed[i] as number
    const r = c & 0xff
    const g = (c >>> 8) & 0xff
    const b = (c >>> 16) & 0xff
    if (selected && !selected[i]) {
      out[3 * i] = Math.round(r * DIM_ALPHA + br * (1 - DIM_ALPHA))
      out[3 * i + 1] = Math.round(g * DIM_ALPHA + bg * (1 - DIM_ALPHA))
      out[3 * i + 2] = Math.round(b * DIM_ALPHA + bb * (1 - DIM_ALPHA))
    } else {
      out[3 * i] = r
      out[3 * i + 1] = g
      out[3 * i + 2] = b
    }
  }
  return out
}

/** The drawn points that are selected (they get a ring). */
export function ringedPoints(
  drawn: Int32Array,
  selected: Uint8Array | null
): Int32Array {
  if (!selected) return new Int32Array(0)
  return drawn.filter((i) => selected[i] === 1)
}
