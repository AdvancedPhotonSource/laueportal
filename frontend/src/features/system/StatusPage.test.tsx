import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import {
  createMemoryHistory,
  createRootRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router"
import { act } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { SystemStatus } from "@/api"
import { byRole, cleanup, click, render } from "@/test/render"
import { makeSystemStatus, makeWorker } from "@/test/system"
import { StatusPage } from "./StatusPage"
import { STATUS_POLL_MS } from "./system-status"

const STATUS: SystemStatus = makeSystemStatus({
  queue: {
    queued_runs: 1,
    running: 1,
    queued_conversions: 0,
    live_slots_in_use: 0,
    live_slots: 2,
  },
  next_up: [
    {
      kind: "run",
      job_id: 13,
      run: { kind: "reconstruction", id: 9, label: "R9" },
      moved_to_front: true,
    },
  ],
  active_runs: [
    {
      job_id: 12,
      run: { kind: "indexing", id: 70, label: "I70" },
      status: "running",
      phase: "running",
      n_inputs: 10,
      n_succeeded: 3,
      n_failed: 0,
      n_not_run: 0,
      heartbeat_age_seconds: 400,
      stale: true,
      stop_requested: true,
      remote: null,
    },
  ],
})

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

/** Render the page (its links need a router) with fetch answering GET /api/v1/system/status. */
async function renderPage(answer: () => Promise<Response>) {
  const fetchMock = vi.fn((_input: RequestInfo | URL) => answer())
  vi.stubGlobal("fetch", fetchMock)
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined)
  const router = createRouter({
    routeTree: createRootRoute({ component: StatusPage }),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  })
  const container = await render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <RouterProvider router={router} />
    </QueryClientProvider>
  )
  await act(() => router.load())
  return { fetchMock, container }
}

