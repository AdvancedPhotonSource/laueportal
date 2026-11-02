import { getGridApi } from "ag-grid-community"
import { act, createRef } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { ApiError, type RunStatus } from "@/api"
import {
  headerLabels,
  rowCheckbox as checkbox,
  rowIds,
  rowText,
  scrollToRow,
  stubGridLayout,
} from "@/test/grid"
import { byRole, cleanup, click, renderWithRouter } from "@/test/render"
import { columnStateKey } from "./column-state"
import type { GridColumn } from "./columns"
import { DataGrid, type DataGridHandle } from "./DataGrid"
import type { GridPage, GridQuery } from "./datasource"

// The DataGrid with AG Grid itself, in jsdom (src/test/grid.ts gives its elements a size).

interface Job {
  job_id: number
  status: RunStatus
  n_inputs: number
  n_succeeded: number
  n_failed: number
  n_not_run: number
}

const COLUMNS: GridColumn<Job>[] = [
  {
    field: "job_id",
    header: "Job ID",
    type: "link",
    link: "job",
    initialSort: "desc",
  },
  { field: "status", header: "Status", type: "status" },
  { field: "n_inputs", header: "Inputs", type: "number" },
]

const AS_OF = "2026-10-19T12:00:00Z"

const finishedOnly = (row: Job) => row.status === "finished"

function job(job_id: number, status: RunStatus, n_succeeded = 1): Job {
  return {
    job_id,
    status,
    n_inputs: 4,
    n_succeeded,
    n_failed: 0,
    n_not_run: 0,
  }
}

/** A list endpoint over `table()`, sorted by job_id, recording every query. */
function server(table: () => Job[]) {
  return vi.fn((query: GridQuery): Promise<GridPage<Job>> => {
    const rows = [...table()].sort((a, b) =>
      query.sort === "job_id" ? a.job_id - b.job_id : b.job_id - a.job_id
    )
    return Promise.resolve({
      rows: rows.slice(query.offset, query.offset + query.limit),
      total: rows.length,
      as_of: AS_OF,
    })
  })
}

beforeEach(() => {
  stubGridLayout()
  localStorage.clear()
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  localStorage.clear()
})

