// @vitest-environment jsdom
import { describe, expect, it } from "vitest"

import { FRAME_AUTO_RANGE } from "@/viz"
import { depthReadout } from "./depth-view"
import { DEFAULT_DEPTH_VIEW } from "./viewer-store"

describe("the Depth View's contrast", () => {
  it("is automatic by default: the frames' 1st to 99.9th percentile", () => {
    // The percentiles themselves are the colorbar's (distribution.test.ts, logic.test.ts).
    expect(DEFAULT_DEPTH_VIEW.colorbar.auto).toBe(FRAME_AUTO_RANGE)
    expect(FRAME_AUTO_RANGE).toBe("p1-99.9")
  })
})

describe("the depth readout", () => {
  it("reads depth µm (first … last), a dash for what is not known yet", () => {
    expect(depthReadout(-12.5, [-50, 50])).toBe("-12.5 µm (-50 … 50)")
    expect(depthReadout(null, [-50, 50])).toBe("— (-50 … 50)")
    expect(depthReadout(3.14159, null)).toBe("3.1416 µm")
  })
})
