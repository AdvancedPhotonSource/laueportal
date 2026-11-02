import { describe, expect, it } from "vitest"

import { earlierTime, LIVE_POLL_MS, planLiveUpdate } from "./live"

interface Job {
  job_id: number
  status: string
  n_processed: number
}

const job = (job_id: number, status: string, n_processed = 0): Job => ({
  job_id,
  status,
  n_processed,
})

function plan(
  loaded: Job[],
  changes: Job[],
  { total = changes.length, sortFields = [] as string[] } = {}
) {
  return planLiveUpdate({
    changes,
    total,
    loaded: new Map(loaded.map((row) => [String(row.job_id), row])),
    getRowId: (row) => String(row.job_id),
    isActive: (row) => row.status === "queued" || row.status === "running",
    sortFields,
  })
}

describe("planLiveUpdate", () => {
  it("polls every 5 s", () => {
    expect(LIVE_POLL_MS).toBe(5000)
  })

  it("updates loaded rows in place when they changed and keep their places", () => {
    const loaded = [
      job(3, "running", 1),
      job(2, "queued"),
      job(1, "finished", 9),
    ]
    expect(plan(loaded, [job(3, "running", 4), job(2, "running", 0)])).toEqual({
      kind: "patch",
      rows: [job(3, "running", 4), job(2, "running", 0)],
    })
  })

  it("leaves unchanged rows alone", () => {
    const loaded = [job(3, "running", 1), job(2, "queued")]
    expect(plan(loaded, [job(3, "running", 1), job(2, "queued")])).toEqual({
      kind: "patch",
      rows: [],
    })
  })

  it("refreshes when a changed row is not loaded (a new run, or one entering the filter)", () => {
    const loaded = [job(3, "running", 1)]
    expect(plan(loaded, [job(4, "queued"), job(3, "running", 1)]).kind).toBe(
      "refresh"
    )
  })

  it("refreshes when an active row is missing from the changes (it left the filter)", () => {
    const loaded = [job(3, "running", 1), job(2, "queued")]
    expect(plan(loaded, [job(3, "running", 2)])).toMatchObject({
      kind: "refresh",
      reason: "row 2 left the filter",
    })
  })

  it("refreshes when a change touches a sorted field (the row may move)", () => {
    const loaded = [job(3, "running", 1), job(2, "running", 5)]
    const changes = [job(3, "running", 7), job(2, "running", 5)]
    expect(plan(loaded, changes, { sortFields: ["n_processed"] }).kind).toBe(
      "refresh"
    )
    expect(plan(loaded, changes, { sortFields: ["job_id"] }).kind).toBe("patch")
  })

  it("refreshes when there are more changes than one answer holds", () => {
    const loaded = [job(3, "running", 1)]
    expect(plan(loaded, [job(3, "running", 2)], { total: 1001 }).kind).toBe(
      "refresh"
    )
  })
})

describe("earlierTime", () => {
  it("keeps the earlier server time, so no change between them is missed", () => {
    const a = "2026-10-19T12:00:00.250000Z"
    const b = "2026-10-19T12:00:05Z"
    expect(earlierTime(undefined, b)).toBe(b)
    expect(earlierTime(a, b)).toBe(a)
    expect(earlierTime(b, a)).toBe(a)
    expect(earlierTime("garbage", b)).toBe(b)
  })
})