describe("DataGrid", () => {
  it("loads its rows through the grid query protocol, sorted as the columns ask", async () => {
    const fetchRows = server(() => [1, 2, 3].map((id) => job(id, "finished")))
    const container = await renderWithRouter(
      <DataGrid
        gridId="jobs"
        columns={COLUMNS}
        fetchRows={fetchRows}
        getRowId={(row) => String(row.job_id)}
      />
    )
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["3", "2", "1"])
    })
    expect(fetchRows).toHaveBeenCalledWith(
      { offset: 0, limit: 100, sort: "-job_id" },
      expect.any(AbortSignal)
    )
    expect(rowText(container, "3")).toContain("Finished")
    // The job ID is a link to its page.
    expect(
      container.querySelector('[row-id="3"] a')?.getAttribute("href")
    ).toBe("/jobs/3")
  })

  it("keeps the selection by row ID when the rows are sorted again", async () => {
    const fetchRows = server(() => [1, 2, 3].map((id) => job(id, "finished")))
    const onSelectionChange = vi.fn()
    const container = await renderWithRouter(
      <DataGrid
        gridId="jobs"
        columns={COLUMNS}
        fetchRows={fetchRows}
        getRowId={(row) => String(row.job_id)}
        selection="multiRow"
        onSelectionChange={onSelectionChange}
      />
    )
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["3", "2", "1"])
    })
    await click(checkbox(container, "2"))
    await vi.waitFor(() => {
      expect(onSelectionChange).toHaveBeenLastCalledWith([job(2, "finished")])
    })

    // Sorting reloads every row from the server (new row nodes); the selection stays. AG Grid's
    // cycle goes from descending to unsorted (the server's order), then to ascending.
    const header = container.querySelector(
      '[col-id="job_id"] .ag-header-cell-label'
    )
    await click(header)
    await vi.waitFor(() => {
      expect(fetchRows).toHaveBeenLastCalledWith(
        { offset: 0, limit: 100 },
        expect.any(AbortSignal)
      )
    })
    await click(header)
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["1", "2", "3"])
    })
    expect(fetchRows).toHaveBeenLastCalledWith(
      {
        offset: 0,
        limit: 100,
        sort: "job_id",
      },
      expect.any(AbortSignal)
    )
    expect(checkbox(container, "2").checked).toBe(true)
    expect(checkbox(container, "1").checked).toBe(false)
    expect(onSelectionChange).toHaveBeenLastCalledWith([job(2, "finished")])
  })

  it("with singleRow selection, checking a row unchecks the other", async () => {
    const fetchRows = server(() => [1, 2, 3].map((id) => job(id, "finished")))
    const onSelectionChange = vi.fn()
    const container = await renderWithRouter(
      <DataGrid
        gridId="jobs"
        columns={COLUMNS}
        fetchRows={fetchRows}
        getRowId={(row) => String(row.job_id)}
        selection="singleRow"
        onSelectionChange={onSelectionChange}
      />
    )
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["3", "2", "1"])
    })
    await click(checkbox(container, "2"))
    await click(checkbox(container, "3"))
    await vi.waitFor(() => {
      expect(onSelectionChange).toHaveBeenLastCalledWith([job(3, "finished")])
    })
    expect(checkbox(container, "2").checked).toBe(false)
    expect(checkbox(container, "3").checked).toBe(true)
  })

  it("with rows, sorts them itself; only the selectable ones can be selected, also by selectRows", async () => {
    const rows = [1, 3, 2, 4].map((id) =>
      job(id, id % 2 === 0 ? "finished" : "failed")
    )
    const grid = createRef<DataGridHandle>()
    const onSelectionChange = vi.fn()
    const onRowCount = vi.fn()
    const container = await renderWithRouter(
      <DataGrid
        ref={grid}
        gridId="client-jobs"
        columns={COLUMNS}
        rows={rows}
        getRowId={(row) => String(row.job_id)}
        selection="multiRow"
        isRowSelectable={finishedOnly}
        onSelectionChange={onSelectionChange}
        onRowCount={onRowCount}
      />
    )
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["4", "3", "2", "1"])
    })
    expect(onRowCount).toHaveBeenLastCalledWith({ total: 4, filtered: false })
    expect(checkbox(container, "3").disabled).toBe(true)
    expect(checkbox(container, "4").disabled).toBe(false)

    // Every row is held, so a page selects by ID; a row that cannot be selected stays unselected.
    act(() => {
      grid.current?.selectRows(["2", "3", "4"])
    })
    await vi.waitFor(() => {
      expect(onSelectionChange).toHaveBeenLastCalledWith([
        job(2, "finished"),
        job(4, "finished"),
      ])
    })
    act(() => {
      grid.current?.selectRows(["4"])
    })
    await vi.waitFor(() => {
      expect(onSelectionChange).toHaveBeenLastCalledWith([job(4, "finished")])
    })
    expect(checkbox(container, "4").checked).toBe(true)
    expect(checkbox(container, "2").checked).toBe(false)
  })

  it("with rows, filters a date by its local day and ranges with their ends; the header box selects the filtered selectable rows", async () => {
    interface Scan {
      id: number
      time: string
      n: number
      status: string
    }
    const at = (day: number, hour: number) =>
      new Date(2022, 3, day, hour).toISOString()
    const scans: Scan[] = [
      { id: 1, time: at(19, 23), n: 1, status: "new" },
      { id: 2, time: at(20, 0), n: 2, status: "new" },
      { id: 3, time: at(20, 15), n: 3, status: "exists" },
      { id: 4, time: at(21, 9), n: 4, status: "new" },
    ]
    const columns: GridColumn<Scan>[] = [
      { field: "id", header: "ID", type: "number" },
      { field: "time", header: "Date", type: "date", initialSort: "asc" },
      { field: "n", header: "N", type: "number" },
      { field: "status", header: "Status", type: "text" },
    ]
    const onSelectionChange = vi.fn()
    const container = await renderWithRouter(
      <DataGrid
        gridId="client-scans"
        columns={columns}
        rows={scans}
        getRowId={(row) => String(row.id)}
        selection="multiRow"
        isRowSelectable={(row) => row.status === "new"}
        onSelectionChange={onSelectionChange}
      />
    )
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["1", "2", "3", "4"])
    })
    // A date column keeps the date filter (no date-and-time input inferred from the rows).
    expect(
      container.querySelector(
        '.ag-floating-filter[aria-colindex] input[type="datetime-local"]'
      )
    ).toBeNull()
    const api = getGridApi(container.querySelector(".ag-root-wrapper"))
    if (!api) throw new Error("no grid")
    const filter = async (model: Record<string, unknown>) => {
      await act(async () => {
        api.setFilterModel(model)
        await Promise.resolve()
      })
    }

    await filter({
      time: {
        filterType: "date",
        type: "equals",
        dateFrom: "2022-04-20 00:00:00",
      },
    })
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["2", "3"])
    })
    await filter({
      time: {
        filterType: "date",
        type: "inRange",
        dateFrom: "2022-04-19 00:00:00",
        dateTo: "2022-04-20 00:00:00",
      },
    })
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["1", "2", "3"])
    })
    await filter({
      n: { filterType: "number", type: "inRange", filter: 2, filterTo: 4 },
    })
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["2", "3", "4"])
    })

    // The header box: the rows shown (2, 3, 4) that can be selected (2, 4); not 1, hidden.
    const header = container.querySelector<HTMLInputElement>(
      '.ag-header-select-all input[type="checkbox"]'
    )
    await click(header)
    await vi.waitFor(() => {
      expect(onSelectionChange).toHaveBeenLastCalledWith([scans[1], scans[3]])
    })
  })

  it("restores the column order, widths and visibility kept for its gridId", async () => {
    localStorage.setItem(
      columnStateKey("jobs"),
      JSON.stringify({
        columnOrder: { orderedColIds: ["status", "job_id", "n_inputs"] },
        columnSizing: { columnSizingModel: [{ colId: "status", width: 333 }] },
        columnVisibility: { hiddenColIds: ["n_inputs"] },
      })
    )
    const container = await renderWithRouter(
      <DataGrid
        gridId="jobs"
        columns={COLUMNS}
        fetchRows={server(() => [job(1, "finished")])}
        getRowId={(row) => String(row.job_id)}
      />
    )
    await vi.waitFor(() => {
      expect(headerLabels(container)).toEqual(["Status", "Job ID"])
    })
    const status = container.querySelector<HTMLElement>(
      '.ag-header-cell[col-id="status"]'
    )
    expect(status?.style.width).toBe("333px")
    // Another grid keeps its own state.
    cleanup()
    const other = await renderWithRouter(
      <DataGrid
        gridId="runs"
        columns={COLUMNS}
        fetchRows={server(() => [job(1, "finished")])}
        getRowId={(row) => String(row.job_id)}
      />
    )
    await vi.waitFor(() => {
      expect(headerLabels(other)).toEqual(["Job ID", "Status", "Inputs"])
    })
  })

  it("shows and hides columns for the page, tells it the columns shown and the row count, and keeps them", async () => {
    const grid = createRef<DataGridHandle>()
    const onShown = vi.fn()
    const onRowCount = vi.fn()
    const container = await renderWithRouter(
      <DataGrid
        ref={grid}
        gridId="jobs"
        columns={[
          ...COLUMNS.slice(0, 2),
          { field: "n_inputs", header: "Inputs", type: "number", hidden: true },
        ]}
        fetchRows={server(() => [job(1, "finished"), job(2, "finished")])}
        getRowId={(row) => String(row.job_id)}
        onShownColumnsChange={onShown}
        onRowCount={onRowCount}
      />
    )
    // Once the first rows show (a choice made while the grid starts is the next test's).
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["2", "1"])
    })
    await vi.waitFor(() => {
      expect(onRowCount).toHaveBeenLastCalledWith({ total: 2, filtered: false })
    })
    expect(onShown).toHaveBeenLastCalledWith(["job_id", "status"])
    act(() => {
      grid.current?.setColumnsShown(["n_inputs"], true)
      grid.current?.setColumnsShown(["status"], false)
    })
    expect(headerLabels(container)).toEqual(["Job ID", "Inputs"])
    await vi.waitFor(() => {
      expect(onShown).toHaveBeenLastCalledWith(["job_id", "n_inputs"])
    })
    // AG Grid tells the state's change after a tick: the grid stores it then.
    await vi.waitFor(() => {
      expect(
        JSON.parse(localStorage.getItem(columnStateKey("jobs")) ?? "{}")
      ).toMatchObject({ columnVisibility: { hiddenColIds: ["status"] } })
    })
  })

  it("keeps a column choice made while it starts, and stores nothing when it only opens", async () => {
    const fetchRows = server(() => [job(1, "finished")])
    const opened = await renderWithRouter(
      <DataGrid
        gridId="jobs"
        columns={COLUMNS}
        fetchRows={fetchRows}
        getRowId={(row) => String(row.job_id)}
      />
    )
    await vi.waitFor(() => {
      expect(rowIds(opened)).toEqual(["1"])
    })
    // AG Grid's start-up state events come a few tasks after the rows show.
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)))
    expect(localStorage.getItem(columnStateKey("jobs"))).toBeNull()
    cleanup()
    // The page hides a column as soon as it hears of the columns: AG Grid holds its state events
    // then, and tells only its start-up event.
    const grid = createRef<DataGridHandle>()
    let chosen = false
    const container = await renderWithRouter(
      <DataGrid
        ref={grid}
        gridId="jobs"
        columns={COLUMNS}
        fetchRows={fetchRows}
        getRowId={(row) => String(row.job_id)}
        onShownColumnsChange={() => {
          if (chosen) return
          chosen = true
          grid.current?.setColumnsShown(["status"], false)
        }}
      />
    )
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["1"])
    })
    expect(headerLabels(container)).toEqual(["Job ID", "Inputs"])
    await vi.waitFor(() => {
      expect(
        JSON.parse(localStorage.getItem(columnStateKey("jobs")) ?? "{}")
      ).toMatchObject({ columnVisibility: { hiddenColIds: ["status"] } })
    })
  })

  it("keeps the checkbox column pinned on the left with a kept column state", async () => {
    // AG Grid takes a kept state without pinning as "nothing pinned" (column-state.ts).
    localStorage.setItem(
      columnStateKey("jobs"),
      JSON.stringify({
        columnSizing: { columnSizingModel: [{ colId: "status", width: 333 }] },
      })
    )
    const container = await renderWithRouter(
      <DataGrid
        gridId="jobs"
        columns={COLUMNS}
        fetchRows={server(() => [job(1, "finished")])}
        getRowId={(row) => String(row.job_id)}
        selection="multiRow"
      />
    )
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["1"])
    })
    expect(
      container.querySelector('[row-id="1"] .ag-cell-last-left-pinned')
    ).toBe(
      container.querySelector(
        '[row-id="1"] .ag-cell[col-id="ag-Grid-SelectionColumn"]'
      )
    )
    expect(
      container.querySelector('[row-id="1"] .ag-cell-last-left-pinned')
    ).not.toBeNull()
  })

  it('shows a status it does not know as "Unknown (x)"', async () => {
    const fetchRows = server(() => [
      { ...job(2, "finished"), status: "paused" as RunStatus },
      job(1, "running"),
    ])
    const container = await renderWithRouter(
      <DataGrid
        gridId="jobs"
        columns={COLUMNS}
        fetchRows={fetchRows}
        getRowId={(row) => String(row.job_id)}
      />
    )
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["2", "1"])
    })
    const status = (id: string) =>
      container.querySelector(`[row-id="${id}"] [col-id="status"]`)?.textContent
    expect(status("2")).toBe("Unknown (paused)")
    expect(status("1")).toBe("Running 1/4")
  })

  it("says when there are no rows", async () => {
    const container = await renderWithRouter(
      <DataGrid
        gridId="jobs"
        columns={COLUMNS}
        fetchRows={server(() => [])}
        getRowId={(row) => String(row.job_id)}
        noun="jobs"
      />
    )
    await vi.waitFor(() => {
      expect(container.textContent).toContain("No jobs")
    })
    expect(byRole("button", "Clear filters", container)).toHaveLength(0)
  })

  it("shows a failed load with a Retry that loads again", async () => {
    let fail = true
    const ok = server(() => [job(1, "finished")])
    const fetchRows = vi.fn((query: GridQuery) =>
      fail
        ? Promise.reject(
            new ApiError(503, "Service Unavailable", {
              title: "Service Unavailable",
              detail: "The jobs could not be read",
            })
          )
        : ok(query)
    )
    const container = await renderWithRouter(
      <DataGrid
        gridId="jobs"
        columns={COLUMNS}
        fetchRows={fetchRows}
        getRowId={(row) => String(row.job_id)}
      />
    )
    await vi.waitFor(() => {
      expect(byRole("alert", undefined, container)[0]?.textContent).toContain(
        "The jobs could not be read"
      )
    })
    fail = false
    await click(byRole("button", "Retry", container)[0])
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["1"])
    })
    expect(byRole("alert", undefined, container)).toHaveLength(0)
  })
})

