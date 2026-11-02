import type {
  CellKeyDownEvent,
  ColumnMovedEvent,
  ColumnResizedEvent,
  ColumnVisibleEvent,
  FullWidthCellKeyDownEvent,
  GetRowIdParams,
  GridApi,
  GridPreDestroyedEvent,
  GridReadyEvent,
  GridState,
  IRowNode,
  ModelUpdatedEvent,
  OverlayType,
  SelectionChangedEvent,
  SelectionColumnDef,
  StateUpdatedEvent,
} from "ag-grid-community"
import { AgGridReact } from "ag-grid-react"
import {
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
  type Ref,
} from "react"

import { ErrorState } from "@/components/ErrorState"
import { isGone } from "@/lib/polling"
import { isActiveStatus } from "@/lib/run-status"
import { cn } from "@/lib/utils"
import {
  browserStorage,
  COLUMN_STATE_SOURCES,
  initialGridState,
  pickColumnState,
  readColumnState,
  sameColumnState,
  SELECTION_COLUMN_ID,
  writeColumnState,
} from "./column-state"
import {
  pinnedColumns,
  statusField,
  toColumnDefs,
  type GridColumn,
} from "./columns"
import {
  BLOCK_SIZE,
  createDatasource,
  MAX_BLOCKS_IN_CACHE,
  type FetchRows,
  type GridDatasource,
} from "./datasource"
import { BlockStatus, heldBlocks, type GridStatus } from "./grid-status"
import { GridOverlay, type OverlayContent } from "./GridOverlay"
import { followCellLink } from "./keyboard"
import {
  CHANGES_LIMIT,
  earlierTime,
  LIVE_POLL_MS,
  livePollDelay,
  planLiveUpdate,
} from "./live"
import { rowSelectionOptions, type GridSelection } from "./selection"
import { GridOverlayContext, GridRetryContext } from "./overlay-context"
import { columnsOverflow, pinnedSide, pinningUpdate } from "./pinning"
import { GRID_MODULES, gridTheme } from "./theme"
import "./DataGrid.css"

export interface DataGridHandle {
  /** Unselect every row, the loaded ones and the others. */
  clearSelection: () => void
  /** Load the rows shown again (a page's Refresh button, or after an action changed them). */
  refresh: () => void
  /**
   * Show or hide these columns (by field), e.g. from a page's column checklist. Kept with the
   * grid's column state (its gridId), as a column hidden any other way.
   */
  setColumnsShown: (fields: readonly string[], shown: boolean) => void
  /**
   * Select exactly these rows (by row ID) among the rows the grid holds: all of them with `rows`
   * (a page's quick select), the loaded ones with `fetchRows`. Rows that cannot be selected stay so.
   */
  selectRows: (ids: readonly string[]) => void
}

/** How many rows the list has: under the filters, and whether any filter applies. */
export interface GridRowCount {
  total: number
  filtered: boolean
}

/** Where a grid's rows come from: the server, or the page (for the grid's whole life). */
export type GridRows<TRow> =
  | {
      /**
       * Loads a page of the grid query protocol, e.g. `(query) => listScans(query)`. The grid calls
       * the latest one at each request, so it may be an inline function: a new function alone
       * reloads nothing (see `listKey`).
       */
      fetchRows: FetchRows<TRow>
      rows?: never
    }
  | {
      /**
       * The rows themselves, for a list the page holds whole (a small one, e.g. a scan log's index):
       * AG Grid's client-side model sorts and filters them in the browser, and the header's box
       * selects every selectable row the filters show. Pass them from the first render (an empty
       * array while they load): the row model is chosen once. A new array updates the rows by ID
       * (the selection kept); another `listKey` drops the selection.
       */
      rows: readonly TRow[]
      fetchRows?: never
    }

/** A DataGrid's props: its options and the source of its rows (`GridRows`). */
export type DataGridProps<TRow> = DataGridOptions<TRow> & GridRows<TRow>

