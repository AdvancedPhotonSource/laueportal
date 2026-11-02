// @vitest-environment jsdom
import { describe, expect, it } from "vitest"

import { packViewLink } from "@/lib/view-link"
import { openIndexingView, packIndexingView, viewLinkUrl } from "./viewer-link"
import { DEFAULT_SETTINGS, type IndexingViewerSettings } from "./viewer-store"

const SETTINGS: IndexingViewerSettings = {
  scope: { pattern0Only: false, skipPeaksAtMost: 7 },
  map: { ...DEFAULT_SETTINGS.map, surface: "custom", markerSize: 12 },
  pole: { ...DEFAULT_SETTINGS.pole, radius: 10 },
  detector: {
    ...DEFAULT_SETTINGS.detector,
    simulate: true,
    colormap: "inferno",
    colorbar: { auto: null, min: 5, max: 900, scale: "sqrt", gamma: 2 },
  },
}

describe("a link to an indexing view", () => {
  it("opens as the view it was copied from", async () => {
    const packed = await packIndexingView(
      { tab: "pole", step: 5, view: "3d", hkl: "110" },
      SETTINGS
    )
    expect(await openIndexingView(packed)).toEqual({
      kind: "apply",
      search: {
        tab: "pole",
        step: 5,
        color: undefined,
        view: "3d",
        hkl: "110",
        point: undefined,
        link: undefined,
      },
      settings: SETTINGS,
    })
    expect(viewLinkUrl("http://host/indexing/1?tab=map", packed)).toBe(
      `http://host/indexing/1?link=${packed}`
    )
  })

  it("is ignored when its schema version is not the viewer's", async () => {
    const packed = await packViewLink({
      v: 2,
      url: { tab: "map" },
      settings: {},
    })
    expect(await openIndexingView(packed)).toEqual({
      kind: "mismatch",
      version: 2,
    })
  })
})
