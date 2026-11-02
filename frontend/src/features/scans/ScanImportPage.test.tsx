import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { createMemoryHistory, type RouterHistory } from "@tanstack/react-router"
import { act } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { ScanImportReport, ScanImportRequest, ScanLogIndex } from "@/api"
import { rowCheckbox, rowText, stubGridLayout } from "@/test/grid"
import { answerFiles, type FakeFiles } from "@/test/files-api"
import { urlOf } from "@/test/programs"
import {
  byRole,
  cleanup,
  click,
  enterText,
  renderWithRouter,
} from "@/test/render"
import { ScanImportPage } from "./ScanImportPage"

// The import page with AG Grid itself (src/test/grid.ts) and fetch answering the index and the import.

const TOKEN = "0123456789abcdef0123456789abcdef"

function logRow(
  scan_index: number,
  scan_number: number,
  status: ScanLogIndex["rows"][number]["status"]
): ScanLogIndex["rows"][number] {
  return {
    scan_index,
    scan_number,
    status,
    time: `2022-04-2${String(scan_index)}T10:00:00Z`,
    user_name: "user",
    energy: 20,
    energy_unit: "keV",
    sample_xyz: "1 2 3",
    n_dimensions: 2,
  }
}

const INDEX: ScanLogIndex = {
  token: TOKEN,
  rows: [
    logRow(0, 100, "new"),
    logRow(1, 101, "exists"),
    logRow(2, 102, "new"),
    logRow(3, 103, "new"),
  ],
}

function problem(status: number, title: string, detail: string): Response {
  return Response.json(
    { type: "about:blank", title, status, detail },
    { status, headers: { "content-type": "application/problem+json" } }
  )
}

// The roots, with the scan-log settings the page must use (not the backend's defaults).
const FILES: FakeFiles = {
  roots: { data: "/data" },
  tree: {
    "/data": { logs: "/" },
    "/data/logs": { "2026": "/" },
    "/data/logs/2026": { "log.xml": 2048 },
  },
  scanLogs: {
    max_bytes: 1024 * 1024,
    extensions: [".xml", ".log"],
    start_folder: "/data/logs/2026",
  },
}

/** fetch for the files API, the index and the import: what was asked and sent, and the answers. */
function stubApi({
  answerImport = () => new Response(null, { status: 500 }),
  answerIndex = () => Response.json(INDEX),
}: {
  answerImport?: (body: ScanImportRequest) => Response
  answerIndex?: (count: number) => Response
}) {
  const sent = {
    indexes: 0,
    imports: [] as ScanImportRequest[],
    browsed: [] as (string | null)[],
  }
  vi.stubGlobal("fetch", (url: RequestInfo | URL, init?: RequestInit) => {
    const { pathname, searchParams } = urlOf(url)
    if (pathname === "/api/v1/files/browse") {
      sent.browsed.push(searchParams.get("path"))
    }
    const files = answerFiles(FILES, url)
    if (files) return Promise.resolve(files)
    if (pathname === "/api/v1/scans/import/index") {
      sent.indexes += 1
      return Promise.resolve(answerIndex(sent.indexes))
    }
    if (pathname === "/api/v1/scans/import" && typeof init?.body === "string") {
      const body = JSON.parse(init.body) as ScanImportRequest
      sent.imports.push(body)
      return Promise.resolve(answerImport(body))
    }
    return Promise.resolve(new Response(null, { status: 404 }))
  })
  return sent
}

async function until(check: () => void) {
  await vi.waitFor(async () => {
    await act(async () => {
      await Promise.resolve()
    })
    check()
  })
}

/** The page, once it has the published scan-log settings (the upload's accept). */
async function mount(history?: RouterHistory) {
  const root = await renderWithRouter(
    <QueryClientProvider client={new QueryClient()}>
      <ScanImportPage />
    </QueryClientProvider>,
    history
  )
  await until(() => {
    expect(
      root.querySelector('input[type="file"]')?.getAttribute("accept")
    ).toBe(".xml,.log")
  })
  return root
}

/** The refusal of a new log: its file's name, and why. */
function refusal(root: HTMLElement) {
  const alert = byRole("alert", "Scan log refused", root)[0]
  return {
    name: alert?.querySelector('[data-slot="alert-title"]')?.textContent,
    message: alert?.querySelector('[data-slot="alert-description"]')
      ?.textContent,
  }
}

/** Choose a file in the page's upload input. */
async function upload(root: HTMLElement, file: File) {
  const input = root.querySelector<HTMLInputElement>('input[type="file"]')
  if (!input) throw new Error("no upload input")
  Object.defineProperty(input, "files", { value: [file], configurable: true })
  await act(async () => {
    input.dispatchEvent(new Event("change", { bubbles: true }))
    await Promise.resolve()
  })
}

function importButton(root: HTMLElement): HTMLButtonElement {
  const [button] = byRole("button", /^Import Selected/, root)
  if (!(button instanceof HTMLButtonElement)) throw new Error("no Import")
  return button
}

beforeEach(() => {
  stubGridLayout()
  // jsdom lays nothing out: the picker scrolls its active entry into view.
  Element.prototype.scrollIntoView = vi.fn()
  localStorage.clear()
})

afterEach(() => {
  cleanup()
  localStorage.clear()
})