interface DataGridOptions<TRow> {
  /**
   * Names the grid's kept column state (`laueportal:grid:<gridId>:v1`). Another gridId makes
   * another grid: its own columns' state, no selection, the default sort and no filters.
   */
  gridId: string
  /** The columns, in their default order; keep the array stable (module scope or useMemo). */
  columns: readonly GridColumn<TRow>[]
  /**
   * Names the list, for a list that depends on the page's parameters (e.g. a scan's runs: the scan
   * ID). Another listKey is another list: the grid loads it with the same sort and filters, without
   * the old list's selection. Without one, the list is always the same.
   */
  listKey?: string | number
  /** A row's ID: selection is kept by it across refreshes, paging, sorting and filtering. */
  getRowId: (row: TRow) => string
  /**
   * Checkbox selection, set per grid: `singleRow` (one checked row: a list whose actions take one
   * source) or `multiRow` (bulk actions, e.g. the Run Monitor's Stop). None by default.
   */
  selection?: GridSelection
  /**
   * Which rows can be selected (all by default); the others' checkboxes are disabled. Keep it stable (module
   * scope or useCallback): a new function resets AG Grid's selection options.
   */
  isRowSelectable?: (row: TRow) => boolean
  /**
   * The selected rows, in the order they were selected, whenever the selection changes, and again
   * when a selected row's data changes (a refresh or live update), so rules on the rows' values
   * (e.g. Stop for running runs) follow them.
   */
  onSelectionChange?: (rows: TRow[]) => void
  /**
   * Refresh the loaded rows every 5 s while a visible row is queued or running (its status column);
   * backed off after failures, stopped by a 404 until an answer comes (live.ts).
   */
  live?: boolean
  /**
   * The list takes `changed_since`: live refresh asks for the changes first, and every 15 s while no
   * visible row is active (a run queued elsewhere appears).
   */
  changedSince?: boolean
  /** What the rows are, plural ("scans"), for the loading and empty states. */
  noun?: string
  /**
   * The row count of each current answer (a page's "N total"), or undefined while the rows of a
   * new sort, filter or list load, or after they failed to load.
   */
  onRowCount?: (count: GridRowCount | undefined) => void
  /** The fields of the columns shown, once the grid is made and whenever a column is shown or hidden. */
  onShownColumnsChange?: (fields: string[]) => void
  className?: string
  ref?: Ref<DataGridHandle>
}

// Always the first column, also when another column is pinned beside it (GridColumn.pinned).
const SELECTION_COLUMN: SelectionColumnDef = {
  pinned: "left",
  lockPinned: true,
  lockPosition: "left",
  width: 40,
  minWidth: 40,
  maxWidth: 40,
  resizable: false,
  sortable: false,
  suppressHeaderMenuButton: true,
}

const SUPPRESSED_OVERLAYS: OverlayType[] = ["noMatchingRows"]

function sortFields(sort: string | undefined): string[] {
  return sort ? sort.split(",").map((field) => field.replace(/^-/, "")) : []
}

function overlayFor(
  status: GridStatus,
  noun: string
): OverlayContent | undefined {
  switch (status.kind) {
    case "idle":
      return undefined
    case "loading":
      return { kind: "loading", label: `Loading ${noun}` }
    case "ready":
      if (status.total > 0) return undefined
      return status.filtered
        ? {
            kind: "empty",
            title: `No ${noun} match the filters`,
            filtered: true,
          }
        : { kind: "empty", title: `No ${noun}`, filtered: false }
    case "error":
      return status.hasRows
        ? undefined
        : { kind: "error", error: status.error, retrying: status.retrying }
  }
}

/**
 * Give the keyboard to the grid: the first row shown (its first column after the checkboxes), or the
 * header when there is no row.
 */
function focusFirstRow<TRow>(api: GridApi<TRow>) {
  const columns = api.getAllDisplayedColumns()
  const column =
    columns.find((candidate) => candidate.getColId() !== SELECTION_COLUMN_ID) ??
    columns[0]
  if (!column) return
  const row = api
    .getRenderedNodes()
    .find((node) => node.data !== undefined && node.rowIndex !== null)
  if (row?.rowIndex != null) api.setFocusedCell(row.rowIndex, column)
  else api.setFocusedHeader(column)
}

