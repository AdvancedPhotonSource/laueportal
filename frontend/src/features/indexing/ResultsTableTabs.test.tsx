import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { PeakPage, PeakRow } from "@/api"
import { headerLabels, rowIds, stubGridLayout } from "@/test/grid"
import { resetIndexingViewerStore } from "@/test/indexing"
import { byRole, cleanup, click, render, SLOW_TEST } from "@/test/render"
import { writeColumnState } from "@/components/DataGrid/column-state"
import { reloadResults } from "./artifact-bar"
import { PEAKS_GRID_ID } from "./results-tables"
import { PeaksTab } from "./ResultsTableTabs"
import { useIndexingViewerStore } from "./viewer-store"

// The Peaks tab with AG Grid in jsdom (the Patterns tab is the same component on its own list):
// the heading's total, the checklist's groups, and a reload of the run's results.

function peak(id: number): PeakRow {
  return {
    id,
    step: id,
    frame_id: `frame_${String(id)}`,
    pattern_index: 0,
    h: 1,
    k: 1,
    l: 1,
    peak_index: id,
    x_pixel: 10.5,
    y_pixel: 20.25,
    intensity: 1000 + id,
    integral: 5,
    qx: 0.1,
    qy: 0.2,
    qz: 0.3,
    rms_error_deg: 0.01,
    goodness: 50,
    n_peaks: 30,
    energy_kev: 12,
    error_deg: 0.02,
    predicted_intensity: 0.5,
    input_image: null,
    hwhm_x: 1,
    hwhm_y: 1,
    tilt: 0,
    chisq: 1,
    background: 3,
    pattern_n_indexed: 20,
    pattern_indexed_fraction: 2 / 3,
  }
}

const ROWS = [peak(0), peak(1), peak(2)]
/** The rows of a scope that keeps every step (min_peaks 0). */
const ALL_ROWS = [...ROWS, peak(3), peak(4)]

/** The peaks lists asked for (each request's URL). */
let asked: URL[] = []

async function openPeaks(indexingId = 1) {
  asked = []
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const where = new URL(
        input instanceof Request ? input.url : input.toString(),
        "http://localhost"
      )
      asked.push(where)
      const rows = where.searchParams.get("min_peaks") === "0" ? ALL_ROWS : ROWS
      const offset = Number(where.searchParams.get("offset") ?? 0)
      const limit = Number(where.searchParams.get("limit") ?? 100)
      const page: PeakPage = {
        rows: rows.slice(offset, offset + limit),
        total: rows.length,
        as_of: "2026-10-21T12:00:00Z",
      }
      return Promise.resolve(Response.json(page))
    })
  )
  const queryClient = new QueryClient()
  const container = await render(
    <QueryClientProvider client={queryClient}>
      <PeaksTab indexingId={indexingId} />
    </QueryClientProvider>
  )
  return { container, queryClient }
}

/** The checklist's box for a column. */
function columnBox(label: string): HTMLElement {
  const row = Array.from(document.querySelectorAll("aside label")).find(
    (element) => element.textContent.trim() === label
  )
  const box = row?.querySelector<HTMLElement>('[role="checkbox"]')
  if (!box) throw new Error(`no checklist box for ${label}`)
  return box
}

/** The grid's block requests (100 rows), not the one-row total. */
const blockRequests = () =>
  asked.filter((url) => url.searchParams.get("limit") === "100")

beforeEach(() => {
  stubGridLayout()
  localStorage.clear()
})

afterEach(() => {
  cleanup()
  resetIndexingViewerStore()
  localStorage.clear()
})

describe("the Peaks tab", () => {
  it("heads the grid with the scope's total, asking both within the applied scope, again when it changes", async () => {
    const { container } = await openPeaks()
    await vi.waitFor(() => {
      expect(byRole("heading", "Indexed Peaks (3 total)")).toHaveLength(1)
      expect(blockRequests()).toHaveLength(1)
    })
    for (const url of asked) {
      expect(url.pathname).toBe("/api/v1/indexing-runs/1/peaks")
      expect(url.searchParams.get("pattern0_only")).toBe("true")
      expect(url.searchParams.get("min_peaks")).toBe("4")
    }

    // Another scope (keep every step): the grid and the heading's total read it.
    asked = []
    act(() => {
      useIndexingViewerStore.getState().setScope({ skipPeaksAtMost: 0 })
    })
    await vi.waitFor(() => {
      expect(byRole("heading", "Indexed Peaks (5 total)")).toHaveLength(1)
      expect(rowIds(container)).toEqual(["0", "1", "2", "3", "4"])
    })
    expect(blockRequests()).toHaveLength(1)
    expect(
      asked.filter((url) => url.searchParams.get("limit") === "1")
    ).toHaveLength(1)
    for (const url of asked) {
      expect(url.searchParams.get("min_peaks")).toBe("0")
    }
  })

  it(
    "shows and hides the groups' columns from the checklist, and opens with the kept choice",
    SLOW_TEST,
    async () => {
      const { container } = await openPeaks()
      // The checklist follows the grid's columns once it is made; choose once the first rows show.
      await vi.waitFor(() => {
        expect(rowIds(container)).not.toHaveLength(0)
      })
      await vi.waitFor(() => {
        expect(columnBox("Goodness").getAttribute("aria-checked")).toBe("true")
      })
      expect(columnBox("Frame").getAttribute("aria-checked")).toBe("false")
      expect(headerLabels(container)).not.toContain("Frame")
      await click(columnBox("Frame")) // Geometry
      await click(columnBox("Goodness")) // Default
      await vi.waitFor(() => {
        expect(columnBox("Goodness").getAttribute("aria-checked")).toBe("false")
      })
      expect(columnBox("Frame").getAttribute("aria-checked")).toBe("true")
      expect(headerLabels(container)).toContain("Frame")
      expect(headerLabels(container)).not.toContain("Goodness")
      // Opened with a kept column state (DataGrid.test.tsx checks that a choice is kept): the Peaks
      // grid reads its own, and the checklist follows it.
      cleanup()
      localStorage.clear() // only the seeded state: the clicks above may have been stored
      writeColumnState(localStorage, PEAKS_GRID_ID, {
        columnVisibility: { hiddenColIds: ["goodness"] },
      })
      const again = await openPeaks()
      await vi.waitFor(
        () => {
          expect(columnBox("Frame").getAttribute("aria-checked")).toBe("true")
        },
        { timeout: 5000 }
      )
      expect(headerLabels(again.container)).toContain("Frame")
      expect(headerLabels(again.container)).not.toContain("Goodness")
    }
  )

  it("loads its rows again when the run's results are read again (reloadResults), not another run's", async () => {
    const { queryClient } = await openPeaks()
    await vi.waitFor(() => {
      expect(blockRequests()).toHaveLength(1)
    })
    await act(() => reloadResults(queryClient, 2))
    expect(blockRequests()).toHaveLength(1)
    await act(() => reloadResults(queryClient, 1))
    await vi.waitFor(() => {
      expect(blockRequests()).toHaveLength(2)
    })
  })
})
