import { createMemoryHistory, RouterProvider } from "@tanstack/react-router"
import { act } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"

import {
  type CreatedLiveRuns,
  getGetLiveSessionQueryKey,
  getListProgramsQueryKey,
  type LiveDaqList,
  type LiveSession,
  type ProgramList,
  type ProgramPrefill,
} from "@/api"
import { Providers } from "@/app/Providers"
import { createQueryClient } from "@/app/query-client"
import { createAppRouter } from "@/app/router"
import { bodyOf, PROGRAMS, urlOf } from "@/test/programs"
import {
  byRole,
  cleanup,
  click,
  enterText,
  render,
  stubMatchMedia,
} from "@/test/render"

// The Live page on the app's real route, its API answered here: off without the live programs; with no
// session, an open one (read-only, its counters, links and arrival status) and an ended one (the form its
// clone); Start, a Start refused by a session open elsewhere, Stop, the issues on the cards, and the DAQ list
// asked only while the form is open.

interface Api {
  programs: ProgramList
  session: LiveSession | null
  /** The session's read fails with this answer, while set. */
  sessionFails: (() => Response) | null
  /** GET /programs fails with this answer, while set. */
  programsFail: (() => Response) | null
  daqs: LiveDaqList
  issues: unknown[]
  /** A validation's answer waits for this, while set (its issues read then). */
  validateHold: Promise<void> | null
  start: () => Response
  calls: { method: string; path: string; body: unknown; key: string | null }[]
}

function run(values: Partial<LiveSession["runs"][number]> = {}) {
  return {
    job_id: 7,
    run: { kind: "reconstruction" as const, id: 3, label: "R3" },
    status: "running" as const,
    n_inputs: 5,
    n_succeeded: 3,
    n_failed: 1,
    stop_requested: false,
    stopped_now: false,
    arrival: null,
    ...values,
  }
}

function makeSession(values: Partial<LiveSession> = {}): LiveSession {
  return {
    open: true,
    stopped: false,
    runs: [
      run({
        arrival: {
          state: "available",
          checked_at: "2026-10-27T10:00:00Z",
          rows_seen: 5,
          error: "1 catalog row(s) not used: x.h5",
          error_at: "2026-10-27T09:59:00Z",
        },
      }),
      run({
        job_id: 8,
        run: { kind: "indexing", id: 4, label: "I4" },
        n_inputs: 30,
        n_succeeded: 28,
        n_failed: 0,
      }),
    ],
    scan: {
      source: "notify",
      daq_id: null,
      folder: "/data/live",
      filename_template: "wire_%d.h5",
      output_folder: "/data/out",
      scan_number: 12,
      author: "alice",
    },
    ...values,
  }
}

function clone(kind: "wire" | "lauego", values: Record<string, unknown>) {
  const prefill: ProgramPrefill = {
    kind,
    schema_id: `${kind}/v1`,
    clone_of: null,
    values: { author: "", notes: "", ...values },
  }
  return Response.json(prefill)
}

function problem(
  status: number,
  title: string,
  detail: string,
  type = "about:blank"
) {
  return Response.json(
    { type, title, status, detail },
    { status, headers: { "Content-Type": "application/problem+json" } }
  )
}

async function openLive(changes: Partial<Api> = {}) {
  stubMatchMedia()
  const api: Api = {
    programs: PROGRAMS,
    session: null,
    sessionFails: null,
    programsFail: null,
    daqs: {
      daqs: [],
      failed: [],
      checked_at: "2026-10-27T10:00:00Z",
      kept_seconds: 30,
    },
    issues: [],
    validateHold: null,
    start: () => problem(500, "Internal Server Error", "unexpected"),
    calls: [],
    ...changes,
  }
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const target = urlOf(input)
    const method = init?.method ?? "GET"
    const headers = new Headers(init?.headers)
    api.calls.push({
      method,
      path: target.pathname,
      body: bodyOf(init),
      key: headers.get("Idempotency-Key"),
    })
    const path = target.pathname
    const answer = (): Response => {
      if (path === "/api/v1/programs") {
        return api.programsFail?.() ?? Response.json(api.programs)
      }
      if (path === "/api/v1/live/session") {
        return api.sessionFails?.() ?? Response.json({ session: api.session })
      }
      if (path === "/api/v1/live/daqs") return Response.json(api.daqs)
      if (path === "/api/v1/programs/wire/prefill")
        return clone("wire", { depth_start: -12 })
      if (path === "/api/v1/programs/lauego/prefill")
        return clone("lauego", { output_xml: "live.xml" })
      if (path === "/api/v1/live-runs/validate")
        return Response.json({ issues: api.issues })
      if (path === "/api/v1/live-runs") return api.start()
      const cancel = /^\/api\/v1\/jobs\/(\d+)\/cancel$/.exec(path)
      if (cancel)
        return Response.json({ job_id: Number(cancel[1]), state: "requested" })
      const now = /^\/api\/v1\/jobs\/(\d+)\/stop-now$/.exec(path)
      if (now)
        return Response.json({ job_id: Number(now[1]), state: "stopping_now" })
      if (path === "/api/v1/health")
        return Response.json({ db: "ok", worker: "running", version: "2" })
      return Response.json({
        rows: [],
        total: 0,
        as_of: "2026-10-27T10:00:00Z",
      })
    }
    if (path === "/api/v1/live-runs/validate" && api.validateHold) {
      return api.validateHold.then(() => Response.json({ issues: api.issues }))
    }
    return Promise.resolve(answer())
  })
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined)
  const queryClient = createQueryClient()
  const router = createAppRouter(
    queryClient,
    createMemoryHistory({ initialEntries: ["/live"] })
  )
  app = { queryClient, router }
  await render(
    <Providers queryClient={queryClient}>
      <RouterProvider router={router} />
    </Providers>
  )
  await act(() => router.load())
  return api
}

