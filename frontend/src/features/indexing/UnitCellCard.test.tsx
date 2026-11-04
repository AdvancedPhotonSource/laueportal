import { QueryClientProvider } from "@tanstack/react-query"
import { act } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { PointDetails } from "@/api"
import { createQueryClient } from "@/app/query-client"
import { ThemeProvider } from "@/app/ThemeProvider"
import {
  makePatternDetails,
  makePointDetails,
  NI_RECIPROCAL as K,
  resetIndexingViewerStore,
} from "@/test/indexing"
import { byRole, cleanup, click, render, stubMatchMedia } from "@/test/render"
import { UnitCellCard } from "./UnitCellCard"
import { useIndexingViewerStore } from "./viewer-store"

const REFERENCE = [
  [K, 0, 0],
  [0, K, 0],
  [0, 0, K],
]

/** The card for run I1, the points API answering `points` by step (any other step is a 404). */
async function renderCard(
  step: number | undefined,
  points: Record<number, PointDetails> = {}
) {
  stubMatchMedia()
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null)
  const requests: string[] = []
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const where = new URL(
        input instanceof Request ? input.url : input.toString(),
        "http://localhost"
      )
      requests.push(where.pathname)
      const found = /\/points\/(\d+)$/.exec(where.pathname)
      const point = found ? points[Number(found[1])] : undefined
      return Promise.resolve(
        point
          ? Response.json(point)
          : Response.json(
              { type: "about:blank", title: "Not Found", status: 404 },
              {
                status: 404,
                headers: { "Content-Type": "application/problem+json" },
              }
            )
      )
    })
  )
  await render(
    <ThemeProvider>
      <QueryClientProvider client={createQueryClient()}>
        <UnitCellCard
          indexingId={1}
          step={step}
          patternIndex={null}
          axes={["X", "Y"]}
          scope={{ pattern0_only: true, min_peaks: 3 }}
        />
      </QueryClientProvider>
    </ThemeProvider>
  )
  return requests
}

async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0))
  })
}

afterEach(() => {
  cleanup()
  resetIndexingViewerStore()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("UnitCellCard", () => {
  it("shows one line and asks for nothing before a click", async () => {
    const requests = await renderCard(undefined)
    expect(document.body.textContent).toContain(
      "Click a point to show its unit cell."
    )
    expect(requests.filter((path) => path.includes("/points/"))).toEqual([])
  })

  it("shows the clicked point's cell and lattice parameters", async () => {
    await renderCard(3, {
      3: makePointDetails([makePatternDetails(0, REFERENCE)]),
    })
    await settle()
    expect(byRole("img", /unit cell of step 3, pattern 0/)).toHaveLength(1)
    const list = byRole("region", "Crystal orientation")[0]
    expect(list?.textContent).toContain("Ni · SG 225")
    expect(list?.textContent).toContain("0.3524 0.3524 0.3524")
    expect(list?.textContent).toContain("0.00°")
    // the pattern's cell is the nominal one: one row of lengths, no "Nominal"
    expect(list?.textContent).not.toContain("Nominal")
  })

  it("says so for a step without an oriented pattern, or one not in the results", async () => {
    await renderCard(3, { 3: makePointDetails([makePatternDetails(0, null)]) })
    await settle()
    expect(document.body.textContent).toContain("Step 3: not indexed.")
    cleanup()
    await renderCard(9)
    await settle()
    expect(document.body.textContent).toContain(
      "Step 9 is not in these results."
    )
  })

  it("chooses among the step's oriented patterns", async () => {
    await renderCard(3, {
      3: makePointDetails([
        makePatternDetails(0, REFERENCE),
        makePatternDetails(1, REFERENCE),
      ]),
    })
    await settle()
    await click(byRole("button", "1")[0])
    expect(byRole("img", /pattern 1/)).toHaveLength(1)
  })

  it("collapses to a strip and opens again, the choice stored", async () => {
    await renderCard(undefined)
    await click(byRole("button", "Collapse the crystal orientation")[0])
    expect(byRole("region", "Crystal orientation")).toHaveLength(0)
    expect(useIndexingViewerStore.getState().map.cellCollapsed).toBe(true)
    await click(byRole("button", "Show the crystal orientation")[0])
    expect(byRole("region", "Crystal orientation")).toHaveLength(1)
    expect(useIndexingViewerStore.getState().map.cellCollapsed).toBe(false)
  })

  it("stores the beam vector's switch", async () => {
    await renderCard(undefined)
    await click(byRole("button", "Beam")[0])
    expect(useIndexingViewerStore.getState().map.cellBeam).toBe(false)
  })
})
