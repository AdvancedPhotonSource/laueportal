import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, useState } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { answerFilesLater, type FakeFiles } from "@/test/files-api"
import { byRole, cleanup, click, render } from "@/test/render"
import { PathField } from "./PathField"
import { PathPickerDialog } from "./PathPickerDialog"
import type { PickerMode } from "./path-picker"

// The path picker on a fake files API (src/test/files-api.ts): the field's Browse opens the dialog
// at the field's folder, which walks the roots with the mouse and the keyboard.

const FILES: FakeFiles = {
  roots: { data: "/net/data", storage: "/net/storage" },
  unavailable: ["/net/storage"],
  tree: {
    "/net/data": { "2026": "/", archive: "/", "readme.txt": 120 },
    "/net/data/2026": {
      scan_7: "/",
      "scan_7.xml": 2048,
      "notes.txt": 10,
      "scan_10.XML": 4096,
    },
    "/net/data/2026/scan_7": { "img_1.h5": 1048576, "img_2.h5": 1048576 },
    "/net/data/archive": {},
    "/net/storage": {},
  },
}

afterEach(cleanup)

function requestUrl(input: RequestInfo | URL): URL {
  return new URL(
    input instanceof Request ? input.url : input.toString(),
    "http://localhost"
  )
}

// jsdom lays nothing out: the list scrolls its active entry into view with the keyboard.
beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn()
})

const onChange = vi.fn<(path: string) => void>()

function Field({
  initial,
  mode,
  extensions,
}: {
  initial: string
  mode?: PickerMode
  extensions?: string[]
}) {
  const [value, setValue] = useState(initial)
  return (
    <>
      <label htmlFor="folder">Files Path</label>
      <PathField
        id="folder"
        value={value}
        onChange={(path) => {
          setValue(path)
          onChange(path)
        }}
        mode={mode}
        extensions={extensions}
        dialogTitle="Choose the data folder"
      />
    </>
  )
}

async function renderField(
  initial: string,
  options: { mode?: PickerMode; extensions?: string[]; files?: FakeFiles } = {}
) {
  onChange.mockReset()
  const fetchMock = vi.fn(
    async (input: RequestInfo | URL) =>
      (await answerFilesLater(options.files ?? FILES, input)) ??
      new Response("unexpected request", { status: 500 })
  )
  vi.stubGlobal("fetch", fetchMock)
  await render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <Field
        initial={initial}
        mode={options.mode}
        extensions={options.extensions}
      />
    </QueryClientProvider>
  )
  await click(byRole("button", "Browse")[0])
  return { fetchMock }
}

const dialog = () => byRole("dialog")[0]
const listbox = () => {
  const list = byRole("listbox")[0]
  if (!list) throw new Error("no list")
  return list
}
const options = () =>
  byRole("option").map((option) => ({
    name: option.querySelector('[data-slot="choice-name"]')?.textContent,
    selected: option.getAttribute("aria-selected") === "true",
    disabled: option.getAttribute("aria-disabled") === "true",
  }))
const optionNames = () => options().map((option) => option.name)
const crumbs = () =>
  Array.from(
    dialog()?.querySelectorAll('[data-slot="breadcrumb-item"]') ?? []
  ).map((item) => item.textContent)

async function waitForList(label: string) {
  await vi.waitFor(() => {
    expect(listbox().getAttribute("aria-label")).toBe(label)
    expect(listbox().getAttribute("aria-busy")).toBe("false")
  })
}

async function press(key: string, init: KeyboardEventInit = {}) {
  await act(async () => {
    listbox().dispatchEvent(
      new KeyboardEvent("keydown", { key, bubbles: true, ...init })
    )
    await Promise.resolve()
  })
}

async function doubleClick(element: Element | undefined) {
  if (!element) throw new Error("nothing to double-click")
  await act(async () => {
    element.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }))
    await Promise.resolve()
  })
}

function option(name: string) {
  return byRole("option").find(
    (element) =>
      element.querySelector('[data-slot="choice-name"]')?.textContent === name
  )
}

