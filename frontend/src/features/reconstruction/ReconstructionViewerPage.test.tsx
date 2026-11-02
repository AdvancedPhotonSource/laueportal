import { focusManager } from "@tanstack/react-query"
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router"
import { act } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { Health, ReconstructionArtifact, ReconstructionPoint } from "@/api"
import { Providers } from "@/app/Providers"
import { createQueryClient } from "@/app/query-client"
import { createAppRouter } from "@/app/router"
import { checkRow, rowCheckbox, rowIds, stubGridLayout } from "@/test/grid"
import {
  answerReconstruction,
  makePoint,
  makePoints,
  makeReconstructionArtifact,
  makeReconstructionRunDetail,
} from "@/test/reconstruction"
import {
  byRole,
  cleanup,
  click,
  render,
  SLOW_TEST,
  stubMatchMedia,
} from "@/test/render"
import { packReconstructionView } from "./viewer-link"
import { DEFAULT_SETTINGS } from "./viewer-store"

// The wire viewer in the app's router, the API answering run R1 (any other run is a 404).

const HEALTH: Health = {
  db: "ok",
  worker: "running",
  version: "2.0.0.dev0",
}

async function openViewer(
  url: string,
  {
    artifact = () => makeReconstructionArtifact(),
    points = makePoints,
  }: {
    artifact?: () => ReconstructionArtifact | Response
    points?: () => ReconstructionPoint[]
  } = {}
) {
  stubMatchMedia()
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined)
  const fetch = vi.fn((input: RequestInfo | URL) => {
    const where = new URL(
      input instanceof Request ? input.url : input.toString(),
      "http://localhost"
    )
    if (where.pathname === "/api/v1/health") {
      return Promise.resolve(Response.json(HEALTH))
    }
    const body = answerReconstruction(where, {
      artifact,
      points,
      runs: (id) => (id === 1 ? makeReconstructionRunDetail() : undefined),
    })
    return Promise.resolve(
      body instanceof Response ? body : Response.json(body ?? {})
    )
  })
  vi.stubGlobal("fetch", fetch)
  const queryClient = createQueryClient()
  const history = createMemoryHistory({ initialEntries: [url] })
  const router = createAppRouter(queryClient, history)
  const root = await render(
    <Providers queryClient={queryClient}>
      <RouterProvider router={router} />
    </Providers>
  )
  await act(() => router.load())
  /** How many times the page asked for a path ending so (`/artifact`, `/points`). */
  const reads = (suffix: string) =>
    fetch.mock.calls.filter(([input]) =>
      (input instanceof Request ? input.url : input.toString()).endsWith(suffix)
    ).length
  return { router, history, root, reads }
}

const bar = () => document.querySelector('[aria-label="Reconstruction"]')
const status = () => bar()?.querySelector('[role="status"]')

beforeEach(stubGridLayout)

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  focusManager.setFocused(undefined)
  localStorage.clear()
})

