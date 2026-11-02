import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { createMemoryHistory, type RouterHistory } from "@tanstack/react-router"
import { act, useEffect, useState } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"
import type { CreatedRun, ProblemItem } from "@/api"
import { PROGRAMS, urlOf } from "@/test/programs"
import {
  byRole,
  cleanup,
  click,
  enterText,
  renderWithRouter,
} from "@/test/render"
import { ProgramForm } from "./ProgramForm"
import { VALIDATE_DELAY_MS } from "./use-live-validation"

const CREATED: CreatedRun = {
  run: { id: 4, kind: "reconstruction", label: "R4" },
  job_id: 9,
  status: "queued",
  target: "local",
  queue_id: "run_9",
  failure: null,
}

/** fetch for the wire form: GET /programs, validate answering `issues`, create answering 201 (counted). */
function stubApi(issues: ProblemItem[] = [], created: CreatedRun = CREATED) {
  const sent = { creates: 0 }
  vi.stubGlobal("fetch", (url: RequestInfo | URL) => {
    const { pathname } = urlOf(url)
    if (pathname === "/api/v1/programs")
      return Promise.resolve(Response.json(PROGRAMS))
    if (pathname === "/api/v1/reconstruction-runs/validate") {
      return Promise.resolve(Response.json({ issues }))
    }
    if (pathname === "/api/v1/reconstruction-runs") {
      sent.creates += 1
      return Promise.resolve(Response.json(created, { status: 201 }))
    }
    return Promise.resolve(new Response(null, { status: 404 }))
  })
  return sent
}

async function until(check: () => void, timeout = 2000) {
  await vi.waitFor(
    async () => {
      await act(async () => {
        await Promise.resolve()
      })
      check()
    },
    { timeout }
  )
}

/** The page's prefill, which a test replaces (another source). */
const page: { prefill?: (values: Record<string, unknown>) => void } = {}

function Page({ initial }: { initial?: Record<string, unknown> }) {
  const [values, setValues] = useState(initial)
  useEffect(() => {
    page.prefill = setValues
  }, [])
  return <ProgramForm kind="wire" values={values} />
}

async function mountWire(
  values?: Record<string, unknown>,
  history?: RouterHistory
) {
  const root = await renderWithRouter(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <Page initial={values} />
    </QueryClientProvider>,
    history
  )
  await until(() => {
    expect(root.querySelector('[data-slot="card-title"]')).not.toBeNull()
  })
  return root
}

