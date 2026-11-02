import type { IGetRowsParams } from "ag-grid-community"
import { afterEach, describe, expect, it, vi } from "vitest"

import { ApiError } from "@/api"
import {
  BLOCK_SIZE,
  createDatasource,
  localDayStart,
  MAX_BLOCKS_IN_CACHE,
  toFilterParam,
  toGridQuery,
  toSortParam,
  type GridPage,
  type GridQuery,
} from "./datasource"

afterEach(() => {
  vi.unstubAllEnvs()
})

function parsed(filterModel: Record<string, unknown>): unknown {
  const filter = toFilterParam(filterModel)
  return filter === undefined ? undefined : JSON.parse(filter)
}

describe("toSortParam", () => {
  it("writes the sort model as the protocol's comma list, - for descending", () => {
    expect(toSortParam([{ colId: "scan_number", sort: "desc" }])).toBe(
      "-scan_number"
    )
    expect(
      toSortParam([
        { colId: "time", sort: "asc" },
        { colId: "scan_number", sort: "desc" },
      ])
    ).toBe("time,-scan_number")
  })

  it("sends no sort when unsorted (the endpoint's default applies)", () => {
    expect(toSortParam([])).toBeUndefined()
  })
})

describe("toFilterParam: text and number", () => {
  it("passes a text condition with its operator and value", () => {
    expect(
      parsed({
        sample_name: { filterType: "text", type: "contains", filter: "Ni" },
      })
    ).toEqual({
      sample_name: { filterType: "text", type: "contains", filter: "Ni" },
    })
  })

  it.each([
    "contains",
    "notContains",
    "equals",
    "notEqual",
    "startsWith",
    "endsWith",
  ])("passes the text operator %s", (type) => {
    expect(
      parsed({ notes: { filterType: "text", type, filter: "x" } })
    ).toEqual({ notes: { filterType: "text", type, filter: "x" } })
  })

  it.each([
    ["equals", 2],
    ["notEqual", 2],
    ["lessThan", 2],
    ["lessThanOrEqual", 2],
    ["greaterThan", 2],
    ["greaterThanOrEqual", 2],
  ])("passes the number operator %s", (type, filter) => {
    expect(
      parsed({ n_dimensions: { filterType: "number", type, filter } })
    ).toEqual({ n_dimensions: { filterType: "number", type, filter } })
  })

  it("passes a number range with both ends", () => {
    expect(
      parsed({
        scan_number: {
          filterType: "number",
          type: "inRange",
          filter: 10,
          filterTo: 100,
        },
      })
    ).toEqual({
      scan_number: {
        filterType: "number",
        type: "inRange",
        filter: 10,
        filterTo: 100,
      },
    })
  })

  it("passes blank and notBlank without a value, for every type", () => {
    expect(
      parsed({
        notes: { filterType: "text", type: "blank", filter: null },
        n_dimensions: { filterType: "number", type: "notBlank" },
        time: {
          filterType: "date",
          type: "blank",
          dateFrom: null,
          dateTo: null,
        },
      })
    ).toEqual({
      notes: { filterType: "text", type: "blank" },
      n_dimensions: { filterType: "number", type: "notBlank" },
      time: { filterType: "date", type: "blank" },
    })
  })

  it("joins two conditions with AND or OR", () => {
    const model = {
      scan_number: {
        filterType: "number",
        operator: "OR",
        conditions: [
          { filterType: "number", type: "lessThan", filter: 100 },
          { filterType: "number", type: "greaterThan", filter: 900 },
        ],
      },
      sample_name: {
        filterType: "text",
        operator: "AND",
        conditions: [
          { filterType: "text", type: "startsWith", filter: "Ni" },
          { filterType: "text", type: "notContains", filter: "film" },
        ],
      },
    }
    expect(parsed(model)).toEqual(model)
  })

  it("leaves out conditions that are not set", () => {
    expect(parsed({ notes: { filterType: "text", type: "empty" } })).toBe(
      undefined
    )
    // One set condition of two is sent as a single condition.
    expect(
      parsed({
        notes: {
          filterType: "text",
          operator: "AND",
          conditions: [
            { filterType: "text", type: "contains", filter: "a" },
            { filterType: "text", type: "empty" },
          ],
        },
      })
    ).toEqual({ notes: { filterType: "text", type: "contains", filter: "a" } })
  })

  it("sends no filter when nothing filters", () => {
    expect(toFilterParam(null)).toBeUndefined()
    expect(toFilterParam({})).toBeUndefined()
  })
})

