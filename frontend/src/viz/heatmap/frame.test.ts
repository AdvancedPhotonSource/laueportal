import { describe, expect, it } from "vitest"

import { decodeFrame } from "./frame"

describe("decodeFrame", () => {
  it("reads a uint16 or float32 body by its shape and type headers", () => {
    const u16 = decodeFrame(
      Uint16Array.from([1, 2, 3, 65535, 0, 7]).buffer,
      "2,3",
      "uint16"
    )
    expect([u16.height, u16.width]).toEqual([2, 3])
    expect(u16.data).toBeInstanceOf(Uint16Array)
    expect(Array.from(u16.data)).toEqual([1, 2, 3, 65535, 0, 7])
    const f32 = decodeFrame(
      Float32Array.from([-1.5, NaN]).buffer,
      "1, 2",
      "float32"
    )
    expect(f32.data).toBeInstanceOf(Float32Array)
    expect(Array.from(f32.data)).toEqual([-1.5, NaN])
    // Headers that disagree with the body are refused.
    const body = new ArrayBuffer(12)
    expect(() => decodeFrame(body, "2,2", "uint16")).toThrow(/needs 8/)
    expect(() => decodeFrame(body, "2,3", "int32")).toThrow(/type/)
    expect(() => decodeFrame(body, "6", "uint16")).toThrow(/shape/)
    expect(() => decodeFrame(body, null, "uint16")).toThrow(/shape/)
  })
})
