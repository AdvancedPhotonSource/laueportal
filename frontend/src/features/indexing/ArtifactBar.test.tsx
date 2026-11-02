import { act } from "react"
import { toast } from "sonner"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { ConversionJob, IndexingArtifact } from "@/api"
import { Providers } from "@/app/Providers"
import { createQueryClient } from "@/app/query-client"
import { makeArtifact } from "@/test/indexing"
import { byRole, cleanup, click, render, stubMatchMedia } from "@/test/render"
import { ARTIFACT_POLL_MS } from "./artifact-bar"
import { ArtifactBar } from "./ArtifactBar"

// The artifact bar with fetch answering the artifact (one answer per read, the last one repeated),
// the conversion and the XML choice.

const SUMMARY_KEY = ["/api/v1/indexing-runs/1/summary", { min_peaks: 4 }]

function conversion(overrides: Partial<ConversionJob> = {}): ConversionJob {
  return {
    id: "convert_results_1",
    kind: "convert_results",
    indexing_id: 1,
    status: "queued",
    message: null,
    enqueued_at: "2026-10-21T10:00:00Z",
    started_at: null,
    ended_at: null,
    held: false,
    ...overrides,
  }
}

const XML_ONLY = makeArtifact({
  state: "xml_only",
  results_path: null,
  xml_path: "/runs/index_1/output.xml",
  xml_source: "recorded",
})

async function openBar(
  artifacts: IndexingArtifact[],
  answers: { convert?: ConversionJob; select?: IndexingArtifact } = {}
) {
  stubMatchMedia()
  const requests: Request[] = []
  let reads = 0
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = input instanceof Request ? input.url : input.toString()
      const request = new Request(new URL(url, "http://localhost"), init)
      requests.push(request)
      const path = new URL(request.url).pathname
      if (path.endsWith("/convert"))
        return Promise.resolve(Response.json(answers.convert, { status: 202 }))
      if (path.endsWith("/select-xml"))
        return Promise.resolve(Response.json(answers.select))
      const artifact = artifacts[Math.min(reads, artifacts.length - 1)]
      reads += 1
      return Promise.resolve(Response.json(artifact))
    })
  )
  const queryClient = createQueryClient()
  queryClient.setQueryData(SUMMARY_KEY, { steps: [] })
  await render(
    <Providers queryClient={queryClient}>
      <ArtifactBar indexingId={1} />
    </Providers>
  )
  return { requests, queryClient }
}

const bar = () => byRole("region", "Results file")[0]
const posts = (requests: Request[]) =>
  requests.filter((request) => request.method === "POST")

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn()
})

afterEach(() => {
  act(() => {
    toast.dismiss()
  })
  vi.useRealTimers()
  cleanup()
})

