import {
  act,
  createRef,
  StrictMode,
  useImperativeHandle,
  useState,
  type Ref,
} from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { ApiError, type RunStatus } from "@/api"
import {
  rowCheckbox,
  rowIds,
  rowText,
  headerLabels,
  stubGridLayout,
  typeInto,
} from "@/test/grid"
import { byRole, cleanup, click, renderWithRouter } from "@/test/render"
import { columnStateKey } from "./column-state"
import type { GridColumn } from "./columns"
import { DataGrid, type DataGridHandle, type DataGridProps } from "./DataGrid"
import type { FetchRows, GridPage, GridQuery } from "./datasource"

// The DataGrid's lifecycle in jsdom (AG Grid itself renders): answers that arrive out of order, a
// new list (listKey), a new fetchRows or gridId on a mounted grid, StrictMode, and live refresh
// while the tab is hidden or filters are set.

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

function job(job_id: number, status: RunStatus = "finished"): Job {
  return {
    job_id,
    status,
    n_inputs: 4,
    n_succeeded: 1,
    n_failed: 0,
    n_not_run: 0,
  }
}

/** A list endpoint over `table()`: sorted by job_id, a status filter applied, queries recorded. */
function server(table: () => Job[]) {
  return vi.fn((query: GridQuery): Promise<GridPage<Job>> => {
    const wanted = query.filter
      ? (JSON.parse(query.filter) as { status?: { filter: string } }).status
          ?.filter
      : undefined
    const rows = table()
      .filter((row) => wanted === undefined || row.status.includes(wanted))
      .sort((a, b) =>
        query.sort === "job_id" ? a.job_id - b.job_id : b.job_id - a.job_id
      )
    return Promise.resolve({
      rows: rows.slice(query.offset, query.offset + query.limit),
      total: rows.length,
      as_of: AS_OF,
    })
  })
}

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined
  let reject: (error: unknown) => void = () => undefined
  const promise = new Promise<T>((onResolve, onReject) => {
    resolve = onResolve
    reject = onReject
  })
  return { promise, resolve, reject }
}

const jobId = (row: Job) => String(row.job_id)

// A server-side grid (fetchRows), whose props the test changes one by one.
type Props = Extract<DataGridProps<Job>, { fetchRows: unknown }>

