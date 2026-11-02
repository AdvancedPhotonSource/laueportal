import { describe, expect, it } from "vitest"

import {
  MAX_POINT_ID_LENGTH,
  searchPatch,
  validateReconstructionSearch,
} from "./viewer-search"

describe("the wire viewer's search", () => {
  it("keeps valid non-default values and drops the rest silently", () => {
    expect(
      validateReconstructionSearch({
        tab: "depth",
        point: "a b/c",
        depth: "12",
        link: "x",
      })
    ).toEqual({ tab: "depth", point: "a b/c", depth: 12, link: "x" })
    expect(
      validateReconstructionSearch({
        tab: "params",
        point: "x".repeat(MAX_POINT_ID_LENGTH + 1),
        depth: "-1",
        link: "",
      })
    ).toEqual({
      tab: undefined,
      point: undefined,
      depth: undefined,
      link: undefined,
    })
    // An unknown tab; a depth is an index.
    expect(
      validateReconstructionSearch({ tab: "other", point: "", depth: "1.5" })
    ).toEqual({
      tab: undefined,
      point: undefined,
      depth: undefined,
      link: undefined,
    })
  })

  it("leaves the default tab and the default point out of the URL", () => {
    expect(searchPatch({ tab: "params" })).toEqual({ tab: undefined })
    expect(searchPatch({ tab: "points" })).toEqual({ tab: "points" })
    expect(searchPatch({ point: "wire_1" }, "wire_1")).toEqual({
      point: undefined,
    })
    expect(searchPatch({ point: "wire_3" }, "wire_1")).toEqual({
      point: "wire_3",
    })
    // Another point opens at its brightest depth: the depth leaves the URL.
    expect(Object.keys(searchPatch({ point: "wire_3" }, "wire_1"))).toContain(
      "depth"
    )
    expect(searchPatch({ depth: 40 }, "wire_1", 40)).toEqual({
      depth: undefined,
    })
    expect(searchPatch({ depth: 3 }, "wire_1", 40)).toEqual({ depth: 3 })
  })
})
