import { createMemoryHistory, RouterProvider } from "@tanstack/react-router"
import { act } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { ProgramPrefill } from "@/api"
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
import { SOURCE_SEARCH_DELAY_MS } from "./source-search"

// The create pages on the app's real routes. The wire form: the URL's prefill taken once and removed, one
// source context applied, an unknown source's message. The Source switch: each form's kinds, the kind a
// prefill opens, the IDs a source sets shown read-only, a kind change clearing the source and its paths.
// The LaueGo form: what is its own (a reconstruction's scan.h5, an indexing clone).

function prefillOf(values: Record<string, unknown>, cloneOf?: number) {
  const prefill: ProgramPrefill = {
    kind: "wire",
    schema_id: "wire/v1",
    clone_of:
      cloneOf === undefined
        ? null
        : { kind: "reconstruction", id: cloneOf, label: `R${String(cloneOf)}` },
    values: { author: "", notes: "", ...values },
  }
  return prefill
}

const SCAN_H5 = "/runs/scan_990001/rec_1/reconstruction/scan.h5"

/** The LaueGo prefill: a reconstruction's scan.h5 with every depth, or a clone of I1; R2 and SN990003 unknown. */
function answerLaueGoPrefill(search: URLSearchParams): Response {
  if (search.get("reconstruction") === "2" || search.get("scan") === "990003")
    return Response.json(
      {
        type: "about:blank",
        title: "Not found",
        status: 404,
        detail: "No such source",
      },
      { status: 404 }
    )
  const prefill: ProgramPrefill = {
    kind: "lauego",
    schema_id: "lauego/v1",
    clone_of: null,
    values: { author: "", notes: "", scan_number: 990001 },
  }
  if (search.get("reconstruction") === "1") {
    prefill.values = {
      ...prefill.values,
      reconstruction_id: 1,
      input_path: SCAN_H5,
      filename_prefixes: ["wire_%d.h5"],
      scan_points: "1",
      depth_range: "",
    }
  } else if (search.get("indexing") === "1") {
    // The run's saved parameters, its author and notes reset.
    prefill.clone_of = { kind: "indexing", id: 1, label: "I1" }
    prefill.values = {
      ...prefill.values,
      reconstruction_id: null,
      input_path: "/data/frames",
      filename_prefixes: ["frame_%d.h5"],
      scan_points: "1-4",
      depth_range: "1-5",
      crystal_file: "/data/Si.xml",
      output_xml: "i1.xml",
      threshold_ratio: 4,
    }
  }
  return Response.json(prefill)
}

/** The prefill the API answers for a query string. */
function answerPrefill(search: URLSearchParams): Response {
  const scan = search.get("scan")
  const reconstruction = search.get("reconstruction")
  if (reconstruction === "1") {
    return Response.json(
      prefillOf(
        { scan_number: 990001, input_path: "/data/wire", depth_start: -25 },
        1
      )
    )
  }
  if (scan === "990001" || scan === "990002") {
    return Response.json(
      prefillOf({ scan_number: Number(scan), input_path: `/data/${scan}` })
    )
  }
  return Response.json(
    {
      type: "about:blank",
      title: "Scan not found",
      status: 404,
      detail: `No scan ${scan ?? ""}`,
    },
    { status: 404 }
  )
}

