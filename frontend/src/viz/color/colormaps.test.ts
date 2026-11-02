import { describe, expect, it } from "vitest"

import {
  COLORMAP_LABELS,
  colormapPixels,
  colormapRgb,
  FRAME_COLORMAPS,
  MAP_PALETTES,
  type ColormapName,
} from "./colormaps"

const hex = (rgb: Uint8Array, i: number) =>
  Array.from(rgb.subarray(3 * i, 3 * i + 3), (v) =>
    v.toString(16).padStart(2, "0")
  ).join("")

describe("colormaps", () => {
  it("builds 256 colours from each map's first stop to its last", () => {
    const ends: Record<ColormapName, [string, string]> = {
      gray: ["000000", "ffffff"],
      gray_r: ["ffffff", "000000"],
      viridis: ["440154", "fde725"],
      plasma: ["0d0887", "f0f921"],
      inferno: ["000004", "fcffa4"],
      magma: ["000004", "fcfdbf"],
      turbo: ["30123b", "7a0402"],
      jet: ["000083", "800000"],
      rainbow: ["96005a", "ff0000"],
      terrain: ["000082", "ffffff"],
      terrain_r: ["ffffff", "000082"],
    }
    const names = new Set<ColormapName>([...FRAME_COLORMAPS, ...MAP_PALETTES])
    expect([...names].sort()).toEqual(Object.keys(COLORMAP_LABELS).sort())
    for (const name of names) {
      const rgb = colormapRgb(name)
      expect(rgb.length, name).toBe(256 * 3)
      expect([hex(rgb, 0), hex(rgb, 255)], name).toEqual(ends[name])
    }
    // Interpolated between the stops: gray's middle is mid-gray, viridis passes its 2nd stop.
    expect(hex(colormapRgb("gray"), 128)).toBe("808080")
    expect(hex(colormapRgb("viridis"), Math.round(255 / 9))).toBe("482878")
  })

  it("reverses a map with `reverse`, and `_r` again", () => {
    const viridis = colormapRgb("viridis")
    const back = colormapRgb("viridis", true)
    for (let i = 0; i < 256; i++)
      expect(hex(back, i)).toBe(hex(viridis, 255 - i))
    expect(colormapRgb("gray_r", true)).toEqual(colormapRgb("gray"))
    expect(colormapRgb("terrain", true)).toEqual(colormapRgb("terrain_r"))
  })

  it("packs the colours as opaque ImageData pixels", () => {
    const pixels = colormapPixels("viridis")
    // Little-endian RGBA bytes: 0x44, 0x01, 0x54, 0xff.
    expect(new Uint8Array(pixels.buffer, 0, 4)).toEqual(
      new Uint8Array([0x44, 0x01, 0x54, 0xff])
    )
  })
})
