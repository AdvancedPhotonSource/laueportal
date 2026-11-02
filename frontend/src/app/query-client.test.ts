import { describe, expect, it } from "vitest"

import { ApiError } from "@/api"
import { shouldRetry } from "./query-client"

describe("shouldRetry", () => {
  it("does not retry client errors", () => {
    expect(shouldRetry(0, new ApiError(404, "Not Found"))).toBe(false)
    expect(shouldRetry(0, new ApiError(422, "Unprocessable Content"))).toBe(
      false
    )
  })

  it("retries timeouts and rate limits", () => {
    expect(shouldRetry(0, new ApiError(408, "Request Timeout"))).toBe(true)
    expect(shouldRetry(1, new ApiError(429, "Too Many Requests"))).toBe(true)
    expect(shouldRetry(2, new ApiError(429, "Too Many Requests"))).toBe(false)
  })

  it("retries server and network errors twice", () => {
    expect(shouldRetry(0, new ApiError(503, "Service Unavailable"))).toBe(true)
    expect(shouldRetry(1, new TypeError("Failed to fetch"))).toBe(true)
    expect(shouldRetry(2, new TypeError("Failed to fetch"))).toBe(false)
  })
})
