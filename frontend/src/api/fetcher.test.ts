import { describe, expect, expectTypeOf, it, vi } from "vitest"

import {
  ApiError,
  apiBaseUrl,
  apiFetch,
  apiUrl,
  NetworkError,
  PORTAL_HEADER,
  type ErrorType,
} from "./fetcher"
import { useGetHealth, type Health } from "./index"

function stubFetch(response: Response) {
  const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
    Promise.resolve(response)
  )
  vi.stubGlobal("fetch", fetchMock)
  return fetchMock
}

describe("apiBaseUrl / apiUrl", () => {
  it("uses no prefix when the app is served at the root", () => {
    expect(apiBaseUrl("/")).toBe("")
    expect(apiUrl("/api/v1/health", "/")).toBe("/api/v1/health")
  })

  it("prefixes the mount point when the app is served below the root", () => {
    expect(apiBaseUrl("/portal/")).toBe("/portal")
    expect(apiUrl("/api/v1/health", "/portal/")).toBe("/portal/api/v1/health")
  })

  it("defaults to Vite's BASE_URL", () => {
    expect(apiUrl("/api/v1/health")).toBe(
      `${import.meta.env.BASE_URL.replace(/\/$/, "")}/api/v1/health`
    )
  })

  it("rejects relative paths", () => {
    expect(() => apiUrl("api/v1/health")).toThrow(/must start with/)
  })
})

