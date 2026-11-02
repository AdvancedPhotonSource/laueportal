import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import {
  createMemoryHistory,
  createRootRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router"
import { act } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { Health } from "@/api"
import { TooltipProvider } from "@/components/ui/tooltip"
import { byRole, cleanup, render } from "@/test/render"
import { HEALTH_POLL_MS } from "./health-indicators"
import { HealthIndicators } from "./HealthIndicators"

const HEALTH_OK: Health = {
  db: "ok",
  worker: "running",
  version: "2.0.0.dev0",
}

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  setVisibility("visible")
})

function setVisibility(state: DocumentVisibilityState) {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => state,
  })
  document.dispatchEvent(new Event("visibilitychange", { bubbles: true }))
}

/** Render the indicators (their links need a router) with fetch answering the health check. */
async function renderIndicators(answer: () => Promise<Response>) {
  const fetchMock = vi.fn(answer)
  vi.stubGlobal("fetch", fetchMock)
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined)
  const router = createRouter({
    routeTree: createRootRoute({ component: HealthIndicators }),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  })
  await render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <TooltipProvider>
        <RouterProvider router={router} />
      </TooltipProvider>
    </QueryClientProvider>
  )
  await act(() => router.load())
  return fetchMock
}

/** The indicators as "<accessible text> (<tone>)". */
function indicators(): string[] {
  const [group] = byRole("group", "Live system status")
  return byRole("link", undefined, group ?? document.body).map(
    (link) =>
      `${link.textContent.trim()} (${link.getAttribute("data-tone") ?? "?"})`
  )
}

async function expectIndicators(expected: string[]) {
  await vi.waitFor(() => {
    expect(indicators()).toEqual(expected)
  })
}

describe("HealthIndicators", () => {
  it.each([
    ["running", "Worker: running (ok)"],
    ["late", "Worker: late (warning)"],
    ["not_running", "Worker: not running (error)"],
    [null, "Worker: unknown (unknown)"],
  ] as const)("shows the worker %s", async (worker, expected) => {
    await renderIndicators(() =>
      Promise.resolve(Response.json({ ...HEALTH_OK, worker }))
    )
    await expectIndicators([expected])
  })

  it("says so when the API does not answer", async () => {
    await renderIndicators(() =>
      Promise.reject(new TypeError("Failed to fetch"))
    )
    await expectIndicators(["API unreachable (error)"])
  })

  it("says so when the health check fails", async () => {
    await renderIndicators(() =>
      Promise.resolve(
        new Response("Bad Gateway", { status: 502, statusText: "Bad Gateway" })
      )
    )
    await expectIndicators(["API error (error)"])
  })

  it("shows that it is checking until the first answer", async () => {
    await renderIndicators(() => new Promise<Response>(() => undefined))
    await expectIndicators(["Worker: checking (unknown)"])
  })

  it("polls every 10 s while the tab is visible, and again when it shows", async () => {
    vi.useFakeTimers()
    const fetchMock = await renderIndicators(() =>
      Promise.resolve(Response.json(HEALTH_OK))
    )
    await act(() => vi.advanceTimersByTimeAsync(100))
    expect(fetchMock).toHaveBeenCalledTimes(1)

    await act(() => vi.advanceTimersByTimeAsync(HEALTH_POLL_MS))
    expect(fetchMock).toHaveBeenCalledTimes(2)

    setVisibility("hidden")
    await act(() => vi.advanceTimersByTimeAsync(3 * HEALTH_POLL_MS))
    expect(fetchMock).toHaveBeenCalledTimes(2)

    setVisibility("visible")
    await act(() => vi.advanceTimersByTimeAsync(100))
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })
})
