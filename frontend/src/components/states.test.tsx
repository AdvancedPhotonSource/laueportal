import { afterEach, describe, expect, it, vi } from "vitest"

import { ApiError } from "@/api"
import { byRole, cleanup, click, render } from "@/test/render"
import { EmptyState } from "./EmptyState"
import { ErrorState } from "./ErrorState"
import { LoadingState } from "./LoadingState"

afterEach(cleanup)

describe("ErrorState", () => {
  it("shows a problem's title, detail and reference, and retries", async () => {
    const onRetry = vi.fn()
    const error = new ApiError(500, "Internal Server Error", {
      title: "Internal Server Error",
      status: 500,
      detail: "The scan list could not be read",
      instance: "req-42",
    })
    await render(<ErrorState error={error} onRetry={onRetry} />)

    const [alert] = byRole("alert")
    expect(alert?.textContent).toContain("Internal Server Error")
    expect(alert?.textContent).toContain("The scan list could not be read")
    expect(alert?.textContent).toContain("req-42")

    await click(byRole("button", "Retry")[0])
    expect(onRetry).toHaveBeenCalledOnce()
  })

  it("shows a reference its detail already names once", async () => {
    const instance = "urn:uuid:5f0c2a7e-1b8e-4f3a-9a51-0c6d2e9b7f10"
    const error = new ApiError(500, "Internal Server Error", {
      title: "Internal Server Error",
      status: 500,
      detail: `Unexpected error. The server log has the details under ${instance}.`,
      instance,
    })
    await render(<ErrorState error={error} />)

    const text = byRole("alert")[0]?.textContent ?? ""
    expect(text.split(instance)).toHaveLength(2)
    expect(text).not.toContain("Reference")
  })

  it("lists a validation problem's issues and says when older data is shown", async () => {
    const error = new ApiError(422, "Unprocessable Content", {
      title: "Validation failed",
      detail: "1 problem",
      errors: [
        {
          loc: ["query", "limit"],
          message: "must be at most 500",
          code: "too_big",
          severity: "error",
        },
      ],
    })
    await render(<ErrorState error={error} stale />)

    const [alert] = byRole("alert")
    expect(alert?.querySelector("li")?.textContent).toBe(
      "query.limit: must be at most 500"
    )
    expect(alert?.textContent).toContain("Showing the last data loaded.")
    expect(byRole("button", "Retry")).toHaveLength(0)
  })

  it("disables the retry while it runs", async () => {
    await render(
      <ErrorState
        error={new Error("boom")}
        onRetry={() => undefined}
        retrying
      />
    )
    expect(byRole("button", "Retry")[0]?.hasAttribute("disabled")).toBe(true)
  })
})

describe("LoadingState", () => {
  it.each(["page", "table", "cards"] as const)(
    "announces its label as a busy status (%s)",
    async (variant) => {
      await render(
        <LoadingState label="Loading scans" variant={variant} count={3} />
      )
      const [status] = byRole("status")
      expect(status?.getAttribute("aria-busy")).toBe("true")
      expect(status?.textContent).toBe("Loading scans")
      expect(
        status?.querySelectorAll('[data-slot="skeleton"]').length
      ).toBeGreaterThanOrEqual(3)
    }
  )
})

describe("EmptyState", () => {
  it("shows the title, the description and the actions", async () => {
    const container = await render(
      <EmptyState title="No scans yet" description="Import a scan log.">
        <button type="button">Import Scans</button>
      </EmptyState>
    )
    expect(container.textContent).toContain("No scans yet")
    expect(container.textContent).toContain("Import a scan log.")
    expect(byRole("button", "Import Scans")).toHaveLength(1)
  })
})
