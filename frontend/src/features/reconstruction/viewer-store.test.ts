// @vitest-environment jsdom
import { describe, expect, it } from "vitest"

import { FRAME_AUTO_RANGE } from "@/viz"
import { createWireViewerStore } from "./viewer-store"

const memory = () => {
  const items = new Map<string, string>()
  return {
    getItem: (key: string) => items.get(key) ?? null,
    setItem: (key: string, value: string) => {
      items.set(key, value)
    },
    removeItem: (key: string) => {
      items.delete(key)
    },
  }
}

describe("the ROI Inspector's settings", () => {
  it("drop a manual colour range when the reference kind changes, and keep the rest", () => {
    const store = createWireViewerStore(memory())
    const { setRoiView } = store.getState()
    const manual = {
      ...store.getState().roi.colorbar,
      auto: null,
      min: 10,
      max: 900,
      scale: "log" as const,
    }
    setRoiView({ colorbar: manual, colormap: "gray" })
    setRoiView({ reference: "sum_reconstructed" })
    expect(store.getState().roi.colorbar).toEqual(manual)

    setRoiView({ reference: "sum_raw" })
    const { roi } = store.getState()
    expect(roi.colorbar.auto).toBe(FRAME_AUTO_RANGE)
    expect(roi.colorbar.scale).toBe("log")
    expect(roi.colormap).toBe("gray")
  })
})
