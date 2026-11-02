import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, type ReactNode, useEffect } from "react"
import { type Control, useForm, type UseFormReturn } from "react-hook-form"
import { afterEach, describe, expect, it, vi } from "vitest"
import type { FileIndices, FilePatterns } from "@/api"
import { answerFiles, type FakeFiles } from "@/test/files-api"
import { bodyOf, fieldOf, specOf, urlOf } from "@/test/programs"
import { byRole, cleanup, click, enterText, render } from "@/test/render"
import { FilenamePatternField, IndicesField } from "./file-helpers"
import {
  HklField,
  NumberField,
  OptionalNumberField,
  PathPickerField,
  RangeField,
  SelectField,
  ToggleField,
} from "./fields"
import { formValuesOf, type FormValues } from "./form-values"
import { CloneField, SourceField } from "./SourcePicker"
import { SOURCE_SEARCH_DELAY_MS } from "./source-search"

const spec = specOf("lauego")
const f = (name: string) => fieldOf("lauego", name)
/** The host's form, to read the values the fields hold. */
const host: { form?: UseFormReturn<FormValues> } = {}

function Host({
  children,
}: {
  children: (control: Control<FormValues>) => ReactNode
}) {
  const form = useForm<FormValues>({ defaultValues: formValuesOf(spec) })
  useEffect(() => {
    host.form = form
  }, [form])
  return <>{children(form.control)}</>
}

/** A value the fields' form holds. */
function valueOf(name: string) {
  return host.form?.getValues(name)
}

async function mount(children: (control: Control<FormValues>) => ReactNode) {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <Host>{children}</Host>
    </QueryClientProvider>
  )
}

const input = (root: ParentNode, name: string) => {
  const element = root.querySelector(`[name="${name}"]`)
  if (!(element instanceof HTMLInputElement)) throw new Error(`no ${name}`)
  return element
}

/** fetch answering one POST path with this JSON, recording the bodies. */
function answer(path: string, json: unknown) {
  const bodies: unknown[] = []
  vi.stubGlobal("fetch", (url: RequestInfo | URL, init?: RequestInit) => {
    const { pathname } = urlOf(url)
    if (pathname !== path)
      return Promise.resolve(new Response(null, { status: 404 }))
    if (init?.body) bodies.push(bodyOf(init))
    return Promise.resolve(Response.json(json))
  })
  return bodies
}

