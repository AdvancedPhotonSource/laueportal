// @vitest-environment jsdom
import { describe, expect, it } from "vitest"

import { packViewLink } from "@/lib/view-link"
import { openReconstructionView, packReconstructionView } from "./viewer-link"
import { DEFAULT_SETTINGS, type WireViewerSettings } from "./viewer-store"

describe("a copied link to the wire viewer", () => {
  it("opens as the view it was copied from, its depth, image and ROI settings too, without another link", async () => {
    // The Depth View's colormap and the user's range (NaN-free: JSON keeps it).
    const settings: WireViewerSettings = {
      depth: {
        colormap: "inferno",
        colorbar: {
          ...DEFAULT_SETTINGS.depth.colorbar,
          auto: null,
          min: 10,
          max: 900,
        },
      },
      roi: {
        ...DEFAULT_SETTINGS.roi,
        reference: "first_raw",
        size: 15,
        axis: "index",
        scale: "log",
        values: "normalized",
      },
    }
    const packed = await packReconstructionView(
      { tab: "depth", point: "wire 3&x", depth: 7, link: "old" },
      settings
    )
    expect(packed).toMatch(/^[A-Za-z0-9_-]+$/)
    expect(await openReconstructionView(packed)).toEqual({
      kind: "apply",
      search: { tab: "depth", point: "wire 3&x", depth: 7, link: undefined },
      settings,
    })
  })

  it("is ignored when its schema version is not the viewer's, or it cannot be read", async () => {
    const other = await packViewLink({
      v: 2,
      url: { tab: "points" },
      settings: {},
    })
    expect(await openReconstructionView(other)).toEqual({
      kind: "mismatch",
      version: 2,
    })
    expect(await openReconstructionView("not-a-link")).toEqual({
      kind: "unreadable",
    })
  })
})