describe("toFilterParam: dates (the local day, with its UTC offset)", () => {
  it.each([
    ["America/Chicago", "2026-01-15 00:00:00", "2026-01-15T00:00:00-06:00"],
    // The offset is the one on that day (daylight-saving time), not today's.
    ["America/Chicago", "2026-07-15 00:00:00", "2026-07-15T00:00:00-05:00"],
    ["Europe/Berlin", "2026-01-15 00:00:00", "2026-01-15T00:00:00+01:00"],
    ["Asia/Kolkata", "2026-01-15 00:00:00", "2026-01-15T00:00:00+05:30"],
    ["Asia/Kathmandu", "2026-01-15", "2026-01-15T00:00:00+05:45"],
    ["UTC", "2026-01-15 00:00:00", "2026-01-15T00:00:00+00:00"],
  ])("in %s, %s is sent as %s", (zone, value, expected) => {
    vi.stubEnv("TZ", zone)
    expect(localDayStart(value)).toBe(expected)
  })

  it("starts the day at its first local time where the clocks skip midnight", () => {
    // Chile moved its clocks from 00:00 to 01:00 on 2026-09-06.
    vi.stubEnv("TZ", "America/Santiago")
    expect(localDayStart("2026-09-06 00:00:00")).toBe(
      "2026-09-06T01:00:00-03:00"
    )
  })

  it("sends a value that is not a date as it is", () => {
    expect(localDayStart("yesterday")).toBe("yesterday")
  })

  it("converts dateFrom and dateTo of every date condition", () => {
    vi.stubEnv("TZ", "America/Chicago")
    expect(
      parsed({
        time: {
          filterType: "date",
          type: "inRange",
          dateFrom: "2026-01-01 00:00:00",
          dateTo: "2026-06-30 00:00:00",
        },
      })
    ).toEqual({
      time: {
        filterType: "date",
        type: "inRange",
        dateFrom: "2026-01-01T00:00:00-06:00",
        dateTo: "2026-06-30T00:00:00-05:00",
      },
    })
    for (const type of ["equals", "lessThan", "greaterThan"]) {
      expect(
        parsed({
          time: {
            filterType: "date",
            type,
            dateFrom: "2026-03-01 00:00:00",
            dateTo: null,
          },
        })
      ).toEqual({
        time: {
          filterType: "date",
          type,
          dateFrom: "2026-03-01T00:00:00-06:00",
        },
      })
    }
    expect(
      parsed({
        time: {
          filterType: "date",
          operator: "OR",
          conditions: [
            { filterType: "date", type: "lessThan", dateFrom: "2026-01-01" },
            { filterType: "date", type: "greaterThan", dateFrom: "2026-12-31" },
          ],
        },
      })
    ).toEqual({
      time: {
        filterType: "date",
        operator: "OR",
        conditions: [
          {
            filterType: "date",
            type: "lessThan",
            dateFrom: "2026-01-01T00:00:00-06:00",
          },
          {
            filterType: "date",
            type: "greaterThan",
            dateFrom: "2026-12-31T00:00:00-06:00",
          },
        ],
      },
    })
  })
})

describe("toGridQuery", () => {
  it("maps a block request to offset, limit, sort and filter", () => {
    expect(
      toGridQuery({
        startRow: 200,
        endRow: 300,
        sortModel: [{ colId: "scan_number", sort: "desc" }],
        filterModel: {
          technique: { filterType: "text", type: "equals", filter: "area" },
        },
      })
    ).toEqual({
      offset: 200,
      limit: 100,
      sort: "-scan_number",
      filter:
        '{"technique":{"type":"equals","filterType":"text","filter":"area"}}',
    })
  })

  it("leaves sort and filter out when there are none", () => {
    expect(
      toGridQuery({ startRow: 0, endRow: 100, sortModel: [], filterModel: {} })
    ).toEqual({ offset: 0, limit: 100 })
  })

  it("asks for blocks of 100 rows and keeps at most 10 of them", () => {
    expect(BLOCK_SIZE).toBe(100)
    expect(MAX_BLOCKS_IN_CACHE).toBe(10)
  })
})

interface Row {
  id: number
}

function request(
  startRow: number,
  sortModel: IGetRowsParams["sortModel"] = []
) {
  return {
    startRow,
    endRow: startRow + 100,
    sortModel,
    filterModel: {},
    successCallback: vi.fn(),
    failCallback: vi.fn(),
    api: undefined as never,
    context: undefined,
  } satisfies IGetRowsParams
}

