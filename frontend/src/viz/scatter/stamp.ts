// CPU point stamping for the 2D scatter: colours are packed once per point, and each marker is a
// precomputed list of row spans filled into the RGBA pixel buffer seen as a Uint32Array
// (little-endian: 0xAABBGGRR).
import type { Size, View } from "./view"

export type MarkerShape = "square" | "circle"

/** The colour of the points that are not indexed ("transparent" hides them). */
export type NonIndexedColor = "gray" | "red" | "blue" | "green" | "transparent"

export const NON_INDEXED_COLORS: readonly NonIndexedColor[] = [
  "gray",
  "red",
  "blue",
  "green",
  "transparent",
]

// Data colours.
const NON_INDEXED_RGB: Record<
  Exclude<NonIndexedColor, "transparent">,
  number
> = {
  gray: 0x969696,
  red: 0xdc2828,
  blue: 0x285adc,
  green: 0x28a03c,
}

/**
 * Per-point colours: r, g, b bytes (the products' uint8 columns), or a scalar per point mapped
 * by a lookup the caller supplies (the colorbar's): 0xRRGGBB, or a negative number to hide it.
 */
export type ScatterColors =
  | {
      kind: "rgb"
      r: ArrayLike<number>
      g: ArrayLike<number>
      b: ArrayLike<number>
    }
  | {
      kind: "lookup"
      values: ArrayLike<number>
      lookup: (value: number) => number
    }

/** What a `ScatterColors` holds: memoize on these, not on the wrapper object. */
export type ColorParts = readonly [
  ArrayLike<number>,
  ArrayLike<number> | ((value: number) => number),
  ArrayLike<number> | null,
]

export function colorParts(colors: ScatterColors): ColorParts {
  return colors.kind === "rgb"
    ? [colors.r, colors.g, colors.b]
    : [colors.values, colors.lookup, null]
}

export function colorsFromParts([a, b, c]: ColorParts): ScatterColors {
  return typeof b === "function"
    ? { kind: "lookup", values: a, lookup: b }
    : { kind: "rgb", r: a, g: b, b: c ?? b }
}

/** One opaque RGBA pixel as the buffer stores it (0 is a transparent pixel). */
export function packRgb(rgb: number, alpha = 255): number {
  return (
    ((alpha << 24) |
      ((rgb & 0xff) << 16) |
      (rgb & 0xff00) |
      ((rgb >> 16) & 0xff)) >>>
    0
  )
}

/** The same colour at another alpha (the dimmed points). */
export function withAlpha(packed: number, alpha: number): number {
  return ((packed & 0x00ffffff) | (alpha << 24)) >>> 0
}

/**
 * Every point's packed colour; a non-indexed point (`indexed[i] === 0`) takes `nonIndexed`, and a
 * hidden one is 0, which the stamping, picking and selection skip.
 */
export function packColors(
  colors: ScatterColors,
  count: number,
  indexed?: ArrayLike<number> | null,
  nonIndexed: NonIndexedColor = "gray"
): Uint32Array {
  const out = new Uint32Array(count)
  if (colors.kind === "rgb") {
    const { r, g, b } = colors
    for (let i = 0; i < count; i++) {
      out[i] =
        ((255 << 24) |
          ((b[i] as number) << 16) |
          ((g[i] as number) << 8) |
          (r[i] as number)) >>>
        0
    }
  } else {
    const { values, lookup } = colors
    for (let i = 0; i < count; i++) {
      const rgb = lookup(values[i] as number)
      out[i] = rgb < 0 ? 0 : packRgb(rgb)
    }
  }
  if (indexed) {
    const other =
      nonIndexed === "transparent" ? 0 : packRgb(NON_INDEXED_RGB[nonIndexed])
    for (let i = 0; i < count; i++) if (indexed[i] === 0) out[i] = other
  }
  return out
}

/**
 * The row spans of a marker `size` pixels wide, as (dy, x start, x end) triples about the point's
 * pixel; a circle keeps the pixels whose centres lie inside it.
 */
export function markerSpans(size: number, shape: MarkerShape): Int16Array {
  const s = Math.max(1, Math.round(size))
  const r = s / 2
  const c = s % 2 === 1 ? 0.5 : 0 // the marker's centre within the point's pixel
  const out: number[] = []
  for (let dy = -Math.floor(r); dy < Math.ceil(r); dy++) {
    if (shape === "square") {
      out.push(dy, -Math.floor(r), Math.ceil(r))
      continue
    }
    const yc = dy + 0.5 - c
    const half = Math.sqrt(Math.max(0, r * r - yc * yc))
    const [a, b] = rowRange(c, half)
    if (a < b) out.push(dy, a, b)
  }
  return Int16Array.from(out)
}

