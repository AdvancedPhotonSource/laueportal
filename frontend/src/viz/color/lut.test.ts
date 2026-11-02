import { describe, expect, it } from "vitest"

import { colormapPixels, colormapRgb } from "./colormaps"
import {
  colorIndex,
  colorizeFloat32,
  colorizeUint16,
  float32Lut,
  LUT_SIZE,
  scalarLookup,
  uint16Lut,
} from "./lut"
import { normalizer, type ColorScale } from "./scale"

const linear: ColorScale = { kind: "linear", gamma: 2 }
/** A table whose "colour" is its own index, to read back which entry a value got. */
const identity = Uint32Array.from({ length: 256 }, (_, i) => i)

describe("colour lookup", () => {
  it("builds a 65,536-entry uint16 table through the range and scale", () => {
    const pixels = colormapPixels("gray")
    const lut = uint16Lut(pixels, 1000, 3000, linear)
    expect(lut.length).toBe(LUT_SIZE)
    expect(lut[0]).toBe(pixels[0])
    expect(lut[1000]).toBe(pixels[0])
    expect(lut[2000]).toBe(pixels[128])
    expect(lut[3000]).toBe(pixels[255])
    expect(lut[65535]).toBe(pixels[255])
    // Other scales reshape the inside only.
    const log = uint16Lut(identity, 1, 10000, { kind: "log", gamma: 2 })
    expect([log[1], log[100], log[10000]]).toEqual([0, 128, 255])
  })

  it("colours a uint16 frame by table lookup, and a float32 frame by quantizing it first", () => {
    const values = [0, 1, 7, 255, 1000, 40000, 65535]
    const u16 = Uint16Array.from(values)
    const f32 = Float32Array.from([...values, NaN, Infinity, -Infinity])
    const viaU16 = new Uint32Array(u16.length)
    colorizeUint16(u16, uint16Lut(identity, 0, 65535, linear), viaU16)
    const viaF32 = new Uint32Array(f32.length)
    const { lut, quantizer } = float32Lut(identity, 0, 65535, linear)
    colorizeFloat32(f32, lut, quantizer, viaF32)
    // The same colours for the same values; NaN is transparent, the infinities clamp.
    expect(Array.from(viaF32.subarray(0, values.length))).toEqual(
      Array.from(viaU16)
    )
    expect(Array.from(viaF32.subarray(values.length))).toEqual([0, 255, 0])
    // Every scale's quantized colour is the exact one, or its neighbour at a bin's edge.
    const scales: ColorScale[] = [
      linear,
      { kind: "sqrt", gamma: 2 },
      { kind: "gamma", gamma: 3 },
      { kind: "log", gamma: 2 },
    ]
    const signed = Float32Array.from(
      { length: 2001 },
      (_, i) => -2.5 + i * 0.01
    )
    for (const s of scales) {
      const lo = s.kind === "log" ? 0.01 : -2
      const exact = normalizer(lo, 17.5, s)
      const q = float32Lut(identity, lo, 17.5, s)
      const out = new Uint32Array(signed.length)
      colorizeFloat32(signed, q.lut, q.quantizer, out)
      signed.forEach((v, i) => {
        expect(
          Math.abs((out[i] as number) - colorIndex(exact(v))),
          `${s.kind} ${String(v)}`
        ).toBeLessThanOrEqual(1)
      })
    }
  })

  it("gives the scatter a 0xRRGGBB lookup that hides NaN", () => {
    const lookup = scalarLookup(colormapRgb("viridis"), 0, 1, linear)
    expect(lookup(0)).toBe(0x440154)
    expect(lookup(1)).toBe(0xfde725)
    expect(lookup(5)).toBe(0xfde725)
    expect(lookup(NaN)).toBe(-1)
  })
})
