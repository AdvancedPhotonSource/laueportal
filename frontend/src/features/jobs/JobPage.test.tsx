import { RouterProvider, createMemoryHistory } from "@tanstack/react-router"
import { act } from "react"
import { toast } from "sonner"
import { afterEach, describe, expect, it, vi } from "vitest"

import type {
  CancelResult,
  FailurePage,
  Health,
  JobDetail,
  JobLog,
} from "@/api"
import { Providers } from "@/app/Providers"
import { createQueryClient } from "@/app/query-client"
import { createAppRouter } from "@/app/router"
import { formatDateTime } from "@/lib/format"
import { HELD_HINT } from "@/lib/run-status"
import {
  endedJob,
  failurePage,
  makeConfiguration,
  makeFailure,
  makeJobDetail,
  makeJobLive,
  makeCruxJob,
  makeJobLog,
  makeObservation,
  makeRunFile,
} from "@/test/jobs"
import { byRole, cleanup, click, render, stubMatchMedia } from "@/test/render"

// The job page on the app's real routes (its loader, its title) with fetch answering the jobs API:
// the job, its failure report's pages, its log and Stop.

const HEALTH: Health = {
  db: "ok",
  worker: "running",
  version: "2.0.0.dev0",
}

interface Sent {
  method: string
  url: URL
}

interface Api {
  job: JobDetail | undefined
  failures: (offset: number, limit: number) => FailurePage
  log: JobLog
  cancel: CancelResult
  requests: Sent[]
}

function problem(status: number, title: string): Response {
  return Response.json(
    { type: "about:blank", status, title },
    { status, headers: { "content-type": "application/problem+json" } }
  )
}

function answer(api: Api, { method, url }: Sent): Response {
  if (url.pathname === "/api/v1/health") return Response.json(HEALTH)
  const path = /^\/api\/v1\/jobs\/(\d+)(\/failures|\/log|\/cancel)?$/.exec(
    url.pathname
  )
  const job = api.job
  if (!path || !job || job.job_id !== Number(path[1])) {
    return problem(404, "Job not found")
  }
  switch (path[2]) {
    case "/failures":
      return Response.json(
        api.failures(
          Number(url.searchParams.get("offset") ?? 0),
          Number(url.searchParams.get("limit") ?? 50)
        )
      )
    case "/log":
      return Response.json(api.log)
    case "/cancel":
      if (method !== "POST") return problem(405, "Method Not Allowed")
      return Response.json(api.cancel)
    default:
      return Response.json(job)
  }
}

async function openJob(
  url: string,
  options: Partial<Omit<Api, "requests">> = {}
) {
  stubMatchMedia()
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined)
  const api: Api = {
    job: "job" in options ? options.job : makeJobDetail(),
    failures:
      options.failures ?? ((offset, limit) => failurePage(3, offset, limit)),
    log: options.log ?? makeJobLog(41),
    cancel: options.cancel ?? {
      job_id: 41,
      state: "requested",
      n_pending: 318,
    },
    requests: [],
  }
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const sent: Sent = {
        method: init?.method ?? "GET",
        url: new URL(
          input instanceof globalThis.Request ? input.url : input.toString(),
          "http://localhost"
        ),
      }
      api.requests.push(sent)
      return Promise.resolve(answer(api, sent))
    })
  )
  const queryClient = createQueryClient()
  const history = createMemoryHistory({ initialEntries: [url] })
  const router = createAppRouter(queryClient, history)
  await render(
    <Providers queryClient={queryClient}>
      <RouterProvider router={router} />
    </Providers>
  )
  await act(() => router.load())
  return { api, router }
}

/** GET requests to `path` (the job, its failures, its log). */
const gets = (api: Api, path: string) =>
  api.requests.filter(
    (request) => request.method === "GET" && request.url.pathname === path
  )

function region(name: string): HTMLElement {
  const element = byRole("region").find(
    (candidate) =>
      document.getElementById(candidate.getAttribute("aria-labelledby") ?? "")
        ?.textContent === name
  )
  if (!element) throw new Error(`no region ${name}`)
  return element
}