describe("DataGrid Retry and the keyboard", () => {
  function failing(failures: (query: GridQuery) => boolean, table: Job[]) {
    const ok = server(() => table)
    return vi.fn((query: GridQuery) =>
      failures(query)
        ? Promise.reject(
            new ApiError(503, "Service Unavailable", {
              title: "Service Unavailable",
              detail: "The jobs could not be read",
            })
          )
        : ok(query)
    )
  }

  async function renderJobs(fetchRows: ReturnType<typeof failing>) {
    return renderWithRouter(
      <DataGrid
        gridId="jobs"
        columns={COLUMNS}
        fetchRows={fetchRows}
        getRowId={(row) => String(row.job_id)}
        selection="multiRow"
        noun="jobs"
      />
    )
  }

  /** Press Retry from the keyboard: it has the focus, then it is clicked. */
  async function pressRetry(container: HTMLElement) {
    const retry = byRole("button", "Retry", container)[0]
    act(() => {
      retry?.focus()
    })
    expect(document.activeElement).toBe(retry)
    await click(retry)
    return retry
  }

  it("gives the first row the keyboard once the error over the rows has loaded", async () => {
    let fail = true
    const fetchRows = failing(
      () => fail,
      [job(2, "finished"), job(1, "failed")]
    )
    const container = await renderJobs(fetchRows)
    await vi.waitFor(() => {
      expect(byRole("alert", undefined, container)).toHaveLength(1)
    })
    fail = false
    await pressRetry(container)
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["2", "1"])
    })
    // The first column after the checkboxes, of the first row.
    await vi.waitFor(() => {
      expect(
        document.activeElement?.closest(".ag-cell")?.getAttribute("col-id")
      ).toBe("job_id")
    })
    expect(
      document.activeElement?.closest("[row-id]")?.getAttribute("row-id")
    ).toBe("2")
  })

  it("keeps the keyboard on Retry while it loads and when it fails again", async () => {
    const fetchRows = failing(() => true, [job(1, "finished")])
    const container = await renderJobs(fetchRows)
    await vi.waitFor(() => {
      expect(byRole("alert", undefined, container)).toHaveLength(1)
    })
    const asked = fetchRows.mock.calls.length
    const retry = await pressRetry(container)
    await vi.waitFor(() => {
      expect(fetchRows.mock.calls.length).toBeGreaterThan(asked)
    })
    await vi.waitFor(() => {
      expect(byRole("alert", undefined, container)).toHaveLength(1)
    })
    // Never disabled, so never dropped to the page's body.
    const button = byRole("button", "Retry", container)[0]
    expect(button).toBe(retry)
    expect(button?.hasAttribute("disabled")).toBe(false)
    expect(document.activeElement).toBe(button)
  })

  it("gives the header the keyboard when the retried list is empty", async () => {
    let fail = true
    const fetchRows = failing(() => fail, [])
    const container = await renderJobs(fetchRows)
    await vi.waitFor(() => {
      expect(byRole("alert", undefined, container)).toHaveLength(1)
    })
    fail = false
    await pressRetry(container)
    await vi.waitFor(() => {
      expect(container.textContent).toContain("No jobs")
    })
    await vi.waitFor(() => {
      expect(
        document.activeElement
          ?.closest(".ag-header-cell")
          ?.getAttribute("col-id")
      ).toBe("job_id")
    })
  })

  it("gives the rows shown the keyboard once the banner's Retry has loaded", async () => {
    // Block 100 fails once, below rows already shown: the banner above the grid.
    const table = Array.from({ length: 150 }, (_, index) =>
      job(150 - index, "finished")
    )
    let failures = 0
    const fetchRows = failing((query) => {
      if (query.offset !== 100 || failures > 0) return false
      failures += 1
      return true
    }, table)
    const container = await renderJobs(fetchRows)
    await vi.waitFor(() => {
      expect(rowIds(container)[0]).toBe("150")
    })
    await scrollToRow(container, 110)
    await vi.waitFor(() => {
      expect(byRole("alert", undefined, container)[0]?.textContent).toContain(
        "Showing the last data loaded."
      )
    })
    await pressRetry(container)
    await vi.waitFor(() => {
      expect(byRole("alert", undefined, container)).toHaveLength(0)
    })
    await vi.waitFor(() => {
      expect(
        document.activeElement?.closest(".ag-cell")?.getAttribute("col-id")
      ).toBe("job_id")
    })
    // A row on screen: the first rendered one, not the list's top.
    const row = Number(
      document.activeElement?.closest("[row-id]")?.getAttribute("row-id")
    )
    expect(rowIds(container)).toContain(String(row))
  })
})

