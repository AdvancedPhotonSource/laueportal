import { describe, expect, it } from "vitest"

import { singleId, validateCreatePrefill } from "./prefill"

describe("validateCreatePrefill", () => {
  it("brings an ID to one type, a string", () => {
    // The router reads `scan=5` as the string "5"; a Link may give the number 5.
    expect(validateCreatePrefill({ scan: 5 })).toEqual({ scan: "5" })
    expect(
      validateCreatePrefill({ scan: "5", reconstruction: 8, indexing: " 9 " })
    ).toEqual({ scan: "5", reconstruction: "8", indexing: "9" })
  })

  it("drops what is not one ID, and other parameters", () => {
    expect(
      validateCreatePrefill({
        scan: "abc",
        reconstruction: -1,
        indexing: 1.5,
      })
    ).toEqual({})
    for (const value of [
      "",
      ",",
      "none",
      "5,6",
      ",2",
      "5;6",
      "5,-6",
      "1e3",
      true,
      null,
      [5],
      { id: 5 },
      2 ** 53,
    ]) {
      expect(
        validateCreatePrefill({ scan: value }),
        JSON.stringify(value)
      ).toEqual({})
    }
    expect(validateCreatePrefill({ tab: "params", scan_id: 5 })).toEqual({})
  })
})

describe("singleId", () => {
  it("is the parameter's ID as a number, null without one", () => {
    expect(singleId("8")).toBe(8)
    expect(singleId(undefined)).toBeNull()
  })
})