describe("the artifact bar", () => {
  it("shows nothing while the results file is ready", async () => {
    const { requests } = await openBar([makeArtifact()])
    await vi.waitFor(() => {
      expect(requests).toHaveLength(1)
    })
    await act(() => Promise.resolve())
    expect(bar()).toBeUndefined()
  })

  it("says which XML it reads, converts it, follows the conversion and reads the results again", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { requests, queryClient } = await openBar(
      [
        XML_ONLY,
        { ...XML_ONLY, conversion: conversion({ status: "running" }) },
        makeArtifact({ results_path: "/runs/index_1/output.h5" }),
      ],
      { convert: conversion() }
    )
    await vi.waitFor(() => {
      expect(bar()?.textContent).toContain(
        "reading output.xml, the XML recorded with the run"
      )
    })
    await click(byRole("button", "Convert to HDF5")[0])
    expect(posts(requests).map((request) => request.url)).toEqual([
      "http://localhost/api/v1/indexing-runs/1/convert",
    ])
    await vi.waitFor(() => {
      expect(byRole("status", /Converting to HDF5/)).toHaveLength(1)
    })
    expect(
      byRole("button", "Convert to HDF5")[0]?.hasAttribute("disabled")
    ).toBe(true)

    await act(() => vi.advanceTimersByTimeAsync(ARTIFACT_POLL_MS))
    await vi.waitFor(() => {
      expect(bar()).toBeUndefined()
    })
    expect(queryClient.getQueryState(SUMMARY_KEY)?.isInvalidated).toBe(true)
  })

  it("shows why a conversion failed, and converts again on request and follows it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { requests } = await openBar(
      [
        {
          ...XML_ONLY,
          conversion: conversion({
            status: "failed",
            message: "Malformed XML",
          }),
        },
        { ...XML_ONLY, conversion: conversion({ status: "running" }) },
        makeArtifact(),
      ],
      { convert: conversion() }
    )
    await vi.waitFor(() => {
      expect(
        byRole("status", "The conversion failed: Malformed XML")
      ).toHaveLength(1)
    })
    await click(byRole("button", "Convert to HDF5 again")[0])
    expect(posts(requests).map((request) => request.url)).toEqual([
      "http://localhost/api/v1/indexing-runs/1/convert",
    ])
    await vi.waitFor(() => {
      expect(byRole("status", /Converting to HDF5/)).toHaveLength(1)
    })
    // Polled until the results file is ready.
    await act(() => vi.advanceTimersByTimeAsync(ARTIFACT_POLL_MS))
    await vi.waitFor(() => {
      expect(bar()).toBeUndefined()
    })
  })

  it("lists the API's problems, and says when a finished conversion's results file cannot be used", async () => {
    await openBar([
      {
        ...XML_ONLY,
        conversion: conversion({ status: "finished" }),
        problems: ["canonical results file unusable: no /entry"],
      },
    ])
    await vi.waitFor(() => {
      expect(
        byRole(
          "status",
          "The conversion finished, but its results file cannot be used: the viewer still reads the XML."
        )
      ).toHaveLength(1)
    })
    expect(bar()?.textContent).toContain(
      "canonical results file unusable: no /entry"
    )
    expect(byRole("button", "Convert to HDF5 again")).toHaveLength(1)
  })

  it("says when it stops following a conversion whose state cannot be read, and checks again", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const unread = {
      ...XML_ONLY,
      problems: [
        "The conversion's state could not be read: the database is busy",
      ],
    }
    const { requests } = await openBar([
      { ...XML_ONLY, conversion: conversion({ status: "running" }) },
      unread,
    ])
    await vi.waitFor(() => {
      expect(byRole("status", /Converting to HDF5/)).toHaveLength(1)
    })
    await act(() => vi.advanceTimersByTimeAsync(ARTIFACT_POLL_MS))
    await vi.waitFor(() => {
      expect(byRole("status", /progress cannot be followed/)).toHaveLength(1)
    })
    expect(bar()?.textContent).toContain(
      "The conversion's state could not be read: the database is busy"
    )
    // Polling has stopped; "Check again" reads the artifact once more.
    const reads = requests.length
    await act(() => vi.advanceTimersByTimeAsync(3 * ARTIFACT_POLL_MS))
    expect(requests).toHaveLength(reads)
    await click(byRole("button", "Check again")[0])
    await vi.waitFor(() => {
      expect(requests).toHaveLength(reads + 1)
    })
  })

  it("records the XML chosen among several", async () => {
    const chosen = {
      ...XML_ONLY,
      xml_path: "/runs/index_1/b.xml",
      xml_source: "chosen" as const,
      xml_candidates: ["a.xml", "b.xml"],
    }
    const { requests } = await openBar(
      [
        makeArtifact({
          state: "ambiguous",
          results_path: null,
          xml_candidates: ["a.xml", "b.xml"],
          problems: [
            "2 XML files in the run directory and the recorded name is absent",
          ],
        }),
      ],
      { select: chosen }
    )
    await vi.waitFor(() => {
      expect(bar()?.textContent).toContain("several XML files")
    })
    expect(bar()?.textContent).toContain(
      "2 XML files in the run directory and the recorded name is absent"
    )
    expect(byRole("button", "Use this XML")[0]?.hasAttribute("disabled")).toBe(
      true
    )
    await click(byRole("combobox")[0])
    await click(byRole("option", "b.xml")[0])
    await click(byRole("button", "Use this XML")[0])
    const [post] = posts(requests)
    expect(post?.url).toBe(
      "http://localhost/api/v1/indexing-runs/1/artifact/select-xml"
    )
    expect(await post?.json()).toEqual({ name: "b.xml" })
    await vi.waitFor(() => {
      expect(bar()?.textContent).toContain(
        "reading b.xml, the XML chosen on this page"
      )
    })
    // A chosen XML can be changed.
    expect(byRole("button", "Change XML")).toHaveLength(1)
  })

  it("changes a chosen XML for another, or cancels the change", async () => {
    const chosen = {
      ...XML_ONLY,
      xml_path: "/runs/index_1/b.xml",
      xml_source: "chosen" as const,
      xml_candidates: ["a.xml", "b.xml"],
    }
    const { requests } = await openBar([chosen], {
      select: { ...chosen, xml_path: "/runs/index_1/a.xml" },
    })
    await vi.waitFor(() => {
      expect(byRole("button", "Change XML")).toHaveLength(1)
    })
    await click(byRole("button", "Change XML")[0])
    await click(byRole("button", "Cancel")[0])
    expect(byRole("combobox")).toHaveLength(0)
    expect(posts(requests)).toHaveLength(0)

    await click(byRole("button", "Change XML")[0])
    // The current XML cannot be chosen again.
    expect(byRole("button", "Use this XML")[0]?.hasAttribute("disabled")).toBe(
      true
    )
    await click(byRole("combobox")[0])
    await click(byRole("option", "a.xml")[0])
    await click(byRole("button", "Use this XML")[0])
    expect(await posts(requests)[0]?.json()).toEqual({ name: "a.xml" })
    await vi.waitFor(() => {
      expect(bar()?.textContent).toContain(
        "reading a.xml, the XML chosen on this page"
      )
    })
    expect(byRole("combobox")).toHaveLength(0)
  })

  it.each([
    ["writing", "Writing results"],
    ["not_on_host", "Run directory not on this host"],
    ["not_responding", "Run directory not responding"],
  ] as const)("says %s as its status alone", async (state, text) => {
    await openBar([makeArtifact({ state, results_path: null })])
    await vi.waitFor(() => {
      expect(bar()?.textContent).toBe(text)
    })
  })

  it("explains why there are no results", async () => {
    await openBar([
      makeArtifact({
        state: "empty",
        results_path: null,
        problems: ["output.h5 is not a results file: no /entry"],
      }),
    ])
    await vi.waitFor(() => {
      expect(bar()?.textContent).toContain("No results for this run.")
    })
    expect(bar()?.textContent).toContain(
      "output.h5 is not a results file: no /entry"
    )
  })
})