describe("DataGrid errors in long lists", () => {
  // AG Grid renders 15 blocks in jsdom: slower than most tests.
  it(
    "Retry clears the banner of a failed block that the grid has since dropped",
    {
      timeout: 20_000,
    },
    async () => {
      // 1500 rows: 15 blocks of 100, more than the 10 the grid keeps. Block 300 fails once.
      const table = Array.from({ length: 1500 }, (_, index) =>
        job(1500 - index, "finished")
      )
      const ok = server(() => table)
      let failed = false
      const fetchRows = vi.fn((query: GridQuery) => {
        if (query.offset === 300 && !failed) {
          failed = true
          return Promise.reject(
            new ApiError(503, "Service Unavailable", {
              title: "Service Unavailable",
              detail: "The jobs could not be read",
            })
          )
        }
        return ok(query)
      })
      const container = await renderWithRouter(
        <DataGrid
          gridId="jobs"
          columns={COLUMNS}
          fetchRows={fetchRows}
          getRowId={(row) => String(row.job_id)}
          noun="jobs"
        />
      )
      await vi.waitFor(() => {
        expect(rowIds(container)[0]).toBe("1500")
      })
      await scrollToRow(container, 300)
      await vi.waitFor(() => {
        expect(byRole("alert", undefined, container)[0]?.textContent).toContain(
          "The jobs could not be read"
        )
      })
      // On to the end: AG Grid drops the least recently used blocks, the failed one among them.
      for (let row = 400; row < 1500; row += 100) {
        await scrollToRow(container, row)
        await vi.waitFor(() => {
          expect(rowIds(container)).toContain(String(1500 - row))
        })
      }
      const offsets = () => fetchRows.mock.calls.map(([query]) => query.offset)
      expect(offsets()).toContain(1400)
      const asked = offsets().length

      await click(byRole("button", "Retry", container)[0])
      await vi.waitFor(() => {
        expect(byRole("alert", undefined, container)).toHaveLength(0)
      })
      // Retry reloaded the blocks the grid holds, not the dropped one.
      await vi.waitFor(() => {
        expect(offsets().length).toBeGreaterThan(asked)
      })
      expect(offsets().slice(asked)).not.toContain(300)

      // Scrolled back to, the dropped block loads afresh.
      await scrollToRow(container, 300)
      await vi.waitFor(() => {
        expect(rowIds(container)).toContain("1200")
      })
      expect(byRole("alert", undefined, container)).toHaveLength(0)
    }
  )
})

