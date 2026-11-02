import { describe, expect, it } from "vitest"

import { ApiError, NetworkError, type Health } from "@/api"
import { shellHealth } from "./health-indicators"

function health(overrides: Partial<Health> = {}): Health {
  return {
    db: "ok",
    worker: "running",
    version: "2.0.0.dev0",
    ...overrides,
  }
}

const success = (data: Health) =>
  ({ status: "success", data, error: null }) as const

describe("shellHealth", () => {
  it("is checking until the first answer", () => {
    expect(
      shellHealth({ status: "pending", data: undefined, error: null })
    ).toEqual({ kind: "checking" })
  })

  it.each([
    ["running", { tone: "ok", label: "Worker", state: "running" }],
    [
      "late",
      {
        tone: "warning",
        label: "Worker",
        state: "late",
        note: "No sign of life for over 45 s",
      },
    ],
    [
      "not_running",
      {
        tone: "error",
        label: "Worker",
        state: "not running",
        note: "Queued runs wait until it starts",
      },
    ],
    [
      null,
      {
        tone: "unknown",
        label: "Worker",
        state: "unknown",
        note: "The database does not answer",
      },
    ],
  ] as const)("shows the worker %s", (worker, expected) => {
    expect(shellHealth(success(health({ worker })))).toEqual({
      kind: "live",
      worker: expected,
    })
  })

  it("says the API is unreachable when no response arrives, even with an older answer", () => {
    expect(
      shellHealth({
        status: "error",
        data: health(),
        error: new NetworkError(new TypeError("Failed to fetch")),
      })
    ).toMatchObject({
      kind: "unreachable",
      label: "API unreachable",
      error: { title: "The server cannot be reached" },
    })
  })

  it("reports an HTTP error from the health check as an API error", () => {
    expect(
      shellHealth({
        status: "error",
        data: undefined,
        error: new ApiError(502, "Bad Gateway"),
      })
    ).toMatchObject({
      kind: "unreachable",
      label: "API error",
      error: { title: "HTTP 502 Bad Gateway", status: 502 },
    })
  })
})
