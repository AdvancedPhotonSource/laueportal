// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest"

import { CloudScene } from "./scene"

afterEach(() => {
  vi.restoreAllMocks()
})

describe("CloudScene", () => {
  // The probe can pass and VTK.js still get no context (e.g. after a GPU crash); the page's Retry
  // then builds a scene again, so each failed attempt must take back what it added.
  it("deletes what it built and leaves no document listener when VTK.js gets no WebGL context", () => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null)
    const live = new Map<string, Set<unknown>>()
    const add = document.addEventListener.bind(document)
    const remove = document.removeEventListener.bind(document)
    vi.spyOn(document, "addEventListener").mockImplementation(
      (type, listener, options) => {
        live.set(type, (live.get(type) ?? new Set()).add(listener))
        add(type, listener, options)
      }
    )
    vi.spyOn(document, "removeEventListener").mockImplementation(
      (type, listener, options) => {
        live.get(type)?.delete(listener)
        remove(type, listener, options)
      }
    )
    const container = document.createElement("div")
    document.body.append(container)
    for (let attempt = 0; attempt < 2; attempt++)
      expect(
        () => new CloudScene(container, { tooltip: null, marker: null })
      ).toThrow()
    expect([...live].filter(([, listeners]) => listeners.size > 0)).toEqual([])
    expect(container.childElementCount).toBe(0)
    container.remove()
  })
})
