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

type Fetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>

/**
 * Render the indicators (their links need a router) with fetch answering the health check. The health query
 * has its own retry rule (`retryHealth`: the app's, retried after 1 and 2 s), whatever the client's default.
 */
async function renderIndicators(answer: Fetch) {
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

const DOT_BASE = new Set(["size-[7px]", "shrink-0", "rounded-full"])

/**
 * The indicator as a user meets it: its name (the sr-only state of the dot, or the pill's words), its tone, the
 * words it shows, its dot's paint (its classes but the size and shape), its tint (a pill's), and its icons.
 */
function indicator() {
  const [group] = byRole("group", "Live system status")
  const links = byRole("link", undefined, group ?? document.body)
  expect(links).toHaveLength(1)
  const [link] = links
  if (!link) throw new Error("no indicator")
  const shown = link.cloneNode(true) as HTMLElement
  for (const hidden of shown.querySelectorAll(".sr-only")) hidden.remove()
  const dot = link.querySelector('[data-slot="status-dot"]')
  return {
    name: link.textContent.trim(),
    href: link.getAttribute("href"),
    tone: link.getAttribute("data-tone"),
    words: shown.textContent.trim() || null,
    dot: [...(dot?.classList ?? [])]
      .filter((name) => !DOT_BASE.has(name))
      .join(" "),
    tint: [...link.classList].find((name) => name.startsWith("bg-")) ?? null,
    icons: link.querySelectorAll("svg").length,
  }
}

const NEVER: Fetch = () => new Promise<Response>(() => undefined)
const answer =
  (worker: Health["worker"]): Fetch =>
  () =>
    Promise.resolve(Response.json({ ...HEALTH_OK, worker }))

/** A health check that never answers; its request rejects when aborted, as a browser's does. */
const HUNG: Fetch = (_input, init) =>
  new Promise<Response>((_resolve, reject) => {
    init?.signal?.addEventListener("abort", () => {
      reject(new DOMException("The request was aborted", "AbortError"))
    })
  })

describe("HealthIndicators", () => {
  // One test per state: a Local dot without an icon, while the worker runs (filled) or the first answer is
  // awaited (a ring); a tinted pill with the tone's dot and words when something is wrong. Each leads to the
  // status page. The clock runs past a failed check's two retries (after 1 and 2 s).
  it.each([
    [
      "the worker running: the quiet dot",
      answer("running"),
      {
        name: "Local Worker: running",
        tone: "ok",
        words: "Local",
        dot: "bg-success-quiet",
        tint: null,
      },
    ],
    [
      "no answer yet: the muted ring, checking",
      NEVER,
      {
        name: "Local Worker: checking",
        tone: "unknown",
        words: "Local",
        dot: "border-[1.5px] border-muted-foreground",
        tint: null,
      },
    ],
    [
      "the worker late: a warning pill",
      answer("late"),
      {
        name: "Worker late",
        tone: "warning",
        words: "Worker late",
        dot: "bg-warning",
        tint: "bg-warning/15",
      },
    ],
    [
      "the worker not running: an error pill",
      answer("not_running"),
      {
        name: "Worker not running",
        tone: "error",
        words: "Worker not running",
        dot: "bg-destructive",
        tint: "bg-destructive/15",
      },
    ],
    [
      "the database not answering: an error pill, Worker unknown",
      () =>
        Promise.resolve(
          Response.json({ ...HEALTH_OK, db: "error", worker: null })
        ),
      {
        name: "Worker unknown",
        tone: "error",
        words: "Worker unknown",
        dot: "bg-destructive",
        tint: "bg-destructive/15",
      },
    ],
    [
      "the API not answering: an error pill, API unreachable",
      () => Promise.reject(new TypeError("Failed to fetch")),
      {
        name: "API unreachable",
        tone: "error",
        words: "API unreachable",
        dot: "bg-destructive",
        tint: "bg-destructive/15",
      },
    ],
    [
      "the health check failing: an error pill, API error",
      () =>
        Promise.resolve(
          new Response("Bad Gateway", {
            status: 502,
            statusText: "Bad Gateway",
          })
        ),
      {
        name: "API error",
        tone: "error",
        words: "API error",
        dot: "bg-destructive",
        tint: "bg-destructive/15",
      },
    ],
  ] as const)("shows %s", async (_state, health, expected) => {
    vi.useFakeTimers()
    await renderIndicators(health)
    await act(() => vi.advanceTimersByTimeAsync(5_000))
    expect(indicator()).toEqual({ ...expected, href: "/", icons: 0 })
  })

  it("says API unreachable when the check has no answer within a poll interval, without retrying", async () => {
    vi.useFakeTimers()
    const fetchMock = await renderIndicators(HUNG)
    await act(() => vi.advanceTimersByTimeAsync(HEALTH_POLL_MS - 100))
    expect(indicator()).toMatchObject({ name: "Local Worker: checking" })

    await act(() => vi.advanceTimersByTimeAsync(200))
    expect(indicator()).toMatchObject({
      name: "API unreachable",
      tone: "error",
      words: "API unreachable",
    })
    // Not retried: a retry would have asked again after 1 s, and the next poll is 10 s after the check.
    await act(() => vi.advanceTimersByTimeAsync(5_000))
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("turns from the dot into a pill and back as the answers change", async () => {
    vi.useFakeTimers()
    let worker: Health["worker"] = "running"
    await renderIndicators(() => answer(worker)(""))
    await act(() => vi.advanceTimersByTimeAsync(100))
    expect(indicator()).toMatchObject({
      name: "Local Worker: running",
      words: "Local",
    })

    worker = "not_running"
    await act(() => vi.advanceTimersByTimeAsync(HEALTH_POLL_MS))
    expect(indicator()).toMatchObject({
      name: "Worker not running",
      words: "Worker not running",
    })

    worker = "running"
    await act(() => vi.advanceTimersByTimeAsync(HEALTH_POLL_MS))
    expect(indicator()).toMatchObject({
      name: "Local Worker: running",
      words: "Local",
    })
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
