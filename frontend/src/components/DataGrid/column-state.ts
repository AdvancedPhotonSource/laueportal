// A grid's column state, kept per gridId in localStorage: the columns' order, widths and visibility,
// under `laueportal:grid:<gridId>:v1`. A value that does not have this version's shape is discarded
// (no migrations). Sort and filters are not kept.

import type {
  ColumnOrderState,
  ColumnSizingState,
  ColumnVisibilityState,
  GridState,
} from "ag-grid-community"

export const COLUMN_STATE_VERSION = 1

export function columnStateKey(gridId: string): string {
  return `laueportal:grid:${gridId}:v${String(COLUMN_STATE_VERSION)}`
}

/** The kept part of AG Grid's state: it is given back as the grid's initialState. */
export interface StoredColumnState {
  columnOrder?: ColumnOrderState
  columnSizing?: ColumnSizingState
  columnVisibility?: ColumnVisibilityState
}

/** The parts of AG Grid's state that, when they change, are saved. */
export const COLUMN_STATE_SOURCES: readonly (keyof GridState)[] = [
  "columnOrder",
  "columnSizing",
  "columnVisibility",
]

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function isStringList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string")
}

function isOptionalNumber(value: unknown): boolean {
  return (
    value === undefined || (typeof value === "number" && Number.isFinite(value))
  )
}

/** The value as a StoredColumnState, or undefined when any part has another shape. */
export function parseColumnState(
  value: unknown
): StoredColumnState | undefined {
  if (!isObject(value)) return undefined
  const { columnOrder, columnSizing, columnVisibility } = value
  const state: StoredColumnState = {}
  if (columnOrder !== undefined) {
    if (!isObject(columnOrder) || !isStringList(columnOrder.orderedColIds)) {
      return undefined
    }
    state.columnOrder = { orderedColIds: columnOrder.orderedColIds }
  }
  if (columnSizing !== undefined) {
    const model = isObject(columnSizing)
      ? columnSizing.columnSizingModel
      : undefined
    if (
      !Array.isArray(model) ||
      !model.every(
        (size) =>
          isObject(size) &&
          typeof size.colId === "string" &&
          isOptionalNumber(size.width) &&
          isOptionalNumber(size.flex)
      )
    ) {
      return undefined
    }
    state.columnSizing = {
      columnSizingModel: model.map((size: Record<string, unknown>) => ({
        colId: size.colId as string,
        ...(size.width !== undefined && { width: size.width as number }),
        ...(size.flex !== undefined && { flex: size.flex as number }),
      })),
    }
  }
  if (columnVisibility !== undefined) {
    if (
      !isObject(columnVisibility) ||
      !isStringList(columnVisibility.hiddenColIds)
    ) {
      return undefined
    }
    state.columnVisibility = { hiddenColIds: columnVisibility.hiddenColIds }
  }
  return state
}

/** AG Grid's ID of the checkbox column (pinned on the left). */
export const SELECTION_COLUMN_ID = "ag-Grid-SelectionColumn"

/**
 * The grid's initialState from its kept column state. AG Grid takes a state without pinning as
 * "nothing pinned", which would unpin the columns that are pinned by definition (`pinnedLeft`: the
 * checkbox column); pinning is not kept, so it is given back as it starts. Undefined without a
 * kept state (the column definitions apply).
 */
export function initialGridState(
  stored: StoredColumnState | undefined,
  pinnedLeft: readonly string[]
): GridState | undefined {
  if (!stored) return undefined
  return {
    ...stored,
    columnPinning: { leftColIds: [...pinnedLeft], rightColIds: [] },
  }
}

/** The kept part of a grid state. */
export function pickColumnState(state: GridState): StoredColumnState {
  const picked: StoredColumnState = {}
  if (state.columnOrder) picked.columnOrder = state.columnOrder
  if (state.columnSizing) picked.columnSizing = state.columnSizing
  if (state.columnVisibility) picked.columnVisibility = state.columnVisibility
  return picked
}

/** The two column states keep the same order, widths and visibility (undefined: none kept). */
export function sameColumnState(
  a: StoredColumnState | undefined,
  b: StoredColumnState | undefined
): boolean {
  // parseColumnState lists the parts and a width's keys in one order, without undefined values.
  return (
    JSON.stringify(parseColumnState(a)) === JSON.stringify(parseColumnState(b))
  )
}

type ReadStorage = Pick<Storage, "getItem" | "removeItem">

/**
 * The column state kept for `gridId`, or undefined when there is none, it cannot be read, or it has
 * another shape (then it is removed).
 */
export function readColumnState(
  storage: ReadStorage | undefined,
  gridId: string
): StoredColumnState | undefined {
  const key = columnStateKey(gridId)
  let text: string | null | undefined
  try {
    text = storage?.getItem(key)
  } catch {
    return undefined // storage disabled or blocked
  }
  if (text == null) return undefined
  let state: StoredColumnState | undefined
  try {
    state = parseColumnState(JSON.parse(text))
  } catch {
    state = undefined
  }
  if (!state) {
    try {
      storage?.removeItem(key)
    } catch {
      // nothing to clean up then
    }
  }
  return state
}

/** Keep the column state of `gridId`; a storage that fails leaves it unsaved. */
export function writeColumnState(
  storage: Pick<Storage, "setItem"> | undefined,
  gridId: string,
  state: StoredColumnState
): void {
  try {
    storage?.setItem(columnStateKey(gridId), JSON.stringify(state))
  } catch {
    // Not kept (storage full, disabled or blocked); the grid works the same.
  }
}

/** The browser's localStorage, or undefined where it cannot be used. */
export function browserStorage(): Storage | undefined {
  try {
    return window.localStorage
  } catch {
    return undefined
  }
}