/** A DataGrid whose props a test can change while it stays mounted. */
function Switchable({
  initial,
  control,
}: {
  initial: Props
  control: Ref<(props: Partial<Props>) => void>
}) {
  const [props, setProps] = useState(initial)
  useImperativeHandle(
    control,
    () => (next: Partial<Props>) => {
      setProps((previous) => ({ ...previous, ...next }))
    },
    []
  )
  return <DataGrid {...props} />
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

describe("answers that arrive out of order", () => {
  for (const [what, settle] of [
    [
      "an empty page",
      (slow: ReturnType<typeof deferred<GridPage<Job>>>) => {
        slow.resolve({ rows: [], total: 0, as_of: AS_OF })
      },
    ],
    [
      "a failure",
      (slow: ReturnType<typeof deferred<GridPage<Job>>>) => {
        slow.reject(new ApiError(503, "Service Unavailable"))
      },
    ],
  ] as const) {
    it(`a superseded sort's answer (${what}) arriving last changes nothing`, async () => {
      const slow = deferred<GridPage<Job>>()
      const list = server(() => [job(1), job(2), job(3)])
      let first = true
      const fetchRows = vi.fn((query: GridQuery) => {
        if (!first) return list(query)
        first = false
        return slow.promise
      })
      const container = await renderWithRouter(
        <DataGrid
          gridId="jobs"
          columns={COLUMNS}
          fetchRows={fetchRows}
          getRowId={jobId}
          noun="jobs"
        />
      )
      // While the first answer is outstanding, sort again (descending to unsorted).
      await vi.waitFor(() => {
        expect(fetchRows).toHaveBeenCalledOnce()
      })
      await click(
        container.querySelector('[col-id="job_id"] .ag-header-cell-label')
      )
      await vi.waitFor(() => {
        expect(rowIds(container)).toEqual(["3", "2", "1"])
      })
      await act(async () => {
        settle(slow)
        await slow.promise.catch(() => undefined)
      })
      // Long enough for a wrongly shown overlay or banner to mount: AG Grid mounts its overlay
      // after React has rendered, outside act.
      await new Promise((resolve) => setTimeout(resolve, 600))
      expect(rowIds(container)).toEqual(["3", "2", "1"])
      expect(container.textContent).not.toContain("No jobs")
      expect(byRole("alert", undefined, container)).toHaveLength(0)
    })
  }
})

/**
 * A page that re-renders whenever the selection changes (it shows the count) and passes the grid a
 * new inline fetchRows on every render.
 */
function InlinePage({ list }: { list: FetchRows<Job> }) {
  const [selected, setSelected] = useState(0)
  return (
    <>
      <p data-testid="selected">{selected}</p>
      <DataGrid
        gridId="jobs"
        columns={COLUMNS}
        fetchRows={(query, signal) => list(query, signal)}
        getRowId={jobId}
        selection="multiRow"
        onSelectionChange={(rows) => {
          setSelected(rows.length)
        }}
      />
    </>
  )
}

describe("a mounted grid", () => {
  it("loads another list when listKey changes, keeping the sort and dropping the selection", async () => {
    const control = createRef<(props: Partial<Props>) => void>()
    const onSelectionChange = vi.fn()
    const first = server(() => [job(1), job(2), job(3)])
    const container = await renderWithRouter(
      <Switchable
        control={control}
        initial={{
          gridId: "jobs",
          columns: COLUMNS,
          fetchRows: first,
          listKey: "scan-1",
          getRowId: jobId,
          selection: "multiRow",
          onSelectionChange,
        }}
      />
    )
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["3", "2", "1"])
    })
    await click(rowCheckbox(container, "2"))
    await vi.waitFor(() => {
      expect(onSelectionChange).toHaveBeenLastCalledWith([job(2)])
    })

    const second: FetchRows<Job> = server(() => [job(10), job(11), job(12)])
    act(() => {
      control.current?.({ fetchRows: second, listKey: "scan-2" })
    })
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["12", "11", "10"])
    })
    expect(second).toHaveBeenCalledWith(
      { offset: 0, limit: 100, sort: "-job_id" },
      expect.any(AbortSignal)
    )
    expect(onSelectionChange).toHaveBeenLastCalledWith([])
    expect(rowCheckbox(container, "11").checked).toBe(false)
    expect(first).toHaveBeenCalledTimes(1)
  })

  it("keeps its rows and selection when the page re-renders with an inline fetchRows", async () => {
    const list = server(() => [job(1), job(2), job(3)])
    const container = await renderWithRouter(<InlinePage list={list} />)
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["3", "2", "1"])
    })
    const calls = list.mock.calls.length

    // Selecting re-renders the page (its count), which passes the grid a new function.
    await click(rowCheckbox(container, "2"))
    await vi.waitFor(() => {
      expect(
        container.querySelector('[data-testid="selected"]')?.textContent
      ).toBe("1")
    })
    await click(rowCheckbox(container, "3"))
    await vi.waitFor(() => {
      expect(
        container.querySelector('[data-testid="selected"]')?.textContent
      ).toBe("2")
    })
    // Long enough for a reload to have been asked for and answered.
    await new Promise((resolve) => setTimeout(resolve, 300))
    expect(list.mock.calls.length).toBe(calls)
    expect(rowCheckbox(container, "2").checked).toBe(true)
    expect(rowCheckbox(container, "3").checked).toBe(true)
    expect(
      container.querySelector('[data-testid="selected"]')?.textContent
    ).toBe("2")
  })

  it("asks a new fetchRows (same listKey) from the next request on, without reloading", async () => {
    const control = createRef<(props: Partial<Props>) => void>()
    const onSelectionChange = vi.fn()
    const first = server(() => [job(1), job(2), job(3)])
    const container = await renderWithRouter(
      <Switchable
        control={control}
        initial={{
          gridId: "jobs",
          columns: COLUMNS,
          fetchRows: first,
          getRowId: jobId,
          selection: "multiRow",
          onSelectionChange,
        }}
      />
    )
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["3", "2", "1"])
    })
    await click(rowCheckbox(container, "1"))
    await vi.waitFor(() => {
      expect(onSelectionChange).toHaveBeenLastCalledWith([job(1)])
    })

    const second = server(() => [job(1), job(2), job(3)])
    act(() => {
      control.current?.({ fetchRows: second })
    })
    await new Promise((resolve) => setTimeout(resolve, 300))
    expect(second).not.toHaveBeenCalled()
    expect(first).toHaveBeenCalledTimes(1)
    expect(rowCheckbox(container, "1").checked).toBe(true)

    // The next request (sorted again: descending to unsorted) goes to the new function.
    await click(
      container.querySelector('[col-id="job_id"] .ag-header-cell-label')
    )
    await vi.waitFor(() => {
      expect(second).toHaveBeenCalledWith(
        { offset: 0, limit: 100 },
        expect.any(AbortSignal)
      )
    })
    expect(first).toHaveBeenCalledTimes(1)
    expect(rowCheckbox(container, "1").checked).toBe(true)
    expect(onSelectionChange).toHaveBeenLastCalledWith([job(1)])
  })

  it("becomes another grid when gridId changes, with that grid's column state", async () => {
    localStorage.setItem(
      columnStateKey("runs-b"),
      JSON.stringify({
        columnOrder: { orderedColIds: ["status", "job_id", "n_inputs"] },
      })
    )
    const control = createRef<(props: Partial<Props>) => void>()
    const fetchRows = server(() => [job(1)])
    const container = await renderWithRouter(
      <Switchable
        control={control}
        initial={{
          gridId: "runs-a",
          columns: COLUMNS,
          fetchRows,
          getRowId: jobId,
        }}
      />
    )
    await vi.waitFor(() => {
      expect(headerLabels(container)).toEqual(["Job ID", "Status", "Inputs"])
    })
    act(() => {
      control.current?.({ gridId: "runs-b" })
    })
    await vi.waitFor(() => {
      expect(headerLabels(container)).toEqual(["Status", "Job ID", "Inputs"])
    })
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["1"])
    })
  })
})

