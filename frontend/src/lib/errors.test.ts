import { describe, expect, it } from "vitest"

import { ApiError, NetworkError } from "@/api"
import { describeError, UNREACHABLE_TITLE } from "./errors"

describe("describeError", () => {
  it("shows a problem's title and detail", () => {
    const error = new ApiError(404, "Not Found", {
      type: "about:blank",
      title: "Not found",
      status: 404,
      detail: "Scan 7 does not exist",
    })
    expect(describeError(error)).toEqual({
      title: "Not found",
      detail: "Scan 7 does not exist",
      status: 404,
    })
  })

  it("keeps a server error's instance and a validation problem's issues", () => {
    expect(
      describeError(
        new ApiError(500, "Internal Server Error", {
          title: "Internal Server Error",
          status: 500,
          detail: "Unexpected error",
          instance: "a1b2c3",
        })
      )
    ).toEqual({
      title: "Internal Server Error",
      detail: "Unexpected error",
      status: 500,
      instance: "a1b2c3",
    })

    expect(
      describeError(
        new ApiError(422, "Unprocessable Content", {
          title: "Validation failed",
          status: 422,
          detail: "1 problem",
          errors: [
            {
              loc: ["rows", 0, "scan_points"],
              message: "Invalid range '1-abc'",
              code: "range_syntax",
              severity: "error",
            },
          ],
        })
      )
    ).toMatchObject({
      title: "Validation failed",
      detail: "1 problem",
      issues: ["rows.0.scan_points: Invalid range '1-abc'"],
    })
  })

  it("does not repeat a detail equal to the title", () => {
    const error = new ApiError(503, "Service Unavailable", {
      title: "The database is busy",
      detail: "The database is busy",
    })
    expect(describeError(error)).toEqual({
      title: "The database is busy",
      status: 503,
    })
  })

  it("falls back to the HTTP status without a problem body", () => {
    expect(describeError(new ApiError(502, "Bad Gateway"))).toEqual({
      title: "HTTP 502 Bad Gateway",
      status: 502,
    })
  })

  it("says the server cannot be reached for a request without a response", () => {
    const summary = describeError(
      new NetworkError(new TypeError("Failed to fetch"))
    )
    expect(summary.title).toBe(UNREACHABLE_TITLE)
    expect(summary.detail).toMatch(/stopped or restarting/)
  })

  it("shows the message of other errors", () => {
    expect(describeError(new Error("x is undefined"))).toEqual({
      title: "Something went wrong",
      detail: "x is undefined",
    })
    expect(describeError(new Error(""))).toEqual({
      title: "Something went wrong",
    })
    expect(describeError("boom")).toEqual({
      title: "Something went wrong",
      detail: "boom",
    })
    expect(describeError(undefined)).toEqual({ title: "Something went wrong" })
  })
})
