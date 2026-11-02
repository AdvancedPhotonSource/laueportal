import { describe, expect, it } from "vitest"

import { packViewLink, unpackViewLink } from "./view-link"

describe("a view link", () => {
  it("packs a view as compressed base64url and unpacks it unchanged", async () => {
    const link = {
      v: 1,
      url: { tab: "map", step: "12", color: "goodness" },
      settings: { scope: { pattern0Only: false, skipPeaksAtMost: 0 } },
    }
    const packed = await packViewLink(link)
    expect(packed).toMatch(/^[A-Za-z0-9_-]+$/)
    expect(await unpackViewLink(packed)).toEqual(link)
  })

  it("is undefined for a value that does not pack one", async () => {
    const notJson = await packViewLink({ v: 1, url: {}, settings: null })
    for (const value of ["3d", "not a link!", notJson.slice(0, 4)]) {
      expect(await unpackViewLink(value)).toBeUndefined()
    }
  })
})