describe("StrictMode (the development build mounts the grid twice)", () => {
  it("keeps the selection handle and live refresh on the grid that stays", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] })
    const handle = createRef<DataGridHandle>()
    const onSelectionChange = vi.fn()
    const fetchRows = server(() => [job(2, "running"), job(1)])
    const container = await renderWithRouter(
      <StrictMode>
        <DataGrid
          ref={handle}
          gridId="jobs"
          columns={COLUMNS}
          fetchRows={fetchRows}
          getRowId={jobId}
          selection="multiRow"
          onSelectionChange={onSelectionChange}
          live
        />
      </StrictMode>
    )
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["2", "1"])
    })
    await click(rowCheckbox(container, "1"))
    await vi.waitFor(() => {
      expect(onSelectionChange).toHaveBeenLastCalledWith([job(1)])
    })
    act(() => {
      handle.current?.clearSelection()
    })
    await vi.waitFor(() => {
      expect(onSelectionChange).toHaveBeenLastCalledWith([])
    })
    expect(rowCheckbox(container, "1").checked).toBe(false)

    const calls = fetchRows.mock.calls.length
    await act(() => vi.advanceTimersByTimeAsync(5000))
    await vi.waitFor(() => {
      expect(fetchRows.mock.calls.length).toBe(calls + 1)
    })
  })
})

describe("live refresh", () => {
  it("pauses while the tab is hidden", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] })
    const hidden = vi.spyOn(Document.prototype, "hidden", "get")
    hidden.mockReturnValue(false)
    const fetchRows = server(() => [job(1, "running")])
    const container = await renderWithRouter(
      <DataGrid
        gridId="jobs"
        columns={COLUMNS}
        fetchRows={fetchRows}
        getRowId={jobId}
        live
      />
    )
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["1"])
    })
    const calls = fetchRows.mock.calls.length
    hidden.mockReturnValue(true)
    await act(() => vi.advanceTimersByTimeAsync(15_000))
    expect(fetchRows.mock.calls.length).toBe(calls)

    hidden.mockReturnValue(false)
    await act(() => vi.advanceTimersByTimeAsync(5000))
    await vi.waitFor(() => {
      expect(fetchRows.mock.calls.length).toBe(calls + 1)
    })
  })

  it("keeps the filters when it reloads", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] })
    let table = [job(3, "running"), job(2, "queued"), job(1, "running")]
    const fetchRows = server(() => table)
    const container = await renderWithRouter(
      <DataGrid
        gridId="jobs"
        columns={COLUMNS}
        fetchRows={fetchRows}
        getRowId={jobId}
        live
      />
    )
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["3", "2", "1"])
    })
    await typeInto(
      container.querySelector<HTMLInputElement>(
        'input[aria-label="Status Filter Input"]'
      ),
      "run"
    )
    const statusFilter = JSON.stringify({
      status: { type: "contains", filterType: "text", filter: "run" },
    })
    await vi.waitFor(() => {
      expect(fetchRows).toHaveBeenLastCalledWith(
        { offset: 0, limit: 100, sort: "-job_id", filter: statusFilter },
        expect.any(AbortSignal)
      )
    })
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["3", "1"])
    })

    // Job 1 finishes; job 3 is still running.
    table = [job(3, "running"), job(2, "queued"), job(1, "finished")]
    const calls = fetchRows.mock.calls.length
    await act(() => vi.advanceTimersByTimeAsync(5000))
    await vi.waitFor(() => {
      expect(rowIds(container)).toEqual(["3"])
    })
    expect(fetchRows.mock.calls.slice(calls).map(([query]) => query)).toEqual([
      { offset: 0, limit: 100, sort: "-job_id", filter: statusFilter },
    ])
    expect(rowText(container, "3")).toContain("Running")
  })
})