/** Wait until `check` passes, letting React apply what each answer changes. */
async function until(check: () => void, timeout = 1000) {
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

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe("the field components", () => {
  it("a number holds the text typed with its unit beside it; an optional one reads automatic when blank", async () => {
    const root = await mount((control) => (
      <>
        <NumberField field={f("box_size")} control={control} />
        <OptionalNumberField field={f("threshold")} control={control} />
      </>
    ))
    expect(input(root, "box_size").value).toBe("18")
    expect(root.textContent).toContain("px")
    await enterText(input(root, "box_size"), "2.5")
    expect(valueOf("box_size")).toBe("2.5")
    expect(input(root, "threshold").placeholder).toBe("Automatic")
  })

  it("an integer range shows the count it holds", async () => {
    const root = await mount((control) => (
      <RangeField field={f("scan_points")} control={control} />
    ))
    await enterText(input(root, "scan_points"), "1-10,15")
    expect(valueOf("scan_points")).toBe("1-10,15")
    expect(root.textContent).toContain("11 points")
  })

  it("HKL is three integers in one row, each its own field", async () => {
    const root = await mount((control) => (
      <HklField
        fields={[f("index_h"), f("index_k"), f("index_l")]}
        control={control}
        issuesOf={() => undefined}
      />
    ))
    expect(root.textContent).toContain("Central HKL")
    expect(byRole("textbox", undefined, root).length).toBe(0) // inputs are labelled H, K, L
    expect(
      ["H", "K", "L"].map(
        (letter) => root.querySelector(`[aria-label="${letter}"]`) !== null
      )
    ).toEqual([true, true, true])
    await enterText(input(root, "index_k"), "-1")
    expect([
      valueOf("index_h"),
      valueOf("index_k"),
      valueOf("index_l"),
    ]).toEqual(["0", "-1", "1"])
  })

  it("a select offers the x-options in their order and holds the chosen value", async () => {
    const root = await mount((control) => (
      <SelectField field={f("peak_shape")} control={control} />
    ))
    const trigger = root.querySelector('[data-slot="select-trigger"]')
    expect(trigger?.textContent).toContain("Lorentzian")
    await click(trigger)
    const options = byRole("option")
    expect(options.map((option) => option.textContent)).toEqual([
      "Lorentzian",
      "Gaussian",
    ])
    await click(options[1])
    expect(valueOf("peak_shape")).toBe("Gaussian")
  })

  it("a toggle holds a boolean", async () => {
    await mount((control) => (
      <ToggleField field={f("smooth")} control={control} />
    ))
    await click(byRole("checkbox")[0])
    expect(valueOf("smooth")).toBe(true)
  })

  it("a path picker holds the path typed (or browsed)", async () => {
    const root = await mount((control) => (
      <PathPickerField field={f("mask_file")} control={control} mode="file" />
    ))
    await enterText(input(root, "mask_file"), "/data/mask.h5")
    expect(valueOf("mask_file")).toBe("/data/mask.h5")
  })

  it("a file picker lists its field's kind of file; the output folder's keeps the template's run ID part", async () => {
    Element.prototype.scrollIntoView = vi.fn()
    const files: FakeFiles = {
      roots: { data: "/net/data" },
      tree: {
        "/net/data": { config: "/", runs: "/" },
        "/net/data/config": { "geo.xml": 10, "notes.txt": 5 },
        "/net/data/runs": { analysis: "/" },
        "/net/data/runs/analysis": {},
      },
    }
    const browsed: URL[] = []
    vi.stubGlobal("fetch", (url: RequestInfo | URL) => {
      browsed.push(urlOf(url))
      return Promise.resolve(
        answerFiles(files, url) ?? new Response(null, { status: 404 })
      )
    })
    const root = await mount((control) => (
      <>
        <PathPickerField
          field={f("geometry_file")}
          control={control}
          mode="file"
        />
        <PathPickerField
          field={f("output_path_template")}
          control={control}
          mode="directory"
          template
        />
      </>
    ))
    await enterText(input(root, "geometry_file"), "/net/data/config/geo.xml")
    await click(byRole("button", "Browse")[0])
    await until(() => {
      expect(
        browsed.find((url) => url.pathname === "/api/v1/files/browse")
      ).toBeDefined()
    })
    const listing = browsed.find(
      (url) => url.pathname === "/api/v1/files/browse"
    )
    expect(listing?.searchParams.get("extensions")).toBe(".xml")
    await click(byRole("button", "Cancel")[0])

    browsed.length = 0
    await enterText(
      input(root, "output_path_template"),
      "/net/data/runs/rec_%d/data"
    )
    await click(byRole("button", "Browse")[1])
    await until(() => {
      expect(byRole("button", "Select folder")[0]).toHaveProperty(
        "disabled",
        false
      )
    })
    expect(
      browsed
        .find((url) => url.pathname === "/api/v1/files/browse")
        ?.searchParams.get("path")
    ).toBe("/net/data/runs")
    await click(byRole("option", /analysis/)[0]) // opens it
    await until(() => {
      const [list] = byRole("listbox", "Contents of /net/data/runs/analysis")
      expect(list?.getAttribute("aria-busy")).toBe("false")
    })
    await click(byRole("button", "Select folder")[0])
    await until(() => {
      expect(valueOf("output_path_template")).toBe(
        "/net/data/runs/analysis/rec_%d/data"
      )
    })
  })

  it("Find Matching Files suggests the folder's templates; a pick sets the Filename and gives its ranges", async () => {
    const patterns: FilePatterns = {
      truncated: false,
      patterns: [
        {
          pattern: "Si_%d_%d.h5",
          label: "Si_%d_%d.h5 (scanPoints: 1-10, depths: 0-5)",
          scan_points: "1-10",
          depth_range: "0-5",
        },
        {
          pattern: "Si_%d.h5",
          label: "Si_%d.h5 (scanPoints: 1-3)",
          scan_points: "1-3",
          depth_range: null,
        },
      ],
    }
    const bodies = answer("/api/v1/files/patterns", patterns)
    const onPick = vi.fn()
    await mount((control) => (
      <FilenamePatternField
        field={f("filename_prefixes")}
        control={control}
        kind="lauego"
        folder="/data/scan_1"
        onPick={onPick}
      />
    ))
    await click(byRole("button", "Find Matching Files")[0])
    await until(() => {
      expect(byRole("button", /^Si_%d\.h5/)).toHaveLength(1)
    })
    expect(bodies).toEqual([{ folder: "/data/scan_1", program: "lauego" }])
    expect(
      document.querySelector('ul[aria-label="Matching files"] button')
        ?.textContent
    ).toBe("Si_%d_%d.h5Scan Indices 1-10 · Depth Indices 0-5") // the form's labels
    await click(byRole("button", /^Si_%d\.h5/)[0])
    expect(valueOf("filename_prefixes")).toBe("Si_%d.h5")
    expect(onPick).toHaveBeenCalledWith(patterns.patterns[1])
  })

  it("Find Indices reads the matching files' indices with the first template and gives them", async () => {
    const indices: FileIndices = {
      scan_points: "1-12",
      depth_range: null,
      files: 12,
      truncated: false,
    }
    const bodies = answer("/api/v1/files/indices", indices)
    const onFound = vi.fn()
    const root = await mount((control) => (
      <IndicesField
        fields={[f("scan_points"), f("depth_range")]}
        control={control}
        issuesOf={() => undefined}
        kind="lauego"
        folder="/data/scan_1"
        template="Si_%d.h5"
        onFound={onFound}
      />
    ))
    await click(byRole("button", "Find Indices")[0])
    await until(() => {
      expect(onFound).toHaveBeenCalledWith(indices)
    })
    expect(bodies).toEqual([
      { folder: "/data/scan_1", pattern: "Si_%d.h5", program: "lauego" },
    ])
    expect(root.textContent).toContain("12 files")
  })

  it("Find Matching Files and Find Indices are off for a file input (a reconstruction's scan.h5), with the reason", async () => {
    await mount((control) => (
      <>
        <FilenamePatternField
          field={f("filename_prefixes")}
          control={control}
          kind="lauego"
          folder="/runs/rec_1/reconstruction/scan.h5"
        />
        <IndicesField
          fields={[f("scan_points"), f("depth_range")]}
          control={control}
          issuesOf={() => undefined}
          kind="lauego"
          folder="/runs/rec_1/reconstruction/scan.h5"
          template="wire_%d.h5"
        />
      </>
    ))
    for (const name of ["Find Matching Files", "Find Indices"]) {
      const button = byRole("button", name)[0]
      expect(button?.hasAttribute("disabled")).toBe(true)
      expect(button?.parentElement?.title).toBe(
        "The input is a file (a reconstruction's scan.h5), not a folder"
      )
    }
  })

  it("a source field searches as one types and holds the chosen ID", async () => {
    vi.stubGlobal("fetch", (url: RequestInfo | URL) => {
      const { pathname, searchParams } = urlOf(url)
      expect(pathname).toBe("/api/v1/scans")
      const rows = searchParams.get("filter")?.includes("Si")
        ? [
            {
              scan_number: 990001,
              sample_name: "Si wafer",
              time: "2026-10-21T18:12:39Z",
            },
          ]
        : []
      return Promise.resolve(
        Response.json({ rows, total: rows.length, as_of: "" })
      )
    })
    const onSourceChange = vi.fn()
    const root = await mount((control) => (
      <SourceField
        field={f("scan_number")}
        control={control}
        kind="scan"
        onSourceChange={onSourceChange}
      />
    ))
    const search = root.querySelector('input[role="combobox"]')
    await click(root.querySelector('[data-slot="input-group"] button'))
    await enterText(search, "Si")
    await until(() => {
      expect(byRole("option")).toHaveLength(1)
    }, SOURCE_SEARCH_DELAY_MS + 1000)
    const option = byRole("option")[0]
    expect(option?.textContent).toContain("SN990001")
    expect(option?.textContent).toContain("Si wafer")
    await click(option)
    expect(valueOf("scan_number")).toBe("990001")
    expect(onSourceChange).toHaveBeenCalledWith(
      990001,
      expect.objectContaining({ id: 990001 })
    )
  })

  it("a source shows its ID when set from outside (a prefill, a reset), as the clone picker does", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve(Response.json({ rows: [], total: 0, as_of: "" }))
    )
    const root = await mount((control) => (
      <>
        <SourceField field={f("scan_number")} control={control} kind="scan" />
        <CloneField program="wire" value={4} onChange={vi.fn()} />
      </>
    ))
    const shown = () =>
      Array.from(
        root.querySelectorAll<HTMLInputElement>('input[role="combobox"]'),
        (combobox) => combobox.value
      )
    expect(shown()).toEqual(["", "R4"])
    act(() => {
      host.form?.reset({ ...formValuesOf(spec), scan_number: "990001" })
    })
    expect(shown()).toEqual(["SN990001", "R4"])
    act(() => {
      host.form?.setValue("scan_number", "990002")
    })
    expect(shown()).toEqual(["SN990002", "R4"])
    const label = root.querySelectorAll("label")[1]
    expect(label?.textContent).toBe("Clone of")
    expect(label?.htmlFor).toBe(
      root.querySelectorAll('input[role="combobox"]')[1]?.id
    )
  })
})