const page = (rows: Row[], total: number): GridPage<Row> => ({
  rows,
  total,
  as_of: "2026-10-19T12:00:00Z",
})

describe("createDatasource", () => {
  it("answers AG Grid with the page's rows and its total as lastRow", async () => {
    const fetchRows = vi.fn((query: GridQuery) =>
      Promise.resolve(page([{ id: query.offset }], 250))
    )
    const onPage = vi.fn()
    const datasource = createDatasource(fetchRows, { onPage })
    const params = request(200)
    datasource.getRows(params)
    expect(datasource.pending).toBe(1)
    await vi.waitFor(() => {
      expect(params.successCallback).toHaveBeenCalledWith([{ id: 200 }], 250)
    })
    expect(fetchRows).toHaveBeenCalledWith(
      { offset: 200, limit: 100 },
      expect.any(AbortSignal)
    )
    expect(onPage).toHaveBeenCalledWith(page([{ id: 200 }], 250), {
      offset: 200,
      limit: 100,
    })
    expect(datasource.pending).toBe(0)
    expect(datasource.lastQuery).toEqual({ offset: 200, limit: 100 })
  })

  it("fails the block and reports the error when the request fails", async () => {
    const error = new ApiError(503, "Service Unavailable")
    const onError = vi.fn()
    const datasource = createDatasource<Row>(() => Promise.reject(error), {
      onError,
    })
    const params = request(0)
    datasource.getRows(params)
    await vi.waitFor(() => {
      expect(params.failCallback).toHaveBeenCalledOnce()
    })
    expect(params.successCallback).not.toHaveBeenCalled()
    expect(onError).toHaveBeenCalledWith(error, { offset: 0, limit: 100 })
    expect(datasource.pending).toBe(0)
  })

  it("fails the block when the answer is not a page of rows", async () => {
    const onError = vi.fn()
    const datasource = createDatasource<Row>(
      () => Promise.resolve({ db: "ok" } as unknown as GridPage<Row>),
      { onError }
    )
    const params = request(0)
    datasource.getRows(params)
    await vi.waitFor(() => {
      expect(params.failCallback).toHaveBeenCalledOnce()
    })
    expect(onError).toHaveBeenCalledOnce()
  })

  it("tells a request after a sort or filter change from a refresh of the same rows", () => {
    const onRequest = vi.fn()
    const datasource = createDatasource<Row>(
      () => Promise.resolve(page([], 0)),
      { onRequest }
    )
    const resets = () =>
      onRequest.mock.calls.map(([info]) => (info as { reset: boolean }).reset)
    datasource.getRows(request(0))
    datasource.getRows(request(100)) // the next block
    datasource.getRows(request(0)) // a refresh
    datasource.getRows(request(0, [{ colId: "id", sort: "asc" }])) // sorted
    expect(resets()).toEqual([true, false, false, true])
  })
})

/** A promise to settle by hand, for answers that arrive out of order. */
function deferred<T>() {
  let resolve: (value: T) => void = () => undefined
  let reject: (error: unknown) => void = () => undefined
  const promise = new Promise<T>((onResolve, onReject) => {
    resolve = onResolve
    reject = onReject
  })
  return { promise, resolve, reject }
}

function filtered(startRow: number, text: string) {
  return {
    ...request(startRow),
    filterModel: {
      notes: { filterType: "text", type: "contains", filter: text },
    },
  }
}

