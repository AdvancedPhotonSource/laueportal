// Live refresh: while a visible row is queued or running, the grid refreshes its loaded blocks every 5
// s. A list that takes `changed_since` is asked first for the rows changed since its last answer (the
// active ones and those changed since, with a 2 s overlap); when those rows are all loaded and keep
// their places, they are updated in place (one small request); anything else reloads every loaded
// block with refreshInfiniteCache(). Such a list with no visible active row still asks for its changes
// every 15 s, so a run queued elsewhere appears. After consecutive failures a poll waits longer
// (src/lib/polling.ts); a 404 stops polling until an answer comes (Retry).
import { backoff } from "@/lib/polling"

/** A live list with a visible queued or running row polls this often. */
export const LIVE_POLL_MS = 5_000

/** A live list that takes `changed_since`, with no visible queued or running row, polls this often. */
export const IDLE_POLL_MS = 15_000

/** The wait before a live list's next poll: active or idle, backed off after failures. */
export function livePollDelay(active: boolean, failures: number): number {
  return backoff(active ? LIVE_POLL_MS : IDLE_POLL_MS, failures)
}

/** At most this many changed rows are asked for (the protocol's largest `limit`). */
export const CHANGES_LIMIT = 1000

export type LivePlan<TRow> =
  { kind: "refresh"; reason: string } | { kind: "patch"; rows: TRow[] }

/**
 * What to do with an answer to `changed_since`: patch the loaded rows in place, or refresh every
 * loaded block because the changes may add, remove or move rows.
 *
 * - `changes`, `total`: the answer (with the grid's filter and sort);
 * - `loaded`: the rows in the grid's cache, by row ID;
 * - `sortFields`: the fields the grid sorts by: a change in one may move its row.
 */
export function planLiveUpdate<TRow extends object>({
  changes,
  total,
  loaded,
  getRowId,
  isActive,
  sortFields,
}: {
  changes: readonly TRow[]
  total: number
  loaded: ReadonlyMap<string, TRow>
  getRowId: (row: TRow) => string
  isActive: (row: TRow) => boolean
  sortFields: readonly string[]
}): LivePlan<TRow> {
  if (total > changes.length) {
    return {
      kind: "refresh",
      reason: "more changed rows than one answer holds",
    }
  }
  const changedIds = new Set<string>()
  const rows: TRow[] = []
  for (const row of changes) {
    const id = getRowId(row)
    changedIds.add(id)
    const current = loaded.get(id)
    // A new row, one that now matches the filter, or one outside the loaded blocks.
    if (!current) return { kind: "refresh", reason: `row ${id} is not loaded` }
    const moved = sortFields.some(
      (field) =>
        JSON.stringify(field in row ? row[field as keyof TRow] : null) !==
        JSON.stringify(field in current ? current[field as keyof TRow] : null)
    )
    if (moved) return { kind: "refresh", reason: `row ${id} may have moved` }
    if (JSON.stringify(row) !== JSON.stringify(current)) rows.push(row)
  }
  // An active row is always in the answer while it matches the filter: one missing has left it.
  for (const [id, row] of loaded) {
    if (isActive(row) && !changedIds.has(id)) {
      return { kind: "refresh", reason: `row ${id} left the filter` }
    }
  }
  return { kind: "patch", rows }
}

/** The earlier of two server times (`as_of`), so that no change between them is missed. */
export function earlierTime(a: string | undefined, b: string): string {
  if (a === undefined) return b
  const timeA = Date.parse(a)
  const timeB = Date.parse(b)
  if (Number.isNaN(timeA)) return b
  if (Number.isNaN(timeB)) return a
  return timeB < timeA ? b : a
}
