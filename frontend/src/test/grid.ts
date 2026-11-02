// Helpers for DataGrid tests in jsdom, where AG Grid itself renders. jsdom lays nothing out, so
// stubGridLayout gives elements a size: AG Grid then renders the rows that fit in its viewport.
import { act } from "react"
import { expect, vi } from "vitest"

/** Give every element a size (call in beforeEach; restoreMocks undoes it). */
export function stubGridLayout(): void {
  for (const [property, size] of [
    ["clientHeight", 600],
    ["offsetHeight", 600],
    ["clientWidth", 1000],
    ["offsetWidth", 1000],
  ] as const) {
    vi.spyOn(HTMLElement.prototype, property, "get").mockReturnValue(size)
  }
}

/** The rendered rows' IDs, top to bottom (a pinned column renders each row twice). */
export function rowIds(root: ParentNode): string[] {
  const rows = Array.from(root.querySelectorAll("[row-id]"))
  return [...new Set(rows.map((row) => row.getAttribute("row-id") ?? ""))]
}

/** A row's text, across its pinned and unpinned parts. */
export function rowText(root: ParentNode, id: string): string {
  return Array.from(root.querySelectorAll(`[row-id="${id}"]`))
    .map((row) => row.textContent)
    .join("")
}

/** A row's selection checkbox. */
export function rowCheckbox(root: ParentNode, id: string): HTMLInputElement {
  const input = root.querySelector<HTMLInputElement>(
    `[row-id="${id}"] input[type="checkbox"]`
  )
  if (!input) throw new Error(`row ${id} has no checkbox`)
  return input
}

/**
 * Checks a row's box as a user does, until `taken()` says the page took it (its URL names the row).
 * AG Grid can still be starting when a row shows checked: a start-up selection the page makes then
 * (`PointsTab`'s point) can land inside the click and undo it, so the page never hears of it. A click
 * that was lost (the box unchecked, nothing taken) is made again; one the page took is never repeated.
 */
export async function checkRow(
  root: ParentNode,
  id: string,
  taken: () => boolean,
  timeout = 5000
): Promise<void> {
  await vi.waitFor(
    async () => {
      const box = rowCheckbox(root, id)
      if (!taken() && !box.checked) {
        await act(async () => {
          box.click()
          await Promise.resolve()
        })
      }
      expect(taken()).toBe(true)
    },
    { timeout }
  )
}

/** A row's cell in a column. */
export function gridCell(root: ParentNode, id: string, colId: string) {
  const cell = root.querySelector<HTMLElement>(
    `[row-id="${id}"] .ag-cell[col-id="${colId}"]`
  )
  if (!cell) throw new Error(`row ${id} has no ${colId} cell`)
  return cell
}

/** The header labels, in order (the checkbox column has none). */
export function headerLabels(root: ParentNode): string[] {
  return Array.from(root.querySelectorAll(".ag-header-cell-text"))
    .map((label) => label.textContent)
    .filter(Boolean)
}

/** Dispatch a key press on `target` (AG Grid listens for keydown); returns the event. */
export async function press(
  target: Element | null,
  key: string,
  init: KeyboardEventInit = {}
): Promise<KeyboardEvent> {
  if (!target) throw new Error(`nothing to press ${key} on`)
  const event = new KeyboardEvent("keydown", {
    key,
    bubbles: true,
    cancelable: true,
    ...init,
  })
  await act(async () => {
    target.dispatchEvent(event)
    await Promise.resolve()
  })
  return event
}

/** Type into an input as a user does: AG Grid's filter inputs listen for input events. */
export async function typeInto(
  input: HTMLInputElement | null,
  text: string
): Promise<void> {
  if (!input) throw new Error("no input to type into")
  await act(async () => {
    input.focus()
    input.value = text
    input.dispatchEvent(new Event("input", { bubbles: true }))
    await Promise.resolve()
  })
}

/**
 * Scroll the grid's rows so that `rowIndex` is at the top, as a user's scroll does: AG Grid reads its
 * viewport's scrollTop on the scroll event. The rows' height is read from a rendered row (in jsdom,
 * AG Grid's default: the theme's CSS is not measured).
 */
export async function scrollToRow(
  root: ParentNode,
  rowIndex: number
): Promise<void> {
  const viewport = root.querySelector<HTMLElement>(".ag-grid-viewport")
  const row = root.querySelector<HTMLElement>(".ag-row[row-id]")
  const height = Number.parseFloat(row?.style.height ?? "")
  if (!viewport || !Number.isFinite(height)) {
    throw new Error("no grid rows to scroll")
  }
  await act(async () => {
    viewport.scrollTop = rowIndex * height
    viewport.dispatchEvent(new Event("scroll"))
    await Promise.resolve()
  })
}
