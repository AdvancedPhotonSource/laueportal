// Keyboard access to the links in grid cells (WCAG 2.1.1). AG Grid moves between cells with the
// arrow keys and Tab, so a link inside a cell never gets the focus by itself:
// - Enter on a focused cell follows the cell's (first) link;
// - Tab goes from the cell to each of its links in turn, then to the next cell; Shift+Tab goes from
//   the cell (reached from the next one) to its links in reverse, then to the previous cell.

function links(cell: Element): HTMLElement[] {
  return Array.from(cell.querySelectorAll<HTMLElement>("a[href]"))
}

function cellOf(target: EventTarget | null): HTMLElement | null {
  return target instanceof Element
    ? target.closest<HTMLElement>(".ag-cell")
    : null
}

/**
 * A link column's `suppressKeyboardEvent`: moves the focus through the cell's links on Tab and
 * Shift+Tab, and returns true when it did (AG Grid then leaves the key alone). Past the last link
 * (the first, going back) it returns false, and AG Grid moves to the next (previous) cell.
 */
export function tabThroughLinks(event: KeyboardEvent): boolean {
  if (event.key !== "Tab" || event.ctrlKey || event.altKey || event.metaKey) {
    return false
  }
  const cell = cellOf(event.target)
  if (!cell) return false
  const cellLinks = links(cell)
  // -1: the cell itself has the focus (the first step either way is into its links).
  const index = cellLinks.indexOf(event.target as HTMLElement)
  const to = event.shiftKey
    ? index === -1
      ? cellLinks.length - 1
      : index - 1
    : index + 1
  const link = cellLinks[to]
  if (!link) return false
  event.preventDefault()
  link.focus()
  return true
}

/**
 * The grid's `cellKeyDown`: Enter on a focused cell follows its first link. Enter on a focused link
 * is the link's own (the browser follows it), so it is left alone. Returns whether it followed one.
 */
export function followCellLink(event: KeyboardEvent): boolean {
  if (event.key !== "Enter" || event.ctrlKey || event.altKey || event.metaKey) {
    return false
  }
  const target = event.target
  if (!(target instanceof Element) || target.closest("a[href]")) return false
  const cell = cellOf(target)
  const link = cell ? links(cell)[0] : undefined
  if (!link) return false
  link.click()
  return true
}