describe("DataGrid live refresh", () => {
  it("reloads the loaded rows every 5 s while a visible row is active, then stops", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] })
    let table = [job(2, "running"), job(1, "finished")]
    const fetchRows = server(() => table)
    const onSelectionChange = vi.fn()
    const container = await renderWithRouter(
      <DataGrid
        gridId="jobs"
        columns={COLUMNS}
        fetchRows={fetchRows}
        getRowId={(row) => String(row.job_id)}
        selection="multiRow"
        onSelectionChange={onSelectionChange}
        live
      />
    )
    await vi.waitFor(() => {
      expect(rowText(container, "2")).toContain("Running 1/4")
    })
    await click(checkbox(container, "1"))
    const calls = fetchRows.mock.calls.length

    table = [job(2, "running", 3), job(1, "finished")]
    await act(() => vi.advanceTimersByTimeAsync(5000))
    await vi.waitFor(() => {
      expect(rowText(container, "2")).toContain("Running 3/4")
    })
    expect(fetchRows.mock.calls.length).toBe(calls + 1)
    expect(fetchRows).toHaveBeenLastCalledWith(
      {
        offset: 0,
        limit: 100,
        sort: "-job_id",
      },
      expect.any(AbortSignal)
    )
    // The refresh keeps the selection.
    expect(checkbox(container, "1").checked).toBe(true)

    table = [job(2, "finished", 4), job(1, "finished")]
    await act(() => vi.advanceTimersByTimeAsync(5000))
    await vi.waitFor(() => {
      expect(rowText(container, "2")).toContain("Finished")
    })
    // Nothing visible is active any more: polling stops.
    const settled = fetchRows.mock.calls.length
    await act(() => vi.advanceTimersByTimeAsync(15_000))
    expect(fetchRows.mock.calls.length).toBe(settled)
    expect(onSelectionChange).toHaveBeenLastCalledWith([job(1, "finished")])
  })

  it("does not poll a grid that is not live", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] })
    const fetchRows = server(() => [job(1, "running")])
    const container = await renderWithRouter(
      <DataGrid
        gridId="jobs"
        columns={COLUMNS}
        fetchRows={fetchRows}
        getRowId={(row) => String(row.job_id)}
      />
    )
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["1"])
    })
    const calls = fetchRows.mock.calls.length
    await act(() => vi.advanceTimersByTimeAsync(15_000))
    expect(fetchRows.mock.calls.length).toBe(calls)
  })

  it("with changed_since, updates the changed rows in place, and reloads when a row appears", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] })
    let table = [job(2, "running"), job(1, "finished")]
    let changes: Job[] = []
    const list = server(() => table)
    const fetchRows = vi.fn((query: GridQuery) =>
      query.changed_since === undefined
        ? list(query)
        : Promise.resolve<GridPage<Job>>({
            rows: changes,
            total: changes.length,
            as_of: "2026-10-19T12:00:05Z",
          })
    )
    const container = await renderWithRouter(
      <DataGrid
        gridId="jobs"
        columns={COLUMNS}
        fetchRows={fetchRows}
        getRowId={(row) => String(row.job_id)}
        live
        changedSince
      />
    )
    await vi.waitFor(() => {
      expect(rowText(container, "2")).toContain("Running 1/4")
    })

    // The running job progressed: one request for the changes, sent with the page's as_of.
    changes = [job(2, "running", 2)]
    await act(() => vi.advanceTimersByTimeAsync(5000))
    await vi.waitFor(() => {
      expect(rowText(container, "2")).toContain("Running 2/4")
    })
    expect(fetchRows).toHaveBeenLastCalledWith(
      {
        offset: 0,
        limit: 1000,
        sort: "-job_id",
        filter: undefined,
        changed_since: AS_OF,
      },
      expect.any(AbortSignal)
    )
    const patched = fetchRows.mock.calls.length

    // A new job: the changes cannot be placed, so the loaded blocks reload.
    table = [job(3, "queued"), job(2, "running", 2), job(1, "finished")]
    changes = [job(3, "queued"), job(2, "running", 2)]
    await act(() => vi.advanceTimersByTimeAsync(5000))
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["3", "2", "1"])
    })
    expect(fetchRows.mock.calls.slice(patched).map(([query]) => query)).toEqual(
      [
        {
          offset: 0,
          limit: 1000,
          sort: "-job_id",
          filter: undefined,
          changed_since: "2026-10-19T12:00:05Z",
        },
        { offset: 0, limit: 100, sort: "-job_id" },
      ]
    )
  })
})