describe("apiFetch", () => {
  it("returns the parsed JSON body and asks for JSON", async () => {
    const fetchMock = stubFetch(Response.json({ db: "ok" }))
    await expect(
      apiFetch("/api/v1/health", { method: "GET" })
    ).resolves.toEqual({ db: "ok" })

    const [url, init] = fetchMock.mock.calls[0] ?? []
    expect(url).toBe(apiUrl("/api/v1/health"))
    expect(new Headers(init?.headers).get("Accept")).toBe("application/json")
    expect(init?.method).toBe("GET")
  })

  it("sends X-Laue-Portal: 1 with every request, which the API needs for a change", async () => {
    // A page on another origin cannot send a custom header without a CORS preflight, which the API never
    // grants: the API refuses a POST, PUT or DELETE without it.
    const fetchMock = stubFetch(new Response(null, { status: 204 }))
    await apiFetch("/api/v1/jobs/1/cancel", { method: "POST" })
    await apiFetch("/api/v1/scans/1/catalog", {
      method: "PUT",
      headers: { "Content-Type": "application/json", "X-Laue-Portal": "0" },
      body: "{}",
    })
    await apiFetch("/api/v1/health")
    expect(PORTAL_HEADER).toEqual(["X-Laue-Portal", "1"])
    for (const [, init] of fetchMock.mock.calls) {
      expect(new Headers(init?.headers).get("X-Laue-Portal")).toBe("1")
    }
    const [, put] = fetchMock.mock.calls[1] ?? []
    expect(new Headers(put?.headers).get("Content-Type")).toBe(
      "application/json"
    )
  })

  it("returns undefined for 204 No Content", async () => {
    stubFetch(new Response(null, { status: 204 }))
    await expect(
      apiFetch("/api/v1/jobs/1/cancel", { method: "POST" })
    ).resolves.toBeUndefined()
  })

  it("returns text for text/* and a Blob for binary bodies", async () => {
    stubFetch(
      new Response("hello", {
        headers: { "Content-Type": "text/plain; charset=utf-8" },
      })
    )
    await expect(apiFetch("/api/v1/jobs/1/log")).resolves.toBe("hello")

    stubFetch(
      new Response(new Uint8Array([1, 2, 3]), {
        headers: { "Content-Type": "application/vnd.apache.arrow.stream" },
      })
    )
    const blob = await apiFetch<Blob>("/api/v1/indexing-runs/1/map")
    expect(blob).toBeInstanceOf(Blob)
    expect(blob.size).toBe(3)
  })

  it("throws ApiError with the problem+json body", async () => {
    const problem = {
      type: "about:blank",
      title: "Not found",
      status: 404,
      detail: "Scan 7 does not exist",
    }
    stubFetch(
      new Response(JSON.stringify(problem), {
        status: 404,
        statusText: "Not Found",
        headers: { "Content-Type": "application/problem+json" },
      })
    )
    const error: unknown = await apiFetch("/api/v1/scans/7").catch(
      (e: unknown) => e
    )
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({
      status: 404,
      problem,
      message: "Scan 7 does not exist",
    })
  })

  it("uses the problem title when there is no detail", async () => {
    stubFetch(
      Response.json(
        { type: "about:blank", title: "Method Not Allowed", status: 405 },
        {
          status: 405,
          statusText: "Method Not Allowed",
          headers: { "Content-Type": "application/problem+json" },
        }
      )
    )
    const error: unknown = await apiFetch("/api/v1/health", {
      method: "POST",
    }).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({
      status: 405,
      message: "Method Not Allowed",
    })
  })

  it.each([
    [
      "FastAPI's default validation body",
      {
        detail: [
          {
            loc: ["query", "limit"],
            msg: "Input should be a valid integer",
            type: "int_parsing",
          },
        ],
      },
    ],
    ["a non-string title", { title: { text: "Invalid" }, status: 422 }],
    ["a non-numeric status", { title: "Invalid", status: "422" }],
    ["a non-array errors member", { title: "Invalid", errors: "limit" }],
    ["a JSON array", [{ msg: "Invalid" }]],
    ["a JSON string", "Invalid"],
    ["JSON null", null],
  ])(
    "throws ApiError with the status when the JSON body is %s",
    async (_case, body) => {
      stubFetch(
        Response.json(body, {
          status: 422,
          statusText: "Unprocessable Entity",
        })
      )
      const error: unknown = await apiFetch("/api/v1/scans?limit=x").catch(
        (e: unknown) => e
      )
      expect(error).toBeInstanceOf(ApiError)
      expect(error).toMatchObject({
        status: 422,
        problem: undefined,
        message: "HTTP 422 Unprocessable Entity",
      })
    }
  )

  it("throws ApiError with the status when the problem body is malformed", async () => {
    stubFetch(
      new Response("{not json", {
        status: 500,
        statusText: "Internal Server Error",
        headers: { "Content-Type": "application/problem+json" },
      })
    )
    const error: unknown = await apiFetch("/api/v1/health").catch(
      (e: unknown) => e
    )
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 500, problem: undefined })
  })

  it("throws NetworkError when no response arrives", async () => {
    const offline = new TypeError("Failed to fetch")
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(offline))
    )
    const error: unknown = await apiFetch("/api/v1/health").catch(
      (e: unknown) => e
    )
    expect(error).toBeInstanceOf(NetworkError)
    expect(error).toMatchObject({ cause: offline })
  })

  it("keeps the AbortError of a cancelled request", async () => {
    const controller = new AbortController()
    controller.abort()
    vi.stubGlobal(
      "fetch",
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
        Promise.reject(init?.signal?.reason as Error)
      )
    )
    const error: unknown = await apiFetch("/api/v1/health", {
      signal: controller.signal,
    }).catch((e: unknown) => e)
    expect(error).not.toBeInstanceOf(NetworkError)
    expect(error).toMatchObject({ name: "AbortError" })
  })

  it("throws ApiError with the status when the body is not JSON", async () => {
    stubFetch(
      new Response("Bad Gateway", { status: 502, statusText: "Bad Gateway" })
    )
    const error: unknown = await apiFetch("/api/v1/health").catch(
      (e: unknown) => e
    )
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({
      status: 502,
      problem: undefined,
      message: "HTTP 502 Bad Gateway",
    })
  })
})

describe("ErrorType", () => {
  it("types the generated hooks' errors as what apiFetch throws", () => {
    expectTypeOf<ErrorType<unknown>>().toEqualTypeOf<ApiError | NetworkError>()
    // The hook with its default error type, as a component calls it.
    type HealthQuery = ReturnType<typeof useGetHealth<Health>>
    expectTypeOf<HealthQuery["error"]>().toEqualTypeOf<
      ApiError | NetworkError | null
    >()
  })
})
