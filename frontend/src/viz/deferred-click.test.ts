import { afterEach, describe, expect, it, vi } from "vitest"

import { deferredClick, DOUBLE_CLICK_MS } from "./deferred-click"

afterEach(() => {
  vi.useRealTimers()
})

describe("deferredClick", () => {
  it("reports a click after the double-click interval, and none for a double-click", () => {
    vi.useFakeTimers()
    const reported: string[] = []
    const gate = deferredClick()
    gate.click(() => reported.push("a"))
    vi.advanceTimersByTime(DOUBLE_CLICK_MS - 1)
    expect(reported).toEqual([])
    vi.advanceTimersByTime(1)
    expect(reported).toEqual(["a"])
    // A double-click: its first click is pending when the dblclick cancels it.
    gate.click(() => reported.push("b"))
    vi.advanceTimersByTime(DOUBLE_CLICK_MS / 2)
    gate.cancel()
    vi.advanceTimersByTime(DOUBLE_CLICK_MS * 2)
    expect(reported).toEqual(["a"])
  })
})
