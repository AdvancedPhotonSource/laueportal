import { act } from "react"
import { toast } from "sonner"
import { afterEach, describe, expect, it, vi } from "vitest"

import { unpackViewLink } from "@/lib/view-link"
import { resetIndexingViewerStore } from "@/test/indexing"
import { byRole, cleanup, click, render } from "@/test/render"
import { CopyViewLinkButton } from "./CopyViewLinkButton"

// "Copy link to this view": the Clipboard API, the selection's copy, then the dialog to copy by hand.

const SEARCH = { tab: "pole", step: 3 } as const

/** The Clipboard API's writeText, or none (an insecure context). */
function stubClipboard(write: ((text: string) => Promise<void>) | null) {
  const writeText = write ? vi.fn(write) : null
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: writeText ? { writeText } : undefined,
  })
  return writeText
}

function stubExecCommand(copies: boolean) {
  const execCommand = vi.fn((command: string) => command === "copy" && copies)
  Object.defineProperty(document, "execCommand", {
    configurable: true,
    value: execCommand,
  })
  return execCommand
}

/** Mount the button and let the view be packed ahead of the click. */
async function mountPacked() {
  await render(<CopyViewLinkButton search={SEARCH} />)
  await act(async () => {
    await new Promise((done) => setTimeout(done, 300))
  })
  const [button] = byRole("button", "Copy link to this view")
  if (!button) throw new Error("no button")
  return button
}

afterEach(() => {
  cleanup()
  resetIndexingViewerStore()
  vi.restoreAllMocks()
  Reflect.deleteProperty(navigator, "clipboard")
  Reflect.deleteProperty(document, "execCommand")
})

describe("CopyViewLinkButton", () => {
  it("writes the packed link with the Clipboard API in the click itself", async () => {
    const writeText = stubClipboard(() => Promise.resolve())
    const execCommand = stubExecCommand(true)
    const success = vi.spyOn(toast, "success")
    const button = await mountPacked()

    // No await between the click and the write: the click's user activation still holds.
    act(() => {
      button.click()
    })
    expect(writeText).toHaveBeenCalledTimes(1)
    await act(() => Promise.resolve())
    expect(success).toHaveBeenCalledWith("Link copied")
    expect(execCommand).not.toHaveBeenCalled()

    const url = new URL(String(writeText?.mock.calls[0]?.[0]))
    const link = await unpackViewLink(url.searchParams.get("link") ?? "")
    expect(link?.url).toMatchObject({ tab: "pole", step: "3" })
  })

  it("copies through the selection where the Clipboard API is missing (plain http)", async () => {
    stubClipboard(null)
    const execCommand = stubExecCommand(true)
    const success = vi.spyOn(toast, "success")
    const button = await mountPacked()

    act(() => {
      button.click()
    })
    expect(execCommand).toHaveBeenCalledWith("copy")
    await act(() => Promise.resolve())
    expect(success).toHaveBeenCalledWith("Link copied")
    expect(document.querySelector("textarea")).toBeNull()
  })

  it("shows the link selected in a dialog when the browser refuses both", async () => {
    stubClipboard(() => Promise.reject(new DOMException("denied")))
    stubExecCommand(false)
    const success = vi.spyOn(toast, "success")
    await click(await mountPacked())

    await vi.waitFor(() => {
      const field = document.querySelector<HTMLInputElement>(
        'input[aria-label="Link to this view"]'
      )
      if (!field) throw new Error("no field")
      expect(field.value).toMatch(/\?link=[A-Za-z0-9_-]{20,}$/)
      expect(document.activeElement).toBe(field)
      expect(field.selectionStart).toBe(0)
      expect(field.selectionEnd).toBe(field.value.length)
    })
    expect(success).not.toHaveBeenCalled()
  })
})
