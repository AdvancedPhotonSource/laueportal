import {
  createMemoryHistory,
  RouterProvider,
  type AnyRoute,
} from "@tanstack/react-router"
import { act } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { Health, ProgramList, SystemStatus } from "@/api"
import { byRole, cleanup, render, stubMatchMedia } from "@/test/render"
import { answerIndexing } from "@/test/indexing"
import { answerReconstruction } from "@/test/reconstruction"
import { failurePage, makeJobDetail, makeJobLog } from "@/test/jobs"
import { PROGRAMS } from "@/test/programs"
import { makeScanDetail } from "@/test/scans"
import { makeSystemStatus } from "@/test/system"
import { Providers } from "./Providers"
import { createQueryClient } from "./query-client"
import { createAppRouter } from "./router"

// The app's real route tree (src/routes), rendered in the shell: page titles, the 404 page of unknown
// URLs (routes/$.tsx) and the create forms' prefill.

const HEALTH_OK: Health = {
  db: "ok",
  worker: "running",
  version: "2.0.0.dev0",
}

const STATUS_OK: SystemStatus = makeSystemStatus({ version: HEALTH_OK.version })

/**
 * What the API answers: the health check, the status page's status, a scan, an indexing run (the
 * run, its artifact and summary), a reconstruction run (the run, its artifact and points), a job (its failures and log), the programs' forms, and empty lists.
 */
function answer(input: RequestInfo | URL): unknown {
  const url = input instanceof Request ? input.url : input
  const path = new URL(url, "http://localhost").pathname
  if (path === "/api/v1/health") return HEALTH_OK
  const indexing = answerIndexing(new URL(url, "http://localhost"))
  if (indexing !== undefined) return indexing
  const reconstruction = answerReconstruction(new URL(url, "http://localhost"))
  if (reconstruction !== undefined) return reconstruction
  if (path === "/api/v1/system/status") return STATUS_OK
  if (path === "/api/v1/programs") return programs
  if (path === "/api/v1/live/session") return { session: null }
  const scan = /^\/api\/v1\/scans\/(\d+)$/.exec(path)
  if (scan) return makeScanDetail({ scan_number: Number(scan[1]) })
  const job = /^\/api\/v1\/jobs\/(\d+)(\/failures|\/log)?$/.exec(path)
  if (job) {
    const jobId = Number(job[1])
    if (job[2] === "/failures") return failurePage(0, 0, 50)
    if (job[2] === "/log") return makeJobLog(jobId)
    return makeJobDetail({ job_id: jobId })
  }
  return { rows: [], total: 0, as_of: "2026-10-19T12:00:00Z" }
}

/** What GET /programs answers: every program (live processing on), unless a test lists fewer. */
let programs: ProgramList = PROGRAMS

afterEach(() => {
  cleanup()
  programs = PROGRAMS
})

async function openApp(url: string) {
  stubMatchMedia()
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const body = answer(input)
      return Promise.resolve(
        body instanceof Response ? body : Response.json(body)
      )
    })
  )
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined)
  const queryClient = createQueryClient()
  const history = createMemoryHistory({ initialEntries: [url] })
  const router = createAppRouter(queryClient, history)
  await render(
    <Providers queryClient={queryClient}>
      <RouterProvider router={router} />
    </Providers>
  )
  await act(() => router.load())
  return { router, history }
}

async function expectPage(title: string, heading: string) {
  await vi.waitFor(() => {
    expect(document.title).toBe(title)
  })
  expect(byRole("heading", heading)).toHaveLength(1)
}

describe("page titles", () => {
  it.each([
    ["/", "Status · 3DMN Portal", "Status"],
    ["/scans", "Scans · 3DMN Portal", "Scans"],
    ["/scans/7", "Scan 7 · 3DMN Portal", "Scan 7"],
    ["/scans/import", "Import Scans · 3DMN Portal", "Import Scans"],
    ["/reconstructions", "Reconstructions · 3DMN Portal", "Reconstructions"],
    [
      "/reconstructions/12",
      "Reconstruction R12 · 3DMN Portal",
      "Reconstruction R12",
    ],
    [
      "/reconstructions/new",
      "New Wire Reconstruction · 3DMN Portal",
      "New Wire Reconstruction",
    ],
    ["/indexing", "Indexations · 3DMN Portal", "Indexations"],
    ["/indexing/70", "Indexing I70 · 3DMN Portal", "Indexing I70"],
    [
      "/indexing/new",
      "New LaueGo Indexation · 3DMN Portal",
      "New LaueGo Indexation",
    ],
    ["/runs", "Run Monitor · 3DMN Portal", "Run Monitor"],
    ["/live", "Live · 3DMN Portal", "Live"],
    ["/jobs/5", "Job 5 · 3DMN Portal", "Job 5"],
    ["/no-such-page", "Page not found · 3DMN Portal", "Page not found"],
  ])("%s is titled %s", async (url, title, heading) => {
    await openApp(url)
    await expectPage(title, heading)
  })

  it("every route sets its title through its head", async () => {
    const { router } = await openApp("/")
    // The generated route types do not narrow to AnyRoute on their own.
    const routes = router.routesById as unknown as Record<string, AnyRoute>
    const untitled = Object.values(routes)
      .filter((route) => !route.isRoot && !route.options.head)
      .map((route) => String(route.id))
    expect(Object.keys(routes)).toContain("/indexing/$indexingId")
    expect(untitled).toEqual([])
  })
})

