import { describe, expect, it } from "vitest"

import {
  searchPatch,
  validateIndexingSearch,
  viewOf,
  type IndexingView,
} from "./viewer-search"

describe("the indexing viewer's URL", () => {
  it("keeps every coarse value the URL gives that is not a default", () => {
    expect(
      validateIndexingSearch({
        tab: "detector",
        step: "12",
        color: "goodness",
        view: "3d",
        hkl: "1,-1,0",
        point: "7",
        link: "eJyrVg",
      })
    ).toEqual({
      tab: "detector",
      step: 12,
      color: "goodness",
      view: "3d",
      hkl: "1-10",
      point: 7,
      link: "eJyrVg",
    })
  })

  it("drops defaults and invalid values silently, and shows the defaults", () => {
    const search = validateIndexingSearch({
      tab: "plot",
      step: "-1",
      color: "cubic_ipf",
      view: "2d",
      hkl: "000",
      point: "x",
      link: "",
      other: "kept by the router, not by the viewer",
    })
    expect(search).toEqual({
      tab: undefined,
      step: undefined,
      color: undefined,
      view: undefined,
      hkl: undefined,
      point: undefined,
      link: undefined,
    })
    // `view` is exactly `3d` (no packed link, no other case)
    expect(validateIndexingSearch({ view: "3D" }).view).toBeUndefined()
    expect(viewOf(search)).toEqual({
      tab: "params",
      step: undefined,
      color: "cubic_ipf",
      mode: "2d",
      hkl: [1, 0, 0],
      point: undefined,
    })
  })

  it("writes a change without its defaults (the summary's default step included)", () => {
    const change: IndexingView = {
      tab: "params",
      step: 3,
      color: "cubic_ipf",
      mode: "2d",
      hkl: [1, 0, 0],
      point: undefined,
    }
    expect(searchPatch(change, 3)).toEqual({
      tab: undefined,
      step: undefined,
      color: undefined,
      view: undefined,
      hkl: undefined,
      point: undefined,
    })
    expect(
      searchPatch({ tab: "pole", step: 4, mode: "3d", hkl: [1, 1, 10] }, 3)
    ).toEqual({ tab: "pole", step: 4, view: "3d", hkl: "1,1,10" })
  })
})
