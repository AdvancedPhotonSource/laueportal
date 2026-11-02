import { act } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { byRole, cleanup, click, enterText, render } from "@/test/render"
import { DepthControl } from "./DepthControl"
import { DEPTH_DEBOUNCE_MS } from "./depth-view"

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

const readout = () =>
  Array.from(document.querySelectorAll("dd")).map((dd) => dd.textContent)

describe("the depth control", () => {
  it("shows a slider move's index and depth at once, and asks for the depth once the slider rests", async () => {
    vi.useFakeTimers()
    const onChange = vi.fn()
    await render(
      <DepthControl
        value={37}
        count={101}
        depthAt={(index) => index - 50}
        range={[-50, 50]}
        onChange={onChange}
      />
    )
    expect(readout()).toEqual(["37 (0 … 100)", "-13 µm (-50 … 50)"])

    const slider = document.querySelector('input[aria-label="Depth index"]')
    for (const value of ["38", "39", "40"]) {
      await enterText(slider, value)
    }
    expect(readout()).toEqual(["40 (0 … 100)", "-10 µm (-50 … 50)"])
    await act(() => vi.advanceTimersByTimeAsync(DEPTH_DEBOUNCE_MS - 1))
    expect(onChange).not.toHaveBeenCalled()
    await act(() => vi.advanceTimersByTimeAsync(1))
    expect(onChange.mock.calls).toEqual([[40]])

    // The buttons ask at once.
    await click(byRole("button", "Next depth")[0])
    expect(onChange.mock.calls).toEqual([[40], [38]])
  })
})
