import { focusManager } from "@tanstack/react-query"
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router"
import { act } from "react"
import { toast } from "sonner"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { DetectorOverlays, Health } from "@/api"
import { Providers } from "@/app/Providers"
import { createQueryClient } from "@/app/query-client"
import { createAppRouter } from "@/app/router"
import { packViewLink } from "@/lib/view-link"
import {
  answerIndexing,
  makeArtifact,
  makeIndexingRunDetail,
  makeSummary,
  resetIndexingViewerStore,
} from "@/test/indexing"
import { byRole, cleanup, click, render, stubMatchMedia } from "@/test/render"
import { useIndexingViewerStore } from "./viewer-store"

// The indexing viewer in the app's router, the API answering run I1 (any other run is a 404).

const HEALTH: Health = {
  db: "ok",
  worker: "running",
  version: "2.0.0.dev0",
}

async function openViewer(
  url: string,
  answer: (where: URL) => Response | undefined = () => undefined
) {
  stubMatchMedia()
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined)
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const where = new URL(
        input instanceof Request ? input.url : input.toString(),
        "http://localhost"
      )
      if (where.pathname === "/api/v1/health") {
        return Promise.resolve(Response.json(HEALTH))
      }
      const answered = answer(where)
      if (answered) return Promise.resolve(answered)
      const body = answerIndexing(where, undefined, (id) =>
        id === 1 ? makeIndexingRunDetail() : undefined
      )
      return Promise.resolve(
        body instanceof Response ? body : Response.json(body ?? {})
      )
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
  return { router, history }
}

/** Step 0's overlays: a 4 × 6 frame, no peaks or patterns. */
const OVERLAYS: DetectorOverlays = {
  detected: [],
  energy_range_kev: null,
  height: 4,
  width: 6,
  patterns: [],
  simulation_error: null,
  summary: {
    detector_id: null,
    frame_id: 0,
    image: {
      available: true,
      dataset: "entry1/data/data",
      path: "/runs/index_1/image_0.h5",
      reason: null,
    },
    in_scope: true,
    indexed_fraction: 0,
    input_image: "image_0.h5",
    n_detected: 0,
    n_indexed: 0,
    sample_position: [0, 0, 0],
    step: 0,
  },
}

const toastText = () => ({
  title: document.querySelector("[data-sonner-toast] [data-title]")
    ?.textContent,
  description: document.querySelector("[data-sonner-toast] [data-description]")
    ?.textContent,
})

afterEach(() => {
  act(() => {
    toast.dismiss()
  })
  cleanup()
  resetIndexingViewerStore()
  vi.useRealTimers()
  focusManager.setFocused(undefined)
})

