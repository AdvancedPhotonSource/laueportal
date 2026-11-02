import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { RunStatus } from "@/api"
import { gridCell, press, rowIds, stubGridLayout } from "@/test/grid"
import { cleanup, renderWithRouter } from "@/test/render"
import type { GridColumn } from "./columns"
import { DataGrid } from "./DataGrid"
import type { GridQuery } from "./datasource"

// Link cells from the keyboard (WCAG 2.1.1), in jsdom with AG Grid itself: Enter on a cell follows
// its link, Tab and Shift+Tab reach each link of a cell with two.

interface Run {
  id: number
  scan_number: number | null
  reconstruction_id: number | null
  status: RunStatus
  author: string
  n_inputs: number
  n_succeeded: number
  n_failed: number
  n_not_run: number
}

const COLUMNS: GridColumn<Run>[] = [
  { field: "id", header: "ID", type: "link", link: "indexing" },
  { field: "scan_number", header: "Source", type: "link", link: "source" },
  { field: "status", header: "Status", type: "status" },
  { field: "author", header: "Author", type: "text" },
]

function run(id: number): Run {
  return {
    id,
    scan_number: 5,
    reconstruction_id: 3,
    status: "finished",
    author: "ann",
    n_inputs: 4,
    n_succeeded: 4,
    n_failed: 0,
    n_not_run: 0,
  }
}

async function openGrid(rows: Run[]) {
  const fetchRows = vi.fn((query: GridQuery) =>
    Promise.resolve({
      rows: rows.slice(query.offset, query.offset + query.limit),
      total: rows.length,
      as_of: "2026-10-19T12:00:00Z",
    })
  )
  const container = await renderWithRouter(
    <DataGrid
      gridId="runs"
      columns={COLUMNS}
      fetchRows={fetchRows}
      getRowId={(row) => String(row.id)}
    />
  )
  await vi.waitFor(() => {
    expect(rowIds(container)).toEqual(rows.map((row) => String(row.id)))
  })
  return container
}

/** The focused element: a link (its href) or a cell (its column). */
function focused(): string {
  const element = document.activeElement
  if (!element) return "nothing"
  const href = element.getAttribute("href")
  if (href) return `link ${href}`
  const cell = element.closest(".ag-cell")
  return cell ? `cell ${cell.getAttribute("col-id") ?? ""}` : element.tagName
}

beforeEach(stubGridLayout)

afterEach(cleanup)

describe("link cells from the keyboard", () => {
  it("Enter on a cell follows its link", async () => {
    const container = await openGrid([run(12)])
    const followed: string[] = []
    // Record the links followed, and stay on the grid (the router would leave it).
    const record = (event: MouseEvent) => {
      const link = (event.target as Element).closest("a")
      if (!link) return
      followed.push(link.getAttribute("href") ?? "")
      event.preventDefault()
    }
    document.addEventListener("click", record, true)
    const cell = gridCell(container, "12", "id")
    cell.focus()
    await press(cell, "Enter")
    // On a two-link cell, Enter follows the first.
    const source = gridCell(container, "12", "scan_number")
    source.focus()
    await press(source, "Enter")
    document.removeEventListener("click", record, true)
    expect(followed).toEqual(["/indexing/12", "/scans/5"])
  })

  it("Tab reaches each link of a cell with two, then the next cell; Shift+Tab comes back", async () => {
    const container = await openGrid([run(12)])
    const source = gridCell(container, "12", "scan_number")
    source.focus()
    const steps: string[] = []
    for (const shiftKey of [false, false, false, true, true, true, true]) {
      await press(document.activeElement, "Tab", { shiftKey })
      steps.push(focused())
    }
    expect(steps).toEqual([
      "link /scans/5",
      "link /reconstructions/3",
      "cell status",
      // Back: into the source cell, then through its links in reverse, then the previous cell.
      "cell scan_number",
      "link /reconstructions/3",
      "link /scans/5",
      "cell id",
    ])
  })
})
