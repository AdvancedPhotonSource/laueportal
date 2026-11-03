import { afterEach, describe, expect, it, vi } from "vitest"
import type { CreatedRun } from "@/api"
import { cleanup, renderWithRouter } from "@/test/render"
import { SubmitMessage } from "./submission-views"
import type { RunSubmission } from "./use-run-submission"

const EXISTING: CreatedRun = {
  run: { id: 7, kind: "indexing", label: "I7" },
  job_id: 31,
  status: "queued",
  target: "local",
  queue_id: "run_31",
  failure: null,
}

afterEach(cleanup)

describe("SubmitMessage", () => {
  it("shows a 409's message with the run its key created and New Submission", async () => {
    const submission = {
      submitState: { status: "idle" },
      message:
        "Idempotency-Key conflict: This Idempotency-Key already created I7 (job 31) with other values",
      conflict: true,
      existing: EXISTING,
      canSubmit: true,
      validateNow: vi.fn(),
      newSession: vi.fn(),
    } as unknown as RunSubmission
    const page = await renderWithRouter(
      <SubmitMessage submission={submission} />
    )
    const links = Array.from(page.querySelectorAll("a"), (link) => [
      link.textContent,
      link.getAttribute("href"),
    ])
    expect(links).toEqual([
      ["I7", "/indexing/7"],
      ["31", "/jobs/31"],
    ])
    expect(page.textContent).toContain("New Submission")
  })
})