function definitions(root: ParentNode) {
  return Object.fromEntries(
    Array.from(root.querySelectorAll("dt")).map((term) => [
      term.textContent,
      term.nextElementSibling?.textContent,
    ])
  )
}

function setVisibility(state: DocumentVisibilityState) {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => state,
  })
  document.dispatchEvent(new Event("visibilitychange", { bubbles: true }))
}

afterEach(() => {
  act(() => {
    toast.dismiss()
  })
  cleanup()
  vi.useRealTimers()
  setVisibility("visible")
})

describe("the job page", () => {
  it("shows a running local job: its run and scan, details, counters and heartbeat", async () => {
    await openJob("/jobs/41")
    await vi.waitFor(() => {
      expect(document.title).toBe("Job 41 · 3DMN Portal")
    })
    expect(byRole("heading", "Job 41")).toHaveLength(1)
    expect(byRole("link", "I72")[0]?.getAttribute("href")).toBe("/indexing/72")
    expect(byRole("link", "990012")[0]?.getAttribute("href")).toBe(
      "/scans/990012"
    )
    // No Calibration link, no priority.
    expect(document.body.textContent).not.toMatch(/Calibration|Priority/)

    const details = definitions(region("Job Details"))
    expect(details.Status).toBe("Running 123/441")
    expect(details.Computer).toBe("compute-host")
    expect(details["Finish Time"]).toBe("—none")
    expect(details.Duration).toBe("00:12:34 (running)")
    expect(region("Job Details").querySelector("pre")?.textContent).toBe(
      "Queued on laue-run-1\nStarted on laue-run-1"
    )

    const progress = region("Run Progress")
    expect(definitions(progress)).toEqual({
      Inputs: "441",
      Processed: "123",
      Succeeded: "120",
      Failed: "3",
      "Not run": "0",
      Pending: "318",
    })
    expect(progress.textContent).toContain(
      "Last heartbeat 4 s ago (stale after 5 min)."
    )
    expect(byRole("button", "Stop Run")[0]?.hasAttribute("disabled")).toBe(
      false
    )
  })

  it("says a queued batch run is held by the open live session", async () => {
    await openJob("/jobs/41", {
      job: makeJobDetail({ status: "queued", phase: "queued", held: true }),
    })
    await vi.waitFor(() => {
      expect(definitions(region("Job Details")).Status).toBe(
        "QueuedHeld by the live session"
      )
    })
    expect(
      region("Job Details")
        .querySelector('[data-testid="held"]')
        ?.getAttribute("title")
    ).toBe(HELD_HINT)
  })

  it("asks for the job every 2 s while it runs, and stops once it has ended", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] })
    const { api } = await openJob("/jobs/41", {
      log: makeJobLog(41, [], {
        run_log: {
          lines: [],
          truncated: false,
          n_lines: null,
          file: makeRunFile("run.json", { state: "missing" }),
        },
      }),
    })
    await vi.waitFor(() => {
      expect(definitions(region("Run Progress")).Processed).toBe("123")
    })
    // While it runs, run.json is still to come.
    await vi.waitFor(() => {
      expect(region("Run Configuration").textContent).toContain(
        "Not written yet"
      )
    })
    const job = api.job ?? makeJobDetail()
    api.job = {
      ...job,
      progress: { ...job.progress, n_processed: 200, n_succeeded: 197 },
    }
    await act(() => vi.advanceTimersByTimeAsync(2000))
    await vi.waitFor(() => {
      expect(definitions(region("Run Progress")).Processed).toBe("200")
    })

    // It finished: its log and failure report are read again, and polling stops.
    api.job = endedJob(job, "finished", {
      succeeded: 438,
      failed: 3,
      notRun: 0,
    })
    api.log = makeJobLog(41, ["frame 1 indexed", "frame 2 indexed"])
    const logs = gets(api, "/api/v1/jobs/41/log").length
    await act(() => vi.advanceTimersByTimeAsync(2000))
    await vi.waitFor(() => {
      expect(definitions(region("Job Details")).Status).toBe("Finished")
    })
    await vi.waitFor(() => {
      expect(
        region("Run Configuration").querySelector("pre")?.textContent
      ).toBe("frame 1 indexed\nframe 2 indexed")
    })
    expect(gets(api, "/api/v1/jobs/41/log").length).toBe(logs + 1)
    const polls = gets(api, "/api/v1/jobs/41").length
    await act(() => vi.advanceTimersByTimeAsync(10_000))
    expect(gets(api, "/api/v1/jobs/41").length).toBe(polls)
    expect(byRole("button", "Stop Run")[0]?.hasAttribute("disabled")).toBe(true)
  })

  it("reads the job once per visit, then every 2 s, and not while the tab is hidden", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] })
    const { api } = await openJob("/jobs/41")
    const reads = () => gets(api, "/api/v1/jobs/41").length
    await vi.waitFor(() => {
      expect(definitions(region("Run Progress")).Processed).toBe("123")
    })
    // The route's loader read it: the page does not read it again when it shows.
    expect(reads()).toBe(1)
    await act(() => vi.advanceTimersByTimeAsync(2000))
    await vi.waitFor(() => {
      expect(reads()).toBe(2)
    })

    setVisibility("hidden")
    await act(() => vi.advanceTimersByTimeAsync(10_000))
    expect(reads()).toBe(2)

    // Shown again, it polls again.
    setVisibility("visible")
    await act(() => vi.advanceTimersByTimeAsync(2000))
    await vi.waitFor(() => {
      expect(reads()).toBeGreaterThan(2)
    })
  })

  it("stops polling once the job is gone (a 404)", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] })
    const { api } = await openJob("/jobs/41")
    const reads = () => gets(api, "/api/v1/jobs/41").length
    await vi.waitFor(() => {
      expect(definitions(region("Run Progress")).Processed).toBe("123")
    })
    api.job = undefined
    await act(() => vi.advanceTimersByTimeAsync(2000))
    await vi.waitFor(() => {
      expect(reads()).toBe(2)
    })
    await act(() => vi.advanceTimersByTimeAsync(60_000))
    expect(reads()).toBe(2)
  })

  it("counts the inputs failed so far from the polled counter, not the report's first answer", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] })
    const notRecorded = makeRunFile("failures.jsonl", {
      state: "not_recorded",
      path: null,
    })
    const { api } = await openJob("/jobs/41", {
      failures: () => ({
        rows: [],
        total: 3,
        has_more: false,
        report: notRecorded,
      }),
    })
    const card = () => region("Failed Inputs")
    await vi.waitFor(() => {
      expect(card().textContent).toContain(
        "3 inputs failed so far: the failure report is written when the run ends."
      )
    })
    const reports = gets(api, "/api/v1/jobs/41/failures").length
    const job = api.job ?? makeJobDetail()
    api.job = {
      ...job,
      progress: { ...job.progress, n_failed: 5, n_processed: 125 },
    }
    await act(() => vi.advanceTimersByTimeAsync(2000))
    await vi.waitFor(() => {
      expect(definitions(region("Run Progress")).Failed).toBe("5")
    })
    expect(card().textContent).toContain(
      "5 inputs failed so far: the failure report is written when the run ends."
    )
    // The report itself is read again only when the run ends.
    expect(gets(api, "/api/v1/jobs/41/failures").length).toBe(reports)
  })

  it("pages the failed inputs on the server, 50 at a time", async () => {
    const failed = endedJob(makeJobDetail(), "failed", {
      succeeded: 321,
      failed: 120,
      notRun: 0,
    })
    const { api } = await openJob("/jobs/41", {
      job: failed,
      failures: (offset, limit) =>
        failurePage(120, offset, limit, (index) =>
          makeFailure(
            index,
            index % 2 === 0 ? {} : { category: "input", error_type: "OSError" }
          )
        ),
    })
    const card = () => region("Failed Inputs")
    await vi.waitFor(() => {
      expect(card().textContent).toContain("Showing 1–50 of 120")
    })
    const rows = () => Array.from(card().querySelectorAll("tbody tr"))
    expect(rows()).toHaveLength(50)
    expect(
      Array.from(card().querySelectorAll("thead th")).map(
        (th) => th.textContent
      )
    ).toEqual(["#", "Input", "Category", "Error", "Message"])
    expect(
      Array.from(rows()[1]?.querySelectorAll("td") ?? []).map(
        (td) => td.textContent
      )
    ).toEqual([
      "1",
      "Ni_2",
      "input",
      "OSError",
      "No pattern indexed within the angle tolerance (1)",
    ])
    expect(rows()[1]?.querySelector('[data-tone="warning"]')).not.toBeNull()
    expect(rows()[0]?.querySelector('[data-tone="error"]')).not.toBeNull()
    expect(
      byRole("button", "Previous", card())[0]?.getAttribute("aria-disabled")
    ).toBe("true")

    await click(byRole("button", "Next", card())[0])
    await vi.waitFor(() => {
      expect(card().textContent).toContain("Showing 51–100 of 120")
    })
    await click(byRole("button", "Next", card())[0])
    await vi.waitFor(() => {
      expect(card().textContent).toContain("Showing 101–120 of 120")
    })
    expect(rows()).toHaveLength(20)
    // Off, but it keeps the keyboard focus (aria-disabled, focusableWhenDisabled).
    expect(
      byRole("button", "Next", card())[0]?.getAttribute("aria-disabled")
    ).toBe("true")
    await click(byRole("button", "Previous", card())[0])
    await vi.waitFor(() => {
      expect(card().textContent).toContain("Showing 51–100 of 120")
    })
    expect(
      gets(api, "/api/v1/jobs/41/failures").map((request) => request.url.search)
    ).toEqual([
      "?offset=0&limit=50",
      "?offset=50&limit=50",
      "?offset=100&limit=50",
    ])
  })

  it("shows a dash for a failed input without a message, as for its other missing values", async () => {
    // A migrated run: a cancelled run's queued inputs, recorded with an empty message.
    const cancelled = endedJob(makeJobDetail(), "cancelled", {
      succeeded: 367,
      failed: 0,
      notRun: 74,
    })
    await openJob("/jobs/41", {
      job: cancelled,
      failures: (offset, limit) =>
        failurePage(74, offset, limit, (index) =>
          makeFailure(index, {
            index: null,
            input_id: null,
            source: null,
            category: index < 50 ? "interrupted" : "cancelled",
            error_type: null,
            message: index < 50 ? (index % 2 ? " " : "") : "Cancelled by user",
          })
        ),
    })
    const card = () => region("Failed Inputs")
    await vi.waitFor(() => {
      expect(card().textContent).toContain("Showing 1–50 of 74")
    })
    const cells = (row: number) =>
      Array.from(
        card().querySelectorAll("tbody tr")[row]?.querySelectorAll("td") ?? []
      )
    for (const row of [0, 1]) {
      expect(cells(row).map((td) => td.textContent)).toEqual([
        "—none",
        "—none",
        "interrupted",
        "—none",
        "—none",
      ])
      expect(cells(row)[4]?.hasAttribute("title")).toBe(false)
    }
    await click(byRole("button", "Next", card())[0])
    await vi.waitFor(() => {
      expect(card().textContent).toContain("Showing 51–74 of 74")
    })
    expect(cells(0)[4]?.textContent).toBe("Cancelled by user")
    expect(cells(0)[4]?.getAttribute("title")).toBe("Cancelled by user")
  })

  it("shows every file state: not on this host, not recorded, unreadable and why", async () => {
    const migrated = endedJob(makeJobDetail(), "failed", {
      succeeded: 400,
      failed: 41,
      notRun: 0,
    })
    await openJob("/jobs/41", {
      job: {
        ...migrated,
        configuration: makeConfiguration({
          run_directory: {
            path: "/net/fileserver/export/data1/runs/index_72",
            state: "missing",
            detail: null,
          },
          files: [
            makeRunFile("request.json", { state: "not_recorded", path: null }),
            makeRunFile("run.json", {
              state: "unreadable",
              detail: "not a JSON object",
            }),
          ],
          problems: ["run.json at /net/…/run.json is not a JSON object"],
          request: null,
        }),
      },
      failures: () => ({
        rows: [],
        total: 41,
        has_more: false,
        report: makeRunFile("failures.jsonl", { state: "missing" }),
      }),
      log: makeJobLog(41, [], {
        run_log: {
          lines: [],
          truncated: false,
          n_lines: null,
          file: makeRunFile("run.json", {
            state: "unreadable",
            detail: "not a JSON object",
          }),
        },
      }),
    })
    const configuration = () => region("Run Configuration")
    await vi.waitFor(() => {
      expect(
        configuration().querySelector('[data-file="run directory"]')
      ).not.toBeNull()
    })
    const fileRow = (name: string) =>
      configuration().querySelector(`[data-file="${name}"]`)?.textContent
    expect(fileRow("run directory")).toBe(
      "run directoryNot available on this host/net/fileserver/export/data1/runs/index_72"
    )
    expect(fileRow("request.json")).toBe(
      "request.jsonNot recordedNo path recorded"
    )
    expect(fileRow("run.json")).toContain("Unreadablenot a JSON object")
    expect(configuration().textContent).toContain(
      "run.json at /net/…/run.json is not a JSON object"
    )
    // Neither the request nor run.json read, no history: the note.
    expect(configuration().textContent).toContain(
      "No request or run summary recorded."
    )
    await vi.waitFor(() => {
      expect(region("Failed Inputs").textContent).toBe(
        "Failed Inputs41 inputs did not succeed, but the failure report is not available on this host."
      )
    })
    await vi.waitFor(() => {
      expect(
        configuration().querySelector('[aria-label="Run log"]')?.textContent
      ).toContain("Unreadablenot a JSON object")
    })
  })

  it("shows a remote run's card and its PBS output, without a heartbeat check", async () => {
    // No observation yet: the card shows DM's state.
    await openJob("/jobs/40", {
      job: makeCruxJob(null),
      log: makeJobLog(40, [], {
        pbs_output: {
          lines: ["PBS: job 1234.aps started", "rank 0: 441 inputs"],
          truncated: true,
          n_lines: null,
          file: makeRunFile("pbs-output.log"),
        },
      }),
    })
    await vi.waitFor(() => {
      expect(definitions(region("Remote Run"))).toEqual({
        Target: "ALCF via DM",
        "DM state": "running",
        "Globus run": "Globus run(opens in a new tab)",
        "Last polled": "25 s ago",
      })
    })
    const globus = byRole("link", /^Globus run/, region("Remote Run"))[0]
    expect(globus?.getAttribute("href")).toBe(
      "https://app.globus.org/runs/0c5e7a12-1d3b-4f6a-8e2c-9b7d5f3a1e60"
    )
    expect(globus?.getAttribute("target")).toBe("_blank")
    expect(globus?.getAttribute("rel")).toBe("noopener noreferrer")
    expect(region("Run Progress").querySelector("dl")?.title).toBe(
      "Progress counters update after the results return"
    )
    expect(region("Run Progress").textContent).not.toContain("heartbeat")
    await vi.waitFor(() => {
      expect(
        region("Run Configuration").querySelector('[aria-label="PBS output"]')
          ?.textContent
      ).toBe(
        "PBS output (last 2 lines)PBS: job 1234.aps started\nrank 0: 441 inputs"
      )
    })
  })

  it("shows a crux run's phase badge, its detail, its PBS ID and status delayed with the observation time", async () => {
    const observed = "2026-10-25T12:00:00Z"
    await openJob("/jobs/40", {
      job: makeCruxJob(makeObservation({ stale: true, observed_at: observed })),
    })
    const phase = await vi.waitFor(() => {
      const found = region("Remote Run").querySelector(
        '[data-testid="remote-phase"]'
      )
      expect(found).not.toBeNull()
      return found as HTMLElement
    })
    expect(definitions(region("Remote Run"))).toMatchObject({
      Target: "ALCF via DM",
      Phase: "RunningProcessing · PBS 123456 · status delayed",
      "DM state": "running",
    })
    expect(phase.querySelector("[data-phase]")?.textContent).toBe("Running")
    const delayed = Array.from(phase.querySelectorAll("span")).find(
      (span) => span.textContent === "status delayed"
    )
    expect(delayed?.title).toBe(`Observed ${formatDateTime(observed)}`)
  })

  it("Stop Run asks first, then says what the stop did", async () => {
    const { api } = await openJob("/jobs/41")
    await vi.waitFor(() => {
      expect(byRole("button", "Stop Run")[0]?.hasAttribute("disabled")).toBe(
        false
      )
    })
    await click(byRole("button", "Stop Run")[0])
    const dialog = await vi.waitFor(() => {
      const found = byRole("alertdialog")[0]
      expect(found).toBeDefined()
      return found as HTMLElement
    })
    expect(dialog.textContent).toContain("Stop job 41?")
    expect(dialog.textContent).toContain(
      "a running run finishes the inputs in flight"
    )
    expect(api.requests.some((request) => request.method === "POST")).toBe(
      false
    )

    await click(byRole("button", "Stop Run", dialog)[0])
    await vi.waitFor(() => {
      expect(
        document.querySelector("[data-sonner-toast] [data-title]")?.textContent
      ).toBe("Stop Run")
    })
    expect(
      document.querySelector("[data-sonner-toast] [data-description]")
        ?.textContent
    ).toBe(
      "Job 41 (I72): stop requested. The inputs in flight finish and completed results are kept; up to 318 pending inputs will be recorded as not run."
    )
    expect(
      api.requests
        .filter((request) => request.method === "POST")
        .map((request) => request.url.pathname)
    ).toEqual(["/api/v1/jobs/41/cancel"])
  })

  it("shows a live run's source, arrivals and why it waits, and its Stop says in one line what it does", async () => {
    const { api } = await openJob("/jobs/41", {
      job: makeJobDetail({
        live: makeJobLive({
          source: "run",
          folder: "/data/scan_1",
          filename_template: "wire_%d.h5",
          follows: { kind: "reconstruction", id: 5, label: "R5" },
          follows_job_id: 40,
          arrivals: 2,
          arrival: {
            state: "available",
            checked_at: "2026-10-27T10:00:00+00:00",
            rows_seen: 3,
            error: "point 'wire_3' (line 2) not admitted yet: missing",
            error_at: "2026-10-27T09:59:00+00:00",
          },
        }),
      }),
    })
    const card = await vi.waitFor(() => region("Live Run"))
    expect(definitions(card)).toMatchObject({
      Source: "Reconstruction R5Job 40",
      Arrivals: "2",
      "Points Read": "3",
      Waiting: "point 'wire_3' (line 2) not admitted yet: missing",
    })
    expect(
      Array.from(card.querySelectorAll("a")).map((link) =>
        link.getAttribute("href")
      )
    ).toEqual(["/reconstructions/5", "/jobs/40"])

    await click(byRole("button", "Stop Run")[0])
    const dialog = await vi.waitFor(() => {
      const found = byRole("alertdialog")[0]
      expect(found).toBeDefined()
      return found as HTMLElement
    })
    expect(dialog.textContent).toContain(
      "No new files are taken; the files taken are finished."
    )
    expect(dialog.textContent).not.toContain("inputs in flight")
    await click(byRole("button", "Stop Run", dialog)[0])
    await vi.waitFor(() => {
      expect(
        document.querySelector("[data-sonner-toast] [data-description]")
          ?.textContent
      ).toBe(
        "Job 41 (I72): stop requested. No new files are taken; the files taken are finished."
      )
    })
    expect(gets(api, "/api/v1/jobs/41").length).toBeGreaterThan(0)
  })

  it("has no Live Run card for a batch run", async () => {
    await openJob("/jobs/41")
    await vi.waitFor(() => region("Run Progress"))
    expect(() => region("Live Run")).toThrow("no region Live Run")
  })

  it("an unknown job is a not-found page", async () => {
    await openJob("/jobs/999")
    await vi.waitFor(() => {
      expect(byRole("heading", "Job 999 not found")).toHaveLength(1)
    })
    expect(document.title).toBe("Page not found · 3DMN Portal")
    expect(
      byRole("link", "Go to the run monitor")[0]?.getAttribute("href")
    ).toBe("/runs")
  })
})
