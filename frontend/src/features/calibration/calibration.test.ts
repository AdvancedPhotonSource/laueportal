import { describe, expect, it } from "vitest"

import type { CalibrationIndexing, CalibrationResult } from "@/api"

import {
  calibrationLayers,
  energyErrorColor,
  energyMismatch,
  surfaceWarns,
  vector,
} from "./calibration"
import { EMPTY_DRAFT, listedPeaks } from "./draft"

const peaks = listedPeaks([{ x: 1, y: 2, intensity: 1 }], {
  ...EMPTY_DRAFT,
  added: [{ x: 5, y: 6 }],
})

const surface = {
  h: 0,
  k: 0,
  l: 1,
  angle_to_expected_deg: 1.1,
  angle_to_incident_deg: 44.8,
  angle_to_exit_deg: 45.2,
  between: true,
}

const indexing = {
  found: true,
  rows: [
    { peak: 0, h: 0, k: 0, l: 4, predicted_x: 1.5, predicted_y: 2.5 },
    { peak: 1, h: 1, k: 1, l: 5, predicted_x: null, predicted_y: null },
  ],
} as unknown as CalibrationIndexing

const result = {
  status: "ok",
  rows: [
    {
      peak: 0,
      h: 0,
      k: 0,
      l: 4,
      predicted_x: 1.1,
      predicted_y: 2.1,
      energy_error_ev: 12,
    },
    {
      peak: 1,
      h: 1,
      k: 1,
      l: 5,
      predicted_x: 5.1,
      predicted_y: 6.1,
      energy_error_ev: null,
    },
  ],
} as unknown as CalibrationResult

describe("calibrationLayers", () => {
  it("draws the found and added peaks and the indexing's predictions with labels", () => {
    const layers = calibrationLayers(peaks, indexing, undefined, {
      labels: true,
    })
    expect(layers.map((layer) => layer.kind)).toEqual([
      "detected",
      "added",
      "indexed",
    ])
    expect(Array.from(layers[2]?.x ?? [])).toEqual([1.5])
    expect(layers[2]?.labels).toEqual(["(0 0 4)"])
  })

  it("draws a fit's predictions instead, coloured by their energy error", () => {
    const layers = calibrationLayers(peaks, indexing, result, { labels: false })
    expect(layers.map((layer) => layer.kind)).toEqual([
      "detected",
      "added",
      "fitted",
    ])
    const fitted = layers[2]
    expect(fitted?.labels).toBeNull()
    expect(fitted?.colors).toEqual([
      energyErrorColor(12),
      energyErrorColor(null),
    ])
  })
})

describe("energyErrorColor", () => {
  it("is red above, blue below and green without a measured energy", () => {
    expect(energyErrorColor(10)).toBe("rgb(220, 60, 60)")
    expect(energyErrorColor(-10)).toBe("rgb(60, 60, 230)")
    expect(energyErrorColor(0)).toBe("rgb(220, 200, 200)")
    expect(energyErrorColor(undefined)).toBe("rgb(40, 160, 70)")
  })
})

describe("the warnings", () => {
  it("warns on a surface reflection away from the normal or not between the beams", () => {
    expect(surfaceWarns(surface, 3)).toBe(false)
    expect(surfaceWarns({ ...surface, angle_to_expected_deg: 29.9 }, 3)).toBe(
      true
    )
    expect(surfaceWarns({ ...surface, between: false }, 3)).toBe(true)
    expect(surfaceWarns(null, 3)).toBe(false)
  })

  it("flags a measured energy of another harmonic", () => {
    expect(energyMismatch(6.43, 6.4335)).toBe(false)
    expect(energyMismatch(12.87, 6.4335)).toBe(true)
    expect(energyMismatch(null, 6.4335)).toBe(false)
  })
})

describe("vector", () => {
  it("formats three values, scaled, with a dash for none", () => {
    expect(vector([1500, -2, null], 1, 0.001)).toBe("1.5, 0.0, —")
  })
})
