import { describe, expect, it } from "vitest"

import {
  drawPoints,
  markerSpans,
  packColors,
  packRgb,
  withAlpha,
} from "./stamp"

// Render the buffer as rows of characters: "." empty, otherwise the code `legend` gives a colour.
function picture(
  buf: Uint32Array,
  w: number,
  legend: Record<number, string>
): string[] {
  const rows: string[] = []
  for (let y = 0; y < buf.length / w; y++) {
    rows.push(
      Array.from(buf.subarray(y * w, (y + 1) * w), (c) =>
        c === 0 ? "." : (legend[c] ?? "?")
      ).join("")
    )
  }
  return rows
}

describe("colour packing", () => {
  it("packs r, g, b bytes as little-endian RGBA and colours non-indexed points", () => {
    const colors = { kind: "rgb" as const, r: [1, 4], g: [2, 5], b: [3, 6] }
    const packed = packColors(colors, 2, Uint8Array.of(1, 0), "red")
    expect(new Uint8Array(packed.buffer).slice(0, 4)).toEqual(
      Uint8Array.of(1, 2, 3, 255)
    )
    expect(packed[1]).toBe(packRgb(0xdc2828))
    expect(packColors(colors, 2, Uint8Array.of(1, 0), "transparent")[1]).toBe(0)
  })

  it("maps scalars through the caller's lookup, a negative result hiding the point", () => {
    const lookup = (v: number) =>
      Number.isNaN(v) ? -1 : v > 0.5 ? 0xff0000 : 0x0000ff
    const packed = packColors(
      { kind: "lookup", values: [0.2, 0.9, NaN], lookup },
      3
    )
    expect(Array.from(packed)).toEqual([
      packRgb(0x0000ff),
      packRgb(0xff0000),
      0,
    ])
  })
})

describe("marker spans", () => {
  it("covers size × size pixels for a square and leaves a circle's corners out", () => {
    const area = (spans: Int16Array) => {
      let sum = 0
      for (let k = 0; k < spans.length; k += 3)
        sum += (spans[k + 2] as number) - (spans[k + 1] as number)
      return sum
    }
    expect(area(markerSpans(4, "square"))).toBe(16)
    expect(area(markerSpans(1, "circle"))).toBe(1)
    expect(area(markerSpans(5, "circle"))).toBe(21) // 5 × 5 less its four corner pixels
  })
})

describe("point stamping", () => {
  const size = { width: 7, height: 5 }
  const view = { x0: 0, y0: 0, x1: 7, y1: 5 }

  it("stamps each point about its pixel, skipping hidden and non-finite points", () => {
    const buf = new Uint32Array(35)
    const red = packRgb(0xff0000)
    const blue = packRgb(0x0000ff)
    const x = [1.5, 5.5, 3.5, NaN]
    const y = [3.5, 1.5, 2.5, 2.5]
    drawPoints(buf, size, view, x, y, Uint32Array.of(red, blue, 0, red), {
      size: 3,
      shape: "square",
    })
    expect(picture(buf, 7, { [red]: "r", [blue]: "b" })).toEqual([
      "rrr....",
      "rrr....",
      "rrr.bbb",
      "....bbb",
      "....bbb",
    ])
  })

  it("dims the unselected points and rings the selected ones", () => {
    const big = { width: 15, height: 15 }
    const buf = new Uint32Array(15 * 15)
    const red = packRgb(0xff0000)
    const ring = packRgb(0x000000)
    drawPoints(
      buf,
      big,
      { x0: 0, y0: 0, x1: 15, y1: 15 },
      [7.5, 1.5],
      [7.5, 13.5],
      Uint32Array.of(red, red),
      {
        size: 3,
        shape: "square",
        selected: Uint8Array.of(1, 0),
        ringSize: 9,
        ringWidth: 1,
        ringColor: ring,
      }
    )
    const rows = picture(buf, 15, {
      [red]: "r",
      [withAlpha(red, 51)]: "d",
      [ring]: "o",
    })
    expect(rows[1]).toBe("ddd............") // the unselected point, dimmed
    expect(rows[7]).toBe("...o..rrr..o...") // the selected point inside its ring
    expect(rows[3]).toBe(".....ooooo.....") // the ring's top
  })
})
