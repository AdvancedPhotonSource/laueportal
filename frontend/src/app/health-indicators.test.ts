import { afterEach, describe, expect, it, vi } from "vitest"

import { ApiError, NetworkError, type Health } from "@/api"
import {
  getHealthWithin,
  HEALTH_POLL_MS,
  HealthTimeout,
  pillLabel,
  retryHealth,
  shellHealth,
} from "./health-indicators"
import { shouldRetry } from "./query-client"

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

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

// One test per state: a dot alone (no words) while the worker runs or the first answer is awaited, a pill with
// words when something is wrong.
describe("shellHealth", () => {
  it("is a ring, checking, until the first answer", () => {
    expect(
      shellHealth({ status: "pending", data: undefined, error: null })
    ).toEqual({
      kind: "checking",
      view: { form: "dot", tone: "unknown", state: "Worker: checking" },
    })
  })

  it("is the quiet dot while the worker runs", () => {
    expect(shellHealth(success(health()))).toEqual({
      kind: "live",
      view: { form: "dot", tone: "ok", state: "Worker: running" },
    })
  })

  it("is a warning pill, Worker late, when the worker is late", () => {
    expect(shellHealth(success(health({ worker: "late" })))).toEqual({
      kind: "live",
      view: {
        form: "pill",
        tone: "warning",
        state: "Worker: late",
        subject: "Worker",
        words: "late",
        note: "No sign of life for over 45 s",
      },
    })
  })

  it("is an error pill, Worker not running, when the worker is not running", () => {
    expect(shellHealth(success(health({ worker: "not_running" })))).toEqual({
      kind: "live",
      view: {
        form: "pill",
        tone: "error",
        state: "Worker: not running",
        subject: "Worker",
        words: "not running",
        note: "Queued runs wait until it starts",
      },
    })
  })

  it("is an error pill, Worker unknown, when the database does not answer", () => {
    const answer = health({ db: "error", worker: null })
    expect(shellHealth(success(answer))).toEqual({
      kind: "live",
      view: {
        form: "pill",
        tone: "error",
        state: "Worker: unknown",
        subject: "Worker",
        words: "unknown",
        note: "The database does not answer",
      },
    })
  })

  it("is an error pill, API unreachable, when no response arrives, even with an older answer", () => {
    expect(
      shellHealth({
        status: "error",
        data: health(),
        error: new NetworkError(new TypeError("Failed to fetch")),
      })
    ).toMatchObject({
      kind: "unreachable",
      view: {
        form: "pill",
        tone: "error",
        state: "API unreachable",
        subject: "API",
        words: "unreachable",
      },
      error: { title: "The server cannot be reached" },
    })
  })

  it("is an error pill, API error, for an HTTP error from the health check", () => {
    expect(
      shellHealth({
        status: "error",
        data: undefined,
        error: new ApiError(502, "Bad Gateway"),
      })
    ).toMatchObject({
      kind: "unreachable",
      view: {
        form: "pill",
        tone: "error",
        state: "API error",
        subject: "API",
        words: "error",
      },
      error: { title: "HTTP 502 Bad Gateway", status: 502 },
    })
  })

  it("names a pill by its subject and words", () => {
    const view = shellHealth(success(health({ worker: "not_running" }))).view
    expect(view.form === "pill" && pillLabel(view)).toBe("Worker not running")
  })
})

/** fetch that never answers, and rejects when its request is aborted, as a browser's does. */
function hungFetch() {
  return vi.fn(
    (_input: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          reject(new DOMException("The request was aborted", "AbortError"))
        })
      })
  )
}

describe("getHealthWithin", () => {
  it("resolves with the answer and leaves no timer behind", async () => {
    vi.useFakeTimers()
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(Response.json(health())))
    )
    await expect(
      getHealthWithin(new AbortController().signal)
    ).resolves.toEqual(health())
    expect(vi.getTimerCount()).toBe(0)
  })

  it("gives up after one poll interval: a hung API is unreachable", async () => {
    vi.useFakeTimers()
    const fetchMock = hungFetch()
    vi.stubGlobal("fetch", fetchMock)
    const settled = vi.fn((_error: unknown) => undefined)
    const request = getHealthWithin(new AbortController().signal).catch(
      (error: unknown) => {
        settled(error)
      }
    )
    await vi.advanceTimersByTimeAsync(HEALTH_POLL_MS - 1)
    expect(settled).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(1)
    await request
    const [error] = settled.mock.calls[0] ?? []
    expect(error).toBeInstanceOf(HealthTimeout)
    expect(error).toBeInstanceOf(NetworkError)
    expect(
      shellHealth({ status: "error", data: undefined, error }).view
    ).toMatchObject({ subject: "API", words: "unreachable" })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("keeps the AbortError of a cancel through the query's signal", async () => {
    vi.useFakeTimers()
    vi.stubGlobal("fetch", hungFetch())
    const query = new AbortController()
    const request = getHealthWithin(query.signal)
    query.abort()
    await expect(request).rejects.toMatchObject({ name: "AbortError" })
    await expect(request).rejects.not.toBeInstanceOf(HealthTimeout)
    expect(vi.getTimerCount()).toBe(0)
  })
})

describe("retryHealth", () => {
  it("does not retry a timed-out check; otherwise follows the app's rule", () => {
    const timeout = new HealthTimeout(new DOMException("", "AbortError"))
    expect(retryHealth(0, timeout)).toBe(false)
    for (const error of [
      new NetworkError(new TypeError("Failed to fetch")),
      new ApiError(502, "Bad Gateway"),
      new ApiError(404, "Not Found"),
    ]) {
      for (const count of [0, 1, 2]) {
        expect(retryHealth(count, error)).toBe(shouldRetry(count, error))
      }
    }
    expect(
      retryHealth(0, new NetworkError(new TypeError("Failed to fetch")))
    ).toBe(true)
  })
})
