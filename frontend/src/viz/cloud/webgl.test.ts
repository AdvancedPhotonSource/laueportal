import { describe, expect, it, vi } from "vitest"

import { canCreateWebGL2, type WebGLProbeCanvas } from "./webgl"

describe("canCreateWebGL2", () => {
  it("detects a WebGL 2 context, releases the probe's, and is false without one or on an error", () => {
    const loseContext = vi.fn()
    const withGl: WebGLProbeCanvas = {
      getContext: () => ({ getExtension: () => ({ loseContext }) }),
    }
    expect(canCreateWebGL2(withGl)).toBe(true)
    expect(loseContext).toHaveBeenCalledOnce()

    const withoutExtension: WebGLProbeCanvas = {
      getContext: () => ({ getExtension: () => null }),
    }
    expect(canCreateWebGL2(withoutExtension)).toBe(true)

    expect(canCreateWebGL2({ getContext: () => null })).toBe(false)
    const throwing: WebGLProbeCanvas = {
      getContext: () => {
        throw new Error("blocked")
      },
    }
    expect(canCreateWebGL2(throwing)).toBe(false)
  })
})