describe("createDatasource: answers out of order", () => {
  it("drops a superseded filter's answer that arrives last (a stale empty page)", async () => {
    const slow = deferred<GridPage<Row>>()
    const fetchRows = vi.fn((query: GridQuery) =>
      query.filter ? slow.promise : Promise.resolve(page([{ id: 1 }], 250))
    )
    const onPage = vi.fn()
    const datasource = createDatasource(fetchRows, { onPage })
    const zzz = filtered(0, "zzz")
    const cleared = request(0)
    datasource.getRows(zzz) // typed in a filter: slow
    datasource.getRows(cleared) // cleared it again: fast
    await vi.waitFor(() => {
      expect(onPage).toHaveBeenCalledOnce()
    })
    slow.resolve(page([], 0))
    await vi.waitFor(() => {
      expect(zzz.successCallback).toHaveBeenCalledWith([], 0)
    })
    // AG Grid gets both answers (it ignores the stale one itself); the grid's state only the
    // current one.
    expect(onPage).toHaveBeenCalledOnce()
    expect(onPage).toHaveBeenCalledWith(page([{ id: 1 }], 250), {
      offset: 0,
      limit: 100,
    })
  })

  it("drops a superseded filter's failure", async () => {
    const slow = deferred<GridPage<Row>>()
    const fetchRows = vi.fn((query: GridQuery) =>
      query.filter ? slow.promise : Promise.resolve(page([{ id: 1 }], 250))
    )
    const onError = vi.fn()
    const onPage = vi.fn()
    const datasource = createDatasource(fetchRows, { onError, onPage })
    const zzz = filtered(0, "zzz")
    datasource.getRows(zzz)
    datasource.getRows(request(0))
    await vi.waitFor(() => {
      expect(onPage).toHaveBeenCalledOnce()
    })
    slow.reject(new ApiError(503, "Service Unavailable"))
    await vi.waitFor(() => {
      expect(zzz.failCallback).toHaveBeenCalledOnce()
    })
    expect(onError).not.toHaveBeenCalled()
    expect(datasource.pending).toBe(0)
  })

  it("drops a block's earlier request when a refresh asks for it again", async () => {
    const first = deferred<GridPage<Row>>()
    const second = deferred<GridPage<Row>>()
    const answers = [first.promise, second.promise]
    const onPage = vi.fn()
    const onError = vi.fn()
    const datasource = createDatasource<Row>(
      () => answers.shift() ?? Promise.reject(new Error("unexpected")),
      { onPage, onError }
    )
    datasource.getRows(request(0))
    datasource.getRows(request(0)) // refreshInfiniteCache: the same block again
    second.resolve(page([{ id: 2 }], 2))
    await vi.waitFor(() => {
      expect(onPage).toHaveBeenCalledOnce()
    })
    first.reject(new Error("too late"))
    await vi.waitFor(() => {
      expect(datasource.pending).toBe(0)
    })
    expect(onError).not.toHaveBeenCalled()
    expect(onPage).toHaveBeenCalledWith(page([{ id: 2 }], 2), {
      offset: 0,
      limit: 100,
    })
  })

  it("aborts a request once its answer is not wanted: asked again, another filter, retired", () => {
    const signals: AbortSignal[] = []
    const datasource = createDatasource<Row>((_query, signal) => {
      signals.push(signal)
      return new Promise<GridPage<Row>>(() => undefined)
    })
    datasource.getRows(request(0))
    datasource.getRows(request(100))
    datasource.getRows(request(0)) // a refresh: block 0 again
    expect(signals.map((signal) => signal.aborted)).toEqual([
      true,
      false,
      false,
    ])
    datasource.getRows(filtered(0, "a")) // another filter: every block of the old one
    expect(signals.map((signal) => signal.aborted)).toEqual([
      true,
      true,
      true,
      false,
    ])
    datasource.retire()
    expect(signals.every((signal) => signal.aborted)).toBe(true)
  })

  it("reports every block of the current sort and filter", async () => {
    const onPage = vi.fn()
    const datasource = createDatasource(
      (query: GridQuery) => Promise.resolve(page([{ id: query.offset }], 250)),
      { onPage }
    )
    datasource.getRows(request(0))
    datasource.getRows(request(100))
    await vi.waitFor(() => {
      expect(onPage).toHaveBeenCalledTimes(2)
    })
  })

  it("counts sort and filter changes; once retired, fails a block at once without a request", async () => {
    const onPage = vi.fn()
    const onRequest = vi.fn()
    const fetchRows = vi.fn(() => Promise.resolve(page([{ id: 1 }], 1)))
    const datasource = createDatasource(fetchRows, { onPage, onRequest })
    expect(datasource.generation).toBe(0)
    datasource.getRows(request(0))
    datasource.getRows(request(0))
    expect(datasource.generation).toBe(1)
    datasource.getRows(filtered(0, "a"))
    expect(datasource.generation).toBe(2)
    await vi.waitFor(() => {
      expect(datasource.pending).toBe(0)
    })
    datasource.retire()
    const calls = [
      fetchRows.mock.calls.length,
      onPage.mock.calls.length,
      onRequest.mock.calls.length,
    ]
    // A block AG Grid had queued before the grid went (it asks after a tick).
    const last = filtered(0, "a")
    datasource.getRows(last)
    expect(last.failCallback).toHaveBeenCalledOnce()
    expect(last.successCallback).not.toHaveBeenCalled()
    expect([
      fetchRows.mock.calls.length,
      onPage.mock.calls.length,
      onRequest.mock.calls.length,
    ]).toEqual(calls)
    expect(datasource.pending).toBe(0)
  })
})