describe("PathField's dialog", () => {
  it("opens at the field's folder: breadcrumbs, folders first, files shown but not chosen", async () => {
    await renderField("/net/data/2026")
    await waitForList("Contents of /net/data/2026")

    expect(dialog()?.textContent).toContain("Choose the data folder")
    expect(crumbs()).toEqual(["Roots", "data", "2026"])
    expect(options()).toEqual([
      { name: "scan_7", selected: true, disabled: false },
      { name: "notes.txt", selected: false, disabled: true },
      { name: "scan_7.xml", selected: false, disabled: true },
      { name: "scan_10.XML", selected: false, disabled: true },
    ])
    // The keyboard starts in the list.
    expect(document.activeElement).toBe(listbox())
    expect(dialog()?.textContent).toContain("/net/data/2026")
  })

  it("opens a folder with a click and selects the folder shown", async () => {
    await renderField("/net/data/2026")
    await waitForList("Contents of /net/data/2026")

    await click(option("scan_7"))
    await waitForList("Contents of /net/data/2026/scan_7")
    expect(crumbs()).toEqual(["Roots", "data", "2026", "scan_7"])
    expect(optionNames()).toEqual(["img_1.h5", "img_2.h5"])

    await click(byRole("button", "Select folder")[0])
    expect(onChange).toHaveBeenCalledExactlyOnceWith("/net/data/2026/scan_7")
    await vi.waitFor(() => {
      expect(byRole("dialog")).toHaveLength(0)
    })
    expect(document.querySelector<HTMLInputElement>("#folder")?.value).toBe(
      "/net/data/2026/scan_7"
    )
  })

  it("walks the roots with the keyboard", async () => {
    await renderField("")
    await waitForList("Allowed roots")
    expect(crumbs()).toEqual(["Roots"])
    expect(options()).toEqual([
      { name: "data", selected: true, disabled: false },
      { name: "storage", selected: false, disabled: true },
    ])
    expect(byRole("button", "Select folder")[0]?.hasAttribute("disabled")).toBe(
      true
    )

    // A root missing on this host does not open.
    await press("ArrowDown")
    expect(options()[1]?.selected).toBe(true)
    await press("Enter")
    expect(listbox().getAttribute("aria-label")).toBe("Allowed roots")

    await press("Home")
    await press("Enter")
    await waitForList("Contents of /net/data")
    expect(optionNames()).toEqual(["2026", "archive", "readme.txt"])
    await press("ArrowDown")
    await press("Enter")
    await waitForList("Contents of /net/data/archive")
    expect(dialog()?.textContent).toContain("This folder is empty.")

    // Up highlights the folder left; up from a root's folder is the roots.
    await press("Backspace")
    await waitForList("Contents of /net/data")
    expect(options().find((entry) => entry.selected)?.name).toBe("archive")
    await press("ArrowUp", { altKey: true })
    await waitForList("Allowed roots")

    await press("Enter")
    await waitForList("Contents of /net/data")
    await press("End")
    expect(options().at(-1)?.selected).toBe(true)
    await press("PageUp")
    await press("Enter")
    await waitForList("Contents of /net/data/2026")
    expect(document.activeElement).toBe(listbox())
  })

  it("goes back up through the breadcrumbs", async () => {
    await renderField("/net/data/2026/scan_7")
    await waitForList("Contents of /net/data/2026/scan_7")

    const crumb = Array.from(dialog()?.querySelectorAll("button") ?? []).find(
      (button) => button.textContent === "data"
    )
    await click(crumb)
    await waitForList("Contents of /net/data")
    expect(crumbs()).toEqual(["Roots", "data"])

    await click(byRole("button", "Roots")[0])
    await waitForList("Allowed roots")
  })

  it.each([
    [
      "/tmp/scans",
      "Path not allowed",
      "/tmp/scans is outside the allowed roots.",
    ],
    [
      "/net/data/2025",
      "Path not found",
      "/net/data/2025 does not exist or is not available on this host.",
    ],
    ["/net/data/readme.txt", "Invalid selection", "is not a directory."],
  ])(
    "opens at the roots, with the reason, when the field's %s cannot be opened",
    async (value, title, detail) => {
      await renderField(value)
      await waitForList("Allowed roots")
      const notice = dialog()?.querySelector('[data-slot="alert"]')
      expect(notice?.textContent).toContain(
        `${value} cannot be opened: ${title}`
      )
      expect(notice?.textContent).toContain(detail)
      expect(optionNames()).toEqual(["data", "storage"])
    }
  )

  it("in file mode lists the extension's files and picks one with Enter", async () => {
    const { fetchMock } = await renderField("/net/data/2026/scan_7.xml", {
      mode: "file",
      extensions: [".XML"],
    })
    await waitForList("Contents of /net/data/2026")

    const browse = fetchMock.mock.calls
      .map(([input]) => requestUrl(input))
      .find((url) => url.pathname === "/api/v1/files/browse")
    expect(browse?.searchParams.get("path")).toBe("/net/data/2026")
    expect(browse?.searchParams.get("extensions")).toBe(".xml")
    // The field's file is highlighted.
    expect(options()).toEqual([
      { name: "scan_7", selected: false, disabled: false },
      { name: "scan_7.xml", selected: true, disabled: false },
      { name: "scan_10.XML", selected: false, disabled: false },
    ])

    await press("ArrowDown")
    expect(dialog()?.textContent).toContain("/net/data/2026/scan_10.XML")
    await press("Enter")
    expect(onChange).toHaveBeenCalledExactlyOnceWith(
      "/net/data/2026/scan_10.XML"
    )
  })

  it("in file mode picks a file with a double-click, or the highlighted one with Select", async () => {
    await renderField("", { mode: "file", extensions: [".xml"] })
    await waitForList("Allowed roots")
    await click(option("data"))
    await waitForList("Contents of /net/data")
    expect(byRole("button", "Select file")[0]?.hasAttribute("disabled")).toBe(
      true
    )
    await click(option("2026"))
    await waitForList("Contents of /net/data/2026")

    await click(option("scan_7.xml"))
    expect(onChange).not.toHaveBeenCalled()
    await click(byRole("button", "Select file")[0])
    expect(onChange).toHaveBeenLastCalledWith("/net/data/2026/scan_7.xml")

    await click(byRole("button", "Browse")[0])
    await waitForList("Contents of /net/data/2026")
    await doubleClick(option("scan_10.XML"))
    expect(onChange).toHaveBeenLastCalledWith("/net/data/2026/scan_10.XML")
  })

  it("in file mode lists a file over maxFileSize but does not let it be chosen", async () => {
    vi.stubGlobal(
      "fetch",
      async (input: RequestInfo | URL) =>
        (await answerFilesLater(FILES, input)) ??
        new Response("unexpected request", { status: 500 })
    )
    onChange.mockReset()
    await render(
      <QueryClientProvider client={new QueryClient()}>
        <PathPickerDialog
          open
          onOpenChange={() => undefined}
          mode="file"
          extensions={[".xml"]}
          maxFileSize={3000}
          value="/net/data/2026/scan_7.xml"
          title="Browse Server Logs"
          onSelect={onChange}
        />
      </QueryClientProvider>
    )
    await waitForList("Contents of /net/data/2026")
    expect(options()).toEqual([
      { name: "scan_7", selected: false, disabled: false },
      { name: "scan_7.xml", selected: true, disabled: false },
      { name: "scan_10.XML", selected: false, disabled: true },
    ])
    expect(option("scan_10.XML")?.textContent).toContain(
      "over the 2.9 KiB limit"
    )
    await click(option("scan_10.XML"))
    expect(byRole("button", "Select file")[0]?.hasAttribute("disabled")).toBe(
      true
    )
    await press("Enter")
    await doubleClick(option("scan_10.XML"))
    expect(onChange).not.toHaveBeenCalled()
  })

  it("closes on Cancel without a change", async () => {
    await renderField("/net/data")
    await waitForList("Contents of /net/data")
    await click(byRole("button", "Cancel")[0])
    await vi.waitFor(() => {
      expect(byRole("dialog")).toHaveLength(0)
    })
    expect(onChange).not.toHaveBeenCalled()
  })

  it("while the next folder loads, dims the last list, ignores it and says what opens", async () => {
    let release: (value?: unknown) => void = () => undefined
    const hold = new Promise((resolve) => {
      release = resolve
    })
    await renderField("/net/data/2026", {
      files: {
        ...FILES,
        tree: {
          ...FILES.tree,
          "/net/data/2026": { ...FILES.tree["/net/data/2026"], scan_8: "/" },
        },
        holds: { "/net/data/2026/scan_7": hold },
      },
    })
    await waitForList("Contents of /net/data/2026")
    expect(optionNames()).toEqual([
      "scan_7",
      "scan_8",
      "notes.txt",
      "scan_7.xml",
      "scan_10.XML",
    ])

    await click(option("scan_7"))
    // The place it opens shows at once; the last list stays, dimmed and inert, until it is read.
    expect(listbox().getAttribute("aria-busy")).toBe("true")
    expect(listbox().getAttribute("aria-label")).toBe(
      "Contents of /net/data/2026/scan_7"
    )
    expect(crumbs()).toEqual(["Roots", "data", "2026", "scan_7"])
    expect(dialog()?.querySelector('[role="status"]')?.textContent).toBe(
      "Opening scan_7"
    )
    expect(options().some((entry) => entry.selected)).toBe(false)
    expect(listbox().hasAttribute("aria-activedescendant")).toBe(false)
    expect(byRole("button", "Select folder")[0]?.hasAttribute("disabled")).toBe(
      true
    )
    await click(option("scan_8"))
    await press("ArrowDown")
    await press("Enter")
    expect(listbox().getAttribute("aria-label")).toBe(
      "Contents of /net/data/2026/scan_7"
    )

    release()
    await waitForList("Contents of /net/data/2026/scan_7")
    expect(optionNames()).toEqual(["img_1.h5", "img_2.h5"])
    expect(dialog()?.querySelector('[role="status"]')).toBeNull()
    await click(byRole("button", "Select folder")[0])
    expect(onChange).toHaveBeenCalledExactlyOnceWith("/net/data/2026/scan_7")
  })

  it("keeps a failed folder's breadcrumbs, and goes up to its parent from it", async () => {
    await renderField("/net/data/2026", {
      files: { ...FILES, failures: { "/net/data/2026/scan_7": 500 } },
    })
    await waitForList("Contents of /net/data/2026")
    await click(option("scan_7"))
    await vi.waitFor(() => {
      expect(
        dialog()?.querySelector('[data-slot="alert"]')?.textContent
      ).toContain("Internal Server Error")
    })
    expect(crumbs()).toEqual(["Roots", "data", "2026", "scan_7"])
    expect(listbox().getAttribute("aria-label")).toBe(
      "Contents of /net/data/2026/scan_7"
    )
    expect(byRole("button", "Select folder")[0]?.hasAttribute("disabled")).toBe(
      true
    )

    await press("Backspace")
    await waitForList("Contents of /net/data/2026")
    expect(options().find((entry) => entry.selected)?.name).toBe("scan_7")
    expect(dialog()?.querySelector('[data-slot="alert"]')).toBeNull()
  })

  it("says how many entries it cannot show for their names", async () => {
    await renderField("/net/data/2026", {
      files: { ...FILES, invalidNames: { "/net/data/2026": 2 } },
    })
    await waitForList("Contents of /net/data/2026")
    expect(dialog()?.textContent).toContain(
      "2 entries are not shown: their names are not valid UTF-8."
    )
  })

  it("lists an entry whose file system does not answer as not responding, and never opens it", async () => {
    await renderField("/net/data/2026", {
      files: { ...FILES, notResponding: { "/net/data/2026": ["old_host"] } },
    })
    await waitForList("Contents of /net/data/2026")
    expect(options().at(-1)).toEqual({
      name: "old_host",
      selected: false,
      disabled: true,
    })
    expect(option("old_host")?.textContent).toContain("Not responding")
    await doubleClick(option("old_host"))
    expect(listbox().getAttribute("aria-label")).toBe(
      "Contents of /net/data/2026"
    )
  })

  it("says when a listing is capped", async () => {
    await renderField("/net/data/2026", { files: { ...FILES, cap: 2 } })
    await waitForList("Contents of /net/data/2026")
    expect(optionNames()).toEqual(["scan_7", "notes.txt"])
    expect(dialog()?.textContent).toContain(
      "Showing 2 entries: the folder holds more."
    )
  })
})
