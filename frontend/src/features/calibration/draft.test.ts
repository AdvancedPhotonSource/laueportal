// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest"

import {
  CALIBRATION_STORE_KEY,
  EMPTY_DRAFT,
  listedPeaks,
  measurementsFor,
  nearestPeak,
  peakKey,
  resetCalibrationDraftStore,
  useCalibrationDraftStore,
} from "./draft"

const found = [
  { x: 100, y: 200, intensity: 9 },
  { x: 300.004, y: 40, intensity: 5 },
  { x: 500, y: 600, intensity: 1 },
]

afterEach(() => {
  resetCalibrationDraftStore()
  localStorage.clear()
})

describe("listedPeaks", () => {
  it("lists the found peaks not removed, then the added ones", () => {
    const draft = {
      ...EMPTY_DRAFT,
      removed: [peakKey(found[1] ?? { x: 0, y: 0 })],
      added: [{ x: 10.5, y: 20.25 }],
    }
    expect(listedPeaks(found, draft)).toEqual([
      { x: 100, y: 200, key: "100.00,200.00", origin: "found" },
      { x: 500, y: 600, key: "500.00,600.00", origin: "found" },
      { x: 10.5, y: 20.25, key: "10.50,20.25", origin: "added" },
    ])
  })

  it("lists a peak added where one was found once", () => {
    const draft = { ...EMPTY_DRAFT, added: [{ x: 100.001, y: 200 }] }
    expect(listedPeaks(found, draft)).toHaveLength(3)
  })
})

describe("measurementsFor", () => {
  it("sends each value by the peak's index in the list, and leaves out empty ones", () => {
    const draft = {
      ...EMPTY_DRAFT,
      removed: ["100.00,200.00"],
      measurements: {
        "500.00,600.00": { energyKev: 6.4335, source: "scan 7", order: null },
        "300.00,40.00": { energyKev: null, source: "", order: null },
        "999.00,999.00": { energyKev: 8, source: "", order: 2 },
      },
    }
    const peaks = listedPeaks(found, draft)
    expect(measurementsFor(peaks, draft)).toEqual([
      { peak: 1, energy_kev: 6.4335, source: "scan 7", order: null },
    ])
  })
})

describe("nearestPeak", () => {
  it("picks the nearest peak within the radius, or none", () => {
    const peaks = listedPeaks(found, EMPTY_DRAFT)
    expect(nearestPeak(peaks, 104, 203, 12)?.key).toBe("100.00,200.00")
    expect(nearestPeak(peaks, 140, 200, 12)).toBeNull()
  })
})

describe("the draft store", () => {
  it("keeps each frame's draft, persisted, and forgets one on Start Over", () => {
    const { updateFrame, clearFrame, setFiles } =
      useCalibrationDraftStore.getState()
    setFiles({ frame: "/data/si.h5" })
    updateFrame("/data/si.h5", (draft) => ({
      ...draft,
      added: [{ x: 1, y: 2 }],
    }))
    updateFrame("/data/other.h5", (draft) => ({
      ...draft,
      removed: ["1.00,2.00"],
    }))
    const stored = JSON.parse(
      localStorage.getItem(CALIBRATION_STORE_KEY) ?? "{}"
    ) as {
      state: { files: { frame: string }; frames: Record<string, unknown> }
    }
    expect(stored.state.files.frame).toBe("/data/si.h5")
    expect(Object.keys(stored.state.frames)).toEqual([
      "/data/si.h5",
      "/data/other.h5",
    ])
    clearFrame("/data/si.h5")
    expect(Object.keys(useCalibrationDraftStore.getState().frames)).toEqual([
      "/data/other.h5",
    ])
  })
})

describe("the label settings", () => {
  it("are kept in the browser", () => {
    useCalibrationDraftStore.getState().setLabels({ labelSize: 14 })
    useCalibrationDraftStore.getState().setLabels({ showLabels: false })
    const stored = JSON.parse(
      localStorage.getItem(CALIBRATION_STORE_KEY) ?? "{}"
    ) as { state: { labelSize: number; showLabels: boolean } }
    expect(stored.state).toMatchObject({ labelSize: 14, showLabels: false })
  })
})