describe("unknown URLs", () => {
  it.each([
    "/scan",
    "/job?job_id=abc",
    "/peakindexing?indexing_id=",
    "/create-recon",
    "/peakindexing/5",
  ])("%s falls through to the 404 page", async (url) => {
    await openApp(url)
    await expectPage("Page not found · 3DMN Portal", "Page not found")
  })
})

describe("search values", () => {
  it("the router writes plain search values: readable commas, nothing quoted", () => {
    const router = createAppRouter(
      createQueryClient(),
      createMemoryHistory({ initialEntries: ["/"] })
    )
    const href = (hkl: string) =>
      router.buildLocation({
        to: "/indexing/$indexingId",
        params: { indexingId: "1" },
        search: { hkl },
      }).href
    // TanStack's default would write hkl=1%2C-10%2C2 and hkl=%22110%22 (src/lib/search.ts).
    expect(href("1,-10,2")).toBe("/indexing/1?hkl=1,-10,2")
    expect(href("110")).toBe("/indexing/1?hkl=110")
  })
})

describe("create-form prefill", () => {
  // The prefill as the route validates it (ID strings, an invalid value dropped) is what the form
  // asks GET /programs/lauego/prefill for; then the URL is clean.
  it.each([
    ["/indexing/new?scan=5", ["?scan=5"]],
    ["/indexing/new?scan=abc&reconstruction=-1&indexing=", []],
  ])("%s asks the prefill %j", async (url, asked) => {
    const { router, history } = await openApp(url)
    await vi.waitFor(() => {
      expect(router.state.location.href).toBe("/indexing/new")
      expect(byRole("heading", "New LaueGo Indexation")).toHaveLength(1)
    })
    await vi.waitFor(() => {
      expect(prefillsAsked()).toEqual(asked)
    })
    expect(history.length).toBe(1)
  })
})

/** The paths fetched so far. */
function fetchedPaths(): string[] {
  return vi.mocked(fetch).mock.calls.map(([input]) => {
    const url = input instanceof Request ? input.url : String(input)
    return new URL(url, "http://localhost").pathname
  })
}

/** The queries of the LaueGo form's prefill requests. */
function prefillsAsked(): string[] {
  return vi
    .mocked(fetch)
    .mock.calls.map(([input]) => {
      const url = input instanceof Request ? input.url : String(input)
      return new URL(url, "http://localhost")
    })
    .filter((url) => url.pathname === "/api/v1/programs/lauego/prefill")
    .map((url) => decodeURIComponent(url.search))
}

describe("the navbar's Live link", () => {
  it("shows only while GET /programs lists the live programs (live processing on)", async () => {
    programs = {
      programs: PROGRAMS.programs.filter(
        (program) => program.kind === "lauego" || program.kind === "wire"
      ),
    }
    await openApp("/runs")
    const nav = document.querySelector('nav[aria-label="Main"]')
    await vi.waitFor(() => {
      expect(fetchedPaths()).toContain("/api/v1/programs")
    })
    await act(() => Promise.resolve())
    expect(
      Array.from(nav?.querySelectorAll("a") ?? []).map((a) => a.textContent)
    ).toEqual(["Scans", "Reconstructions", "Indexations", "Run Monitor"])
  })
})

describe("the navbar's current link", () => {
  it.each([
    ["/scans", "Scans", "page"],
    ["/scans/7", "Scans", "true"],
    ["/scans/import", "Scans", "true"],
    ["/reconstructions/new", "Reconstructions", "true"],
    ["/indexing/70", "Indexations", "true"],
    ["/runs", "Run Monitor", "page"],
    ["/live", "Live", "page"],
    ["/", undefined, undefined],
    ["/jobs/5", undefined, undefined],
  ])("%s marks %s (aria-current %s)", async (url, label, current) => {
    await openApp(url)
    const nav = document.querySelector('nav[aria-label="Main"]')
    await vi.waitFor(() => {
      expect(nav?.querySelectorAll("a")).toHaveLength(5)
    })
    const marked = Array.from(
      nav?.querySelectorAll("a[aria-current]") ?? []
    ).map((link) => [
      link.textContent,
      link.getAttribute("aria-current"),
      link.getAttribute("data-section"),
    ])
    // The page itself is the "page"; a page below it marks its section's link "true".
    expect(marked).toEqual(label ? [[label, current, "active"]] : [])
  })
})
