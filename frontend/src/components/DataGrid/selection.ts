// A grid's checkbox selection, set per grid (DataGridProps.selection): one checked row for a list
// whose actions take one source, several for bulk actions.
import type { IRowNode, RowSelectionOptions } from "ag-grid-community"

/** A grid's checkbox selection (AG Grid's `rowSelection.mode`). */
export type GridSelection = "singleRow" | "multiRow"

// The Infinite Row Model cannot select rows it has not loaded, so there is no "select all" box (the
// client-side model's multi-select has one: it selects the rows the filters show). Clicking a row
// selects nothing: its text can be selected and its links followed. In `singleRow`, checking a row
// unchecks the other.
export function rowSelectionOptions<TRow>(
  mode: GridSelection,
  clientSide: boolean,
  isSelectable: ((row: TRow) => boolean) | undefined
): RowSelectionOptions<TRow> {
  const common = {
    checkboxes: true,
    enableClickSelection: false,
    ...(isSelectable && {
      isRowSelectable: (node: IRowNode<TRow>) =>
        node.data !== undefined && isSelectable(node.data),
    }),
  }
  return mode === "singleRow"
    ? { mode, ...common }
    : { mode, headerCheckbox: clientSide, selectAll: "filtered", ...common }
}