async function openPage(url: string) {
  stubMatchMedia()
  const prefills: string[] = []
  const validated: Record<string, unknown>[] = []
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const target = urlOf(input)
    if (target.pathname === "/api/v1/programs")
      return Promise.resolve(Response.json(PROGRAMS))
    if (target.pathname === "/api/v1/programs/wire/prefill") {
      prefills.push(decodeURIComponent(target.search))
      return Promise.resolve(answerPrefill(target.searchParams))
    }
    if (target.pathname === "/api/v1/programs/lauego/prefill") {
      prefills.push(decodeURIComponent(target.search))
      return Promise.resolve(answerLaueGoPrefill(target.searchParams))
    }
    if (
      /^\/api\/v1\/(reconstruction|indexing)-runs\/validate$/.test(
        target.pathname
      )
    ) {
      validated.push(bodyOf(init) as Record<string, unknown>)
      return Promise.resolve(Response.json({ issues: [] }))
    }
    // The pickers' searches: the scan 990002, the reconstructions R1 and R2, the indexing run I1.
    if (target.pathname === "/api/v1/scans") {
      const rows = [
        {
          scan_number: 990002,
          sample_name: "Si",
          time: "2026-10-21T18:00:00Z",
        },
      ]
      return Promise.resolve(Response.json({ rows, total: 1, as_of: "" }))
    }
    if (
      target.pathname === "/api/v1/reconstruction-runs" ||
      target.pathname === "/api/v1/indexing-runs"
    ) {
      const rows = [1, 2].map((id) => ({
        id,
        sample_name: "Si",
        created_at: "2026-10-21T18:00:00Z",
      }))
      return Promise.resolve(Response.json({ rows, total: 1, as_of: "" }))
    }
    return Promise.resolve(
      Response.json({ rows: [], total: 0, as_of: "2026-10-22T12:00:00Z" })
    )
  })
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
  return { router, history, prefills, validated }
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

const field = (name: string) =>
  document.querySelector<HTMLInputElement>(`[name="${name}"]`)

/** The input a label names (the clone picker has no form name). */
function labelled(text: string): HTMLInputElement | null {
  const label = Array.from(document.querySelectorAll("label")).find(
    (element) => element.textContent.trim() === text
  )
  return label ? document.querySelector(`#${CSS.escape(label.htmlFor)}`) : null
}

/** Pick the match whose ID ends with `typed` in the source picker labelled `label`. */
async function pick(label: string, typed: string) {
  const input = labelled(label)
  await click(
    input?.closest('[data-slot="input-group"]')?.querySelector("button")
  )
  await enterText(input, typed)
  const option = () =>
    byRole("option").find((item) =>
      item.querySelector("span")?.textContent.endsWith(typed)
    )
  await vi.waitFor(
    () => {
      expect(option()).toBeDefined()
    },
    { timeout: SOURCE_SEARCH_DELAY_MS + 2000 }
  )
  await click(option())
}

/** The Source switch's kinds, and the one pressed. */
const kinds = () =>
  Array.from(
    document.querySelectorAll('[data-slot="toggle-group-item"]'),
    (item) => item.textContent
  )
const pressed = () =>
  document.querySelector('[data-slot="toggle-group-item"][aria-pressed="true"]')
    ?.textContent

async function switchTo(kind: string) {
  await click(byRole("button", kind)[0])
}

/** The source pickers on the form (one per form). */
const pickers = () =>
  Array.from(
    document.querySelectorAll<HTMLInputElement>('input[role="combobox"]')
  )

