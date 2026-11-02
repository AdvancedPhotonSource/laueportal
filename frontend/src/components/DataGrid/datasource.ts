// AG Grid's Infinite Row Model on the server's grid query protocol: a block request's rows, sort model
// and filter model become `offset`, `limit`, `sort` and `filter`, and the answer's `total` is AG
// Grid's `lastRow`.

import type {
  FilterModel,
  IDatasource,
  IGetRowsParams,
  SortModelItem,
} from "ag-grid-community"

/** Rows per block request (the protocol's default `limit`). */
export const BLOCK_SIZE = 100

/** Blocks kept in memory (1000 rows); scrolling further drops the least recently used. */
export const MAX_BLOCKS_IN_CACHE = 10

/** One request of the grid query protocol. */
export interface GridQuery {
  offset: number
  limit: number
  /** Comma list of fields, `-` for descending (`-scan_number,time`). */
  sort?: string
  /** The filter model as JSON. */
  filter?: string
  /** Live lists only: the rows changed since this server time, the previous answer's `as_of`. */
  changed_since?: string
}

/** A page of a grid list: every grid list answers so (ScanPage, JobPage, ...). */
export interface GridPage<TRow> {
  rows: TRow[]
  /** Rows matching the filter: AG Grid's lastRow. */
  total: number
  /** The server's time (UTC) just before the page was read. */
  as_of: string
}

/**
 * Loads one page, e.g. `(query, signal) => listScans(query, { signal })`. The signal aborts a request
 * whose answer nobody wants any more: its block was asked again (a refresh), the sort or filter
 * changed, or the grid has another list or is gone. Aborting frees the browser's connection and
 * drops the answer early; it does not stop the server's work (a sync route runs to its end). A list
 * that ignores the signal still works: AG Grid ignores the answer for a block it no longer holds.
 */
export type FetchRows<TRow> = (
  query: GridQuery,
  signal: AbortSignal
) => Promise<GridPage<TRow>>

function pad(value: number, width = 2): string {
  return String(Math.trunc(value)).padStart(width, "0")
}

/** A UTC offset in minutes east as `+05:30` or `-06:00`. */
function formatOffset(minutesEast: number): string {
  const sign = minutesEast < 0 ? "-" : "+"
  const minutes = Math.abs(Math.round(minutesEast))
  return `${sign}${pad(minutes / 60)}:${pad(minutes % 60)}`
}

const DAY = /^(\d{4})-(\d{2})-(\d{2})(?:$|[ T])/

/**
 * A date of AG Grid's date filter (`2026-01-01 00:00:00`, a naive local date) as the start of that
 * local day with its UTC offset (`2026-01-01T00:00:00-06:00`), which the API reads as an instant
 * (without an offset it would read a UTC day). The offset is the one in force on that day, not
 * today's; where the clocks skip midnight, the day starts at the first local time after it. A value
 * that is not a date is sent as it is (the API refuses it with a 422).
 */
export function localDayStart(value: string): string {
  const match = DAY.exec(value)
  if (!match) return value
  const [, year, month, day] = match.map(Number) as [
    number,
    number,
    number,
    number,
  ]
  const start = new Date(2000, month - 1, day)
  start.setFullYear(year, month - 1, day) // years 0-99 stay themselves
  start.setHours(0, 0, 0, 0)
  const date = `${pad(start.getFullYear(), 4)}-${pad(start.getMonth() + 1)}-${pad(start.getDate())}`
  const time = `${pad(start.getHours())}:${pad(start.getMinutes())}:${pad(start.getSeconds())}`
  return `${date}T${time}${formatOffset(-start.getTimezoneOffset())}`
}

/** The sort model as the protocol's `sort`; undefined when unsorted (the endpoint's default). */
export function toSortParam(
  sortModel: readonly SortModelItem[]
): string | undefined {
  const fields = sortModel.map(
    (item) => `${item.sort === "desc" ? "-" : ""}${item.colId}`
  )
  return fields.length > 0 ? fields.join(",") : undefined
}

type Model = Record<string, unknown>

