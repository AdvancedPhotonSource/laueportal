// Columns kept in view while a grid scrolls sideways (GridColumn.pinned): a list whose columns cannot
// all fit (e.g. /reconstructions below about 1200 px) pins its ID on the left, beside the checkboxes,
// and its status on the right, so only the middle columns scroll. They are pinned only then: where
// every column fits, the grid looks as it would without pinning (AG Grid draws a border at a pinned
// column's edge).

/** A displayed column's width, as AG Grid sizes it. */
export interface ColumnWidth {
  pinned: "left" | "right" | null
  /** Its width now. */
  width: number
  /** The narrowest a flex column gets (fixed columns keep their width). */
  minWidth: number
  /** It shares the free width (`flex`), so it can shrink to `minWidth`. */
  flex: boolean
}

/**
 * Whether the columns need more room than the grid gives them, so that it scrolls sideways.
 * `centerWidth` is the width the unpinned columns are shown in (the center viewport): the room for
 * every column is that plus the pinned columns' widths. A column needs its width, a flex one only
 * its minimum.
 */
export function columnsOverflow(
  columns: readonly ColumnWidth[],
  centerWidth: number
): boolean {
  let room = centerWidth
  let needed = 0
  for (const column of columns) {
    if (column.pinned !== null) room += column.width
    needed += column.flex ? column.minWidth : column.width
  }
  // Half a pixel for the rounding of fractional widths.
  return needed > room + 0.5
}

/** AG Grid's pinning of a column (`true` is the left) as a side, or null. */
export function pinnedSide(
  pinned: "left" | "right" | boolean | null | undefined
): "left" | "right" | null {
  if (pinned === "left" || pinned === true) return "left"
  return pinned === "right" ? "right" : null
}

/** A column to keep in view, and the side it is pinned to. */
export interface PinnedColumn {
  colId: string
  side: "left" | "right"
}

/**
 * The pinning to apply (AG Grid column state), or undefined when the columns are already as they
 * should be: pinned to their sides while the columns overflow, unpinned otherwise.
 */
export function pinningUpdate(
  wanted: readonly PinnedColumn[],
  current: (colId: string) => "left" | "right" | null,
  overflow: boolean
): { colId: string; pinned: "left" | "right" | null }[] | undefined {
  const state = wanted.map(({ colId, side }) => ({
    colId,
    pinned: overflow ? side : null,
  }))
  const changed = state.some(({ colId, pinned }) => current(colId) !== pinned)
  return changed ? state : undefined
}
