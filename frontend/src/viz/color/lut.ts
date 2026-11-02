// Colour lookup. A frame is coloured through a 65,536-entry table: for uint16 the table is indexed
// by the value itself; a float32 frame's values are first quantized to 65,536 steps across the range
// (in log space for the log scale). A range or scale change rebuilds only the table (65,536 entries)
// and then recolours the frame with one lookup per pixel: nothing else in the frame is computed
// again.
import { normalizer, powerOf, type ColorScale } from "./scale"

export const LUT_SIZE = 65536
const TOP = LUT_SIZE - 1

/** The colormap entry (0–255) for a position t in [0, 1]: equal-width bins, as matplotlib. */
export function colorIndex(t: number): number {
  return t >= 1 ? 255 : t > 0 ? Math.floor(t * 256) : 0
}

/** A uint16 frame's table: value → the packed pixel (`colormapPixels`) of its colour. */
export function uint16Lut(
  colors: Uint32Array,
  lo: number,
  hi: number,
  scale: ColorScale,
  out: Uint32Array = new Uint32Array(LUT_SIZE)
): Uint32Array {
  const f = normalizer(lo, hi, scale)
  for (let v = 0; v < LUT_SIZE; v++) {
    out[v] = colors[colorIndex(f(v))] as number
  }
  return out
}

/** How a float32 frame's values become table indices: (value − a) × k, or (ln value − a) × k. */
export interface Quantizer {
  a: number
  k: number
  log: boolean
}

/** A float32 frame's table and quantizer for the range and scale. */
export function float32Lut(
  colors: Uint32Array,
  lo: number,
  hi: number,
  scale: ColorScale,
  out: Uint32Array = new Uint32Array(LUT_SIZE)
): { lut: Uint32Array; quantizer: Quantizer } {
  const log = scale.kind === "log" && lo > 0 && hi > lo
  const power = log ? 1 : powerOf(scale)
  for (let i = 0; i < LUT_SIZE; i++) {
    const t = i / TOP
    out[i] = colors[colorIndex(power === 1 ? t : t ** power)] as number
  }
  const quantizer = log
    ? { a: Math.log(lo), k: TOP / (Math.log(hi) - Math.log(lo)), log }
    : { a: lo, k: hi > lo ? TOP / (hi - lo) : 0, log }
  return { lut: out, quantizer }
}

/** Recolour a uint16 frame: one lookup per pixel. */
export function colorizeUint16(
  data: Uint16Array,
  lut: Uint32Array,
  out: Uint32Array
): void {
  for (let i = 0; i < data.length; i++)
    out[i] = lut[data[i] as number] as number
}

/** Recolour a float32 frame; NaN pixels are transparent. */
export function colorizeFloat32(
  data: Float32Array,
  lut: Uint32Array,
  { a, k, log }: Quantizer,
  out: Uint32Array
): void {
  for (let i = 0; i < data.length; i++) {
    const v = data[i] as number
    if (v !== v) {
      out[i] = 0
      continue
    }
    const x = log ? (v > 0 ? (Math.log(v) - a) * k : 0) : (v - a) * k
    out[i] = lut[x <= 0 ? 0 : x >= TOP ? TOP : (x + 0.5) | 0] as number
  }
}

/**
 * The scatter's `lookup` for a scalar mode: value → 0xRRGGBB from `colormapRgb`'s table, or -1
 * (hidden) for NaN.
 */
export function scalarLookup(
  rgb: Uint8Array,
  lo: number,
  hi: number,
  scale: ColorScale
): (value: number) => number {
  const f = normalizer(lo, hi, scale)
  return (value) => {
    const t = f(value)
    if (t !== t) return -1
    const i = 3 * colorIndex(t)
    return (
      ((rgb[i] as number) << 16) |
      ((rgb[i + 1] as number) << 8) |
      (rgb[i + 2] as number)
    )
  }
}