describe("the indexing viewer", () => {
  it("is a not-found page for a run the API does not know", async () => {
    await openViewer("/indexing/999")
    await vi.waitFor(() => {
      expect(byRole("heading", "Indexing I999 not found")).toHaveLength(1)
    })
    expect(document.title).toBe("Page not found · 3DMN Portal")
    expect(
      byRole("link", "Go to the indexations")[0]?.getAttribute("href")
    ).toBe("/indexing")
  })

  it.each([
    [
      "of another version",
      () => packViewLink({ v: 2, url: { tab: "map" }, settings: {} }),
      "This link is from another version of the viewer",
    ],
    [
      "that cannot be read",
      () => Promise.resolve("not-a-link"),
      "This link's view cannot be read",
    ],
  ])(
    "ignores a link %s with a toast, removes it with replace and keeps the page's settings",
    async (_case, link, title) => {
      useIndexingViewerStore.getState().setScope({ skipPeaksAtMost: 9 })
      const { router, history } = await openViewer(
        `/indexing/1?tab=pole&link=${await link()}`
      )
      await vi.waitFor(() => {
        expect(toastText().title).toBe(title)
      })
      expect(toastText().description).toMatch(
        /The page opens with your own settings\.$/
      )
      await vi.waitFor(() => {
        expect(router.state.location.href).toBe("/indexing/1?tab=pole")
      })
      expect(history.length).toBe(1)
      expect(useIndexingViewerStore.getState().scope.skipPeaksAtMost).toBe(9)
    }
  )

  it("says there are no results to show in a tab when the run has none (the bar says why)", async () => {
    await openViewer("/indexing/1?tab=map", (where) =>
      where.pathname === "/api/v1/indexing-runs/1/artifact"
        ? Response.json(
            makeArtifact({
              state: "empty",
              results_path: null,
              problems: ["no results file and no XML document found"],
            })
          )
        : undefined
    )
    await vi.waitFor(() => {
      expect(
        [...document.querySelectorAll('[data-slot="empty-title"]')].map(
          (title) => title.textContent
        )
      ).toEqual(["No results to show"])
    })
  })

  it("says so with Retry when the results summary fails, instead of loading for ever", async () => {
    let unreadable = true
    await openViewer("/indexing/1?tab=map", (where) =>
      unreadable && where.pathname === "/api/v1/indexing-runs/1/summary"
        ? Response.json(
            {
              title: "Results unreadable",
              status: 409,
              detail: "output.h5 cannot be read",
            },
            {
              status: 409,
              headers: { "content-type": "application/problem+json" },
            }
          )
        : undefined
    )
    await vi.waitFor(() => {
      expect(document.body.textContent).toContain("Results unreadable")
    })
    expect(
      document.querySelectorAll('aside[aria-label="Color map settings"]')
    ).toHaveLength(0)
    unreadable = false
    await click(byRole("button", "Retry")[0])
    await vi.waitFor(() => {
      expect(
        document.querySelectorAll('aside[aria-label="Color map settings"]')
      ).toHaveLength(1)
    })
    expect(document.body.textContent).not.toContain("Results unreadable")
  })

  it("polls an open live run's artifact every 5 s and reads its replaced results again, the detector's step kept", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    focusManager.setFocused(true)
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: 0, width: 800, height: 600 })
    )
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null)
    let version = 1
    const reads = { artifact: 0, summary: 0 }
    await openViewer("/indexing/1?tab=detector", (where) => {
      const path = where.pathname
      if (path.endsWith("/artifact")) {
        reads.artifact += 1
        return Response.json(
          makeArtifact({
            live_open: true,
            results_version: `v${String(version)}`,
          })
        )
      }
      if (path.endsWith("/summary")) {
        reads.summary += 1
        // The first frames found no pattern (default: the first step); the third did (default: it).
        return Response.json(
          version === 1
            ? makeSummary({ steps: [0, 1], default_step: 0 })
            : makeSummary({ steps: [0, 1, 2], default_step: 2 })
        )
      }
      const step = /\/detector\/(\d+)\/overlays$/.exec(path)
      if (step) {
        const summary = { ...OVERLAYS.summary, step: Number(step[1]) }
        return Response.json({ ...OVERLAYS, summary })
      }
      if (/\/detector\/\d+\/image$/.test(path)) {
        return new Response(
          Uint16Array.from({ length: 24 }, (_, i) => i),
          {
            headers: {
              "content-type": "application/octet-stream",
              "x-frame-shape": "4,6",
              "x-frame-dtype": "uint16",
            },
          }
        )
      }
      return undefined
    })
    const stepShown = () =>
      document.querySelector('[aria-label="Step summary"]')?.textContent
    const tick = async () => {
      await act(() => vi.advanceTimersByTimeAsync(5000))
    }
    await vi.waitFor(() => {
      expect(stepShown()).toContain("Step #0")
    })
    expect(reads).toEqual({ artifact: 1, summary: 1 })

    // Unchanged (the same version): read again, nothing reloaded.
    await tick()
    expect(reads).toEqual({ artifact: 2, summary: 1 })
    // Replaced: the results read again; the step shown stays, though the default moved.
    version = 2
    await tick()
    await vi.waitFor(() => {
      expect(reads).toEqual({ artifact: 3, summary: 2 })
    })
    await tick()
    expect(stepShown()).toContain("Step #0")
    expect(reads.summary).toBe(2)
    // The user steps on to the new default (not written to the URL): it is shown.
    await click(byRole("button", "Next step")[0])
    await vi.waitFor(() => {
      expect(stepShown()).toContain("Step #1")
    })
    await click(byRole("button", "Next step")[0])
    await vi.waitFor(() => {
      expect(stepShown()).toContain("Step #2")
    })
  })

  it("waits out a busy detector image, its step's overlays shown meanwhile", async () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: 0, width: 800, height: 600 })
    )
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null)
    const step = "/api/v1/indexing-runs/1/detector/0"
    const images: string[] = []
    await openViewer("/indexing/1?tab=detector", (where) => {
      if (where.pathname === `${step}/overlays`) return Response.json(OVERLAYS)
      if (where.pathname !== `${step}/image`) return undefined
      images.push(where.pathname)
      if (images.length === 1) {
        return Response.json(
          {
            title: "Too many detector images waiting",
            status: 503,
            detail: "34 detector images are already waiting.",
          },
          {
            status: 503,
            headers: {
              "content-type": "application/problem+json",
              "retry-after": "1",
            },
          }
        )
      }
      return new Response(
        Uint16Array.from({ length: 24 }, (_, i) => i),
        {
          headers: {
            "content-type": "application/octet-stream",
            "x-frame-shape": "4,6",
            "x-frame-dtype": "uint16",
            etag: '"image-0"',
          },
        }
      )
    })
    const plotStatus = () =>
      document.querySelector('[aria-busy="true"] > p[role="status"]')
    const colourScale = () =>
      document.querySelector('section[aria-label="Colour scale"]')

    await vi.waitFor(() => {
      expect(plotStatus()?.textContent).toBe("Busy: retrying in 1 s")
    })
    expect(
      document.querySelector('[aria-label="Step summary"]')?.textContent
    ).toContain("Step #0")
    expect(colourScale()).toBeNull()
    await vi.waitFor(
      () => {
        expect(colourScale()).not.toBeNull()
      },
      { timeout: 3000 }
    )
    expect(plotStatus()).toBeNull()
    expect(byRole("alert")).toEqual([])
    expect(images).toHaveLength(2)
  })
})
