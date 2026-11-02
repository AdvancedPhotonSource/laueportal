import { describe, expect, it } from "vitest"

import { ApiError } from "@/api"
import {
  adjacentStep,
  checkedPatterns,
  detectorStep,
  isImageNotFound,
  overlayRequests,
} from "./detector"

const SUMMARY = { steps: [2, 3, 5, 8], default_step: 3 }
const SCOPE = { pattern0_only: true, min_peaks: 4 }

describe("the detector view's step", () => {
  it("is the URL's when the scope keeps it, else the summary's default, and none without steps", () => {
    expect(detectorStep(5, SUMMARY)).toBe(5)
    expect(detectorStep(undefined, SUMMARY)).toBe(3)
    // A step the scope drops (4) or the results lack (99) shows the default.
    expect(detectorStep(4, SUMMARY)).toBe(3)
    expect(detectorStep(99, SUMMARY)).toBe(3)
    expect(detectorStep(5, { steps: [], default_step: null })).toBeNull()
  })

  it("keeps the default it showed while that step is there, the URL's step first (a live run's results read again)", () => {
    expect(detectorStep(undefined, SUMMARY, 2)).toBe(2)
    expect(detectorStep(5, SUMMARY, 2)).toBe(5)
    expect(detectorStep(undefined, SUMMARY, 4)).toBe(3)
  })

  it("moves to the step in scope before or after it, and stops at either end", () => {
    expect(adjacentStep(SUMMARY.steps, 3, 1)).toBe(5)
    expect(adjacentStep(SUMMARY.steps, 3, -1)).toBe(2)
    expect(adjacentStep(SUMMARY.steps, 2, -1)).toBeNull()
    expect(adjacentStep(SUMMARY.steps, 8, 1)).toBeNull()
  })
})

describe("the detector overlays' requests", () => {
  it("send the scope, and ask for a simulation only when it is on", () => {
    expect(overlayRequests(SCOPE, false)).toEqual({
      plain: SCOPE,
      simulated: null,
    })
    // The fast request still runs, so the peaks show while the simulation does.
    expect(overlayRequests(SCOPE, true)).toEqual({
      plain: SCOPE,
      simulated: { ...SCOPE, simulate: true },
    })
  })
})

describe("the detector view's other rules", () => {
  it("knows the image's not-found problem from other 404s", () => {
    const notFound = new ApiError(404, "Not Found", {
      type: "about:blank",
      title: "Detector image not found",
      status: 404,
      detail: "I1, step 3: /data/a.h5: no such file",
    })
    expect(isImageNotFound(notFound)).toBe(true)
    expect(
      isImageNotFound(
        new ApiError(404, "Not Found", {
          type: "about:blank",
          title: "Not found",
          status: 404,
        })
      )
    ).toBe(false)
  })

  it("checks every pattern but the unchecked ones", () => {
    const patterns = [0, 1, 2].map((pattern_index) => ({
      pattern_index,
      goodness: null,
      in_scope: true,
      indexed: true,
      n_indexed: 3,
      peaks: [],
      rms_error_deg: null,
      simulated: null,
    }))
    expect(checkedPatterns(patterns, new Set([1]))).toEqual(new Set([0, 2]))
  })
})
