import { afterEach, expect, it, vi } from "vitest"

import { getRemoteReadinessWithin } from "./remote-readiness"

afterEach(() => {
  vi.unstubAllGlobals()
})

it("rejects a malformed status reply so it cannot break the navigation", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(Response.json({ state: "ok" }))
  )
  await expect(
    getRemoteReadinessWithin(new AbortController().signal)
  ).rejects.toThrow()
})