const input = (root: ParentNode, name: string) =>
  root.querySelector<HTMLInputElement>(`[name="${name}"]`)

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe("ProgramForm", () => {
  it("shows the program's sections from x-layout, the prefill's values, and Run on beside Submit", async () => {
    stubApi()
    const root = await mountWire({
      scan_number: 990001,
      input_path: "/data/scan",
    })
    const titles = Array.from(
      root.querySelectorAll('[data-slot="card-title"]'),
      (title) => title.textContent
    )
    expect(titles).toEqual([
      "Identity",
      "File Paths",
      "Wire Reconstruction Parameters",
      "Notes",
    ])
    expect(byRole("button", "Restore Default")).toHaveLength(1)
    expect(input(root, "input_path")?.value).toBe("/data/scan")
    expect(input(root, "depth_start")?.value).toBe("-50")
    expect(root.textContent).toContain("Run on")
    expect(root.textContent).toContain("APS")
  })

  it("Restore Default puts back the section's configured defaults", async () => {
    stubApi()
    const root = await mountWire()
    await enterText(input(root, "depth_start"), "5")
    await enterText(input(root, "percent_brightest"), "20")
    await click(byRole("button", "Restore Default")[0])
    expect([
      input(root, "depth_start")?.value,
      input(root, "percent_brightest")?.value,
    ]).toEqual(["-50", "100"])
  })

  it("counts every issue from the start, but marks a field only once it is touched, an error red and a warning amber", async () => {
    stubApi([
      {
        loc: ["depth_start"],
        severity: "error",
        code: "depth_order",
        message: "Start after end",
      },
      {
        loc: ["scan_number"],
        severity: "warning",
        code: "no_catalog",
        message: "No catalog",
      },
      { loc: ["depth_end"], severity: "error", code: "x", message: "Too far" },
    ])
    const root = await mountWire({ scan_number: 990001 })
    await new Promise((resolve) => setTimeout(resolve, VALIDATE_DELAY_MS))
    const summary = () =>
      root.querySelector('section[aria-label="Validation"]')?.textContent
    await until(() => {
      expect(summary()).toContain("2 errors · 1 warning")
    })
    const marks = (severity: string) =>
      Array.from(
        root.querySelectorAll(`li[data-severity="${severity}"]`),
        (item) => [item.textContent, item.className]
      )
    // Untouched (the prefilled Scan Number too): nothing marked yet.
    expect([marks("error"), marks("warning")]).toEqual([[], []])
    expect(input(root, "depth_start")?.getAttribute("aria-invalid")).toBeNull()

    await enterText(input(root, "depth_start"), "60")
    await until(() => {
      expect(input(root, "depth_start")?.getAttribute("aria-invalid")).toBe(
        "true"
      )
    })
    act(() => {
      input(root, "depth_start")?.focus()
      input(root, "depth_start")?.blur() // left: touched
    })
    await enterText(input(root, "depth_start"), "-50") // back to its default, but touched
    expect(marks("error")).toEqual([
      ["Start after end", "text-destructive"], // under the field
      ["Depth Start: Start after end", "text-destructive"], // the summary names it once
    ])
    expect(marks("warning")).toEqual([])
    expect(input(root, "depth_end")?.getAttribute("aria-invalid")).toBeNull()
  })

  it("a Submit with errors marks every field, focuses the first error and sends nothing", async () => {
    const sent = stubApi([
      {
        loc: ["scan_number"],
        severity: "warning",
        code: "no_catalog",
        message: "No catalog",
      },
      { loc: ["depth_end"], severity: "error", code: "x", message: "Too far" },
      { loc: [], severity: "error", code: "invalid", message: "Cannot run" },
    ])
    const root = await mountWire()
    const submit = byRole("button", "Submit")[0]
    expect(submit).toHaveProperty("disabled", false)
    await click(submit)
    await until(() => {
      expect(document.activeElement).toBe(input(root, "depth_end"))
    })
    expect(input(root, "depth_end")?.getAttribute("aria-invalid")).toBe("true")
    const summary = root.querySelector('section[aria-label="Validation"]')
    const lines = Array.from(
      summary?.querySelectorAll("li") ?? [],
      (item) => item.textContent
    )
    expect(lines).toEqual([
      "Depth End: Too far", // errors first
      "Cannot run",
      "Scan Number: No catalog",
    ])
    expect(sent.creates).toBe(0)
  })

  it("a new prefill replaces the fields it derives from its source and keeps what the user changed", async () => {
    stubApi()
    const root = await mountWire({
      scan_number: 990001,
      input_path: "/data/scan_1",
    })
    await enterText(input(root, "input_path"), "/data/mine")
    await enterText(input(root, "depth_start"), "5")
    await enterText(root.querySelector('[name="author"]'), "jdoe")
    act(() => {
      page.prefill?.({ scan_number: 990002, input_path: "/data/scan_2" })
    })
    await until(() => {
      expect(input(root, "input_path")?.value).toBe("/data/scan_2")
    })
    expect(input(root, "depth_start")?.value).toBe("5")
    expect(input(root, "author")?.value).toBe("jdoe")
    expect(root.querySelector('input[role="combobox"]')).toHaveProperty(
      "value",
      "SN990002"
    )
  })

  it("opens the new run's page once it is created and queued", async () => {
    stubApi()
    const history = createMemoryHistory({
      initialEntries: ["/reconstructions/new"],
    })
    const root = await mountWire(undefined, history)
    // an edited form: the unsaved-changes guard lets the created run's page open
    await enterText(input(root, "author"), "jdoe")
    await click(byRole("button", "Submit")[0])
    await until(() => {
      expect(history.location.pathname).toBe("/reconstructions/4")
    })
  })

  it("keeps a run created but not queued in the Submitted panel, linking to it and its job, Submit off", async () => {
    stubApi([], {
      ...CREATED,
      status: "failed",
      queue_id: null,
      failure: "The queue is unavailable",
    })
    const history = createMemoryHistory({
      initialEntries: ["/reconstructions/new"],
    })
    const root = await mountWire(undefined, history)
    await click(byRole("button", "Submit")[0])
    await until(() => {
      expect(
        root.querySelector('section[aria-label="Submitted"]')
      ).not.toBeNull()
    })
    expect(
      root.querySelector('section[aria-label="Submitted"] [role="alert"]')
        ?.textContent
    ).toBe("The queue is unavailable")
    expect(history.location.pathname).toBe("/reconstructions/new")
    const links = Array.from(
      root.querySelectorAll('section[aria-label="Submitted"] a'),
      (link) => [link.textContent, link.getAttribute("href")]
    )
    expect(links).toEqual([
      ["R4", "/reconstructions/4"],
      ["9", "/jobs/9"],
    ])
    expect(byRole("button", "Submit")[0]).toHaveProperty("disabled", true)
  })
})
