import { RouterProvider, createMemoryHistory } from "@tanstack/react-router"
import { act } from "react"
import { toast } from "sonner"
import {
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest"

import {
  getListReconstructionRunsQueryKey,
  type Health,
  type IndexingRunSummary,
  type ReconstructionRunSummary,
  type ScanCatalogUpdate,
  type ScanDetail,
} from "@/api"
import { Providers } from "@/app/Providers"
import { createQueryClient } from "@/app/query-client"
import { createAppRouter } from "@/app/router"
import { answerFiles, type FakeFiles } from "@/test/files-api"
import { rowCheckbox, stubGridLayout } from "@/test/grid"
import {
  byRole,
  cleanup,
  click,
  enterText,
  render,
  stubMatchMedia,
} from "@/test/render"
import { makeCatalog, makeScanDetail } from "@/test/scans"

// The scan page on the app's real routes (its loader, its `tab` search, its title) with fetch
// answering the API: the scan, its catalog and notes, its run lists and the files API.

const HEALTH: Health = {
  db: "ok",
  worker: "running",
  version: "2.0.0.dev0",
}

const FILES: FakeFiles = {
  roots: { data: "/net/data" },
  tree: {
    "/net/data": { "2026": "/" },
    "/net/data/2026": { scan_42: "/", scan_43: "/" },
    "/net/data/2026/scan_42": { "Ni_1.h5": 10 },
    "/net/data/2026/scan_43": { "Ni_1.h5": 10 },
  },
}

function indexingRun(
  id: number,
  reconstructionId: number | null
): IndexingRunSummary {
  return {
    id,
    method: "lauego",
    scan_number: 42,
    sample_name: "Si",
    reconstruction_id: reconstructionId,
    n_points: 21,
    n_depths: 81,
    box_size: 5,
    crystal_file: "Ni.xml",
    author: "adoe",
    notes: null,
    created_at: "2026-01-05T08:00:00Z",
    status: "finished",
    n_inputs: 21,
    n_succeeded: 21,
    n_failed: 0,
    n_not_run: 0,
    n_processed: 21,
  }
}

function reconstructionRun(id: number): ReconstructionRunSummary {
  return {
    id,
    method: "wire",
    scan_number: 42,
    sample_name: "Si",
    n_points: 21,
    percent_brightest: 5,
    depth_start: -50,
    depth_end: 50,
    author: "adoe",
    notes: null,
    created_at: "2026-01-05T08:00:00Z",
    start_time: "2026-01-05T08:01:00Z",
    finish_time: "2026-01-05T08:30:00Z",
    status: "finished",
    n_inputs: 21,
    n_succeeded: 21,
    n_failed: 0,
    n_not_run: 0,
    n_processed: 21,
  }
}

interface Request {
  method: string
  url: URL
  body: unknown
}

interface Api {
  scan: ScanDetail | undefined
  indexations: IndexingRunSummary[]
  reconstructions: ReconstructionRunSummary[]
  /** Answers PUT .../catalog (default: saves the body). */
  saveCatalog?: (body: ScanCatalogUpdate) => Response
  /** GET of the scan fails (403) while set. */
  scanFails?: boolean
  requests: Request[]
}

function problem(status: number, body: Record<string, unknown>): Response {
  return Response.json(
    { type: "about:blank", status, ...body },
    { status, headers: { "content-type": "application/problem+json" } }
  )
}

/** The API's 409 for a save over a catalog changed since it was loaded (scans.service). */
const CATALOG_CHANGED = {
  type: "urn:laueportal:problem:catalog-changed",
  title: "Catalog changed",
  detail:
    "Scan 42's catalog changed since it was loaded; reload it and apply the changes again.",
}

let versions = 1
/** A stored catalog's next version, as the API makes one for every change. */
const nextVersion = () => {
  versions += 1
  return `W/"catalog-${String(versions)}"`
}

function answer(api: Api, request: Request): Response {
  const { method, url, body } = request
  const files = answerFiles(FILES, url)
  if (files) return files
  if (url.pathname === "/api/v1/health") return Response.json(HEALTH)
  const scanPath = /^\/api\/v1\/scans\/(\d+)(\/catalog|\/notes)?$/.exec(
    url.pathname
  )
  if (scanPath) {
    const scan = api.scan
    if (!scan || scan.scan_number !== Number(scanPath[1])) {
      return problem(404, { title: "Scan not found", detail: "No such scan" })
    }
    if (method === "PUT" && scanPath[2] === "/catalog") {
      const update = body as ScanCatalogUpdate
      if (api.saveCatalog) return api.saveCatalog(update)
      if (update.version !== (scan.catalog?.version ?? null)) {
        return problem(409, CATALOG_CHANGED)
      }
      const catalog = { ...update, version: nextVersion() }
      api.scan = { ...scan, catalog }
      return Response.json(catalog)
    }
    if (method === "POST" && scanPath[2] === "/notes") {
      const { text } = body as { text: string }
      const old = scan.catalog ?? makeCatalog()
      const notes = old.notes ? `${old.notes}\n${text}` : text
      const catalog = { ...old, notes, version: nextVersion() }
      api.scan = { ...scan, catalog }
      return Response.json(catalog)
    }
    if (api.scanFails) {
      return problem(403, { title: "Forbidden", detail: "No access to scans" })
    }
    return Response.json(scan)
  }
  const now = "2026-10-19T12:00:00Z"
  if (url.pathname === "/api/v1/indexing-runs") {
    return Response.json({
      rows: api.indexations,
      total: api.indexations.length,
      as_of: now,
    })
  }
  if (url.pathname === "/api/v1/reconstruction-runs") {
    const offset = Number(url.searchParams.get("offset") ?? 0)
    const limit = Number(url.searchParams.get("limit") ?? 100)
    return Response.json({
      rows: api.reconstructions.slice(offset, offset + limit),
      total: api.reconstructions.length,
      as_of: now,
    })
  }
  return problem(404, { title: "Not Found" })
}

async function openScan(
  url: string,
  options: Partial<
    Pick<Api, "scan" | "indexations" | "reconstructions" | "saveCatalog">
  > = {}
) {
  stubMatchMedia()
  stubGridLayout()
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined)
  const api: Api = {
    scan: "scan" in options ? options.scan : makeScanDetail(),
    indexations: options.indexations ?? [],
    reconstructions: options.reconstructions ?? [],
    saveCatalog: options.saveCatalog,
    requests: [],
  }
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const request: Request = {
        method: init?.method ?? "GET",
        url: new URL(
          input instanceof globalThis.Request ? input.url : input.toString(),
          "http://localhost"
        ),
        body:
          typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
      }
      api.requests.push(request)
      return Promise.resolve(answer(api, request))
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
  return { api, router, history, queryClient }
}

