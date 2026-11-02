import { act } from "react"
import { toast } from "sonner"
import { afterEach, describe, expect, it, vi } from "vitest"

import { ApiError } from "@/api"
import { ThemeProvider } from "@/app/ThemeProvider"
import { Toaster } from "@/components/ui/sonner"
import { cleanup, render, stubMatchMedia } from "@/test/render"
import { ERROR_TOAST_MS, toastError } from "./toast"

afterEach(() => {
  act(() => {
    toast.dismiss()
  })
  cleanup()
})

const problem = new ApiError(503, "Service Unavailable", {
  title: "Service Unavailable",
  status: 503,
  detail: "The database is busy",
})

async function renderToaster() {
  stubMatchMedia()
  await render(
    <ThemeProvider>
      <Toaster />
    </ThemeProvider>
  )
}

async function toastTexts(): Promise<string[]> {
  await vi.waitFor(() => {
    expect(document.querySelectorAll("[data-sonner-toast]").length).toBe(1)
  })
  const toastElement = document.querySelector("[data-sonner-toast]")
  return [
    toastElement?.querySelector("[data-title]")?.textContent ?? "",
    toastElement?.querySelector("[data-description]")?.textContent ?? "",
  ]
}

describe("toastError", () => {
  it("shows the problem's title and detail", async () => {
    await renderToaster()
    act(() => {
      toastError(problem)
    })
    expect(await toastTexts()).toEqual([
      "Service Unavailable",
      "The database is busy",
    ])
  })

  it("stays as long as an error report (15 s), not sonner's 4 s", () => {
    const show = vi.spyOn(toast, "error")
    toastError(problem)
    expect(show).toHaveBeenCalledWith(
      "Service Unavailable",
      expect.objectContaining({ duration: ERROR_TOAST_MS })
    )
    expect(ERROR_TOAST_MS).toBe(15_000)
    show.mockRestore()
  })

  it("puts what failed first when given", async () => {
    await renderToaster()
    act(() => {
      toastError(problem, "Job 12 was not stopped")
    })
    expect(await toastTexts()).toEqual([
      "Job 12 was not stopped",
      "Service Unavailable: The database is busy",
    ])
  })
})