describe("the scan import page", () => {
  it("refuses a log over the published limit without sending it", async () => {
    const sent = stubApi({})
    const root = await mount()
    const file = new File(["<scanLog/>"], "big.xml")
    Object.defineProperty(file, "size", { value: 1024 * 1024 + 1 })
    await upload(root, file)
    await until(() => {
      expect(refusal(root)).toEqual({
        name: "big.xml",
        message: "1.1 MiB, over the 1 MiB limit",
      })
    })
    expect(sent.indexes).toBe(0)
  })

  it("Browse Server Logs opens at the published start folder", async () => {
    const sent = stubApi({})
    const root = await mount()
    await click(byRole("button", "Browse Server Logs", root)[0])
    await until(() => {
      expect(byRole("listbox")[0]?.getAttribute("aria-label")).toBe(
        "Contents of /data/logs/2026"
      )
    })
    expect(sent.browsed[0]).toBe("/data/logs/2026")
  })

  it("names a refused new log with the API's message; the last import's summary goes, its table stays", async () => {
    stubApi({
      answerIndex: (count) =>
        count === 1
          ? Response.json(INDEX)
          : problem(
              422,
              "Invalid scan log",
              "The file is not well-formed XML."
            ),
      answerImport: (body) =>
        Response.json({
          results: body.scan_ids.map((scan_number) => ({
            scan_number,
            outcome: "imported",
            error: null,
          })),
          summary: { imported: body.scan_ids.length, exists: 0, failed: 0 },
        } satisfies ScanImportReport),
    })
    const root = await mount()
    await upload(root, new File(["<scanLog/>"], "log.xml"))
    await until(() => {
      expect(rowText(root, "0")).toContain("New")
    })
    await click(rowCheckbox(root, "0"))
    await until(() => {
      expect(importButton(root).disabled).toBe(false)
    })
    await click(importButton(root))
    await until(() => {
      expect(byRole("status", "Import summary", root)).toHaveLength(1)
    })

    await upload(root, new File(["<scan"], "broken.xml"))
    await until(() => {
      expect(refusal(root)).toEqual({
        name: "broken.xml",
        message: "The file is not well-formed XML.",
      })
    })
    expect(byRole("status", "Import summary", root)).toHaveLength(0)
    expect(rowText(root, "0")).toContain("Imported")
  })

  it("imports the selected New scans and takes their statuses from the results, without indexing again", async () => {
    const sent = stubApi({
      answerImport: (body) => {
        const report: ScanImportReport = {
          results: body.scan_ids.map((scan_number) =>
            scan_number === 103
              ? { scan_number, outcome: "failed", error: "Bad scan" }
              : { scan_number, outcome: "imported", error: null }
          ),
          summary: { imported: body.scan_ids.length - 1, exists: 0, failed: 1 },
        }
        return Response.json(report)
      },
    })
    const root = await mount()
    await upload(root, new File(["<scanLog/>"], "log.xml"))
    await until(() => {
      expect(rowText(root, "3")).toContain("New")
    })
    expect(rowCheckbox(root, "1").disabled).toBe(true)
    expect(importButton(root).disabled).toBe(true)

    // Quick select: scan IDs greater than 100 (New rows only, so not 101).
    await enterText(root.querySelector('input[inputmode="numeric"]'), "100")
    await click(byRole("button", "Select by scan ID", root)[0])
    const quickLine = () =>
      byRole("status", undefined, root).find((line) =>
        line.textContent.includes("New scans selected")
      )?.textContent
    await until(() => {
      expect(importButton(root).textContent).toBe("Import Selected (2)")
    })
    expect(quickLine()).toBe("2 New scans selected")
    // Another selection ends the line; the criteria stay for the next Select.
    await click(rowCheckbox(root, "2"))
    await until(() => {
      expect(quickLine()).toBeUndefined()
    })
    await click(byRole("button", "Select by scan ID", root)[0])
    await until(() => {
      expect(quickLine()).toBe("2 New scans selected")
    })
    await click(importButton(root))
    await until(() => {
      expect(rowText(root, "2")).toContain("Imported")
    })
    expect(sent.imports).toEqual([
      {
        token: TOKEN,
        scan_ids: [102, 103],
        catalog_defaults: {
          aperture: "wire",
          sample_name: "",
          filefolder: "",
          filename_prefix: [],
          notes: "",
        },
      },
    ])
    expect(rowText(root, "3")).toContain("Failed")
    expect(rowText(root, "0")).toContain("New")
    const summary = byRole("status", "Import summary", root)[0]?.textContent
    expect(summary).toContain("Imported 1 · Exists 0 · Failed 1")
    expect(summary).toContain("103: Bad scan")
    expect(sent.indexes).toBe(1)
    expect(importButton(root).textContent).toBe("Import Selected (0)")
  })

  it("asks before leaving while the log holds New scans no import took", async () => {
    stubApi({})
    const history = createMemoryHistory({ initialEntries: ["/"] })
    const root = await mount(history)
    await upload(root, new File(["<scanLog/>"], "log.xml"))
    await until(() => {
      expect(rowText(root, "3")).toContain("New")
    })
    act(() => {
      history.push("/scans")
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
    expect(history.location.pathname).toBe("/")
    expect(rowText(root, "3")).toContain("New")
  })

  it("shows the API's message when it refuses the import", async () => {
    stubApi({
      answerImport: () =>
        problem(
          404,
          "Not Found",
          "The staged scan log has expired: index it again."
        ),
    })
    const root = await mount()
    await upload(root, new File(["<scanLog/>"], "log.xml"))
    await until(() => {
      expect(rowText(root, "0")).toContain("New")
    })
    await click(rowCheckbox(root, "0"))
    await until(() => {
      expect(importButton(root).disabled).toBe(false)
    })
    await click(importButton(root))
    await until(() => {
      expect(root.textContent).toContain(
        "The staged scan log has expired: index it again."
      )
    })
    expect(rowText(root, "0")).toContain("New")
  })
})
