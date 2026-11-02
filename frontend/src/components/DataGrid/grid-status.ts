// What the DataGrid shows besides its rows (loading, empty, error), from its datasource's current
// answers (stale ones never get here: see createDatasource). An error stays while any block of the
// current sort and filter that the grid still holds is failed, so a good answer for another block
// does not hide it; a reload forgets the failures of blocks AG Grid has dropped from its cache.

import type { GridPage, GridQuery } from "./datasource"

// "idle" until the first request: an overlay shown before AG Grid has laid out its header keeps
// the header's height without the floating filters (AG Grid recomputes it only on resize).
export type GridStatus =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; total: number; filtered: boolean }
  | { kind: "error"; error: unknown; hasRows: boolean; retrying: boolean }

type ReadyStatus = Extract<GridStatus, { kind: "ready" }>

/** The status of one datasource's blocks (by their first row, the query's `offset`). */
export class BlockStatus {
  private readonly failed = new Map<number, unknown>()
  private readonly retrying = new Set<number>()
  private rowsShown = false
  // The latest answer's status, shown again when the last failure is forgotten.
  private ready: ReadyStatus | undefined

  /** A request starts; returns the new status, or undefined when it does not change. */
  request(reset: boolean, query: GridQuery): GridStatus | undefined {
    if (reset) {
      this.failed.clear()
      this.retrying.clear()
      this.rowsShown = false
      this.ready = undefined
      return { kind: "loading" }
    }
    if (!this.failed.has(query.offset)) return undefined
    this.retrying.add(query.offset)
    return this.errorStatus()
  }

  /** A block's current answer. */
  page(page: GridPage<unknown>, query: GridQuery): GridStatus {
    this.failed.delete(query.offset)
    this.retrying.delete(query.offset)
    this.rowsShown = page.total > 0
    this.ready = {
      kind: "ready",
      total: page.total,
      filtered: query.filter !== undefined,
    }
    if (this.failed.size > 0) return this.errorStatus()
    return this.ready
  }

  /** A block's current request failed. */
  error(error: unknown, query: GridQuery): GridStatus {
    this.failed.delete(query.offset) // the latest failure is shown
    this.failed.set(query.offset, error)
    this.retrying.delete(query.offset)
    return this.errorStatus()
  }

  /**
   * Forget the failures of the blocks the grid no longer holds (`held`: their offsets). AG Grid drops
   * the least recently used blocks beyond its cache, a failed one too, and a reload asks again only
   * for the blocks it holds: a dropped block is asked for afresh when it is scrolled to. Returns the
   * new status, or undefined when nothing was forgotten.
   */
  retain(held: ReadonlySet<number>): GridStatus | undefined {
    const dropped = [...this.failed.keys()].filter(
      (offset) => !held.has(offset)
    )
    if (dropped.length === 0) return undefined
    for (const offset of dropped) {
      this.failed.delete(offset)
      this.retrying.delete(offset)
    }
    if (this.failed.size > 0) return this.errorStatus()
    return this.ready ?? { kind: "loading" }
  }

  private errorStatus(): GridStatus {
    return {
      kind: "error",
      error: [...this.failed.values()].at(-1),
      hasRows: this.rowsShown,
      retrying: this.retrying.size > 0,
    }
  }
}

/**
 * The first rows (offsets) of the blocks in AG Grid's cache, from `api.getCacheBlockState()`: an
 * object of `{ startRow, ... }` by block number.
 */
export function heldBlocks(cacheBlockState: unknown): Set<number> {
  const offsets = new Set<number>()
  if (typeof cacheBlockState !== "object" || cacheBlockState === null) {
    return offsets
  }
  const blocks: unknown[] = Object.values(cacheBlockState)
  for (const block of blocks) {
    const startRow: unknown =
      typeof block === "object" && block !== null && "startRow" in block
        ? block.startRow
        : undefined
    if (typeof startRow === "number") offsets.add(startRow)
  }
  return offsets
}
