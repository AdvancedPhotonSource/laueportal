import { act } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { cleanup, enterText, render } from "@/test/render"
import { NumberField } from "./NumberField"

afterEach(cleanup)

describe("NumberField", () => {
  it("applies a typed number on Enter or on leaving it, never per keystroke, and restores an invalid one", async () => {
    const onCommit = vi.fn()
    const container = await render(
      <NumberField
        aria-label="Marker size"
        value={40}
        isValid={(n) => Number.isInteger(n) && n >= 1 && n <= 75}
        onCommit={onCommit}
      />
    )
    const input = container.querySelector("input") as HTMLInputElement

    await enterText(input, "1")
    await enterText(input, "12")
    expect(onCommit).not.toHaveBeenCalled()
    act(() => {
      input.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true })
      )
    })
    expect(onCommit).toHaveBeenCalledExactlyOnceWith(12)

    await enterText(input, "20")
    act(() => {
      input.dispatchEvent(new FocusEvent("focusout", { bubbles: true }))
    })
    expect(onCommit).toHaveBeenLastCalledWith(20)

    await enterText(input, "80")
    expect(input.getAttribute("aria-invalid")).toBe("true")
    act(() => {
      input.dispatchEvent(new FocusEvent("focusout", { bubbles: true }))
    })
    expect(onCommit).toHaveBeenCalledTimes(2)
    expect(input.value).toBe("40")
  })
})