let app:
  | {
      queryClient: ReturnType<typeof createQueryClient>
      router: ReturnType<typeof createAppRouter>
    }
  | undefined

/** The session read again now (the page reads it every 5 s only while it is open). */
async function readAgain() {
  await act(async () => {
    await app?.queryClient.refetchQueries({
      queryKey: getGetLiveSessionQueryKey(),
    })
  })
}

/** The session's state, as its badge says. */
function badge(): string | null | undefined {
  return region("Session")
    .querySelector('[data-slot="card-title"] [data-status]')
    ?.getAttribute("data-status")
}

/** Let `ms` pass (the validation's 500 ms pause, an answer that should not come). */
async function settle(ms: number) {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms))
  })
}

async function until(check: () => void) {
  await vi.waitFor(
    async () => {
      await act(async () => {
        await Promise.resolve()
      })
      check()
    },
    { timeout: 3000 }
  )
}

function region(name: string): HTMLElement {
  const found = document.querySelector<HTMLElement>(
    `[role="region"][aria-label="${name}"]`
  )
  if (!found) throw new Error(`no region ${name}`)
  return found
}

function input(card: string, name: string): HTMLInputElement {
  const found = region(card).querySelector<HTMLInputElement>(`[name="${name}"]`)
  if (!found) throw new Error(`no ${name} in ${card}`)
  return found
}

function button(name: string): HTMLButtonElement {
  const [found] = byRole("button", name, region("Session"))
  if (!(found instanceof HTMLButtonElement)) throw new Error(`no ${name}`)
  return found
}

function form(): HTMLFieldSetElement {
  const found = document.querySelector<HTMLFieldSetElement>(
    'fieldset[aria-label="Live session form"]'
  )
  if (!found) throw new Error("no form")
  return found
}