/** The row spans of a circular ring `outer` pixels across with a `width`-pixel line. */
export function ringSpans(outer: number, width: number): Int16Array {
  const s = Math.max(1, Math.round(outer))
  const r = s / 2
  const inner = Math.max(0, r - width)
  const c = s % 2 === 1 ? 0.5 : 0
  const out: number[] = []
  for (let dy = -Math.floor(r); dy < Math.ceil(r); dy++) {
    const yc = dy + 0.5 - c
    const [a, b] = rowRange(c, Math.sqrt(Math.max(0, r * r - yc * yc)))
    if (a >= b) continue
    if (Math.abs(yc) >= inner) {
      out.push(dy, a, b)
      continue
    }
    const [ia, ib] = rowRange(c, Math.sqrt(inner * inner - yc * yc))
    if (a < ia) out.push(dy, a, ia)
    if (ib < b) out.push(dy, ib, b)
  }
  return Int16Array.from(out)
}

/** Fill `spans` about the pixel (px, py) with `color`, clipped to the w × h buffer. */
export function stampSpans(
  buf: Uint32Array,
  w: number,
  h: number,
  spans: Int16Array,
  px: number,
  py: number,
  color: number
): void {
  for (let k = 0; k < spans.length; k += 3) {
    const yy = py + (spans[k] as number)
    if (yy < 0 || yy >= h) continue
    const a = Math.max(0, px + (spans[k + 1] as number))
    const b = Math.min(w, px + (spans[k + 2] as number))
    if (a < b) buf.fill(color, yy * w + a, yy * w + b)
  }
}

export interface StampOptions {
  /** Marker size in buffer pixels. */
  size: number
  shape: MarkerShape
  /** With a selection (1 = selected), the others are dimmed and the selected ringed. */
  selected?: Uint8Array | null
  /** The dimmed points' alpha (opacity 0.2). */
  dimAlpha?: number
  /** The ring's packed colour, outer size and line width (buffer pixels). */
  ringColor?: number
  ringSize?: number
  ringWidth?: number
}

/**
 * Clear the w × h buffer and stamp every visible point under `view`; a point whose colour is 0 or
 * whose coordinates are not finite is skipped.
 */
export function drawPoints(
  buf: Uint32Array,
  size: Size,
  view: View,
  x: ArrayLike<number>,
  y: ArrayLike<number>,
  colors: Uint32Array,
  options: StampOptions
): void {
  const { width: w, height: h } = size
  const {
    selected = null,
    dimAlpha = 51,
    ringColor = 0xff000000,
    ringWidth = 2,
  } = options
  const spans = markerSpans(options.size, options.shape)
  const ringSize = options.ringSize ?? options.size + 8
  const sx = w / (view.x1 - view.x0)
  const sy = h / (view.y1 - view.y0)
  const { x0, y1 } = view
  const margin =
    Math.ceil(Math.max(options.size, selected ? ringSize : 0) / 2) + 1
  buf.fill(0)
  for (let pass = 0; pass < (selected ? 3 : 1); pass++) {
    // Pass 0: the unselected points (dimmed under a selection); 1: the selected points' rings;
    // 2: the selected points, on top, so a dense selection keeps its colours inside its outline.
    const ring = pass === 1 ? ringSpans(ringSize, ringWidth) : spans
    for (let i = 0; i < colors.length; i++) {
      const color = colors[i] as number
      if (color === 0) continue
      if (selected && (selected[i] === 1) !== pass > 0) continue
      const fx = ((x[i] as number) - x0) * sx
      const fy = (y1 - (y[i] as number)) * sy
      if (!(
        fx >= -margin &&
        fx <= w + margin &&
        fy >= -margin &&
        fy <= h + margin
      ))
        continue
      const paint =
        pass === 1
          ? ringColor
          : pass === 0 && selected
            ? withAlpha(color, dimAlpha)
            : color
      stampSpans(buf, w, h, ring, Math.floor(fx), Math.floor(fy), paint)
    }
  }
}

function rowRange(c: number, half: number): [number, number] {
  // The pixels k whose centres k + 0.5 lie within [c - half, c + half].
  return [Math.ceil(c - half - 0.5), Math.floor(c + half - 0.5) + 1]
}