describe("the wire reconstruction viewer", () => {
  it("is a not-found page for a run the API does not know", async () => {
    await openViewer("/reconstructions/999")
    await vi.waitFor(() => {
      expect(byRole("heading", "Reconstruction R999 not found")).toHaveLength(1)
    })
    expect(document.title).toBe("Page not found · 3DMN Portal")
    expect(
      byRole("link", "Go to the reconstructions")[0]?.getAttribute("href")
    ).toBe("/reconstructions")
  })

  it("shows an unreadable reconstruction as the bar's status, and its error in the Points tab, naming no point", async () => {
    // The URL's point cannot load: the bar does not name it.
    await openViewer("/reconstructions/1?tab=points&point=foo", {
      artifact: () =>
        Response.json(
          {
            title: "Reconstruction unreadable",
            status: 409,
            detail: "scan.h5 cannot be read: 'OSError: truncated'",
          },
          {
            status: 409,
            headers: { "content-type": "application/problem+json" },
          }
        ),
    })
    await vi.waitFor(() => {
      expect(status()?.textContent).toBe("Reconstruction unreadable")
    })
    expect(status()?.getAttribute("title")).toBe(
      "scan.h5 cannot be read: 'OSError: truncated'"
    )
    expect(bar()?.textContent).toContain("No point selected")
    expect(byRole("button", "Retry")).toHaveLength(1)
  })

  it("says a run's directory is not on this host in the bar and every point tab (a migrated run on another host)", async () => {
    const { router } = await openViewer("/reconstructions/1?tab=points", {
      artifact: () =>
        makeReconstructionArtifact({
          state: "not_on_host",
          format: null,
          path: null,
          points_complete: null,
          points_total: null,
        }),
    })
    await vi.waitFor(() => {
      expect(status()?.textContent).toBe("Not on this host")
    })
    expect(status()?.getAttribute("title")).toBe(
      "Run directory not on this host"
    )
    for (const tab of ["points", "depth", "roi"] as const) {
      await act(() => router.navigate({ to: ".", search: { tab } }))
      await vi.waitFor(() => {
        expect(
          [...document.querySelectorAll('[data-slot="empty-title"]')].map(
            (title) => title.textContent
          )
        ).toEqual(["Run directory not on this host"])
      })
    }
  })

  it(
    "selects the first complete point, and a checked point is the page's and the URL's",
    SLOW_TEST,
    async () => {
      const { router, history, root } = await openViewer(
        "/reconstructions/1?tab=points"
      )
      // The grid starts slowly in jsdom when the suite runs in parallel.
      await vi.waitFor(
        () => {
          expect(rowIds(root)).toEqual(["wire_1", "wire_2", "wire_3"])
        },
        { timeout: 5000 }
      )
      expect(bar()?.textContent).toContain("Point wire_1")
      expect(status()?.textContent).toBe("2 of 3 points complete")
      await vi.waitFor(() => {
        expect(rowCheckbox(root, "wire_1").checked).toBe(true)
      })
      // Not complete: not selectable, its explanation on its status.
      expect(rowCheckbox(root, "wire_2").disabled).toBe(true)
      expect(
        root
          .querySelector('[row-id="wire_2"] [data-status="pending"]')
          ?.getAttribute("title")
      ).toMatch(/^Point wire_2 has not been reconstructed yet/)
      const entries = history.length

      await checkRow(root, "wire_3", () =>
        history.location.search.includes("point=wire_3")
      )
      await vi.waitFor(() => {
        expect(bar()?.textContent).toContain("Point wire_3")
      })
      await vi.waitFor(() => {
        expect(router.state.location.href).toBe(
          "/reconstructions/1?tab=points&point=wire_3"
        )
      })
      // A point is a history entry, as a tab.
      expect(history.length).toBe(entries + 1)

      // Unchecking the point keeps it.
      await click(rowCheckbox(root, "wire_3"))
      await vi.waitFor(() => {
        expect(rowCheckbox(root, "wire_3").checked).toBe(true)
      })
      expect(bar()?.textContent).toContain("Point wire_3")
    }
  )

  it("reads the points once when Refresh finds a changed artifact, and once when it does not", async () => {
    let complete = 1
    const { root, reads } = await openViewer("/reconstructions/1?tab=points", {
      artifact: () =>
        makeReconstructionArtifact({
          points_complete: complete,
          points_total: 2,
        }),
      points: () =>
        complete === 1
          ? [makePoint()]
          : [makePoint(), makePoint({ point_id: "wire_2", index: 1 })],
    })
    await vi.waitFor(() => {
      expect(rowIds(root)).toEqual(["wire_1"])
    })
    expect(reads("/points")).toBe(1)
    complete = 2
    await click(byRole("button", "Refresh")[0])
    await vi.waitFor(() => {
      expect(rowIds(root)).toEqual(["wire_1", "wire_2"])
    })
    expect(status()?.textContent).toBe("2 of 2 points complete")
    // Unchanged: the button reads them itself (their sources' states may have changed).
    await click(byRole("button", "Refresh")[0])
    await vi.waitFor(() => {
      expect(reads("/artifact")).toBe(3)
    })
    await vi.waitFor(() => {
      expect(reads("/points")).toBe(3)
    })
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(reads("/points")).toBe(3)
  })

  it("polls a running run every 5 s, reads the points only when the artifact changes, and stops once it ends", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    focusManager.setFocused(true)
    const running = (complete: number) =>
      makeReconstructionArtifact({
        run_status: "running",
        points_complete: complete,
        points_total: 3,
      })
    const answers = [
      running(1),
      running(1),
      running(1),
      running(2),
      makeReconstructionArtifact({ points_complete: 3, points_total: 3 }),
    ]
    let read = 0
    const { reads } = await openViewer("/reconstructions/1?tab=points", {
      artifact: () =>
        answers[Math.min(read++, answers.length - 1)] ?? running(1),
    })
    const tick = async () => {
      await act(() => vi.advanceTimersByTimeAsync(5000))
    }
    await vi.waitFor(() => {
      expect(reads("/points")).toBe(1)
    })
    // Unchanged for two polls: no points read.
    await tick()
    await tick()
    expect([reads("/artifact"), reads("/points")]).toEqual([3, 1])
    // Changed: one.
    await tick()
    await vi.waitFor(() => {
      expect([reads("/artifact"), reads("/points")]).toEqual([4, 2])
    })
    // Finished: the points once more, then no more polls.
    await tick()
    await vi.waitFor(() => {
      expect([reads("/artifact"), reads("/points")]).toEqual([5, 3])
    })
    await tick()
    await tick()
    expect([reads("/artifact"), reads("/points")]).toEqual([5, 3])
  })

  it("writes the point it shows to the URL, with replace, when points complete out of order", async () => {
    let phase = 1
    const { router, history } = await openViewer(
      "/reconstructions/1?tab=points",
      {
        artifact: () =>
          makeReconstructionArtifact({
            run_status: "running",
            points_complete: phase,
            points_total: 2,
          }),
        points: () => [
          phase === 1
            ? makePoint({
                point_id: "p1",
                status: "writing",
                explanation: "Point p1 is being written.",
                n_depths: null,
                frame_shape: null,
              })
            : makePoint({ point_id: "p1" }),
          makePoint({ point_id: "p2", index: 1 }),
        ],
      }
    )
    // p2 completes first: the page shows it, the URL's default (a fresh load picks it too).
    await vi.waitFor(() => {
      expect(bar()?.textContent).toContain("Point p2")
    })
    expect(router.state.location.href).toBe("/reconstructions/1?tab=points")
    const entries = history.length
    // Then p1: a fresh load would pick p1, so the URL names p2, the point shown.
    phase = 2
    await click(byRole("button", "Refresh")[0])
    await vi.waitFor(() => {
      expect(status()?.textContent).toBe("2 of 2 points complete")
    })
    await vi.waitFor(() => {
      expect(router.state.location.href).toBe(
        "/reconstructions/1?tab=points&point=p2"
      )
    })
    expect(bar()?.textContent).toContain("Point p2")
    expect(history.length).toBe(entries)
  })

  it(
    "follows the newest complete point of an open live run with replace, until the user picks one; no switch once it has ended",
    SLOW_TEST,
    async () => {
      let phase = 1
      const point = (id: string, index: number, complete: boolean) =>
        complete
          ? makePoint({ point_id: id, index })
          : makePoint({
              point_id: id,
              index,
              status: "pending",
              explanation: `Point ${id} has not been reconstructed yet.`,
              n_depths: null,
              frame_shape: null,
            })
      const { history, root } = await openViewer(
        "/reconstructions/1?tab=points&depth=7",
        {
          artifact: () =>
            makeReconstructionArtifact({
              run_status: phase < 4 ? "running" : "finished",
              live_open: phase < 4,
              points_complete: Math.min(phase, 3),
              points_total: 3,
            }),
          // Arrival order: w3 arrived before w2; w2 completes last.
          points: () => [
            point("w1", 0, true),
            point("w3", 1, phase >= 2),
            point("w2", 2, phase >= 3),
          ],
        }
      )
      const follow = () => byRole("switch", "Follow newest point")[0]
      await vi.waitFor(() => {
        expect(bar()?.textContent).toContain("Point w1")
      })
      expect(follow()?.hasAttribute("data-checked")).toBe(false)
      await click(follow())
      const entries = history.length

      // A new complete point: selected, written with replace, at its brightest depth.
      phase = 2
      await click(byRole("button", "Refresh")[0])
      await vi.waitFor(() => {
        expect(bar()?.textContent).toContain("Point w3")
      })
      await vi.waitFor(() => {
        expect(history.location.search).toBe("?tab=points&point=w3")
      })
      phase = 3
      await click(byRole("button", "Refresh")[0])
      await vi.waitFor(() => {
        expect(bar()?.textContent).toContain("Point w2")
      })
      expect(history.length).toBe(entries)

      // The user's pick: pushed, and following is off.
      await checkRow(
        root,
        "w1",
        () => !history.location.search.includes("point=w")
      )
      await vi.waitFor(() => {
        expect(follow()?.hasAttribute("data-checked")).toBe(false)
      })
      expect(history.length).toBe(entries + 1)
      expect(bar()?.textContent).toContain("Point w1")

      // Ended: nothing more arrives, no switch.
      phase = 4
      await click(byRole("button", "Refresh")[0])
      await vi.waitFor(() => {
        expect(follow()).toBeUndefined()
      })
      expect(bar()?.textContent).toContain("Point w1")
    }
  )

  it("has no Follow newest point for a batch run", async () => {
    await openViewer("/reconstructions/1", {
      artifact: () => makeReconstructionArtifact({ run_status: "running" }),
    })
    await vi.waitFor(() => {
      expect(status()?.textContent).toBe("2 of 3 points complete")
    })
    expect(byRole("switch", "Follow newest point")).toEqual([])
  })

  it("opens a copied link's view, then removes the link with replace", async () => {
    const packed = await packReconstructionView(
      { tab: "points", point: "wire_3" },
      DEFAULT_SETTINGS
    )
    const { router, history } = await openViewer(
      `/reconstructions/1?link=${packed}`
    )
    await vi.waitFor(() => {
      expect(router.state.location.href).toBe(
        "/reconstructions/1?tab=points&point=wire_3"
      )
    })
    expect(history.length).toBe(1)
    await vi.waitFor(() => {
      expect(bar()?.textContent).toContain("Point wire_3")
    })
  })
})
