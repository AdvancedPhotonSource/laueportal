import { RouterProvider, createMemoryHistory } from "@tanstack/react-router"
import { act } from "react"
import { toast } from "sonner"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { Health, JobSummary } from "@/api"
import { Providers } from "@/app/Providers"
import { createQueryClient } from "@/app/query-client"
import { createAppRouter } from "@/app/router"
import { rowCheckbox, rowText, stubGridLayout } from "@/test/grid"
import { HELD_HINT } from "@/lib/run-status"
import { makeJobSummary, makeObservation } from "@/test/jobs"
import { LIVE_STOP_EXPLANATION } from "./run-monitor"
import {
  byRole,
  cleanup,
  click,
  render,
  SLOW_TEST,
  stubMatchMedia,
} from "@/test/render"

// The run monitor on the app's real route, with fetch answering the jobs list, Stop and Move to
// Front: the buttons follow the selected jobs' statuses (also as they change), each action asks
// first and its toast says what happened to each job. e2e/run-monitor.spec.ts runs it on the stack.

const HEALTH: Health = {
  db: "ok",
  worker: "running",
  version: "2.0.0.dev0",
}

interface Sent {
  method: string
  path: string
}

interface Api {
  jobs: JobSummary[]
  /** Answers a POST to /api/v1/jobs/{id}/{cancel|move-to-front}. */
  act: (id: number, action: string) => Response
  sent: Sent[]
}

function problem(status: number, title: string, detail?: string): Response {
  return Response.json(
    { type: "about:blank", status, title, detail },
    { status, headers: { "content-type": "application/problem+json" } }
  )
}

function answer(api: Api, method: string, url: URL): Response {
  if (url.pathname === "/api/v1/health") return Response.json(HEALTH)
  if (url.pathname === "/api/v1/jobs") {
    const rows = [...api.jobs].sort((a, b) => b.job_id - a.job_id)
    return Response.json({
      rows,
      total: rows.length,
      as_of: "2026-10-20T08:00:00Z",
    })
  }
  const action = /^\/api\/v1\/jobs\/(\d+)\/(cancel|move-to-front)$/.exec(
    url.pathname
  )
  if (action && method === "POST") {
    return api.act(Number(action[1]), action[2] ?? "")
  }
  return problem(404, "Not Found")
}

async function openRunMonitor(jobs: JobSummary[], act_: Api["act"]) {
  stubMatchMedia()
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined)
  const api: Api = { jobs, act: act_, sent: [] }
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(
        input instanceof globalThis.Request ? input.url : input.toString(),
        "http://localhost"
      )
      const method = init?.method ?? "GET"
      api.sent.push({ method, path: url.pathname })
      return Promise.resolve(answer(api, method, url))
    })
  )
  const queryClient = createQueryClient()
  const router = createAppRouter(
    queryClient,
    createMemoryHistory({ initialEntries: ["/runs"] })
  )
  await render(
    <Providers queryClient={queryClient}>
      <RouterProvider router={router} />
    </Providers>
  )
  await act(() => router.load())
  return api
}

const button = (name: string) => {
  const found = byRole("button", name)[0]
  if (!found) throw new Error(`no ${name} button`)
  return found
}

const enabled = (name: string) => !button(name).hasAttribute("disabled")

/**
 * Wait until the page has the whole selection: AG Grid reports a selection change from a later
 * task than the click, so a button's state alone can still be the previous selection's.
 */
async function selected(count: number) {
  await vi.waitFor(() => {
    expect(
      byRole("status").some(
        (status) => status.textContent === `${String(count)} selected`
      )
    ).toBe(true)
  })
}

async function dialog() {
  return vi.waitFor(() => {
    const found = byRole("alertdialog")[0]
    expect(found).toBeDefined()
    return found as HTMLElement
  })
}

async function toastLines() {
  await vi.waitFor(() => {
    expect(document.querySelector("[data-sonner-toast]")).not.toBeNull()
  })
  const element = document.querySelector("[data-sonner-toast]")
  return {
    title: element?.querySelector("[data-title]")?.textContent,
    lines: Array.from(element?.querySelectorAll("[data-job]") ?? []).map(
      (line) => line.textContent
    ),
  }
}

/** AG Grid's first rows take a while in jsdom (under a full run's load): wait longer for them. */
const FIRST_LOAD = { timeout: 5000 }

const RUNNING = makeJobSummary(5, { status: "running", phase: "running" })
const QUEUED = makeJobSummary(7, {
  status: "queued",
  phase: "queued",
  start_time: null,
})
const FINISHED = makeJobSummary(3)