describe("DataGrid live refresh when idle or failing", () => {
  /** A list endpoint over `table()` that answers `changed_since` with `changes()`. */
  function changesServer(table: () => Job[], changes: () => Job[]) {
    const list = server(table)
    return vi.fn((query: GridQuery): Promise<GridPage<Job>> =>
      query.changed_since === undefined
        ? list(query)
        : Promise.resolve({
            rows: changes(),
            total: changes().length,
            as_of: "2026-10-19T12:00:15Z",
          })
    )
  }

  const changeQueries = (fetchRows: ReturnType<typeof changesServer>) =>
    fetchRows.mock.calls
      .map(([query]) => query.changed_since)
      .filter((since) => since !== undefined)

  it("with changed_since and no active row shown, asks for the changes every 15 s; a run queued elsewhere appears", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] })
    let table = [job(2, "finished"), job(1, "finished")]
    let changes: Job[] = []
    const fetchRows = changesServer(
      () => table,
      () => changes
    )
    const container = await renderWithRouter(
      <DataGrid
        gridId="jobs"
        columns={COLUMNS}
        fetchRows={fetchRows}
        getRowId={(row) => String(row.job_id)}
        live
        changedSince
      />
    )
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["2", "1"])
    })
    await act(() => vi.advanceTimersByTimeAsync(10_000))
    expect(changeQueries(fetchRows)).toEqual([])
    await act(() => vi.advanceTimersByTimeAsync(5_000))
    expect(fetchRows).toHaveBeenLastCalledWith(
      {
        offset: 0,
        limit: 1000,
        sort: "-job_id",
        filter: undefined,
        changed_since: AS_OF,
      },
      expect.any(AbortSignal)
    )

    // Another tab queues job 3: the next idle poll finds it, and the grid reloads with it.
    table = [job(3, "queued"), ...table]
    changes = [job(3, "queued")]
    await act(() => vi.advanceTimersByTimeAsync(10_000))
    expect(rowIds(container)).toEqual(["2", "1"])
    await act(() => vi.advanceTimersByTimeAsync(5_000))
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["3", "2", "1"])
    })
    expect(changeQueries(fetchRows)).toEqual([AS_OF, "2026-10-19T12:00:15Z"])
  })

  it("backs off after consecutive failed polls: 10, 20, then 40 s apart", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] })
    let down = false
    const list = server(() => [job(2, "running"), job(1, "finished")])
    const fetchRows = vi.fn((query: GridQuery) =>
      down
        ? Promise.reject(new ApiError(503, "Service Unavailable"))
        : list(query)
    )
    const container = await renderWithRouter(
      <DataGrid
        gridId="jobs"
        columns={COLUMNS}
        fetchRows={fetchRows}
        getRowId={(row) => String(row.job_id)}
        live
      />
    )
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["2", "1"])
    })
    down = true
    // The first poll fails; then each waits twice as long as the one before. (AG Grid asks for a
    // block on a timer of its own, so each count is awaited.)
    const polls = [5, 15, 35, 75]
    let calls = fetchRows.mock.calls.length
    for (let second = 5; second <= 80; second += 5) {
      await act(() => vi.advanceTimersByTimeAsync(5000))
      if (polls.includes(second)) calls += 1
      await vi.waitFor(() => {
        expect(fetchRows.mock.calls.length).toBe(calls)
      })
    }
    expect(rowIds(container)).toEqual(["2", "1"])
  })

  it("stops polling on a 404, shows the error with Retry, and polls again after an answer", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] })
    let gone = false
    const list = server(() => [job(2, "running")])
    const fetchRows = vi.fn((query: GridQuery): Promise<GridPage<Job>> => {
      if (gone) return Promise.reject(new ApiError(404, "Not Found"))
      return query.changed_since === undefined
        ? list(query)
        : Promise.resolve({ rows: [job(2, "running")], total: 1, as_of: AS_OF })
    })
    const container = await renderWithRouter(
      <DataGrid
        gridId="jobs"
        columns={COLUMNS}
        fetchRows={fetchRows}
        getRowId={(row) => String(row.job_id)}
        live
        changedSince
      />
    )
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["2"])
    })
    gone = true
    await act(() => vi.advanceTimersByTimeAsync(5000))
    // The changes' 404, then one reload, whose 404 shows with Retry.
    await vi.waitFor(() => {
      expect(byRole("button", "Retry", container)).not.toHaveLength(0)
    })
    expect(changeQueries(fetchRows)).toHaveLength(1)
    const calls = fetchRows.mock.calls.length
    await act(() => vi.advanceTimersByTimeAsync(120_000))
    expect(fetchRows.mock.calls.length).toBe(calls)

    // Retry answers: the polling starts again.
    gone = false
    await click(byRole("button", "Retry", container)[0])
    await vi.waitFor(() => {
      expect(byRole("button", "Retry", container)).toHaveLength(0)
    })
    await act(() => vi.advanceTimersByTimeAsync(5000))
    await vi.waitFor(() => {
      expect(changeQueries(fetchRows)).toHaveLength(2)
    })
  })
})

