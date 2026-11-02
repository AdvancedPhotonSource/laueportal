import { act } from "react"
import { afterEach, describe, expect, it } from "vitest"

import { resetIndexingViewerStore } from "@/test/indexing"
import { cleanup, enterText, render } from "@/test/render"
import { ScopeBar } from "./ScopeBar"
import { useIndexingViewerStore } from "./viewer-store"

const field = () =>
  document.querySelector<HTMLInputElement>('input[type="number"]')
const hint = () =>
  document.getElementById(field()?.getAttribute("aria-describedby") ?? "")
    ?.textContent ?? null
const scope = () => useIndexingViewerStore.getState()

afterEach(() => {
  cleanup()
  resetIndexingViewerStore()
})

describe("the scope bar", () => {
  it("says what N may be only while it is invalid, and restores the saved N when left", async () => {
    await render(<ScopeBar />)
    field()?.focus()
    await enterText(field(), "-1")
    expect(field()?.getAttribute("aria-invalid")).toBe("true")
    expect(hint()).toBe("a whole number, 0 to 99999")
    expect(scope().scope.skipPeaksAtMost).toBe(3)

    act(() => {
      field()?.blur()
    })
    expect(field()?.value).toBe("3")
    expect(field()?.hasAttribute("aria-invalid")).toBe(false)
    expect(hint()).toBeNull()
    expect(field()?.title).toBe("0 keeps every step")
  })

  it("saves a typed N at once and applies it on Enter or when the field is left", async () => {
    await render(<ScopeBar />)
    field()?.focus()
    await enterText(field(), "12")
    expect(scope().scope.skipPeaksAtMost).toBe(12)
    expect(scope().appliedScope.skipPeaksAtMost).toBe(3)
    act(() => {
      field()?.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true })
      )
    })
    expect(scope().appliedScope.skipPeaksAtMost).toBe(12)

    await enterText(field(), "7")
    expect(scope().appliedScope.skipPeaksAtMost).toBe(12)
    act(() => {
      field()?.blur()
    })
    expect(scope().appliedScope.skipPeaksAtMost).toBe(7)
  })
})
