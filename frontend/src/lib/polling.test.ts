import { describe, expect, it } from "vitest"

import { ApiError } from "@/api"
import { backoff, failureStreak, isGone } from "./polling"

describe("polling robustness", () => {
  it("doubles the wait per consecutive failure, up to a minute", () => {
    expect([0, 1, 2, 3, 4].map((failures) => backoff(5000, failures))).toEqual([
      5000, 10_000, 20_000, 40_000, 60_000,
    ])
    // A wait longer than the cap stays as it is.
    expect(backoff(90_000, 2)).toBe(90_000)
  })

  it("counts a query's errors since its last success; a 404 is gone", () => {
    const streak = failureStreak()
    const failed = new ApiError(503, "Service Unavailable")
    expect(streak({ error: null, errorUpdateCount: 0 })).toBe(0)
    expect(streak({ error: failed, errorUpdateCount: 1 })).toBe(1)
    expect(streak({ error: failed, errorUpdateCount: 2 })).toBe(2)
    expect(streak({ error: null, errorUpdateCount: 2 })).toBe(0)
    expect(streak({ error: failed, errorUpdateCount: 3 })).toBe(1)
    expect(isGone(new ApiError(404, "Not Found"))).toBe(true)
    expect(isGone(failed)).toBe(false)
  })
})