const asked = (api: Api, path: string, method = "GET") =>
  api.calls.filter((call) => call.path === path && call.method === method)

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe("the Live page", () => {
  it("says live processing is off when GET /programs lists no live program", async () => {
    await openLive({
      programs: {
        programs: PROGRAMS.programs.filter(
          (program) => program.kind === "wire" || program.kind === "lauego"
        ),
      },
    })
    await until(() => {
      expect(document.body.textContent).toContain("Live processing is off")
    })
    expect(document.querySelector("fieldset")).toBeNull()
  })

  it("a failed read again of GET /programs (a 503) keeps the form and the navbar's Live (the last known)", async () => {
    const api = await openLive()
    await until(() => {
      expect(form().disabled).toBe(false)
    })
    await enterText(input("Scan", "author"), "bob")
    app?.queryClient.setQueryDefaults(getListProgramsQueryKey(), {
      retry: false,
    })
    api.programsFail = () =>
      problem(503, "Service Unavailable", "Remote execution is not usable")
    await act(async () => {
      await app?.queryClient.refetchQueries({
        queryKey: getListProgramsQueryKey(),
      })
    })
    await until(() => {
      expect(
        app?.queryClient.getQueryState(getListProgramsQueryKey())?.status
      ).toBe("error")
    })
    expect(input("Scan", "author").value).toBe("bob")
    expect(
      document.querySelector('nav[aria-label="Main"] a[href="/live"]')
    ).not.toBeNull()
  })

  it("with no session: Idle, the form open with the configured defaults, the DAQ list asked once", async () => {
    const api = await openLive()
    await until(() => {
      expect(region("Session").textContent).toContain("No live session")
    })
    expect(
      region("Session").querySelector('[data-status="idle"]')
    ).not.toBeNull()
    await until(() => {
      expect(form().disabled).toBe(false)
    })
    expect(button("Start").disabled).toBe(false)
    expect(button("Stop").disabled).toBe(true)
    expect(input("Scan", "output_folder").value).toBe("/data/runs/analysis")
    await until(() => {
      expect(asked(api, "/api/v1/live/daqs")).toHaveLength(1)
    })
  })

  it("titles every card with a heading, the Session card's first, its badge beside it", async () => {
    await openLive()
    await until(() => {
      expect(form().disabled).toBe(false)
    })
    const titles = Array.from(document.querySelectorAll("h2"), (heading) =>
      heading.textContent.trim()
    )
    expect(titles[0]).toBe("Session")
    expect(titles).toEqual(
      expect.arrayContaining(["Scan", "Reconstruction", "Indexing"])
    )
    const title = region("Session").querySelector('[data-slot="card-title"]')
    expect(title?.querySelector("h2")?.textContent).toBe("Session")
    expect(title?.querySelector("[data-status]")).not.toBeNull()
  })

  it("each program card's switch comes first in its header, before its title", async () => {
    await openLive()
    await until(() => {
      expect(form().disabled).toBe(false)
    })
    for (const title of ["Reconstruction", "Indexing"]) {
      const header = region(title).querySelector('[data-slot="card-header"]')
      const toggle = header?.querySelector('[role="switch"]')
      const heading = header?.querySelector('[data-slot="card-title"]')
      expect(heading?.textContent).toBe(title)
      expect(header?.firstElementChild).toBe(toggle)
      expect(toggle && heading && toggle.compareDocumentPosition(heading)).toBe(
        Node.DOCUMENT_POSITION_FOLLOWING
      )
    }
  })

  it("picking a DAQ as the Source fills the folder with where it writes and shows its folders", async () => {
    const daq = {
      id: "d1",
      experiment: "e1",
      status: "Running",
      data_directory: "/detector/e1/raw",
      storage_directory: "/data/e1",
      dest_directory: "34ide",
      destination: "/data/e1/34ide",
    }
    await openLive({
      daqs: {
        daqs: [daq],
        failed: [],
        checked_at: "2026-10-27T10:00:00Z",
        kept_seconds: 30,
      },
    })
    await until(() => {
      expect(form().disabled).toBe(false)
    })
    await click(region("Scan").querySelector('[data-slot="select-trigger"]'))
    const options = byRole("option")
    expect(options.map((option) => option.textContent)).toEqual([
      "Notifications",
      "e1 · e1/raw (Running)",
    ])
    await click(options[1])
    await until(() => {
      expect(input("Scan", "folder").value).toBe("/data/e1/34ide")
    })
    expect(
      region("Scan").querySelector('dl[aria-label="DAQ"]')?.textContent
    ).toContain("/detector/e1/raw")
  })

  it("a DAQ session's values keep its folder as recorded (the DAQ fills the folder only when picked)", async () => {
    const api = await openLive({
      daqs: {
        daqs: [
          {
            id: "d1",
            experiment: "e1",
            status: "Running",
            data_directory: "/detector/e1/raw",
            storage_directory: "/data/e1",
            dest_directory: "34ide",
            destination: "/data/e1/34ide",
          },
        ],
        failed: [],
        checked_at: "2026-10-27T10:00:00Z",
        kept_seconds: 30,
      },
    })
    await until(() => {
      expect(asked(api, "/api/v1/live/daqs")).toHaveLength(1)
    })
    // Recorded resolved: the storage behind a link, so not inside the listed destination by its text.
    const recorded = "/net/store/e1/34ide/scan5"
    const scan = makeSession().scan
    api.session = makeSession({
      open: false,
      runs: [run({ status: "finished" })],
      scan: scan && { ...scan, source: "daq", daq_id: "d1", folder: recorded },
    })
    await readAgain()
    await until(() => {
      expect(badge()).toBe("finished")
    })
    await until(() => {
      expect(input("Scan", "folder").value).toBe(recorded)
    })
    expect(
      region("Scan").querySelector('[data-slot="select-trigger"]')?.textContent
    ).toContain("e1 · e1/raw (Running)")
    await act(async () => {
      await Promise.resolve()
    })
    expect(input("Scan", "folder").value).toBe(recorded)
  })

  it("an open session: its runs' counters, arrival status and links, the form read-only, no DAQ list", async () => {
    const api = await openLive({ session: makeSession() })
    await until(() => {
      expect(region("Session").querySelector('[data-job="7"]')).not.toBeNull()
    })
    expect(form().disabled).toBe(true)
    const wire = region("Session").querySelector('[data-job="7"]')
    const counts = (row: Element | null) =>
      ["arrived", "done", "failed"].map(
        (name) => row?.querySelector(`[data-count="${name}"]`)?.textContent
      )
    expect(counts(wire)).toEqual(["5", "3", "1"])
    expect(wire?.querySelector("[data-arrival]")?.textContent).toBe(
      "1 catalog row(s) not used: x.h5"
    )
    expect(
      wire?.querySelector('a[href="/reconstructions/3"]')?.textContent
    ).toBe("R3")
    expect(wire?.querySelector('a[href="/jobs/7"]')).not.toBeNull()
    expect(counts(region("Session").querySelector('[data-job="8"]'))).toEqual([
      "30",
      "28",
      "0",
    ])
    expect(
      region("Session").querySelector('[data-status="running"]')
    ).not.toBeNull()
    await until(() => {
      expect(input("Scan", "folder").value).toBe("/data/live")
    })
    expect(input("Scan", "author").value).toBe("alice")
    expect(input("Reconstruction", "depth_start").value).toBe("-12")
    // Base UI's switch and checkbox are not native controls: they are told, not the fieldset.
    expect(
      byRole("switch", "Reconstruction")[0]?.hasAttribute("data-disabled")
    ).toBe(true)
    expect(
      region("Indexing")
        .querySelector('[role="checkbox"]')
        ?.hasAttribute("data-disabled")
    ).toBe(true)
    expect([button("Validate").disabled, button("Start").disabled]).toEqual([
      true,
      true,
    ])
    expect(button("Stop").disabled).toBe(false)
    expect(asked(api, "/api/v1/live/daqs")).toHaveLength(0)
  })

  it("an ended session: its state, and the form its clone, editable (the author reset, the runs' values)", async () => {
    const ended = makeSession({
      open: false,
      runs: [run({ status: "finished" })],
    })
    await openLive({ session: ended })
    await until(() => {
      expect(input("Scan", "folder").value).toBe("/data/live")
    })
    expect(form().disabled).toBe(false)
    expect(
      region("Session").querySelector('[data-status="finished"]')
    ).not.toBeNull()
    expect(input("Scan", "author").value).toBe("")
    expect(input("Scan", "filename_template").value).toBe("wire_%d.h5")
    expect(input("Reconstruction", "depth_start").value).toBe("-12")
    // A session of a wire run alone: the Indexing card is switched off.
    expect(region("Indexing").querySelector("[name]")).toBeNull()
    expect([button("Start").disabled, button("Stop").disabled]).toEqual([
      false,
      true,
    ])
  })

  it("a failed read again keeps the session, the form, Validate and Start, with an error line", async () => {
    const api = await openLive({
      session: makeSession({
        open: false,
        runs: [run({ status: "finished" })],
      }),
    })
    await until(() => {
      expect(input("Scan", "folder").value).toBe("/data/live")
    })
    // A failed read at once (the client's retries are a few seconds).
    app?.queryClient.setQueryDefaults(getGetLiveSessionQueryKey(), {
      retry: false,
    })
    api.sessionFails = () =>
      problem(503, "Service Unavailable", "The database is busy")
    await readAgain()
    const line = () =>
      [...region("Session").querySelectorAll('[role="status"]')].find((node) =>
        node.textContent.includes("Session not refreshed")
      )
    await until(() => {
      expect(line()?.textContent).toBe(
        "Session not refreshed: The database is busy"
      )
    })
    expect(badge()).toBe("finished")
    expect(region("Session").querySelector('[data-job="7"]')).not.toBeNull()
    expect(form().disabled).toBe(false)
    expect([button("Validate").disabled, button("Start").disabled]).toEqual([
      false,
      false,
    ])

    api.sessionFails = null
    await click(button("Retry"))
    await until(() => {
      expect(line()).toBeUndefined()
    })
    expect(badge()).toBe("finished")
  })

  it("Start sends the session with a key, each part switched on, then shows the session it made", async () => {
    const created: CreatedLiveRuns = {
      runs: [
        {
          run: { kind: "reconstruction", id: 3, label: "R3" },
          job_id: 7,
          status: "queued",
          target: "local",
          queue_id: "live_7",
          failure: null,
        },
      ],
    }
    const api = await openLive()
    api.start = () => {
      api.session = makeSession({ runs: [run({ status: "queued" })] })
      return Response.json(created, { status: 201 })
    }
    await until(() => {
      expect(form().disabled).toBe(false)
    })
    await enterText(input("Scan", "folder"), "/data/live")
    await enterText(input("Scan", "filename_template"), "wire_%d.h5")
    await enterText(input("Scan", "author"), "alice")
    const indexing = byRole("switch", "Indexing")[0]
    await click(indexing)
    await click(button("Start"))
    await until(() => {
      expect(asked(api, "/api/v1/live-runs", "POST")).toHaveLength(1)
    })
    const [sent] = asked(api, "/api/v1/live-runs", "POST")
    expect(sent?.key).toMatch(/^[0-9a-f-]{36}$/)
    expect(sent?.body).toMatchObject({
      scan: {
        source: "notify",
        daq_id: null,
        folder: "/data/live",
        filename_template: "wire_%d.h5",
        author: "alice",
      },
      indexing: null,
    })
    expect(
      (sent?.body as { reconstruction: object }).reconstruction
    ).toHaveProperty("geometry_file")
    await until(() => {
      expect(form().disabled).toBe(true)
    })
    expect(region("Session").querySelector('[data-job="7"]')).not.toBeNull()
    // Its own values stay on screen (not replaced by the session's clone while it runs).
    expect(input("Scan", "author").value).toBe("alice")
  })

  it("after its own session has ended, leaving the page does not ask (its typed values went into it)", async () => {
    const created: CreatedLiveRuns = {
      runs: [
        {
          run: { kind: "reconstruction", id: 3, label: "R3" },
          job_id: 7,
          status: "queued",
          target: "local",
          queue_id: "live_7",
          failure: null,
        },
      ],
    }
    const api = await openLive()
    api.start = () => {
      api.session = makeSession({ runs: [run({ status: "queued" })] })
      return Response.json(created, { status: 201 })
    }
    await until(() => {
      expect(form().disabled).toBe(false)
    })
    await enterText(input("Scan", "folder"), "/data/live")
    await enterText(input("Scan", "filename_template"), "wire_%d.h5")
    await enterText(input("Scan", "author"), "bob")
    await click(button("Start"))
    await until(() => {
      expect(form().disabled).toBe(true)
    })

    api.session = makeSession({
      open: false,
      runs: [run({ status: "finished" })],
    })
    await readAgain()
    await until(() => {
      expect(form().disabled).toBe(false)
    })
    // The author typed for the session stays (a clone resets it, but it was typed).
    expect(input("Scan", "author").value).toBe("bob")
    act(() => {
      void app?.router.navigate({ to: "/runs" })
    })
    await until(() => {
      expect(app?.router.state.location.pathname).toBe("/runs")
    })
    expect(byRole("alertdialog")).toHaveLength(0)
  })

  it("a Start refused by a session open elsewhere shows why, and the session is read again", async () => {
    const api = await openLive()
    api.start = () => {
      api.session = makeSession()
      return problem(
        409,
        "Conflict",
        "Live run job 7 is Running: one live session at a time",
        "urn:laueportal:problem:live-session-open"
      )
    }
    await until(() => {
      expect(form().disabled).toBe(false)
    })
    await enterText(input("Scan", "folder"), "/data/live")
    await enterText(input("Scan", "author"), "bob")
    await click(button("Start"))
    await until(() => {
      expect(region("Session").textContent).toContain(
        "Live run job 7 is Running: one live session at a time"
      )
    })
    // The session open elsewhere is shown read-only with its own values.
    await until(() => {
      expect(input("Scan", "author").value).toBe("alice")
    })
    expect(form().disabled).toBe(true)
  })

  it.each([
    [
      "urn:laueportal:problem:live-session-open",
      "R3 (job 7) is Running: one live session at a time",
      {
        open_run: {
          job_id: 7,
          run: makeSession().runs[0]?.run,
          status: "running",
        },
      },
    ],
    [
      "urn:laueportal:problem:key-created",
      "This Idempotency-Key already started live session R3 (job 7) with other values",
      {
        created: {
          runs: [
            {
              run: { kind: "reconstruction", id: 3, label: "R3" },
              job_id: 7,
              status: "queued",
              target: "local",
              queue_id: "live_7",
              failure: null,
            },
          ],
        },
      },
    ],
  ])(
    "a 409 (%s) shows until the session it names ends; the next Start is a new key",
    async (type, detail, named) => {
      const api = await openLive()
      api.start = () => {
        api.session = makeSession()
        return Response.json(
          { type, title: "Conflict", status: 409, detail, ...named },
          {
            status: 409,
            headers: { "Content-Type": "application/problem+json" },
          }
        )
      }
      await until(() => {
        expect(form().disabled).toBe(false)
      })
      await enterText(input("Scan", "folder"), "/data/live")
      await enterText(input("Scan", "filename_template"), "wire_%d.h5")
      await enterText(input("Scan", "author"), "bob")
      await click(button("Start"))
      await until(() => {
        expect(form().disabled).toBe(true)
      })
      expect(region("Session").textContent).toContain(detail)

      // The session it names has ended: the message goes, and Start sends a new key.
      api.session = makeSession({
        open: false,
        runs: [
          run({ status: "finished" }),
          run({ job_id: 8, status: "finished" }),
        ],
      })
      api.start = () => Response.json({ runs: [] }, { status: 201 })
      await readAgain()
      await until(() => {
        expect(badge()).toBe("finished")
      })
      await until(() => {
        expect(region("Session").textContent).not.toContain(detail)
      })
      await until(() => {
        expect(button("Start").disabled).toBe(false)
      })
      await click(button("Start"))
      await until(() => {
        expect(asked(api, "/api/v1/live-runs", "POST")).toHaveLength(2)
      })
      const [first, second] = asked(api, "/api/v1/live-runs", "POST")
      expect(second?.key).toMatch(/^[0-9a-f-]{36}$/)
      expect(second?.key).not.toBe(first?.key)
    }
  )

  it("Stop asks first, then stops the session's first run", async () => {
    const api = await openLive({ session: makeSession() })
    await until(() => {
      expect(button("Stop").disabled).toBe(false)
    })
    await click(button("Stop"))
    const dialog = byRole("alertdialog")[0]
    expect(dialog?.textContent).toContain(
      "No new files are taken; the files taken are finished."
    )
    expect(asked(api, "/api/v1/jobs/7/cancel", "POST")).toHaveLength(0)
    await click(byRole("button", "Stop", dialog)[0])
    await until(() => {
      expect(asked(api, "/api/v1/jobs/7/cancel", "POST")).toHaveLength(1)
    })
    expect(asked(api, "/api/v1/jobs/8/cancel", "POST")).toHaveLength(0)
    // Its report says the same line (the Run Monitor's wording is a batch run's).
    await until(() => {
      expect(
        document.querySelector("[data-sonner-toast] [data-description]")
          ?.textContent
      ).toMatch(
        /: stop requested\. No new files are taken; the files taken are finished\.$/
      )
    })
  })

  it("a Both session is Stopping from its wire run's stop until its follower ends: Stop is Stop now, the wait its badge's tooltip", async () => {
    const both = (
      wire: Partial<LiveSession["runs"][number]>,
      follower: Partial<LiveSession["runs"][number]>,
      open = true
    ) =>
      makeSession({
        open,
        runs: [
          run(wire),
          run({
            job_id: 8,
            run: { kind: "indexing", id: 4, label: "I4" },
            ...follower,
          }),
        ],
      })
    const api = await openLive({ session: both({}, {}) })
    await until(() => {
      expect(button("Stop").disabled).toBe(false)
    })
    expect(badge()).toBe("running")
    await click(button("Stop"))
    api.session = both({ stop_requested: true }, {})
    await click(byRole("button", "Stop", byRole("alertdialog")[0])[0])
    await until(() => {
      expect(badge()).toBe("stopping")
    })
    // Stopping: Stop is Stop now.
    expect(button("Stop now").disabled).toBe(false)

    // The wire run has ended; the follower waits on a point it cannot read: its reason is the Stopping badge's
    // tooltip.
    const reason =
      "point 'wire_3' (line 3 of the followed run's completed.jsonl) not admitted yet: /x/wire_3.h5 does not exist"
    api.session = both(
      { status: "finished" },
      {
        arrival: {
          state: "available",
          checked_at: "2026-10-27T10:00:00Z",
          rows_seen: 3,
          error: reason,
          error_at: "2026-10-27T09:59:00Z",
        },
      }
    )
    await readAgain()
    await until(() => {
      expect(
        region("Session").querySelector(
          '[data-job="7"] [data-status="finished"]'
        )
      ).not.toBeNull()
    })
    expect(badge()).toBe("stopping")
    expect(
      region("Session")
        .querySelector('[data-slot="card-title"] [data-status="stopping"]')
        ?.getAttribute("title")
    ).toBe(`I4: ${reason}`)
    expect(form().disabled).toBe(true)

    // Stop now asks in one line, then stops every open run: here the follower alone.
    await click(button("Stop now"))
    const dialog = byRole("alertdialog")[0]
    expect(dialog?.textContent).toContain(
      "Every open run is stopped; a run already stopping is killed and ends Failed, keeping what it published."
    )
    expect(asked(api, "/api/v1/jobs/8/stop-now", "POST")).toHaveLength(0)
    await click(byRole("button", "Stop now", dialog)[0])
    await until(() => {
      expect(asked(api, "/api/v1/jobs/8/stop-now", "POST")).toHaveLength(1)
    })
    expect(asked(api, "/api/v1/jobs/7/stop-now", "POST")).toHaveLength(0)

    // Both ended, the session stopped (its wire run's Stop), the follower Failed by its Stop now (killed, no failed
    // file): the Stop's end, so no session shown; Start on, Stop off.
    api.session = {
      ...both(
        { status: "finished" },
        { status: "failed", stopped_now: true, n_failed: 0 },
        false
      ),
      stopped: true,
    }
    await readAgain()
    await until(() => {
      expect(badge()).toBe("idle")
    })
    expect(region("Session").querySelector("[data-job]")).toBeNull()
    expect([button("Start").disabled, button("Stop").disabled]).toEqual([
      false,
      true,
    ])
    expect(asked(api, "/api/v1/jobs/7/cancel", "POST")).toHaveLength(1)
    expect(asked(api, "/api/v1/jobs/8/cancel", "POST")).toHaveLength(0)
  })

  it("after Stop, once every run has ended, the page shows no session (Idle, no runs table), the form its values", async () => {
    const api = await openLive({ session: makeSession() })
    await until(() => {
      expect(button("Stop").disabled).toBe(false)
    })
    await click(button("Stop"))
    api.session = makeSession({
      stopped: true,
      runs: [run({ stop_requested: true }), run({ job_id: 8 })],
    })
    await click(byRole("button", "Stop", byRole("alertdialog")[0])[0])
    await until(() => {
      expect(badge()).toBe("stopping")
    })
    expect(region("Session").querySelectorAll("[data-job]")).toHaveLength(2)

    api.session = makeSession({
      open: false,
      stopped: true,
      runs: [
        run({ status: "finished" }),
        run({ job_id: 8, status: "finished" }),
      ],
    })
    await readAgain()
    await until(() => {
      expect(badge()).toBe("idle")
    })
    expect(region("Session").textContent).toContain("No live session")
    expect(region("Session").querySelector("table")).toBeNull()
    // The form still fills from it, for the next Start.
    await until(() => {
      expect(form().disabled).toBe(false)
    })
    expect(input("Scan", "folder").value).toBe("/data/live")
    expect(input("Scan", "output_folder").value).toBe("/data/out")
    expect(input("Reconstruction", "depth_start").value).toBe("-12")
    expect([button("Start").disabled, button("Stop").disabled]).toEqual([
      false,
      true,
    ])
  })

  it("a session that ended without a Stop (a run failed) stays shown, its runs and its state", async () => {
    await openLive({
      session: makeSession({
        open: false,
        runs: [
          run({ status: "failed" }),
          run({ job_id: 8, status: "finished" }),
        ],
      }),
    })
    await until(() => {
      expect(badge()).toBe("failed")
    })
    expect(region("Session").querySelectorAll("[data-job]")).toHaveLength(2)
    expect(region("Session").textContent).not.toContain("No live session")
  })

  it("Output Folder follows the Folder's mirror until it is edited; From data folder mirrors it again", async () => {
    await openLive()
    await until(() => {
      expect(form().disabled).toBe(false)
    })
    const output = () => input("Scan", "output_folder").value
    const fromData = () => {
      const [found] = byRole("button", "From data folder", region("Scan"))
      if (!(found instanceof HTMLButtonElement))
        throw new Error("no From data folder")
      return found
    }
    // The configured default at first; the button off with nothing to mirror, its tooltip on a wrapper.
    expect(output()).toBe("/data/runs/analysis")
    expect(fromData().disabled).toBe(true)
    expect(fromData().parentElement?.getAttribute("title")).toBe(
      "From data folder"
    )
    expect(fromData().textContent).toBe("")

    await enterText(input("Scan", "folder"), "/x/exp/data/scan_12")
    expect(output()).toBe("/x/exp/analysis/scan_12")
    expect(fromData().disabled).toBe(false)
    // A folder with no data component gives the configured default, never the earlier Folder's mirror; so does a
    // blank one.
    await enterText(input("Scan", "folder"), "/x/exp/raw/scan_12")
    expect(output()).toBe("/data/runs/analysis")
    expect(fromData().disabled).toBe(true)
    await enterText(input("Scan", "folder"), "/x/exp/data/scan_12")
    expect(output()).toBe("/x/exp/analysis/scan_12")
    await enterText(input("Scan", "folder"), "")
    expect(output()).toBe("/data/runs/analysis")

    // Edited: it stays, whatever the Folder; From data folder mirrors it again, and it follows from then on.
    await enterText(input("Scan", "folder"), "/x/exp/data/scan_12")
    await enterText(input("Scan", "output_folder"), "/y/out")
    await enterText(input("Scan", "folder"), "/z/data/scan_2")
    expect(output()).toBe("/y/out")
    await click(fromData())
    expect(output()).toBe("/z/analysis/scan_2")
    await enterText(input("Scan", "folder"), "/z/data/scan_3")
    expect(output()).toBe("/z/analysis/scan_3")
  })

  it("a DAQ picked fills Folder and Output Folder its mirror", async () => {
    await openLive({
      daqs: {
        daqs: [
          {
            id: "d1",
            experiment: "e1",
            status: "Running",
            data_directory: "/detector/e1/raw",
            storage_directory: "/data/e1",
            dest_directory: "34ide/data/scan_5",
            destination: "/data/e1/34ide/data/scan_5",
          },
        ],
        failed: [],
        checked_at: "2026-10-27T10:00:00Z",
        kept_seconds: 30,
      },
    })
    await until(() => {
      expect(form().disabled).toBe(false)
    })
    await click(region("Scan").querySelector('[data-slot="select-trigger"]'))
    await click(byRole("option")[1])
    await until(() => {
      expect(input("Scan", "folder").value).toBe("/data/e1/34ide/data/scan_5")
    })
    expect(input("Scan", "output_folder").value).toBe(
      "/data/e1/34ide/analysis/scan_5"
    )
  })

  it("a form filled from the last session keeps its Output Folder (an edit) until From data folder", async () => {
    const scan = makeSession().scan
    await openLive({
      session: makeSession({
        open: false,
        runs: [run({ status: "finished" })],
        scan: scan && {
          ...scan,
          folder: "/x/exp/data/scan_7",
          output_folder: "/data/out",
          filename_template: null,
        },
      }),
    })
    await until(() => {
      expect(input("Scan", "folder").value).toBe("/x/exp/data/scan_7")
    })
    expect(input("Scan", "output_folder").value).toBe("/data/out")
    // A session with no template fills the field blank (every .h5 file).
    expect(input("Scan", "filename_template").value).toBe("")
    await enterText(input("Scan", "folder"), "/x/exp/data/scan_8")
    expect(input("Scan", "output_folder").value).toBe("/data/out")
    const [fromData] = byRole("button", "From data folder", region("Scan"))
    await click(fromData)
    expect(input("Scan", "output_folder").value).toBe("/x/exp/analysis/scan_8")
  })

  it("a form filled from the last session whose Output Folder is its Folder's mirror keeps following", async () => {
    const scan = makeSession().scan
    await openLive({
      session: makeSession({
        open: false,
        stopped: true,
        runs: [run({ status: "finished" })],
        scan: scan && {
          ...scan,
          folder: "/x/exp/data/scan_7",
          output_folder: "/x/exp/analysis/scan_7",
        },
      }),
    })
    await until(() => {
      expect(input("Scan", "folder").value).toBe("/x/exp/data/scan_7")
    })
    expect(input("Scan", "output_folder").value).toBe("/x/exp/analysis/scan_7")
    await enterText(input("Scan", "folder"), "/x/exp/data/scan_8")
    expect(input("Scan", "output_folder").value).toBe("/x/exp/analysis/scan_8")
  })

  it("its own session's end checks the form again: the report from before Start is not kept", async () => {
    const created: CreatedLiveRuns = {
      runs: [
        {
          run: { kind: "reconstruction", id: 3, label: "R3" },
          job_id: 7,
          status: "queued",
          target: "local",
          queue_id: "live_7",
          failure: null,
        },
      ],
    }
    const api = await openLive()
    api.start = () => {
      api.session = makeSession({ runs: [run({ status: "queued" })] })
      return Response.json(created, { status: 201 })
    }
    await until(() => {
      expect(form().disabled).toBe(false)
    })
    // The values the ended session gives back (its Scan card, its wire run's clone): the same request, the same key.
    await click(byRole("switch", "Indexing")[0])
    await enterText(input("Scan", "folder"), "/data/live")
    await enterText(input("Scan", "filename_template"), "wire_%d.h5")
    await enterText(input("Scan", "output_folder"), "/data/out")
    await enterText(input("Scan", "scan_number"), "12")
    await enterText(input("Scan", "author"), "alice")
    await enterText(input("Reconstruction", "depth_start"), "-12")
    const summary = () =>
      document.querySelector('section[aria-label="Validation"]')?.textContent ??
      ""
    await click(button("Validate"))
    await until(() => {
      expect(summary()).toContain("No issues")
    })
    await click(button("Start"))
    await until(() => {
      expect(form().disabled).toBe(true)
    })
    const checks = asked(api, "/api/v1/live-runs/validate", "POST").length

    // The server's answer for the same values has changed: the session left something in the folder.
    api.issues = [
      {
        loc: ["scan", "output_folder"],
        severity: "warning",
        code: "not_empty",
        message: "Not empty",
      },
    ]
    api.session = makeSession({
      open: false,
      runs: [run({ status: "finished" })],
    })
    await readAgain()
    await until(() => {
      expect(form().disabled).toBe(false)
    })
    await until(() => {
      expect(summary()).toContain("Output Folder: Not empty")
    })
    await settle(800)
    const after = asked(api, "/api/v1/live-runs/validate", "POST")
    // One request, for the same values as before Start: only the session's end asked again.
    expect(after).toHaveLength(checks + 1)
    expect(after.at(-1)?.body).toEqual(after[checks - 1]?.body)
  })

  it("a Start right after its own session's end waits for the new check and asks to confirm its warnings", async () => {
    const created: CreatedLiveRuns = {
      runs: [
        {
          run: { kind: "reconstruction", id: 3, label: "R3" },
          job_id: 7,
          status: "queued",
          target: "local",
          queue_id: "live_7",
          failure: null,
        },
      ],
    }
    const api = await openLive()
    api.start = () => {
      api.session = makeSession({ runs: [run({ status: "queued" })] })
      return Response.json(created, { status: 201 })
    }
    await until(() => {
      expect(form().disabled).toBe(false)
    })
    // The values the ended session gives back: the same request, so only the stale mark asks again.
    await click(byRole("switch", "Indexing")[0])
    await enterText(input("Scan", "folder"), "/data/live")
    await enterText(input("Scan", "filename_template"), "wire_%d.h5")
    await enterText(input("Scan", "output_folder"), "/data/out")
    await enterText(input("Scan", "scan_number"), "12")
    await enterText(input("Scan", "author"), "alice")
    await enterText(input("Reconstruction", "depth_start"), "-12")
    await click(button("Start"))
    await until(() => {
      expect(form().disabled).toBe(true)
    })
    expect(asked(api, "/api/v1/live-runs", "POST")).toHaveLength(1)

    let release: () => void = () => undefined
    api.validateHold = new Promise<void>((resolve) => {
      release = resolve
    })
    api.issues = [
      {
        loc: ["scan", "output_folder"],
        severity: "warning",
        code: "not_empty",
        message: "Not empty",
      },
    ]
    api.session = makeSession({
      open: false,
      runs: [run({ status: "finished" })],
    })
    await readAgain()
    await until(() => {
      expect(button("Start").disabled).toBe(false)
    })
    await click(button("Start"))
    await settle(300)
    // The report from before Start is stale: the Start waits for the new check and creates nothing yet.
    expect(asked(api, "/api/v1/live-runs", "POST")).toHaveLength(1)
    expect(byRole("alertdialog")).toHaveLength(0)
    await act(async () => {
      release()
      await Promise.resolve()
    })
    await until(() => {
      expect(byRole("alertdialog")[0]?.textContent).toContain(
        "Start with warnings?"
      )
    })
    expect(asked(api, "/api/v1/live-runs", "POST")).toHaveLength(1)
  })

  it("opening the page on an ended session checks its filled values once", async () => {
    const scan = makeSession().scan
    const api = await openLive({
      session: makeSession({
        open: false,
        stopped: true,
        runs: [run({ status: "finished" })],
        scan: scan && { ...scan, folder: "/x/exp/data/scan_7" },
      }),
    })
    await until(() => {
      expect(input("Scan", "folder").value).toBe("/x/exp/data/scan_7")
    })
    await until(() => {
      expect(asked(api, "/api/v1/live-runs/validate", "POST")).toHaveLength(1)
    })
    await settle(800)
    const checks = asked(api, "/api/v1/live-runs/validate", "POST")
    expect(checks).toHaveLength(1)
    expect((checks[0]?.body as { scan: { folder: string } }).scan.folder).toBe(
      "/x/exp/data/scan_7"
    )
  })

  it("a form filled from a session whose Folder was typed through a link keeps following", async () => {
    // Recorded: the Folder by its real path, the Output Folder as typed (through the link).
    const scan = makeSession().scan
    await openLive({
      session: makeSession({
        open: false,
        stopped: true,
        runs: [run({ status: "finished" })],
        scan: scan && {
          ...scan,
          folder: "/r/beamline/exp/data/scan_12",
          output_folder: "/r/link/exp/analysis/scan_12",
        },
      }),
    })
    await until(() => {
      expect(input("Scan", "folder").value).toBe("/r/beamline/exp/data/scan_12")
    })
    expect(input("Scan", "output_folder").value).toBe(
      "/r/link/exp/analysis/scan_12"
    )
    await enterText(input("Scan", "folder"), "/r/link/exp/data/scan_13")
    expect(input("Scan", "output_folder").value).toBe(
      "/r/link/exp/analysis/scan_13"
    )
  })

  it("Validate shows each issue on its card's field (a DAQ's on Source) and in the summary", async () => {
    const api = await openLive()
    api.issues = [
      {
        loc: ["scan", "daq_id"],
        code: "daq_refused",
        message: "Not this DAQ's folder",
      },
      {
        loc: ["reconstruction", "geometry_file"],
        code: "does_not_load",
        message: "Does not load as a geometry file",
      },
      {
        loc: ["indexing", "geometry_file"],
        severity: "warning",
        code: "w",
        message: "Checked at Start",
      },
    ]
    await until(() => {
      expect(form().disabled).toBe(false)
    })
    await click(button("Validate"))
    await until(() => {
      expect(region("Scan").textContent).toContain("Not this DAQ's folder")
    })
    expect(region("Reconstruction").textContent).toContain(
      "Does not load as a geometry file"
    )
    expect(region("Indexing").textContent).toContain("Checked at Start")
    expect(region("Reconstruction").textContent).not.toContain(
      "Checked at Start"
    )
    const summary = document.querySelector('section[aria-label="Validation"]')
    expect(summary?.textContent).toContain(
      "Reconstruction · Geometry File: Does not load as a geometry file"
    )
  })
})
