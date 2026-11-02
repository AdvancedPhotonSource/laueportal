// Colormaps: the detector frames' nine and the map's scalar palettes, with the names the backend
// echoes (lower case; `_r` is the reverse). The tables are embedded as the stops of the Plotly
// scales (viridis, plasma, inferno, magma and turbo are Plotly's samples of matplotlib's; `terrain`
// is plotly.js's Earth, as the backend's Palette says), interpolated in RGB to 256 colours, so no
// dependency is needed.
import { Palette } from "@/api"

/** The detector frame's colormaps (`gray` is the default). */
export const FRAME_COLORMAPS = [
  "gray",
  "gray_r",
  "viridis",
  "plasma",
  "inferno",
  "magma",
  "turbo",
  "jet",
  "terrain_r",
] as const
export type FrameColormap = (typeof FRAME_COLORMAPS)[number]

/** The map's scalar palettes: the backend's `Palette`, each reversible. */
export const MAP_PALETTES = Object.values(Palette)
export type MapPalette = Palette

export type ColormapName = FrameColormap | MapPalette

/** Names for menus. */
export const COLORMAP_LABELS: Record<ColormapName, string> = {
  gray: "Gray",
  gray_r: "Gray reversed",
  viridis: "Viridis",
  plasma: "Plasma",
  inferno: "Inferno",
  magma: "Magma",
  turbo: "Turbo",
  jet: "Jet",
  rainbow: "Rainbow",
  terrain: "Terrain",
  terrain_r: "Terrain reversed",
}

type Base =
  | "gray"
  | "viridis"
  | "plasma"
  | "inferno"
  | "magma"
  | "turbo"
  | "jet"
  | "rainbow"
  | "terrain"

/** Evenly spaced stops as one hex string (6 digits a colour), or [position, hex] pairs. */
const STOPS: Record<Base, string | readonly (readonly [number, string])[]> = {
  gray: "000000ffffff",
  viridis: "4401544828783e498931688e26828e1f9e8935b7796ece58b5de2bfde725",
  plasma: "0d088746039f7201a89c179ebd3786d8576bed7953fb9f3afdca26f0f921",
  inferno: "0000041b0c414a0c6b781c6da52c60cf4446ed6925fb9b06f7d13dfcffa4",
  magma: "000004180f3d440f76721f819e2f7fcd4071f1605dfd9668feca8dfcfdbf",
  turbo:
    "30123b4145ab4675ed39a2fc1bcfd424eca661fc6ca4fc3bd1e834f3c63afe9b2df36315d93806b119017a0402",
  // plotly.py's Jet: its six colours at even stops. plotly.js's own Jet spaces the same colours
  // unevenly (at 0, .125, .375, .625, .875 and 1).
  jet: "000083003caa05ffffffff00fa0000800000",
  rainbow: "96005a0000c80019ff0098ff2cff9697ff00ffea00ff6f00ff0000",
  // plotly.js's Earth: dark blue, cyan, green, yellow, brown, white.
  terrain: [
    [0, "000082"],
    [0.1, "00b4b4"],
    [0.2, "28d228"],
    [0.4, "e6e632"],
    [0.6, "784614"],
    [1, "ffffff"],
  ],
}

const SIZE = 256
const cache = new Map<string, Uint8Array>()

/** The base map and whether it runs backwards (`_r`, or `reverse`, which flips it again). */
function parse(name: ColormapName, reverse: boolean): [Base, boolean] {
  const reversed = name.endsWith("_r")
  return [(reversed ? name.slice(0, -2) : name) as Base, reversed !== reverse]
}

/** 256 colours as r, g, b bytes, from the low end of the range to the high end. */
export function colormapRgb(name: ColormapName, reverse = false): Uint8Array {
  const [base, backwards] = parse(name, reverse)
  const key = `${base}:${String(backwards)}`
  let table = cache.get(key)
  if (!table) {
    table = interpolate(stops(base))
    if (backwards) table = reverseRgb(table)
    cache.set(key, table)
  }
  return table
}

/** The same colours packed as ImageData pixels (little-endian ABGR, opaque). */
export function colormapPixels(
  name: ColormapName,
  reverse = false
): Uint32Array {
  const rgb = colormapRgb(name, reverse)
  const out = new Uint32Array(SIZE)
  for (let i = 0; i < SIZE; i++) {
    const r = rgb[3 * i] as number
    const g = rgb[3 * i + 1] as number
    const b = rgb[3 * i + 2] as number
    out[i] = ((255 << 24) | (b << 16) | (g << 8) | r) >>> 0
  }
  return out
}

function stops(base: Base): [number, number, number, number][] {
  const s = STOPS[base]
  if (typeof s !== "string") {
    return s.map(([t, hex]) => [t, ...rgbOf(hex)])
  }
  const n = s.length / 6
  return Array.from({ length: n }, (_, i) => [
    i / (n - 1),
    ...rgbOf(s.slice(6 * i, 6 * i + 6)),
  ])
}

function rgbOf(hex: string): [number, number, number] {
  const v = parseInt(hex, 16)
  return [(v >> 16) & 255, (v >> 8) & 255, v & 255]
}

function interpolate(points: [number, number, number, number][]): Uint8Array {
  const out = new Uint8Array(3 * SIZE)
  let k = 0
  for (let i = 0; i < SIZE; i++) {
    const t = i / (SIZE - 1)
    while (
      k < points.length - 2 &&
      t > ((points[k + 1] as number[])[0] as number)
    )
      k++
    const [t0, r0, g0, b0] = points[k] as [number, number, number, number]
    const [t1, r1, g1, b1] = points[k + 1] as [number, number, number, number]
    const f = t1 > t0 ? Math.min(1, Math.max(0, (t - t0) / (t1 - t0))) : 0
    out[3 * i] = Math.round(r0 + (r1 - r0) * f)
    out[3 * i + 1] = Math.round(g0 + (g1 - g0) * f)
    out[3 * i + 2] = Math.round(b0 + (b1 - b0) * f)
  }
  return out
}

function reverseRgb(rgb: Uint8Array): Uint8Array {
  const out = new Uint8Array(rgb.length)
  for (let i = 0; i < SIZE; i++) {
    const j = SIZE - 1 - i
    out[3 * i] = rgb[3 * j] as number
    out[3 * i + 1] = rgb[3 * j + 1] as number
    out[3 * i + 2] = rgb[3 * j + 2] as number
  }
  return out
}
