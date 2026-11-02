import { act, useState } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { byRole, cleanup, click, enterText, render } from "@/test/render"
import { useDistribution } from "../color/compute"
import { ColorBar } from "./ColorBar"
import {
  colorbarSettings,
  FRAME_AUTO_RANGE,
  type ColorbarSettings,
} from "./logic"

afterEach(cleanup)

/** The bar on the gamma scale, keeping its settings as a page does; `seen` records each gamma. */
async function renderGamma(seen: number[]) {
  function Page() {
    const [value, setValue] = useState<ColorbarSettings>({
      ...colorbarSettings(FRAME_AUTO_RANGE),
      scale: "gamma",
    })
    return (
      <ColorBar
        distribution={null}
        colormap="gray"
        value={value}
        onChange={(next) => {
          seen.push(next.gamma)
          setValue(next)
        }}
        defaultAuto={FRAME_AUTO_RANGE}
      />
    )
  }
  await render(<Page />)
  const field = document.querySelector('input[aria-label="Gamma"]')
  if (!(field instanceof HTMLInputElement)) throw new Error("no gamma field")
  return field
}

describe("ColorBar", () => {
  it("keeps the gamma field's text while typed and commits it on Enter or blur, clamped", async () => {
    const seen: number[] = []
    const field = await renderGamma(seen)
    act(() => {
      field.focus()
    })
    // "0.5" passes through "0" and "0.", which are not gammas yet.
    for (const text of ["", "0", "0.", "0.5"]) await enterText(field, text)
    expect(field.value).toBe("0.5")
    expect(seen).toEqual([])
    await act(async () => {
      field.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true })
      )
      await Promise.resolve()
    })
    expect(seen).toEqual([0.5])
    expect(field.value).toBe("0.5")
    // Beyond the limits: clamped on blur; not a number: reverts.
    await enterText(field, "200")
    act(() => {
      field.blur()
    })
    expect(seen).toEqual([0.5, 20])
    act(() => {
      field.focus()
    })
    await enterText(field, "abc")
    act(() => {
      field.blur()
    })
    expect(seen).toEqual([0.5, 20])
    expect(field.value).toBe("20")
  })

  it("shows a failed computation on the bar, and a retry computes it in the page", async () => {
    // A worker that fails as one out of memory does; jsdom draws no canvas.
    vi.stubGlobal(
      "Worker",
      class {
        onerror: ((e: Event) => void) | null = null
        constructor() {
          setTimeout(() => {
            this.onerror?.(
              new ErrorEvent("error", { message: "out of memory" })
            )
          })
        }
        postMessage() {}
        terminate() {}
      }
    )
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null)
    const frame = new Uint16Array((1 << 18) + 1).fill(7)
    function Page() {
      const { distribution, error, retry } = useDistribution(frame)
      return (
        <>
          <output>{distribution ? distribution.n : "none"}</output>
          <ColorBar
            distribution={distribution}
            error={error}
            onRetry={retry}
            colormap="gray"
            value={colorbarSettings(FRAME_AUTO_RANGE)}
            onChange={() => {}}
            defaultAuto={FRAME_AUTO_RANGE}
          />
        </>
      )
    }
    const flush = () =>
      act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 5))
      })
    await render(<Page />)
    await flush()
    expect(byRole("alert")[0]?.textContent).toContain("out of memory")
    expect(document.querySelector("output")?.textContent).toBe("none")
    await click(byRole("button", "Retry")[0])
    await flush()
    expect(byRole("alert")).toHaveLength(0)
    expect(document.querySelector("output")?.textContent).toBe(
      String(frame.length)
    )
  })
})