/** The IDs the source sets, read-only: "Scan Number: SN990001". */
const sourceIds = () =>
  Array.from(
    document.querySelectorAll('dl[aria-label="Source IDs"] > div'),
    (row) =>
      `${row.querySelector("dt")?.textContent ?? ""}: ${row.querySelector("dd")?.textContent ?? ""}`
  )

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("the wire form's prefill", () => {
  it.each([["/reconstructions/new?scan=990001", "?scan=990001"]])(
    "%s is taken once (%s) and removed from the URL with replace",
    async (url, query) => {
      const { router, history, prefills } = await openPage(url)
      await until(() => {
        expect(field("input_path")?.value).toMatch(/^\/data\//)
      })
      expect(prefills).toEqual([query])
      expect(router.state.location.pathname).toBe("/reconstructions/new")
      expect(router.state.location.searchStr).toBe("")
      expect(history.length).toBe(1)
    }
  )

  it("shows an unknown source's message", async () => {
    await openPage("/reconstructions/new?scan=123")
    await until(() => {
      expect(document.body.textContent).toContain("Scan not found")
    })
    expect(document.body.textContent).toContain("No scan 123")
    expect(field("input_path")?.value).toBe("")
  })

  it("a clone, then another kind: the clone's source and paths are cleared, its parameters stay; a scan then fills the paths", async () => {
    const { prefills } = await openPage("/reconstructions/new")
    await until(() => {
      expect(field("author")).not.toBeNull()
    })
    await enterText(field("author"), "keep-me")
    await switchTo("Clone")
    // The first source: the form stays on screen while it loads.
    await pick("Clone of", "1")
    await until(() => {
      expect(field("input_path")?.value).toBe("/data/wire")
    })
    expect(prefills).toEqual(["?reconstruction=1"])
    expect(field("depth_start")?.value).toBe("-25")
    expect(field("author")?.value).toBe("keep-me")

    await switchTo("Scan")
    await until(() => {
      expect(field("input_path")?.value).toBe("")
    })
    expect(labelled("Scan Number")?.value).toBe("")
    expect(sourceIds()).toEqual([])
    expect(field("depth_start")?.value).toBe("-25")

    await pick("Scan Number", "990002")
    await until(() => {
      expect(field("input_path")?.value).toBe("/data/990002")
    })
    expect(prefills.at(-1)).toBe("?scan=990002")
    expect(field("depth_start")?.value).toBe("-25")
    expect(field("author")?.value).toBe("keep-me")
  })

  it("asks before leaving with edits", async () => {
    const { router } = await openPage("/reconstructions/new?scan=990001")
    await until(() => {
      expect(field("input_path")?.value).toBe("/data/990001")
    })
    await enterText(field("author"), "adoe")
    act(() => {
      void router.navigate({ to: "/runs" })
    })
    await until(() => {
      expect(byRole("alertdialog")[0]?.textContent).toContain(
        "Leave without saving?"
      )
    })
    await click(byRole("button", "Stay")[0])
    await until(() => {
      expect(byRole("alertdialog")).toHaveLength(0)
    })
    expect(router.state.location.pathname).toBe("/reconstructions/new")
    expect(field("author")?.value).toBe("adoe")
  })

  it("takes a later link's prefill on the page it is on, and removes it", async () => {
    const { router, prefills } = await openPage(
      "/reconstructions/new?scan=990001"
    )
    await until(() => {
      expect(field("input_path")?.value).toBe("/data/990001")
    })
    await act(() =>
      router.navigate({
        to: "/reconstructions/new",
        search: { scan: "990002" },
      })
    )
    await until(() => {
      expect(field("input_path")?.value).toBe("/data/990002")
    })
    expect(prefills).toEqual(["?scan=990001", "?scan=990002"])
    expect(router.state.location.searchStr).toBe("")
  })
})

describe("the Source switch", () => {
  it.each([
    ["/reconstructions/new", ["Scan", "Clone"]],
    ["/indexing/new", ["Scan", "Reconstruction", "Clone"]],
  ])(
    "%s offers %j, Scan by default, with one picker for the chosen kind",
    async (url, offered) => {
      await openPage(url)
      await until(() => {
        expect(kinds()).toEqual(offered)
      })
      expect(pressed()).toBe("Scan")
      expect(pickers()).toEqual([labelled("Scan Number")])
      await switchTo("Clone")
      expect(pressed()).toBe("Clone")
      expect(pickers()).toEqual([labelled("Clone of")])
    }
  )

  it.each([
    ["/reconstructions/new?scan=990001", "Scan", "Scan Number", "SN990001", []],
    [
      "/reconstructions/new?scan=990001&reconstruction=1",
      "Clone",
      "Clone of",
      "R1",
      ["Scan Number: SN990001"],
    ],
    ["/indexing/new?scan=990001", "Scan", "Scan Number", "SN990001", []],
    [
      "/indexing/new?scan=990001&reconstruction=1",
      "Reconstruction",
      "Reconstruction ID",
      "R1",
      ["Scan Number: SN990001"],
    ],
    [
      "/indexing/new?indexing=1",
      "Clone",
      "Clone of",
      "I1",
      ["Scan Number: SN990001"],
    ],
  ])(
    "%s opens %s: %s %s, the IDs it sets read-only %j",
    async (url, kind, label, value, ids) => {
      await openPage(url)
      await until(() => {
        expect(labelled(label)?.value).toBe(value)
      })
      expect(pressed()).toBe(kind)
      expect(pickers()).toHaveLength(1)
      expect(sourceIds()).toEqual(ids)
      if (ids.length > 0) {
        expect(byRole("link", "SN990001")[0]?.getAttribute("href")).toBe(
          "/scans/990001"
        )
      }
    }
  )
})

describe("the LaueGo form's sources", () => {
  it("from a clone, Reconstruction clears its ranges and keeps its parameters; a reconstruction gives its scan.h5 with the depth range blank (every depth)", async () => {
    const { prefills } = await openPage("/indexing/new?indexing=1")
    await until(() => {
      expect(field("depth_range")?.value).toBe("1-5")
    })
    await switchTo("Reconstruction")
    await until(() => {
      expect(field("depth_range")?.value).toBe("")
    })
    expect(field("scan_points")?.value).toBe("")
    expect(field("threshold_ratio")?.value).toBe("4")
    // Not data fields: the clone's crystal and output name stay.
    expect(field("crystal_file")?.value).toBe("/data/Si.xml")
    expect(field("output_xml")?.value).toBe("i1.xml")

    await pick("Reconstruction ID", "1")
    await until(() => {
      expect(field("input_path")?.value).toBe(SCAN_H5)
    })
    expect(prefills).toEqual(["?indexing=1", "?reconstruction=1"])
    expect(field("depth_range")?.value).toBe("")
    expect(field("scan_points")?.value).toBe("1")
    expect(field("threshold_ratio")?.value).toBe("4")
    expect(field("crystal_file")?.value).toBe("/data/Si.xml")
    expect(labelled("Reconstruction ID")?.value).toBe("R1")
    expect(sourceIds()).toEqual(["Scan Number: SN990001"])
  })

  it("a later link of another kind clears the shown source before its prefill applies: a refused one leaves none of R1", async () => {
    const { router, validated } = await openPage(
      "/indexing/new?reconstruction=1&scan=990001"
    )
    await until(() => {
      expect(field("input_path")?.value).toBe(SCAN_H5)
    })
    await act(() =>
      router.navigate({ to: "/indexing/new", search: { scan: "990003" } })
    )
    await until(() => {
      expect(document.body.textContent).toContain("No such source")
    })
    expect(pressed()).toBe("Scan")
    await until(() => {
      expect(validated.at(-1)).toMatchObject({
        scan_number: null,
        reconstruction_id: null,
        scan_points: "",
      })
    })
    expect(validated.at(-1)?.input_path).not.toBe(SCAN_H5)
    expect(sourceIds()).toEqual([])
  })

  it("a picked source whose prefill is refused leaves none of the previous one", async () => {
    const { validated } = await openPage(
      "/indexing/new?reconstruction=1&scan=990001"
    )
    await until(() => {
      expect(field("input_path")?.value).toBe(SCAN_H5)
    })
    await pick("Reconstruction ID", "2")
    await until(() => {
      expect(document.body.textContent).toContain("No such source")
    })
    await until(() => {
      expect(validated.at(-1)).toMatchObject({
        scan_number: null,
        reconstruction_id: null,
        scan_points: "",
      })
    })
    expect(validated.at(-1)?.input_path).not.toBe(SCAN_H5)
    expect(sourceIds()).toEqual([])
    expect(field("threshold_ratio")?.value).toBe("")
  })

  it("clones an indexing run: its parameters copied, author and notes blank, its scan read-only", async () => {
    const { prefills } = await openPage("/indexing/new")
    await until(() => {
      expect(kinds()).toHaveLength(3)
    })
    await switchTo("Clone")
    await pick("Clone of", "1")
    await until(() => {
      expect(field("input_path")?.value).toBe("/data/frames")
    })
    expect(prefills).toEqual(["?indexing=1"])
    expect(labelled("Clone of")?.value).toBe("I1")
    expect(field("threshold_ratio")?.value).toBe("4")
    expect(field("scan_points")?.value).toBe("1-4")
    expect(field("author")?.value).toBe("")
    expect(field("notes")?.value).toBe("")
    // I1 indexes its scan directly: no reconstruction.
    expect(sourceIds()).toEqual(["Scan Number: SN990001"])
  })
})