describe("StatusPage", () => {
  it("polls every 5 s while the tab is visible, and again when it shows", async () => {
    vi.useFakeTimers()
    const { fetchMock } = await renderPage(() =>
      Promise.resolve(Response.json(STATUS))
    )
    await act(() => vi.advanceTimersByTimeAsync(100))
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/system/status",
      expect.anything()
    )

    await act(() => vi.advanceTimersByTimeAsync(STATUS_POLL_MS))
    expect(fetchMock).toHaveBeenCalledTimes(2)

    setVisibility("hidden")
    await act(() => vi.advanceTimersByTimeAsync(3 * STATUS_POLL_MS))
    expect(fetchMock).toHaveBeenCalledTimes(2)

    setVisibility("visible")
    await act(() => vi.advanceTimersByTimeAsync(100))
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it("shows the cards, an active run's badges and its links", async () => {
    const { container } = await renderPage(() =>
      Promise.resolve(Response.json(STATUS))
    )
    await vi.waitFor(() => {
      expect(byRole("region", undefined, container)).toHaveLength(5)
    })
    const regions = byRole("region", undefined, container)
    expect(
      regions.map((region) => region.querySelector("h2")?.textContent)
    ).toEqual(["Connection", "Worker", "Queue", "Quick Actions", "Active Runs"])
    const [, worker, queue] = regions
    expect(worker?.querySelector("[data-tone]")?.textContent).toBe("Running")
    expect(worker?.textContent).toContain("beamline-host")
    expect(worker?.textContent).toContain("Dispatcher")
    expect(worker?.textContent).toContain("4 s ago")
    expect(worker?.textContent).toContain("Remote poller")
    expect(
      worker?.querySelector('[data-tone][title="Late after 3 min"]')
        ?.textContent
    ).toBe("OK")
    const next = queue?.querySelector('[data-item="run-13"]')
    expect(next?.textContent).toContain("R9")
    expect(next?.textContent).toContain("Moved to front")
    expect(next?.querySelector("a")?.getAttribute("href")).toBe(
      "/reconstructions/9"
    )
    const row = container.querySelector('tr[data-job="12"]')
    expect(row?.textContent).toContain("Running 3/10")
    expect(row?.textContent).toContain("Stop requested")
    expect(row?.textContent).toContain("Stale")
    expect(row?.textContent).toContain("6 min 40 s ago")
    expect(row?.textContent).toContain("Local")
    const links = Array.from(row?.querySelectorAll("a") ?? []).map((link) =>
      link.getAttribute("href")
    )
    expect(links).toEqual(["/jobs/12", "/indexing/70"])
    expect(byRole("alert", undefined, container)).toHaveLength(0)
  })

  it("says plainly when the database is down", async () => {
    const { container } = await renderPage(() =>
      Promise.resolve(
        Response.json({
          ...STATUS,
          database: { status: "error", path: "/data/laueportal.sqlite" },
          worker: null,
          queue: null,
          next_up: null,
          active_runs: null,
        } satisfies SystemStatus)
      )
    )
    await vi.waitFor(() => {
      expect(byRole("alert", undefined, container)).toHaveLength(1)
    })
    const [alert] = byRole("alert", undefined, container)
    expect(alert?.textContent).toContain("The database does not answer")
    const [, worker, queue] = byRole("region", undefined, container)
    expect(worker?.textContent).toBe("WorkerUnknown")
    expect(queue?.textContent).toBe("QueueUnknown")
    expect(container.textContent).toContain(
      "The runs cannot be read: the database does not answer."
    )
  })

  it("says plainly when the worker is not running, and when the queue is empty", async () => {
    const { container } = await renderPage(() =>
      Promise.resolve(
        Response.json(
          makeSystemStatus({
            worker: makeWorker({
              state: "not_running",
              stopped_at: "2026-10-25T09:00:00Z",
              threads: makeWorker().threads.map((thread) => ({
                ...thread,
                state: "off",
                late_after_seconds: null,
              })),
            }),
          })
        )
      )
    )
    await vi.waitFor(() => {
      expect(byRole("alert", undefined, container)).toHaveLength(1)
    })
    const [alert] = byRole("alert", undefined, container)
    expect(alert?.textContent).toBe("The worker is not running")
    const [, worker, queue] = byRole("region", undefined, container)
    expect(worker?.querySelector("[data-tone]")?.textContent).toBe(
      "Not running"
    )
    expect(worker?.textContent).toContain("Stopped")
    // An Off thread cannot be late: no limit in a tooltip.
    const threads = worker?.querySelectorAll(
      '[aria-label="Threads"] [data-tone]'
    )
    expect(
      [...(threads ?? [])].map((badge) => badge.getAttribute("title"))
    ).toEqual([null, null, null])
    expect(queue?.textContent).toContain("Queue empty")
  })

  it("keeps its error across polls while the API stays down, Retry usable and focused", async () => {
    vi.useFakeTimers()
    // Each answer takes 300 ms, so that a poll's fetch can be seen running.
    const { fetchMock, container } = await renderPage(
      () =>
        new Promise((resolve) => {
          setTimeout(() => {
            resolve(
              Response.json(
                {
                  type: "about:blank",
                  title: "Service Unavailable",
                  status: 503,
                  detail: "database is locked",
                },
                {
                  status: 503,
                  headers: { "content-type": "application/problem+json" },
                }
              )
            )
          }, 300)
        })
    )
    await act(() => vi.advanceTimersByTimeAsync(400))
    const [alert] = byRole("alert", undefined, container)
    expect(alert?.textContent).toContain("database is locked")
    const [retry] = byRole("button", "Retry", container)
    retry?.focus()

    // Three polls, looked at every 100 ms: the same alert all along (not made anew, so not
    // announced again), no loading skeleton, and Retry enabled and focused.
    for (let step = 0; step < (3 * STATUS_POLL_MS) / 100; step += 1) {
      await act(() => vi.advanceTimersByTimeAsync(100))
      expect(byRole("alert", undefined, container)).toEqual([alert])
      expect(byRole("status", undefined, container)).toHaveLength(0)
      expect(retry?.hasAttribute("disabled")).toBe(false)
      expect(document.activeElement).toBe(retry)
    }
    // The polls went on (at least two during the loop).
    expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(3)
  })

  it("gives the heading the keyboard once Retry has loaded, and keeps it on Retry when it fails", async () => {
    let down = true
    const { container } = await renderPage(() =>
      Promise.resolve(
        down
          ? Response.json(
              {
                type: "about:blank",
                title: "Service Unavailable",
                status: 503,
                detail: "database is locked",
              },
              {
                status: 503,
                headers: { "content-type": "application/problem+json" },
              }
            )
          : Response.json(STATUS)
      )
    )
    await vi.waitFor(() => {
      expect(byRole("button", "Retry", container)).toHaveLength(1)
    })
    const [retry] = byRole("button", "Retry", container)
    retry?.focus()
    await click(retry)
    // Still down: Retry keeps the keyboard.
    await vi.waitFor(() => {
      expect(retry?.querySelector(".animate-spin")).toBeNull()
    })
    expect(document.activeElement).toBe(retry)

    down = false
    await click(retry)
    await vi.waitFor(() => {
      expect(byRole("alert", undefined, container)).toHaveLength(0)
    })
    await vi.waitFor(() => {
      expect(document.activeElement).toBe(byRole("heading", "Status")[0])
    })
  })

  it("keeps Refresh enabled and focused while a poll runs", async () => {
    vi.useFakeTimers()
    const { fetchMock, container } = await renderPage(
      () =>
        new Promise((resolve) => {
          setTimeout(() => {
            resolve(Response.json(STATUS))
          }, 300)
        })
    )
    await act(() => vi.advanceTimersByTimeAsync(400))
    const [refresh] = byRole("button", "Refresh", container)
    refresh?.focus()
    for (let step = 0; step < (2 * STATUS_POLL_MS) / 100; step += 1) {
      await act(() => vi.advanceTimersByTimeAsync(100))
      expect(refresh?.hasAttribute("disabled")).toBe(false)
      expect(document.activeElement).toBe(refresh)
    }
    // A click while a poll's fetch runs waits for it rather than starting another one.
    const before = fetchMock.mock.calls.length
    while (fetchMock.mock.calls.length === before) {
      await act(() => vi.advanceTimersByTimeAsync(100))
    }
    await act(async () => {
      refresh?.click()
      await Promise.resolve()
    })
    expect(fetchMock.mock.calls.length).toBe(before + 1)
    // Once it is answered, a click fetches again.
    await act(() => vi.advanceTimersByTimeAsync(400))
    await act(async () => {
      refresh?.click()
      await Promise.resolve()
    })
    expect(fetchMock.mock.calls.length).toBe(before + 2)
  })

  it("lays its definition lists out as groups of terms and definitions", async () => {
    const { container } = await renderPage(() =>
      Promise.resolve(Response.json(STATUS))
    )
    await vi.waitFor(() => {
      expect(container.querySelectorAll("dl")).toHaveLength(4)
    })
    for (const list of container.querySelectorAll("dl")) {
      for (const group of list.children) {
        expect(group.tagName).toBe("DIV")
        const tags = Array.from(group.children, (child) => child.tagName)
        expect(tags[0]).toBe("DT")
        expect(tags.slice(1).every((tag) => tag === "DD")).toBe(true)
      }
    }
  })
})