function isModel(value: unknown): value is Model {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

/** One condition, with only the members the protocol reads; undefined for an unset one. */
function toCondition(model: Model): Model | undefined {
  const { filterType, type } = model
  if (typeof type !== "string" || type === "" || type === "empty") {
    return undefined
  }
  const condition: Model = { type }
  if (filterType !== undefined) condition.filterType = filterType
  if (filterType === "date") {
    for (const key of ["dateFrom", "dateTo"] as const) {
      const value = model[key]
      if (typeof value === "string" && value !== "") {
        condition[key] = localDayStart(value)
      }
    }
  } else {
    for (const key of ["filter", "filterTo"] as const) {
      const value = model[key]
      if (value !== undefined && value !== null) condition[key] = value
    }
  }
  return condition
}

/** A column's model: one condition, or two joined by AND/OR. */
function toColumnFilter(model: Model): Model | undefined {
  if (!Array.isArray(model.conditions)) return toCondition(model)
  const conditions = model.conditions
    .filter(isModel)
    .map(toCondition)
    .filter((condition) => condition !== undefined)
  if (conditions.length < 2) return conditions[0]
  const combined: Model = { operator: model.operator, conditions }
  if (model.filterType !== undefined) combined.filterType = model.filterType
  return combined
}

/**
 * The filter model as the protocol's `filter` (JSON); undefined when nothing filters. Date
 * conditions get their local day's offset (localDayStart); unset conditions are left out.
 */
export function toFilterParam(
  filterModel: FilterModel | null | undefined
): string | undefined {
  if (!filterModel) return undefined
  const filter: Record<string, Model> = {}
  for (const [field, model] of Object.entries(filterModel)) {
    if (!isModel(model)) continue
    const converted = toColumnFilter(model)
    if (converted) filter[field] = converted
  }
  return Object.keys(filter).length > 0 ? JSON.stringify(filter) : undefined
}

/** A block request of the Infinite Row Model as a grid query. */
export function toGridQuery(
  params: Pick<
    IGetRowsParams,
    "startRow" | "endRow" | "sortModel" | "filterModel"
  >
): GridQuery {
  const query: GridQuery = {
    offset: params.startRow,
    limit: params.endRow - params.startRow,
  }
  const sort = toSortParam(params.sortModel)
  const filter = toFilterParam(params.filterModel as FilterModel | null)
  if (sort !== undefined) query.sort = sort
  if (filter !== undefined) query.filter = filter
  return query
}

function isGridPage(value: unknown): value is GridPage<unknown> {
  return (
    isModel(value) &&
    Array.isArray(value.rows) &&
    typeof value.total === "number" &&
    typeof value.as_of === "string"
  )
}

export interface DatasourceEvents<TRow> {
  /**
   * A block request starts. `reset` is true for the first request after the sort or filter changed
   * (or the first of all), when AG Grid shows no rows until it answers.
   */
  onRequest?: (info: { reset: boolean; query: GridQuery }) => void
  /**
   * A block's answer, only when it is current: the latest request for that block (its `offset`)
   * under the grid's current sort and filter. An answer to a request that a sort or filter change,
   * a refresh or a reload has replaced is dropped here, as AG Grid drops it.
   */
  onPage?: (page: GridPage<TRow>, query: GridQuery) => void
  /** A block's request failed; only when it is current, as for onPage. */
  onError?: (error: unknown, query: GridQuery) => void
}

export interface GridDatasource extends IDatasource {
  /** Block requests not answered yet. */
  readonly pending: number
  /** The query of the latest block request (its sort and filter are the grid's). */
  readonly lastQuery: GridQuery | undefined
  /** Counts sort and filter changes: an answer asked for under another generation is stale. */
  readonly generation: number
  /** Stop reporting and abort the requests still out: the grid has another datasource now. */
  retire: () => void
}

/**
 * The Infinite Row Model's datasource: each block request goes to `fetchRows` as a grid query, and
 * its page answers AG Grid (`total` as lastRow). Only current answers reach `events`.
 */
export function createDatasource<TRow>(
  fetchRows: FetchRows<TRow>,
  events: DatasourceEvents<TRow> = {}
): GridDatasource {
  let pending = 0
  let lastQuery: GridQuery | undefined
  let modelsKey: string | undefined
  let generation = 0
  let retired = false
  let lastRequest = 0
  // The latest request of each block (by its first row) under the current generation.
  const latest = new Map<number, number>()
  // The requests still out that are current, by block: one that stops being current is aborted.
  const inFlight = new Map<number, AbortController>()
  const abortAll = () => {
    for (const controller of inFlight.values()) controller.abort()
    inFlight.clear()
  }
  return {
    get pending() {
      return pending
    },
    get lastQuery() {
      return lastQuery
    },
    get generation() {
      return generation
    },
    retire() {
      retired = true
      abortAll()
    },
    getRows(params: IGetRowsParams) {
      // AG Grid asks a queued block after a tick, even when the grid has gone or has another list
      // meanwhile: a retired datasource fails it at once, without a request (nobody wants the answer).
      if (retired) {
        params.failCallback()
        return
      }
      const query = toGridQuery(params)
      const key = JSON.stringify([query.sort ?? "", query.filter ?? ""])
      const reset = key !== modelsKey
      if (reset) {
        generation += 1
        latest.clear()
        abortAll()
      }
      modelsKey = key
      lastQuery = query
      lastRequest += 1
      const request = lastRequest
      const asked = generation
      latest.set(params.startRow, request)
      inFlight.get(params.startRow)?.abort()
      const controller = new AbortController()
      inFlight.set(params.startRow, controller)
      const current = () =>
        !retired &&
        asked === generation &&
        latest.get(params.startRow) === request
      pending += 1
      events.onRequest?.({ reset, query })
      void fetchRows(query, controller.signal)
        .then((page: unknown) => {
          if (!isGridPage(page)) {
            throw new Error("The list's answer is not a page of rows")
          }
          // AG Grid itself ignores the answer for a block it has replaced.
          params.successCallback(page.rows, page.total)
          if (current()) events.onPage?.(page as GridPage<TRow>, query)
        })
        .catch((error: unknown) => {
          params.failCallback()
          if (current()) events.onError?.(error, query)
        })
        .finally(() => {
          pending -= 1
          if (inFlight.get(params.startRow) === controller) {
            inFlight.delete(params.startRow)
          }
        })
    },
  }
}