beforeEach(() => {
  stubGridLayout()
  localStorage.clear()
})

afterEach(() => {
  act(() => {
    toast.dismiss()
  })
  cleanup()
  localStorage.clear()
})

describe("the run monitor", () => {
  it(
    "enables Stop for queued or running jobs and Move to Front for queued ones",
    SLOW_TEST,
    async () => {
      await openRunMonitor(
        [QUEUED, RUNNING, FINISHED],
        () => new Response(null, { status: 204 })
      )
      await vi.waitFor(() => {
        expect(rowText(document.body, "7")).toContain("Queued")
      }, FIRST_LOAD)
      expect(rowText(document.body, "7")).not.toContain("Held")
      expect(enabled("Stop")).toBe(false)
      expect(enabled("Move to Front")).toBe(false)

      await click(rowCheckbox(document.body, "3"))
      await selected(1)
      expect(enabled("Stop")).toBe(false)
      expect(enabled("Move to Front")).toBe(false)

      await click(rowCheckbox(document.body, "5"))
      await selected(2)
      expect(enabled("Stop")).toBe(true)
      expect(enabled("Move to Front")).toBe(false)

      await click(rowCheckbox(document.body, "7"))
      await selected(3)
      expect(enabled("Stop")).toBe(true)
      expect(enabled("Move to Front")).toBe(true)
    }
  )

  it(
    "Stop lists the job IDs, stops each job in turn, says what happened to each, and reloads",
    SLOW_TEST,
    async () => {
      const api = await openRunMonitor([QUEUED, RUNNING, FINISHED], (id) => {
        api.jobs = api.jobs.map((job) =>
          job.job_id === id
            ? { ...job, status: "cancelled", phase: "cancelled" }
            : job
        )
        return Response.json(
          id === 7
            ? {
                job_id: 7,
                state: "cancelled",
                n_pending: 441,
              }
            : {
                job_id: 5,
                state: "requested",
                n_pending: 318,
              }
        )
      })
      await vi.waitFor(() => {
        expect(rowText(document.body, "5")).toContain("Running")
      }, FIRST_LOAD)
      await click(rowCheckbox(document.body, "7"))
      await click(rowCheckbox(document.body, "5"))
      await click(rowCheckbox(document.body, "3"))
      await selected(3)
      expect(enabled("Stop")).toBe(true)
      await click(button("Stop"))
      const confirm = await dialog()
      expect(confirm.textContent).toContain("Stop 2 runs?")
      expect(
        Array.from(confirm.querySelectorAll("li[data-job]")).map(
          (li) => li.textContent
        )
      ).toEqual(["Job 5I36Running", "Job 7I38Queued"])
      expect(confirm.textContent).toContain(
        "1 selected run has ended and is left alone."
      )
      expect(api.sent.some((request) => request.method === "POST")).toBe(false)

      await click(byRole("button", "Stop Runs", confirm)[0])
      expect(await toastLines()).toEqual({
        title: "Stop Runs",
        lines: [
          "Job 5 (I36): stop requested. The inputs in flight finish and completed results are kept; up to 318 pending inputs will be recorded as not run.",
          "Job 7 (I38) is cancelled; none of its inputs were processed.",
        ],
      })
      expect(
        api.sent
          .filter((request) => request.method === "POST")
          .map((request) => request.path)
      ).toEqual(["/api/v1/jobs/5/cancel", "/api/v1/jobs/7/cancel"])
      // The grid reloads: the selected jobs have ended, so Stop is off again (still selected).
      await vi.waitFor(() => {
        expect(rowText(document.body, "7")).toContain("Cancelled")
      })
      await vi.waitFor(() => {
        expect(enabled("Stop")).toBe(false)
      })
      expect(rowCheckbox(document.body, "7").checked).toBe(true)
    }
  )

  it(
    "Stop of an open live run says what a live stop does, in its dialog and its report",
    SLOW_TEST,
    async () => {
      const live = makeJobSummary(4, {
        status: "running",
        phase: "running",
        live: true,
      })
      await openRunMonitor([live], () =>
        Response.json({ job_id: 4, state: "requested", n_pending: 2 })
      )
      await vi.waitFor(() => {
        expect(rowText(document.body, "4")).toContain("Running")
      }, FIRST_LOAD)
      await click(rowCheckbox(document.body, "4"))
      await selected(1)
      await click(button("Stop"))
      const confirm = await dialog()
      expect(confirm.textContent).toContain(LIVE_STOP_EXPLANATION)
      expect(confirm.textContent).not.toContain("Cancelled")
      await click(byRole("button", "Stop Run", confirm)[0])
      expect(await toastLines()).toEqual({
        title: "Stop Runs",
        lines: [`Job 4 (I35): stop requested. ${LIVE_STOP_EXPLANATION}`],
      })
    }
  )

  it("Move to Front says why a job did not move", SLOW_TEST, async () => {
    const queued = [
      makeJobSummary(8, { status: "queued" }),
      makeJobSummary(9, {
        status: "queued",
        target: "aps",
        dm_state: "pending",
      }),
      QUEUED,
    ]
    const api = await openRunMonitor(queued, (id) => {
      if (id === 9) {
        return problem(
          409,
          "Job runs remotely",
          "Job 9 runs on a remote target"
        )
      }
      if (id === 8)
        return problem(
          409,
          "Job not queued",
          "Job 8 is not waiting in the queue"
        )
      return new Response(null, { status: 204 })
    })
    await vi.waitFor(() => {
      expect(rowText(document.body, "9")).toContain("Queued")
    }, FIRST_LOAD)
    for (const id of ["9", "7", "8"])
      await click(rowCheckbox(document.body, id))
    await selected(3)
    expect(enabled("Move to Front")).toBe(true)
    await click(button("Move to Front"))
    const confirm = await dialog()
    expect(confirm.textContent).toContain("Move 3 queued runs to the front?")
    expect(confirm.textContent).toContain(
      "Remote runs are ordered by their scheduler: they do not move."
    )
    await click(byRole("button", "Move to Front", confirm)[0])
    expect(await toastLines()).toEqual({
      title: "Move to Front",
      lines: [
        "Job 7 (I38) moved to the front of the queue.",
        "Job 8 (I39) was not moved: it is no longer waiting in the queue.",
        "Job 9 (I40) was not moved: it runs on APS, whose scheduler orders it.",
      ],
    })
    // Newest first, each to the front: the moved ones keep their order ahead of the others.
    expect(
      api.sent
        .filter((request) => request.method === "POST")
        .map((request) => request.path)
    ).toEqual([
      "/api/v1/jobs/9/move-to-front",
      "/api/v1/jobs/8/move-to-front",
      "/api/v1/jobs/7/move-to-front",
    ])
  })

  it(
    "says a queued batch run is held by the open live session",
    SLOW_TEST,
    async () => {
      await openRunMonitor(
        [{ ...QUEUED, held: true }, RUNNING],
        () => new Response(null, { status: 204 })
      )
      await vi.waitFor(() => {
        expect(rowText(document.body, "7")).toContain("QueuedHeld")
      }, FIRST_LOAD)
      expect(document.querySelector("[data-held]")?.getAttribute("title")).toBe(
        HELD_HINT
      )
      expect(rowText(document.body, "5")).not.toContain("Held")
    }
  )

  it(
    "shows a crux run's remote phase in its Status cell, its notes the tooltip, and an ended one's status",
    SLOW_TEST,
    async () => {
      const crux = {
        target: "crux",
        execution: "ALCF",
        dm_state: "running",
      } as const
      const paused = makeObservation({
        phase: "login_required",
        detail: "Sign in to Globus and resume this run",
        pbs_id: null,
        stale: true, // read while Flows did not answer: status delayed
        observed_at: "2026-10-20T07:59:30Z",
      })
      await openRunMonitor(
        [
          { ...RUNNING, ...crux, observation: paused },
          { ...FINISHED, ...crux, observation: null },
        ],
        () => new Response(null, { status: 204 })
      )
      await vi.waitFor(() => {
        expect(rowText(document.body, "5")).toContain("Login needed")
      }, FIRST_LOAD)
      const cell = document.querySelector('[row-id="5"] [data-remote-status]')
      expect(cell?.getAttribute("title")).toBe(
        "Sign in to Globus and resume this run · status delayed"
      )
      expect(cell?.querySelector("[data-phase]")?.className).toContain(
        "bg-warning/15"
      )
      expect(rowText(document.body, "3")).toContain("Finished")
    }
  )

  it("Refresh loads the jobs again", SLOW_TEST, async () => {
    const api = await openRunMonitor(
      [FINISHED],
      () => new Response(null, { status: 204 })
    )
    await vi.waitFor(() => {
      expect(rowText(document.body, "3")).toContain("Finished")
    }, FIRST_LOAD)
    const lists = () =>
      api.sent.filter((request) => request.path === "/api/v1/jobs").length
    const before = lists()
    api.jobs = [...api.jobs, makeJobSummary(4, { status: "queued" })]
    await click(button("Refresh"))
    await vi.waitFor(() => {
      expect(rowText(document.body, "4")).toContain("Queued")
    })
    expect(lists()).toBe(before + 1)
  })
})
