import { describe, expect, it } from "vitest"

import { RunStatus } from "@/api"
import {
  isActiveStatus,
  isRunStatus,
  RUN_STATUS_LABELS,
  runProgress,
  runStatusText,
} from "./run-status"

describe("run statuses", () => {
  it("labels the API's statuses (Queued … Cancelled)", () => {
    expect(Object.values(RunStatus).map((s) => RUN_STATUS_LABELS[s])).toEqual([
      "Queued",
      "Running",
      "Finished",
      "Failed",
      "Cancelled",
    ])
  })

  it("knows the API's values only", () => {
    expect(isRunStatus("running")).toBe(true)
    expect(isRunStatus("Running")).toBe(false)
    expect(isRunStatus(1)).toBe(false)
    expect(isRunStatus("toString")).toBe(false)
  })

  it("counts queued and running as active", () => {
    expect(isActiveStatus("queued")).toBe(true)
    expect(isActiveStatus("running")).toBe(true)
    for (const status of ["finished", "failed", "cancelled", undefined]) {
      expect(isActiveStatus(status)).toBe(false)
    }
  })

  it("shows processed/total for a running run only", () => {
    const progress = { processed: 3, total: 10 }
    expect(runStatusText("running", progress)).toBe("Running 3/10")
    expect(runStatusText("running")).toBe("Running")
    expect(runStatusText("running", { processed: 0, total: 0 })).toBe("Running")
    expect(runStatusText("queued", progress)).toBe("Queued")
    expect(runStatusText("finished", progress)).toBe("Finished")
  })

  it("names an unknown status", () => {
    expect(runStatusText("paused")).toBe("Unknown (paused)")
  })
})

describe("runProgress", () => {
  const counts = { n_inputs: 10, n_succeeded: 5, n_failed: 1, n_not_run: 2 }

  it("writes the progress text: processed/total, then failed and not run when any", () => {
    expect(runProgress(counts, "running").text).toBe(
      "6/10 processed, 1 failed, 2 not run"
    )
    expect(
      runProgress({ ...counts, n_failed: 0, n_not_run: 0 }, "running").text
    ).toBe("5/10 processed")
  })

  it("splits the bar into succeeded, failed, not run and pending inputs", () => {
    const { segments, processed, total } = runProgress(counts, "running")
    expect({ processed, total }).toEqual({ processed: 6, total: 10 })
    expect(
      segments.map(({ kind, count, percent, title }) => [
        kind,
        count,
        percent,
        title,
      ])
    ).toEqual([
      ["succeeded", 5, 50, "5 succeeded"],
      ["failed", 1, 10, "1 failed"],
      ["notRun", 2, 20, "2 not run"],
      ["pending", 2, 20, "2 pending"],
    ])
  })

  it("moves the pending inputs while the run runs only", () => {
    const pending = (status: RunStatus) =>
      runProgress(counts, status).segments.find((s) => s.kind === "pending")
    expect(pending("running")?.animated).toBe(true)
    expect(pending("queued")?.animated).toBe(false)
    expect(pending("queued")?.title).toBe("2 pending")
    // After the run ended, inputs neither processed nor marked not run are unaccounted.
    expect(pending("failed")?.title).toBe("2 unaccounted")
    expect(pending("failed")?.animated).toBe(false)
  })

  it("leaves empty segments out", () => {
    const done = runProgress(
      { n_inputs: 4, n_succeeded: 4, n_failed: 0, n_not_run: 0 },
      "finished"
    )
    expect(done.segments.map((s) => s.kind)).toEqual(["succeeded"])
    expect(done.text).toBe("4/4 processed")
  })

  it('says "No inputs" for a run without inputs', () => {
    const none = runProgress(
      { n_inputs: 0, n_succeeded: 0, n_failed: 0, n_not_run: 0 },
      "queued"
    )
    expect(none).toEqual({
      total: 0,
      processed: 0,
      text: "No inputs",
      segments: [],
    })
  })

  it("never shows negative pending inputs", () => {
    const over = runProgress(
      { n_inputs: 2, n_succeeded: 3, n_failed: 0, n_not_run: 0 },
      "running"
    )
    expect(over.segments.map((s) => s.kind)).toEqual(["succeeded"])
  })
})
