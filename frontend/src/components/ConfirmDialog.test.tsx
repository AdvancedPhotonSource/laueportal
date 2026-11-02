import { useState } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { ApiError } from "@/api"
import { byRole, cleanup, click, render } from "@/test/render"
import { ConfirmDialog } from "./ConfirmDialog"

afterEach(cleanup)

const dialog = () => byRole("alertdialog")

async function renderStopDialog(onConfirm: () => unknown) {
  await render(
    <ConfirmDialog
      trigger={<button type="button">Stop</button>}
      title="Stop 2 jobs?"
      description="The selected jobs are stopped."
      confirmLabel="Stop jobs"
      destructive
      onConfirm={onConfirm}
    >
      <p>Jobs 12, 13</p>
    </ConfirmDialog>
  )
  await click(byRole("button", "Stop")[0])
  await vi.waitFor(() => {
    expect(dialog()).toHaveLength(1)
  })
}

describe("ConfirmDialog", () => {
  it("opens from its trigger, runs the action on confirm and closes", async () => {
    const onConfirm = vi.fn(() => Promise.resolve())
    await renderStopDialog(onConfirm)
    expect(dialog()[0]?.textContent).toContain("Stop 2 jobs?")
    expect(dialog()[0]?.textContent).toContain("Jobs 12, 13")

    await click(byRole("button", "Stop jobs")[0])
    expect(onConfirm).toHaveBeenCalledOnce()
    await vi.waitFor(() => {
      expect(dialog()).toHaveLength(0)
    })
  })

  it("closes on Cancel without running the action", async () => {
    const onConfirm = vi.fn()
    await renderStopDialog(onConfirm)
    await click(byRole("button", "Cancel")[0])
    await vi.waitFor(() => {
      expect(dialog()).toHaveLength(0)
    })
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it("disables its buttons while the action runs, keeping keyboard focus", async () => {
    let finish: () => void = () => undefined
    const onConfirm = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve
        })
    )
    await renderStopDialog(onConfirm)
    const confirm = byRole("button", "Stop jobs")[0]
    const cancel = byRole("button", "Cancel")[0]
    confirm?.focus()
    await click(confirm)
    for (const button of [confirm, cancel]) {
      expect(button?.getAttribute("aria-disabled")).toBe("true")
      // A button with the disabled attribute would drop the focus to the page.
      expect(button?.hasAttribute("disabled")).toBe(false)
    }
    expect(document.activeElement).toBe(confirm)

    // Neither button acts while the action runs.
    await click(confirm)
    await click(cancel)
    expect(onConfirm).toHaveBeenCalledOnce()
    expect(dialog()).toHaveLength(1)

    await vi.waitFor(() => {
      finish()
      expect(dialog()).toHaveLength(0)
    })
  })

  it("stays open with the problem when the action fails, keeping focus on Confirm", async () => {
    const onConfirm = vi.fn(() =>
      Promise.reject(
        new ApiError(503, "Service Unavailable", {
          title: "Service Unavailable",
          status: 503,
          detail: "The database is busy",
        })
      )
    )
    await renderStopDialog(onConfirm)
    const confirm = byRole("button", "Stop jobs")[0]
    confirm?.focus()
    await click(confirm)

    await vi.waitFor(() => {
      expect(byRole("alert", undefined, dialog()[0])).toHaveLength(1)
    })
    expect(dialog()[0]?.textContent).toContain("The database is busy")
    // Ready to try again, with the keyboard still on Confirm.
    expect(confirm?.getAttribute("aria-disabled")).not.toBe("true")
    expect(document.activeElement).toBe(confirm)
  })

  it("forgets the error once closed, also when its parent closes it", async () => {
    // The parent opens and closes the dialog itself (no onOpenChange).
    function Parent() {
      const [open, setOpen] = useState(true)
      return (
        <>
          <button
            type="button"
            onClick={() => {
              setOpen((current) => !current)
            }}
          >
            Toggle
          </button>
          <ConfirmDialog
            open={open}
            title="Stop job 12?"
            confirmLabel="Stop job"
            onConfirm={() => Promise.reject(new Error("The database is busy"))}
          />
        </>
      )
    }
    await render(<Parent />)
    await vi.waitFor(() => {
      expect(dialog()).toHaveLength(1)
    })
    await click(byRole("button", "Stop job")[0])
    await vi.waitFor(() => {
      expect(byRole("alert", undefined, dialog()[0])).toHaveLength(1)
    })

    await click(byRole("button", "Toggle")[0])
    await vi.waitFor(() => {
      expect(dialog()).toHaveLength(0)
    })
    await click(byRole("button", "Toggle")[0])
    await vi.waitFor(() => {
      expect(dialog()).toHaveLength(1)
    })
    expect(byRole("alert", undefined, dialog()[0])).toHaveLength(0)
  })

  it("can be controlled by its parent", async () => {
    const onOpenChange = vi.fn()
    await render(
      <ConfirmDialog
        open
        onOpenChange={onOpenChange}
        title="Move job 7 to the front?"
        confirmLabel="Move to Front"
        onConfirm={() => undefined}
      />
    )
    await vi.waitFor(() => {
      expect(dialog()).toHaveLength(1)
    })
    await click(byRole("button", "Move to Front")[0])
    await vi.waitFor(() => {
      expect(onOpenChange).toHaveBeenCalledWith(false)
    })
  })
})
