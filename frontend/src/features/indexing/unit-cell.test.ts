// @vitest-environment jsdom
import { describe, expect, it } from "vitest"

import {
  makePatternDetails as pattern,
  makePointDetails as makePoint,
  NI_RECIPROCAL as K,
} from "@/test/indexing"
import {
  direction,
  orientedPatterns,
  readUnitCell,
  shownPattern,
  triple,
} from "./unit-cell"

/** Ni's reference cell turned 30° about Z (lab), as rows a*, b*, c*. */
const TURNED = [
  [K * Math.cos(Math.PI / 6), K * Math.sin(Math.PI / 6), 0],
  [-K * Math.sin(Math.PI / 6), K * Math.cos(Math.PI / 6), 0],
  [0, 0, K],
]

describe("the shown pattern", () => {
  const point = makePoint([
    pattern(0, null),
    pattern(1, TURNED),
    pattern(2, TURNED),
  ])

  it("is the wanted one when it has an orientation, else the first that has", () => {
    expect(orientedPatterns(point)).toEqual([1, 2])
    expect(shownPattern(point, 2)).toBe(2)
    expect(shownPattern(point, 0)).toBe(1)
    expect(shownPattern(point, null)).toBe(1)
  })

  it("is none when no pattern has an orientation", () => {
    expect(shownPattern(makePoint([pattern(0, null)]), 0)).toBeNull()
    expect(shownPattern(makePoint([]), null)).toBeNull()
  })
})

describe("a pattern's unit cell", () => {
  it("has the measured and nominal parameters, and the rotation from the reference", () => {
    const reading = readUnitCell(makePoint([pattern(0, TURNED)]), 0)
    expect(reading).not.toBeNull()
    if (!reading) return
    expect(triple(reading.measured.lengths, 4)).toBe("0.3524 0.3524 0.3524")
    expect(triple(reading.measured.angles, 3)).toBe("90.000 90.000 90.000")
    expect(reading.nominal?.lengths).toEqual([0.35238, 0.35238, 0.35238])
    expect(reading.rotation?.angleDeg).toBeCloseTo(30, 6)
    expect(direction(reading.rotation?.axis ?? [0, 0, 0])).toBe(
      "[0.00 0.00 1.00]"
    )
    expect(reading.symmetry).toBe("cubic")
    expect(reading.refined).toBe(false)
    expect(direction(reading.beam)).toBe("[0.00 0.00 1.00]")
  })

  it("has no nominal cell and no rotation without a crystal", () => {
    const reading = readUnitCell(makePoint([pattern(0, TURNED)], false), 0)
    expect(reading).toMatchObject({
      nominal: null,
      rotation: null,
      symmetry: "none",
      refined: false,
    })
  })

  it("takes the equivalent setting nearest the reference: 100° about Z is 10°", () => {
    const c = Math.cos((100 * Math.PI) / 180)
    const s = Math.sin((100 * Math.PI) / 180)
    const turned = [
      [K * c, K * s, 0],
      [-K * s, K * c, 0],
      [0, 0, K],
    ]
    const reading = readUnitCell(makePoint([pattern(0, turned)]), 0)
    expect(reading?.rotation?.angleDeg).toBeCloseTo(10, 6)
  })

  it("is refined when the pattern's cell is not the nominal one", () => {
    const stretched = TURNED.map((row, i) =>
      i === 0 ? row.map((v) => v / 1.001) : row
    )
    const reading = readUnitCell(makePoint([pattern(0, stretched)]), 0)
    expect(reading?.refined).toBe(true)
    expect(triple(reading?.measured.lengths ?? [0, 0, 0], 5)).toBe(
      "0.35273 0.35238 0.35238"
    )
  })

  it("is none for a pattern without an orientation", () => {
    expect(readUnitCell(makePoint([pattern(0, null)]), 0)).toBeNull()
  })
})

it("writes a direction without -0.00", () => {
  expect(direction([-0.001, 0.5, -1])).toBe("[0.00 0.50 -1.00]")
})