/**
 * The app's data grid: AG Grid's Infinite Row Model on the grid query protocol, in blocks of 100
 * rows (at most 10 blocks kept), or its client-side model on rows the page holds (`rows`); with
 * floating filters, checkbox multi-select kept by row ID, column order, widths and visibility
 * kept per `gridId`, live refresh, the app's theme, and loading, empty and error states.
 */
export function DataGrid<TRow extends object>({
  gridId,
  columns,
  fetchRows,
  rows,
  listKey,
  getRowId,
  selection,
  isRowSelectable,
  onSelectionChange,
  live = false,
  changedSince = false,
  noun = "rows",
  onRowCount,
  onShownColumnsChange,
  className,
  ref,
}: DataGridProps<TRow>) {
  const apiRef = useRef<GridApi<TRow> | null>(null)
  const datasourceRef = useRef<GridDatasource | null>(null)
  const blocksRef = useRef<BlockStatus | null>(null)
  // The `changed_since` to send next: the earliest `as_of` of the rows loaded since the last update.
  const baselineRef = useRef<string | undefined>(undefined)
  // The latest answer's error (a block's or the changes'), undefined after an answer with rows: live
  // refresh backs off while answers fail and stops on a 404.
  const lastErrorRef = useRef<unknown>(undefined)
  // A Retry was pressed: once its reload has loaded, the rows get the keyboard (see Retry below).
  const focusAfterRetryRef = useRef(false)
  const fetchRowsRef = useRef(fetchRows)
  // The row model is AG Grid's initial option: the one the grid starts with stays (`rows`).
  const [clientSide] = useState(rows !== undefined)
  const rowSelection = useMemo(
    () =>
      selection &&
      rowSelectionOptions<TRow>(selection, clientSide, isRowSelectable),
    [selection, clientSide, isRowSelectable]
  )
  const listKeyRef = useRef(listKey)
  const getRowIdRef = useRef(getRowId)
  const onSelectionChangeRef = useRef(onSelectionChange)
  const onRowCountRef = useRef(onRowCount)
  const onShownColumnsChangeRef = useRef(onShownColumnsChange)
  // The selected rows last told to the page (as JSON), to tell it again only when one has changed.
  const emittedRef = useRef<string | undefined>(undefined)
  // A column was shown, hidden or moved and its state is not stored yet (see onStateUpdated).
  const columnsChangedRef = useRef(false)
  // The columns kept in view while the grid scrolls sideways (GridColumn.pinned).
  const pinned = useMemo(() => pinnedColumns(columns), [columns])
  const pinnedRef = useRef(pinned)
  useEffect(() => {
    fetchRowsRef.current = fetchRows
    getRowIdRef.current = getRowId
    onSelectionChangeRef.current = onSelectionChange
    onRowCountRef.current = onRowCount
    onShownColumnsChangeRef.current = onShownColumnsChange
    pinnedRef.current = pinned
  })

  const [status, setStatus] = useState<GridStatus>({ kind: "idle" })
  // Tell the page the row count of each current answer (none while loading or failed).
  const readyTotal = status.kind === "ready" ? status.total : undefined
  const readyFiltered = status.kind === "ready" ? status.filtered : undefined
  useEffect(() => {
    onRowCountRef.current?.(
      readyTotal === undefined || readyFiltered === undefined
        ? undefined
        : { total: readyTotal, filtered: readyFiltered }
    )
  }, [readyTotal, readyFiltered])
  const columnDefs = useMemo(() => toColumnDefs(columns), [columns])
  // Read when the grid is made (it is made again for another gridId, see the key below).
  const initialState = useMemo(
    () =>
      initialGridState(readColumnState(browserStorage(), gridId), [
        SELECTION_COLUMN_ID,
      ]),
    [gridId]
  )
  const liveField = live && !clientSide ? statusField(columns) : undefined

  const rowId = useCallback(
    ({ data }: GetRowIdParams<TRow>) => getRowIdRef.current(data),
    []
  )

  // Tell the page the selected rows (selection changes always; data changes only when they differ).
  const emitSelection = useCallback(
    (api: GridApi<TRow>, onlyIfChanged: boolean) => {
      const rows = api.getSelectedRows()
      const key = JSON.stringify(rows)
      if (onlyIfChanged && key === emittedRef.current) return
      emittedRef.current = key
      onSelectionChangeRef.current?.(rows)
    },
    []
  )

  // A datasource for the grid: made with the grid (in onGridReady) and again for another list.
  // Only its current answers set the status (see createDatasource), block by block.
  const installDatasource = useCallback((api: GridApi<TRow>) => {
    datasourceRef.current?.retire()
    baselineRef.current = undefined
    const blocks = new BlockStatus()
    blocksRef.current = blocks
    const datasource = createDatasource<TRow>(
      (query, signal) =>
        fetchRowsRef.current
          ? fetchRowsRef.current(query, signal)
          : Promise.reject(new Error("The grid has no fetchRows")),
      {
        onRequest: ({ reset, query }) => {
          if (reset) baselineRef.current = undefined
          const next = blocks.request(reset, query)
          if (next) setStatus(next)
        },
        onPage: (page, query) => {
          lastErrorRef.current = undefined
          baselineRef.current = earlierTime(baselineRef.current, page.as_of)
          setStatus(blocks.page(page, query))
        },
        onError: (error, query) => {
          // Failed (again): Retry, still enabled, keeps the focus. Only an answer says so: a status
          // made before the retried block is asked again (another block's answer) is no failure.
          focusAfterRetryRef.current = false
          lastErrorRef.current = error
          setStatus(blocks.error(error, query))
        },
      }
    )
    datasourceRef.current = datasource
    api.setGridOption("datasource", datasource)
  }, [])

  // Tell the page the columns shown (its column checklist).
  const emitShownColumns = useCallback((api: GridApi<TRow>) => {
    const shown = (api.getColumns() ?? [])
      .filter((column) => column.isVisible())
      .map((column) => column.getColId())
      .filter((colId) => colId !== SELECTION_COLUMN_ID)
    onShownColumnsChangeRef.current?.(shown)
  }, [])

  const onGridReady = (event: GridReadyEvent<TRow>) => {
    apiRef.current = event.api
    columnsChangedRef.current = false
    setStatus({ kind: "idle" })
    emitShownColumns(event.api)
    // A new grid (another gridId) starts without a selection: tell the page.
    emitSelection(event.api, false)
    if (!clientSide) installDatasource(event.api)
  }

  // The client-side model's count: the rows the filters show.
  const countClientRows = (api: GridApi<TRow>) => {
    setStatus({
      kind: "ready",
      total: api.getDisplayedRowCount(),
      filtered: api.isAnyFilterPresent(),
    })
  }

  // Reload the blocks AG Grid holds (Retry, live refresh). The failures of the blocks it has dropped
  // are forgotten first: a reload does not ask for those again (scrolling back to one does).
  // Counts the reloads: a live update asked before the latest one is older than its rows.
  const reloadsRef = useRef(0)
  const reload = useCallback(
    (api: GridApi<TRow>) => {
      reloadsRef.current += 1
      baselineRef.current = undefined
      const next = blocksRef.current?.retain(
        heldBlocks(api.getCacheBlockState())
      )
      if (next) setStatus(next)
      if (!clientSide) api.refreshInfiniteCache()
    },
    [clientSide]
  )

  useImperativeHandle(
    ref,
    () => ({
      clearSelection: () => {
        apiRef.current?.deselectAll()
      },
      refresh: () => {
        if (apiRef.current) reload(apiRef.current)
      },
      setColumnsShown: (fields, shown) => {
        apiRef.current?.setColumnsVisible([...fields], shown)
      },
      selectRows: (ids) => {
        const api = apiRef.current
        if (!api) return
        const wanted = new Set(ids)
        const select: IRowNode<TRow>[] = []
        const unselect: IRowNode<TRow>[] = []
        api.forEachNode((node) => {
          if (node.id === undefined || !node.data) return
          if (wanted.has(node.id)) {
            if (!node.isSelected()) select.push(node)
          } else if (node.isSelected()) unselect.push(node)
        })
        if (unselect.length)
          api.setNodesSelected({ nodes: unselect, newValue: false })
        if (select.length)
          api.setNodesSelected({ nodes: select, newValue: true })
      },
    }),
    [reload]
  )

  // Keep the `pinned` columns in view while the columns overflow the grid (it scrolls sideways), and
  // unpin them when every column fits (pinning.ts). Run when the grid's size or the columns change.
  const fitPinning = useCallback((api: GridApi<TRow>) => {
    const wanted = pinnedRef.current
    if (wanted.length === 0 || api.isDestroyed()) return
    const { left, right } = api.getHorizontalPixelRange()
    if (right - left <= 0) return // not laid out yet
    const overflow = columnsOverflow(
      api.getAllDisplayedColumns().map((column) => ({
        pinned: pinnedSide(column.getPinned()),
        width: column.getActualWidth(),
        minWidth: column.getMinWidth(),
        flex: (column.getFlex() ?? 0) > 0,
      })),
      right - left
    )
    const state = pinningUpdate(
      wanted,
      (colId) => pinnedSide(api.getColumn(colId)?.getPinned()),
      overflow
    )
    if (state) api.applyColumnState({ state })
  }, [])

  useEffect(() => {
    if (apiRef.current) fitPinning(apiRef.current)
  }, [pinned, fitPinning])

  // Another listKey is another list (e.g. another scan's runs): load it with the same sort and
  // filters, without the old list's selection. (The ref effect above has the new fetchRows.)
  useEffect(() => {
    if (listKeyRef.current === listKey) return
    listKeyRef.current = listKey
    const api = apiRef.current
    if (!api) return
    api.deselectAll()
    if (!clientSide) installDatasource(api)
  }, [listKey, installDatasource, clientSide])

  // Live refresh while the tab shows: every 5 s while a visible row is queued or running, else every
  // 15 s for a list that takes changed_since; a tick every 5 s decides (live.ts).
  useEffect(() => {
    if (liveField === undefined) return
    const isActive = (row: TRow) =>
      isActiveStatus((row as Record<string, unknown>)[liveField])
    let busy = false
    // Since the last poll; and the consecutive polls whose answers failed.
    let waited = 0
    let failures = 0
    // A poll was made whose outcome is not counted yet (its answers come through the datasource).
    let counting = false
    // Aborts the changes asked for when the polling stops.
    const controller = new AbortController()
    const tick = async () => {
      waited += LIVE_POLL_MS
      const api = apiRef.current
      const datasource = datasourceRef.current
      if (!api || !datasource || busy) return
      if (datasource.pending > 0) return
      if (counting) {
        counting = false
        failures = lastErrorRef.current === undefined ? 0 : failures + 1
      }
      // Gone (a 404): no more polls until an answer comes (Retry, another sort, filter or list).
      if (isGone(lastErrorRef.current) || document.hidden) return
      const visible = api.getRenderedNodes()
      const active = visible.some((node) => node.data && isActive(node.data))
      if (!active && !changedSince) return
      if (waited < livePollDelay(active, failures)) return
      waited = 0
      counting = true
      busy = true
      try {
        const query = datasource.lastQuery
        const since = baselineRef.current
        if (!changedSince || !query || since === undefined) {
          reload(api)
          return
        }
        const generation = datasource.generation
        const reloads = reloadsRef.current
        const fetchPage = fetchRowsRef.current
        if (!fetchPage) return
        const page = await fetchPage(
          {
            offset: 0,
            limit: CHANGES_LIMIT,
            sort: query.sort,
            filter: query.filter,
            changed_since: since,
          },
          controller.signal
        )
        // Asked under another sort or filter, or for another list, or before a reload (Refresh, or
        // after a page's action): stale, its rows older than those loaded since.
        const current =
          apiRef.current === api &&
          datasourceRef.current === datasource &&
          datasource.generation === generation &&
          reloadsRef.current === reloads
        if (!current) return
        const loaded = new Map<string, TRow>()
        api.forEachNode((node) => {
          if (node.id !== undefined && node.data) loaded.set(node.id, node.data)
        })
        const plan = planLiveUpdate({
          changes: page.rows,
          total: page.total,
          loaded,
          getRowId: getRowIdRef.current,
          isActive,
          sortFields: sortFields(query.sort),
        })
        lastErrorRef.current = undefined
        if (plan.kind === "refresh") {
          reload(api)
          return
        }
        for (const row of plan.rows) {
          api.getRowNode(getRowIdRef.current(row))?.setData(row)
        }
        emitSelection(api, true)
        baselineRef.current = page.as_of
      } catch (error) {
        // The changes could not be read: reload the blocks instead (their errors show, with Retry);
        // not when the polling has stopped (it aborted them). A 404 stops the polling: its reload,
        // once, shows the list's own answer (the error, or rows again, which resume it).
        if (controller.signal.aborted) return
        if (apiRef.current === api && datasourceRef.current === datasource) {
          lastErrorRef.current = error
          reload(api)
        }
      } finally {
        busy = false
      }
    }
    const timer = window.setInterval(() => {
      void tick()
    }, LIVE_POLL_MS)
    return () => {
      window.clearInterval(timer)
      controller.abort()
    }
  }, [liveField, changedSince, reload, emitSelection])

  // Retry (the error over the rows, or the banner over rows already loaded): once its reload has
  // loaded, its button is gone with the error, so the keyboard goes to the rows (the effect below).
  // A request that fails meanwhile clears it (installDatasource's onError): Retry stays, focused.
  const bannerRef = useRef<HTMLDivElement>(null)
  const retryOverlay = useCallback(() => {
    focusAfterRetryRef.current = true
    apiRef.current?.refreshInfiniteCache()
  }, [])
  const retryBanner = useCallback(() => {
    focusAfterRetryRef.current = true
    if (apiRef.current) reload(apiRef.current)
  }, [reload])
  useEffect(() => {
    if (!focusAfterRetryRef.current || status.kind !== "ready") return
    focusAfterRetryRef.current = false
    // After AG Grid has drawn the rows (and taken the error's overlay down).
    requestAnimationFrame(() => {
      const api = apiRef.current
      if (!api || api.isDestroyed()) return
      const active = document.activeElement
      const lost =
        active === null ||
        active === document.body ||
        active.closest(".ag-overlay") !== null ||
        bannerRef.current?.contains(active) === true
      if (lost) focusFirstRow(api)
    })
  }, [status])

  const overlay = overlayFor(status, noun)

  return (
    <div className={cn("flex min-h-0 flex-col gap-2", className)}>
      {status.kind === "error" && status.hasRows && (
        <div ref={bannerRef}>
          <ErrorState
            error={status.error}
            onRetry={retryBanner}
            refreshing={status.retrying}
            stale
          />
        </div>
      )}
      <div className="min-h-0 flex-1" data-slot="data-grid">
        <GridRetryContext value={retryOverlay}>
          <GridOverlayContext value={overlay}>
            <AgGridReact<TRow>
              // Another gridId is another grid (its own column state, see DataGridProps.gridId).
              key={gridId}
              modules={GRID_MODULES}
              theme={gridTheme}
              containerStyle={{ height: "100%", width: "100%" }}
              rowModelType={clientSide ? "clientSide" : "infinite"}
              rowData={clientSide ? (rows as TRow[] | undefined) : undefined}
              cacheBlockSize={clientSide ? undefined : BLOCK_SIZE}
              maxBlocksInCache={clientSide ? undefined : MAX_BLOCKS_IN_CACHE}
              columnDefs={columnDefs}
              getRowId={rowId}
              rowSelection={rowSelection}
              selectionColumnDef={SELECTION_COLUMN}
              onSelectionChanged={(event: SelectionChangedEvent<TRow>) => {
                emitSelection(event.api, false)
              }}
              // Loaded rows (a refresh, a live reload, another page of rows): a selected row's
              // data may have changed.
              onModelUpdated={(event: ModelUpdatedEvent<TRow>) => {
                if (clientSide) countClientRows(event.api)
                emitSelection(event.api, true)
              }}
              initialState={initialState}
              // Store the column state when it changes. While it starts, AG Grid holds its state
              // events at each start-up step (columns made, grid ready, row count, first rows
              // drawn) for one tick, and ends each step with its own `gridInitializing` event: a
              // column shown, hidden or moved in such a tick (a page's checklist right after it
              // opens) is stored then, if the state differs from the stored one. A width dragged
              // in such a tick is not kept. A page that only opens stores nothing.
              onStateUpdated={(event: StateUpdatedEvent<TRow>) => {
                const state = pickColumnState(event.state)
                const storage = browserStorage()
                const changed = event.sources.some((source) =>
                  COLUMN_STATE_SOURCES.includes(source as keyof GridState)
                )
                const changedWhileStarting =
                  !changed &&
                  columnsChangedRef.current &&
                  event.sources.includes("gridInitializing") &&
                  !sameColumnState(state, readColumnState(storage, gridId))
                if (changed || changedWhileStarting) {
                  writeColumnState(storage, gridId, state)
                }
                // The change is stored, or a start-up event has the state with it.
                if (changed || event.sources.includes("gridInitializing")) {
                  columnsChangedRef.current = false
                }
              }}
              onGridReady={onGridReady}
              onGridSizeChanged={({ api }) => {
                fitPinning(api)
              }}
              onDisplayedColumnsChanged={({ api }) => {
                fitPinning(api)
              }}
              onColumnVisible={({ api, source }: ColumnVisibleEvent<TRow>) => {
                if (source !== "gridInitializing") {
                  columnsChangedRef.current = true
                }
                emitShownColumns(api)
              }}
              onColumnMoved={({ source }: ColumnMovedEvent<TRow>) => {
                if (source !== "gridInitializing") {
                  columnsChangedRef.current = true
                }
              }}
              onColumnResized={({
                api,
                finished,
              }: ColumnResizedEvent<TRow>) => {
                if (finished) fitPinning(api)
              }}
              onGridPreDestroyed={(event: GridPreDestroyedEvent<TRow>) => {
                // Under StrictMode a first grid can go after the second is ready: keep the second.
                if (apiRef.current !== event.api) return
                apiRef.current = null
                datasourceRef.current?.retire()
                datasourceRef.current = null
              }}
              // Enter on a cell follows its link (Tab reaches the links: see keyboard.ts).
              onCellKeyDown={(
                event: CellKeyDownEvent<TRow> | FullWidthCellKeyDownEvent<TRow>
              ) => {
                if (event.event instanceof KeyboardEvent) {
                  followCellLink(event.event)
                }
              }}
              // Dragging a header off the grid would hide its column, and nothing shows it again.
              suppressDragLeaveHidesColumns
              enableCellTextSelection
              ensureDomOrder
              // Every state shows in AG Grid's no-rows overlay: unlike a custom overlay (modal), it
              // covers only the rows, so the filters stay usable. Its content comes from context.
              noRowsOverlayComponent={GridOverlay}
              activeOverlay={overlay ? "agNoRowsOverlay" : undefined}
              // AG Grid's own "no matching rows" text could show for a moment before ours.
              suppressOverlays={SUPPRESSED_OVERLAYS}
            />
          </GridOverlayContext>
        </GridRetryContext>
      </div>
    </div>
  )
}