const sent = (api: Api, method: string) =>
  api.requests.filter((request) => request.method === method)

// The first page opened in this file pays for loading the route's lazy component (code splitting)
// with the run cards' grid (AG Grid) and for the first render: about 2 s here, more under load, which
// once made the first test time out at 5 s. Open the page once before the tests, so each test's time
// is its own.
beforeAll(async () => {
  await openScan("/scans/42")
  await vi.waitFor(() => {
    expect(document.querySelector("h1")?.textContent).toBe("Scan 42")
  })
  cleanup()
}, 30_000)

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn()
})

afterEach(() => {
  act(() => {
    toast.dismiss()
  })
  cleanup()
})

function panel(name: string) {
  const tab = byRole("tab", name)[0]
  const id = tab?.getAttribute("aria-controls")
  const element = id ? document.getElementById(id) : null
  if (!element) throw new Error(`no panel for ${name}`)
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

async function toastTitle() {
  await vi.waitFor(() => {
    expect(document.querySelector("[data-sonner-toast]")).not.toBeNull()
  })
  return document.querySelector("[data-sonner-toast] [data-title]")?.textContent
}

/** The picker's list shows this folder, read (not the last one, while it loads). */
async function listed(label: string) {
  await vi.waitFor(() => {
    const list = byRole("listbox")[0]
    expect(list?.getAttribute("aria-label")).toBe(label)
    expect(list?.getAttribute("aria-busy")).toBe("false")
  })
}

const folderInput = () =>
  panel("Catalog").querySelector<HTMLInputElement>('input[name="filefolder"]')

describe("the scan page", () => {
  it("shows Scan Info: the scan's fields, its dimensions and an empty note", async () => {
    await openScan("/scans/42")
    await vi.waitFor(() => {
      expect(document.title).toBe("Scan 42 · 3DMN Portal")
    })
    expect(byRole("heading", "Scan 42")).toHaveLength(1)
    const info = panel("Scan Info")
    expect(definitions(info)).toMatchObject({
      User: "adoe",
      Dimensions: "2D",
      "Scan Type": "depth + area",
      Aperture: "Wire",
      Sample: "Ni film",
      "Data Folder": "/net/data/2026/scan_42",
    })
    const rows = Array.from(info.querySelectorAll("tr")).map((row) =>
      Array.from(row.children).map((cell) => cell.textContent)
    )
    expect(rows).toEqual([
      ["Motor Group", "Start", "End", "Step"],
      ["Dim 2 · Completed 80/81"],
      ["Depth", "-50", "50", "1.25"],
      ["Dim 1 · Completed 21/21"],
      ["Sample", "-10", "10", "1"],
      ["Sample", "0,5"],
    ])
    expect(info.querySelector("textarea")?.value).toBe("")
  })

  it("keeps its tab in the URL, replacing the history entry", async () => {
    const { router, history } = await openScan("/scans/42?tab=metadata")
    await vi.waitFor(() => {
      expect(byRole("tab", "Metadata")[0]?.getAttribute("aria-selected")).toBe(
        "true"
      )
    })
    expect(definitions(panel("Metadata"))).toMatchObject({
      "ID Gap": "11.5 mm",
      "ID Taper": "0.25 mrad",
    })

    await click(byRole("tab", "Catalog")[0])
    await vi.waitFor(() => {
      expect(router.state.location.href).toBe("/scans/42?tab=catalog")
    })
    expect(byRole("tab", "Catalog")[0]?.getAttribute("aria-selected")).toBe(
      "true"
    )
    await click(byRole("tab", "Scan Info")[0])
    await vi.waitFor(() => {
      expect(router.state.location.href).toBe("/scans/42")
    })
    expect(history.length).toBe(1)
  })

  it("shows Scan Info for a tab it does not know", async () => {
    await openScan("/scans/42?tab=plot")
    await vi.waitFor(() => {
      expect(byRole("tab", "Scan Info")[0]?.getAttribute("aria-selected")).toBe(
        "true"
      )
    })
  })

  it("adds a note to the catalog, then empties its box", async () => {
    const { api } = await openScan("/scans/42")
    const note = panel("Scan Info").querySelector("textarea")
    const add = byRole("button", "Add to DB")[0]
    expect(add?.hasAttribute("disabled")).toBe(true)

    await enterText(note, "submit indexing")
    await click(add)

    expect(await toastTitle()).toBe("Note added to scan 42")
    expect(sent(api, "POST").map((request) => request.body)).toEqual([
      { text: "submit indexing" },
    ])
    expect(note?.value).toBe("")
    expect(document.activeElement).toBe(note)
    // The Catalog tab shows the notes with the note appended.
    await vi.waitFor(() => {
      expect(panel("Catalog").querySelector("textarea")?.value).toBe(
        "first note\nsubmit indexing"
      )
    })
  })

  it("asks before leaving with a note not added; a tab is not leaving", async () => {
    const { router } = await openScan("/scans/42")
    // (The browser's own prompt on closing or reloading the tab: e2e/scan-detail.spec.ts.)
    await enterText(panel("Scan Info").querySelector("textarea"), "draft")

    await click(byRole("tab", "Catalog")[0])
    await vi.waitFor(() => {
      expect(router.state.location.href).toBe("/scans/42?tab=catalog")
    })
    expect(byRole("alertdialog")).toHaveLength(0)

    // Not awaited: the navigation waits for the dialog.
    const leave = () => {
      act(() => {
        void router.navigate({ to: "/scans" })
      })
    }
    leave()
    await vi.waitFor(() => {
      expect(byRole("alertdialog")[0]?.textContent).toContain(
        "Leave without saving?"
      )
    })
    await click(byRole("button", "Stay")[0])
    await vi.waitFor(() => {
      expect(byRole("alertdialog")).toHaveLength(0)
    })
    expect(router.state.location.pathname).toBe("/scans/42")
    expect(panel("Scan Info").querySelector("textarea")?.value).toBe("draft")

    leave()
    await vi.waitFor(() => {
      expect(byRole("button", "Leave")).toHaveLength(1)
    })
    await click(byRole("button", "Leave")[0])
    await vi.waitFor(() => {
      expect(router.state.location.pathname).toBe("/scans")
    })
  })

  it("prompts to fill in the catalog when the scan has none", async () => {
    const { router } = await openScan("/scans/42", {
      scan: makeScanDetail({ catalog: null }),
    })
    const info = panel("Scan Info")
    expect(info.textContent).toContain("This scan has no catalog entry yet")
    expect(info.querySelector("textarea")).toBeNull()

    await click(byRole("button", "Fill in the catalog")[0])
    await vi.waitFor(() => {
      expect(router.state.location.href).toBe("/scans/42?tab=catalog")
    })
    expect(panel("Catalog").textContent).toContain("No catalog entry yet.")
  })

  it("keeps a refresh's error over the scan, and gives the heading the keyboard once Retry loads", async () => {
    const { api } = await openScan("/scans/42")
    api.scanFails = true
    // Adding a note reads the scan again: that read fails.
    await enterText(panel("Scan Info").querySelector("textarea"), "a note")
    await click(byRole("button", "Add to DB")[0])
    await vi.waitFor(() => {
      expect(byRole("alert")[0]?.textContent).toContain("No access to scans")
    })
    expect(byRole("alert")[0]?.textContent).toContain(
      "Showing the last data loaded."
    )
    expect(byRole("tab", "Scan Info")).toHaveLength(1)

    api.scanFails = false
    const [retry] = byRole("button", "Retry")
    act(() => {
      retry?.focus()
    })
    await click(retry)
    await vi.waitFor(() => {
      expect(byRole("button", "Retry")).toHaveLength(0)
    })
    await vi.waitFor(() => {
      expect(document.activeElement).toBe(byRole("heading", "Scan 42")[0])
    })
  })

  it("gives the Catalog tab's first field the keyboard from Fill in the catalog", async () => {
    await openScan("/scans/42", { scan: makeScanDetail({ catalog: null }) })
    const fill = byRole("button", "Fill in the catalog")[0]
    act(() => {
      fill?.focus()
    })
    await click(fill)
    await vi.waitFor(() => {
      expect(document.activeElement?.id).toMatch(/-aperture$/)
    })
    expect(document.activeElement?.closest('[role="tabpanel"]')).toBe(
      panel("Catalog")
    )
  })

  it("is not found for a scan the database does not have", async () => {
    await openScan("/scans/7")
    await vi.waitFor(() => {
      expect(byRole("heading", "Scan 7 not found")).toHaveLength(1)
    })
    expect(document.title).toBe("Page not found · 3DMN Portal")
    expect(byRole("link", "Go to the scans")[0]?.getAttribute("href")).toBe(
      "/scans"
    )
    expect(byRole("link", "Import Scans")[0]?.getAttribute("href")).toBe(
      "/scans/import"
    )
    expect(document.body.textContent).toContain(
      "There is no scan 7 in the database."
    )
    expect(document.body.textContent).not.toContain("imported yet")
  })
})

describe("the Catalog tab", () => {
  it("needs an aperture and an absolute data folder before it saves", async () => {
    const { api } = await openScan("/scans/42?tab=catalog", {
      scan: makeScanDetail({
        catalog: makeCatalog({ aperture: "synthetic", filefolder: "" }),
      }),
    })
    const catalog = panel("Catalog")
    expect(catalog.textContent).toContain(
      "Stored as “synthetic”, which is none of these: choose one to save."
    )

    await click(byRole("button", "Save")[0])
    await vi.waitFor(() => {
      expect(
        Array.from(catalog.querySelectorAll('[data-slot="field-error"]')).map(
          (error) => error.textContent
        )
      ).toEqual(["Choose an aperture.", "Enter the data folder."])
    })

    await enterText(folderInput(), "data/scan_42")
    await vi.waitFor(() => {
      expect(catalog.textContent).toContain(
        "Enter an absolute path (it starts with /)."
      )
    })
    expect(sent(api, "PUT")).toEqual([])
  })

  it("shows a migrated catalog's None as None and saves it as none", async () => {
    const { api } = await openScan("/scans/42?tab=catalog", {
      scan: makeScanDetail({ catalog: makeCatalog({ aperture: "None" }) }),
    })
    const aperture = panel("Catalog").querySelector(
      '[data-slot="select-value"]'
    )
    expect(aperture?.textContent).toBe("None")

    await click(byRole("button", "Save")[0])
    expect(await toastTitle()).toBe("Catalog saved for scan 42")
    expect(sent(api, "PUT").map((request) => request.body)).toEqual([
      {
        version: 'W/"catalog-1"',
        aperture: "none",
        sample_name: "Ni film",
        filefolder: "/net/data/2026/scan_42",
        filename_prefix: ["Ni_%d.h5", "Si_%d.h5"],
        notes: "first note",
      },
    ])
  })

  it("shows the server's reason for a folder outside the roots at the Files Path", async () => {
    const reason = "/tmp/scans is outside the allowed roots (data)."
    await openScan("/scans/42?tab=catalog", {
      saveCatalog: () =>
        problem(400, {
          title: "Path not allowed",
          detail: reason,
          errors: [
            {
              loc: ["filefolder"],
              message: reason,
              code: "path_not_allowed",
              severity: "error",
            },
          ],
        }),
    })
    await enterText(folderInput(), "/tmp/scans")
    await click(byRole("button", "Save")[0])

    await vi.waitFor(() => {
      expect(folderInput()?.getAttribute("aria-invalid")).toBe("true")
    })
    const error = panel("Catalog").querySelector('[data-slot="field-error"]')
    expect(error?.textContent).toBe(reason)
    expect(folderInput()?.getAttribute("aria-describedby")).toContain(error?.id)
    expect(document.activeElement).toBe(folderInput())
    // The field says it; no toast repeats it.
    expect(document.querySelector("[data-sonner-toast]")).toBeNull()
  })

  it("saves the whole catalog, the folder picked with Browse", async () => {
    const { api } = await openScan("/scans/42?tab=catalog")
    const catalog = panel("Catalog")
    await enterText(
      catalog.querySelector('input[name="sample_name"]'),
      "  Ni film B "
    )
    await click(byRole("button", "Remove filename prefix 1")[0])
    // The prefix in its place takes the keyboard.
    await vi.waitFor(() => {
      expect(document.activeElement?.getAttribute("aria-label")).toBe(
        "Filename prefix 1"
      )
    })
    await click(byRole("button", "Add prefix")[0])
    await enterText(
      byRole("textbox", "Filename prefix 2")[0] ??
        document.querySelector('[aria-label="Filename prefix 2"]'),
      "Ni_%d.h5"
    )

    await click(byRole("button", "Browse")[0])
    await listed("Contents of /net/data/2026/scan_42")
    await click(byRole("button", "2026")[0])
    await listed("Contents of /net/data/2026")
    const scan43 = byRole("option").find((option) =>
      option.textContent.startsWith("scan_43")
    )
    await click(scan43)
    await listed("Contents of /net/data/2026/scan_43")
    await click(byRole("button", "Select folder")[0])
    await vi.waitFor(() => {
      expect(folderInput()?.value).toBe("/net/data/2026/scan_43")
    })

    await click(byRole("button", "Save")[0])
    expect(await toastTitle()).toBe("Catalog saved for scan 42")
    expect(sent(api, "PUT").map((request) => request.body)).toEqual([
      {
        version: 'W/"catalog-1"',
        aperture: "wire",
        sample_name: "Ni film B",
        filefolder: "/net/data/2026/scan_43",
        filename_prefix: ["Si_%d.h5", "Ni_%d.h5"],
        notes: "first note",
      },
    ])
    // Saved: nothing left to discard, and Scan Info shows the new folder.
    expect(byRole("button", "Discard changes")).toHaveLength(0)
    await vi.waitFor(() => {
      expect(definitions(panel("Scan Info"))["Data Folder"]).toBe(
        "/net/data/2026/scan_43"
      )
    })
  })
})

describe("the Catalog tab's edits", () => {
  it("stay while a note is added, which the catalog then saves with them", async () => {
    const { api } = await openScan("/scans/42?tab=catalog")
    const sample = () =>
      panel("Catalog").querySelector<HTMLInputElement>(
        'input[name="sample_name"]'
      )
    await enterText(sample(), "Ni film B")

    await click(byRole("tab", "Scan Info")[0])
    await enterText(panel("Scan Info").querySelector("textarea"), "second")
    await click(byRole("button", "Add to DB")[0])
    expect(await toastTitle()).toBe("Note added to scan 42")

    await click(byRole("tab", "Catalog")[0])
    await vi.waitFor(() => {
      expect(panel("Catalog").querySelector("textarea")?.value).toBe(
        "first note\nsecond"
      )
    })
    expect(sample()?.value).toBe("Ni film B")
    const appended = api.scan?.catalog?.version
    act(() => {
      toast.dismiss()
    })
    await click(byRole("button", "Save")[0])
    await vi.waitFor(() => {
      expect(sent(api, "PUT")).toHaveLength(1)
    })
    // The edits keep the note, so they carry the appended catalog's version: the save goes through.
    expect(sent(api, "PUT")[0]?.body).toMatchObject({
      version: appended,
      sample_name: "Ni film B",
      notes: "first note\nsecond",
    })
    expect(await toastTitle()).toBe("Catalog saved for scan 42")
  })

  it("explain a save over a catalog changed elsewhere, and Reload shows it", async () => {
    const { api } = await openScan("/scans/42?tab=catalog")
    const sample = () =>
      panel("Catalog").querySelector<HTMLInputElement>(
        'input[name="sample_name"]'
      )
    await enterText(sample(), "Ni film B")
    // Another tab appends a note: this page has not read it.
    const scan = makeScanDetail()
    api.scan = {
      ...scan,
      catalog: makeCatalog({ notes: "first note\nfrom B", version: 'W/"b"' }),
    }

    await click(byRole("button", "Save")[0])
    const alert = await vi.waitFor(() => {
      const found = panel("Catalog").querySelector('[role="alert"]')
      expect(found?.textContent).toContain(
        "Catalog changed since it was loaded"
      )
      return found
    })
    expect(alert?.textContent).toContain(
      "Reload it, then apply your changes again."
    )
    expect(document.querySelector("[data-sonner-toast]")).toBeNull()
    // Nothing was overwritten, and the edits stay on screen until Reload.
    expect(api.scan.catalog?.notes).toBe("first note\nfrom B")
    expect(sample()?.value).toBe("Ni film B")

    await click(byRole("button", "Reload")[0])
    await vi.waitFor(() => {
      expect(panel("Catalog").querySelector("textarea")?.value).toBe(
        "first note\nfrom B"
      )
    })
    expect(sample()?.value).toBe("Ni film")
    expect(panel("Catalog").querySelector('[role="alert"]')).toBeNull()
    await enterText(sample(), "Ni film B")
    await click(byRole("button", "Save")[0])
    expect(await toastTitle()).toBe("Catalog saved for scan 42")
    expect(sent(api, "PUT").map((request) => request.body)).toMatchObject([
      { version: 'W/"catalog-1"' },
      { version: 'W/"b"', notes: "first note\nfrom B" },
    ])
  })
})

describe("the Catalog tab's version", () => {
  it("does not move on with a note when the catalog changed elsewhere: the save is refused", async () => {
    const { api } = await openScan("/scans/42?tab=catalog")
    const sample = () =>
      panel("Catalog").querySelector<HTMLInputElement>(
        'input[name="sample_name"]'
      )
    await enterText(sample(), "Ni film B")
    // Another client changes the sample: this page has not read it.
    const scan = makeScanDetail()
    api.scan = {
      ...scan,
      catalog: makeCatalog({ sample_name: "other", version: 'W/"b"' }),
    }
    // The note's answer is that catalog with the note, not the one the edits started from.
    await click(byRole("tab", "Scan Info")[0])
    await enterText(panel("Scan Info").querySelector("textarea"), "second")
    await click(byRole("button", "Add to DB")[0])
    expect(await toastTitle()).toBe("Note added to scan 42")
    act(() => {
      toast.dismiss()
    })

    await click(byRole("tab", "Catalog")[0])
    await click(byRole("button", "Save")[0])
    await vi.waitFor(() => {
      expect(
        panel("Catalog").querySelector('[role="alert"]')?.textContent
      ).toContain("Catalog changed since it was loaded")
    })
    expect(sent(api, "PUT").map((request) => request.body)).toMatchObject([
      { version: 'W/"catalog-1"', sample_name: "Ni film B" },
    ])
    expect(api.scan.catalog).toMatchObject({
      sample_name: "other",
      notes: "first note\nsecond",
    })
  })

  it("keeps the edits and the alert when Reload cannot read the catalog", async () => {
    const { api } = await openScan("/scans/42?tab=catalog")
    const sample = () =>
      panel("Catalog").querySelector<HTMLInputElement>(
        'input[name="sample_name"]'
      )
    await enterText(sample(), "Ni film B")
    api.scan = {
      ...makeScanDetail(),
      catalog: makeCatalog({ version: 'W/"b"' }),
    }
    await click(byRole("button", "Save")[0])
    await vi.waitFor(() => {
      expect(byRole("button", "Reload")).toHaveLength(1)
    })

    api.scanFails = true
    await click(byRole("button", "Reload")[0])
    expect(await toastTitle()).toBe("The catalog was not reloaded")
    expect(sample()?.value).toBe("Ni film B")
    expect(
      panel("Catalog").querySelector('[role="alert"]')?.textContent
    ).toContain("Catalog changed since it was loaded")
  })
})

describe("a note added while Notes has edits", () => {
  it("is appended to the edits, said so, and saved with them", async () => {
    const { api } = await openScan("/scans/42?tab=catalog")
    const notes = () => panel("Catalog").querySelector("textarea")
    await enterText(notes(), "first note, edited")

    await click(byRole("tab", "Scan Info")[0])
    await enterText(panel("Scan Info").querySelector("textarea"), "second")
    await click(byRole("button", "Add to DB")[0])
    expect(await toastTitle()).toBe("Note added to scan 42")
    expect(api.scan?.catalog?.notes).toBe("first note\nsecond")

    await click(byRole("tab", "Catalog")[0])
    await vi.waitFor(() => {
      expect(notes()?.value).toBe("first note, edited\nsecond")
    })
    const notice =
      "The note added on Scan Info is appended here, after your edits: Save keeps it."
    expect(panel("Catalog").textContent).toContain(notice)
    act(() => {
      toast.dismiss()
    })
    await click(byRole("button", "Save")[0])
    await vi.waitFor(() => {
      expect(sent(api, "PUT")).toHaveLength(1)
    })
    expect(sent(api, "PUT")[0]?.body).toMatchObject({
      notes: "first note, edited\nsecond",
    })
    await vi.waitFor(() => {
      expect(panel("Catalog").textContent).not.toContain(notice)
    })
    expect(notes()?.value).toBe("first note, edited\nsecond")
  })

  it("leaves unedited Notes to the stored notes, without a notice", async () => {
    await openScan("/scans/42")
    await enterText(panel("Scan Info").querySelector("textarea"), "second")
    await click(byRole("button", "Add to DB")[0])
    expect(await toastTitle()).toBe("Note added to scan 42")
    await vi.waitFor(() => {
      expect(panel("Catalog").querySelector("textarea")?.value).toBe(
        "first note\nsecond"
      )
    })
    expect(panel("Catalog").textContent).not.toContain("appended here")
  })
})

describe("the run cards", () => {
  it("show Reconstructions for a depth scan, and ask for this scan's runs", async () => {
    const { api } = await openScan("/scans/42")
    await vi.waitFor(() => {
      expect(byRole("region").map((region) => region.textContent)).toEqual([
        expect.stringContaining("Reconstructions"),
        expect.stringContaining("Peak Indexing"),
      ])
    })
    await vi.waitFor(() => {
      const lists = api.requests
        .filter((request) => request.url.pathname.endsWith("-runs"))
        .map(
          (request) =>
            `${request.url.pathname}?scan_id=${String(request.url.searchParams.get("scan_id"))}`
        )
      expect(new Set(lists)).toEqual(
        new Set([
          "/api/v1/reconstruction-runs?scan_id=42",
          "/api/v1/indexing-runs?scan_id=42",
        ])
      )
    })
    // A depth scan needs no count.
    expect(
      api.requests.some(
        (request) => request.url.searchParams.get("limit") === "1"
      )
    ).toBe(false)
  })

  it("show Reconstructions for a scan of another type that has reconstructions", async () => {
    const { api } = await openScan("/scans/42", {
      scan: makeScanDetail({ technique: "area" }),
      reconstructions: [reconstructionRun(2), reconstructionRun(1)],
    })
    await vi.waitFor(() => {
      expect(byRole("heading", "Reconstructions")).toHaveLength(1)
    })
    const card = byRole("region").find((region) =>
      region.textContent.includes("Reconstructions")
    )
    await vi.waitFor(() => {
      expect(card?.querySelectorAll('[row-id="2"]').length).toBeGreaterThan(0)
    })
    // Counted by asking for one run.
    const counts = api.requests.filter(
      (request) =>
        request.url.pathname === "/api/v1/reconstruction-runs" &&
        request.url.searchParams.get("limit") === "1"
    )
    expect(
      counts.map((request) => request.url.searchParams.get("scan_id"))
    ).toEqual(["42"])
  })

  it("show no Reconstructions for a scan of another type without reconstructions", async () => {
    const { queryClient } = await openScan("/scans/42", {
      scan: makeScanDetail({ technique: "area" }),
    })
    // Once the count (one run asked for) has answered "none".
    await vi.waitFor(() => {
      const count = queryClient.getQueryState(
        getListReconstructionRunsQueryKey({ scan_id: 42, limit: 1 })
      )
      expect(count?.status).toBe("success")
    })
    expect(byRole("heading", "Peak Indexing")).toHaveLength(1)
    expect(byRole("heading", "Reconstructions")).toHaveLength(0)
  })

  it("link New Recon and New Index to this scan, or to it with the one run selected", async () => {
    await openScan("/scans/42", {
      indexations: [indexingRun(9, 3), indexingRun(8, null)],
    })
    const indexing = () => {
      const region = byRole("region").find((element) =>
        element.textContent.includes("Peak Indexing")
      )
      if (!region) throw new Error("no Peak Indexing card")
      return region
    }
    const hrefs = () =>
      byRole("link", /^New (Recon|Index)$/, indexing()).map((link) =>
        link.getAttribute("href")
      )
    expect(hrefs()).toEqual([
      "/reconstructions/new?scan=42",
      "/indexing/new?scan=42",
    ])

    await vi.waitFor(() => {
      expect(
        indexing().querySelectorAll('[row-id="9"]').length
      ).toBeGreaterThan(0)
    })
    // One checked row: checking I9 unchecks I8.
    await click(rowCheckbox(indexing(), "8"))
    await click(rowCheckbox(indexing(), "9"))
    await vi.waitFor(() => {
      expect(hrefs()).toEqual([
        "/reconstructions/new?scan=42&reconstruction=3",
        "/indexing/new?scan=42&reconstruction=3&indexing=9",
      ])
    })
    expect(rowCheckbox(indexing(), "8").checked).toBe(false)
    expect(indexing().textContent).toContain("1 selected")
  })
})