describe("DataGrid refresh and the selected rows' data", () => {
  it("refresh() loads the rows shown again, keeping the selection", async () => {
    let table = [job(2, "queued"), job(1, "finished")]
    const fetchRows = server(() => table)
    const handle = createRef<DataGridHandle>()
    const container = await renderWithRouter(
      <DataGrid
        ref={handle}
        gridId="jobs"
        columns={COLUMNS}
        fetchRows={fetchRows}
        getRowId={(row) => String(row.job_id)}
        selection="multiRow"
      />
    )
    await vi.waitFor(() => {
      expect(rowText(container, "2")).toContain("Queued")
    })
    await click(checkbox(container, "2"))
    const calls = fetchRows.mock.calls.length

    table = [job(2, "cancelled"), job(1, "finished")]
    act(() => {
      handle.current?.refresh()
    })
    await vi.waitFor(() => {
      expect(rowText(container, "2")).toContain("Cancelled")
    })
    expect(fetchRows.mock.calls.length).toBe(calls + 1)
    expect(checkbox(container, "2").checked).toBe(true)
  })

  it("tells the page the selected rows again when their data changes, and only then", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] })
    let table = [job(3, "running"), job(2, "queued"), job(1, "finished")]
    const fetchRows = server(() => table)
    const onSelectionChange = vi.fn()
    const handle = createRef<DataGridHandle>()
    const container = await renderWithRouter(
      <DataGrid
        ref={handle}
        gridId="jobs"
        columns={COLUMNS}
        fetchRows={fetchRows}
        getRowId={(row) => String(row.job_id)}
        selection="multiRow"
        onSelectionChange={onSelectionChange}
        live
      />
    )
    await vi.waitFor(() => {
      expect(rowText(container, "3")).toContain("Running 1/4")
    })
    await click(checkbox(container, "2"))
    await click(checkbox(container, "3"))
    await vi.waitFor(() => {
      expect(onSelectionChange).toHaveBeenLastCalledWith([
        job(2, "queued"),
        job(3, "running"),
      ])
    })
    const told = onSelectionChange.mock.calls.length

    // A live refresh that changes no selected row: nothing new to tell.
    table = [job(3, "running"), job(2, "queued"), job(1, "cancelled")]
    await act(() => vi.advanceTimersByTimeAsync(5000))
    await vi.waitFor(() => {
      expect(rowText(container, "1")).toContain("Cancelled")
    })
    expect(onSelectionChange.mock.calls.length).toBe(told)

    // The selected running job progressed and the queued one started (live refresh).
    table = [job(3, "running", 3), job(2, "running", 0), job(1, "cancelled")]
    await act(() => vi.advanceTimersByTimeAsync(5000))
    await vi.waitFor(() => {
      expect(onSelectionChange).toHaveBeenLastCalledWith([
        job(2, "running", 0),
        job(3, "running", 3),
      ])
    })

    // Both were stopped (a refresh after the action): the page sees them ended, still selected.
    table = [
      job(3, "cancelled", 3),
      job(2, "cancelled", 0),
      job(1, "cancelled"),
    ]
    act(() => {
      handle.current?.refresh()
    })
    await vi.waitFor(() => {
      expect(onSelectionChange).toHaveBeenLastCalledWith([
        job(2, "cancelled", 0),
        job(3, "cancelled", 3),
      ])
    })
    expect(checkbox(container, "2").checked).toBe(true)
    expect(checkbox(container, "3").checked).toBe(true)
  })

  it("drops a live update asked before a refresh: the rows loaded since are newer", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] })
    let table = [job(2, "running"), job(1, "finished")]
    const list = server(() => table)
    let answer: (page: GridPage<Job>) => void = () => undefined
    const fetchRows = vi.fn((query: GridQuery) =>
      query.changed_since === undefined
        ? list(query)
        : new Promise<GridPage<Job>>((resolve) => {
            answer = resolve
          })
    )
    const onSelectionChange = vi.fn()
    const handle = createRef<DataGridHandle>()
    const container = await renderWithRouter(
      <DataGrid
        ref={handle}
        gridId="jobs"
        columns={COLUMNS}
        fetchRows={fetchRows}
        getRowId={(row) => String(row.job_id)}
        selection="multiRow"
        onSelectionChange={onSelectionChange}
        live
        changedSince
      />
    )
    await vi.waitFor(() => {
      expect(rowText(container, "2")).toContain("Running 1/4")
    })
    await click(checkbox(container, "2"))
    await vi.waitFor(() => {
      expect(onSelectionChange).toHaveBeenLastCalledWith([job(2, "running")])
    })

    // A live update asks for the changes; its answer is slow.
    await act(() => vi.advanceTimersByTimeAsync(5000))
    expect(fetchRows).toHaveBeenLastCalledWith(
      expect.objectContaining({ changed_since: AS_OF }),
      expect.any(AbortSignal)
    )

    // Meanwhile the job is stopped and the page reloads its rows.
    table = [job(2, "cancelled"), job(1, "finished")]
    act(() => {
      handle.current?.refresh()
    })
    await vi.waitFor(() => {
      expect(rowText(container, "2")).toContain("Cancelled")
    })
    await vi.waitFor(() => {
      expect(onSelectionChange).toHaveBeenLastCalledWith([job(2, "cancelled")])
    })

    // The slow answer, from before the stop, arrives: it does not put the old row back.
    await act(async () => {
      answer({
        rows: [job(2, "running", 2)],
        total: 1,
        as_of: "2026-10-19T12:00:05Z",
      })
      await new Promise((resolve) => setTimeout(resolve, 50))
    })
    expect(rowText(container, "2")).toContain("Cancelled")
    expect(onSelectionChange).toHaveBeenLastCalledWith([job(2, "cancelled")])
  })
})
