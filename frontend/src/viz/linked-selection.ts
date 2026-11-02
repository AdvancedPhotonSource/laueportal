// The linked selection: the patterns selected in one view (the map, the pole figure, later the 3D
// view), keyed by (step, pattern_index) as `selection-stats` takes them, so every view highlights
// the same ones. A small external store read with useSyncExternalStore; the page creates one per
// indexing and hands it to its views. Transient interaction state: nothing is persisted.
import { useSyncExternalStore } from "react"

/** A pattern: its step and its index within the step (-1 for a step's frame-only record). */
export type SelectionPair = readonly [step: number, patternIndex: number]

export interface LinkedSelection {
  /** The distinct pairs, in the order the selecting view reported them. */
  readonly pairs: readonly SelectionPair[]
  /** The pairs' keys (`selectionKey`), for membership tests. */
  readonly keys: ReadonlySet<number>
}

export const EMPTY_SELECTION: LinkedSelection = { pairs: [], keys: new Set() }

// pattern_index + 1 takes the low 21 bits (up to about two million patterns per step); a step is an
// int32, so the key stays below 2^52 and exact.
const PATTERN_SPAN = 2 ** 21

/** One number per (step, pattern_index) pair. */
export function selectionKey(step: number, patternIndex: number): number {
  return step * PATTERN_SPAN + (patternIndex + 1)
}

/** The distinct (step, pattern_index) pairs of the selected points, in index order. */
export function selectionPairs(
  indices: ArrayLike<number>,
  step: ArrayLike<number>,
  patternIndex: ArrayLike<number>
): SelectionPair[] {
  const seen = new Set<number>()
  const pairs: SelectionPair[] = []
  for (let k = 0; k < indices.length; k++) {
    const i = indices[k] as number
    const s = step[i] as number
    const p = patternIndex[i] as number
    const key = selectionKey(s, p)
    if (seen.has(key)) continue
    seen.add(key)
    pairs.push([s, p])
  }
  return pairs
}

/** A selection from pairs (duplicates dropped). */
export function linkedSelection(
  pairs: readonly SelectionPair[]
): LinkedSelection {
  if (pairs.length === 0) return EMPTY_SELECTION
  const keys = new Set<number>()
  const distinct: SelectionPair[] = []
  for (const [s, p] of pairs) {
    const key = selectionKey(s, p)
    if (keys.has(key)) continue
    keys.add(key)
    distinct.push([s, p])
  }
  return { pairs: distinct, keys }
}

/** Per point, 1 when its (step, pattern_index) is selected; null when nothing is selected. */
export function selectionMask(
  selection: LinkedSelection | null | undefined,
  step: ArrayLike<number>,
  patternIndex: ArrayLike<number>
): Uint8Array | null {
  if (!selection || selection.keys.size === 0) return null
  const mask = new Uint8Array(step.length)
  const { keys } = selection
  for (let i = 0; i < step.length; i++) {
    if (keys.has(selectionKey(step[i] as number, patternIndex[i] as number)))
      mask[i] = 1
  }
  return mask
}

export interface SelectionStore {
  getSnapshot: () => LinkedSelection
  subscribe: (listener: () => void) => () => void
  /** Replace the selection (an empty list clears it). */
  select: (pairs: readonly SelectionPair[]) => void
  clear: () => void
}

export function createSelectionStore(): SelectionStore {
  let current = EMPTY_SELECTION
  const listeners = new Set<() => void>()
  const set = (next: LinkedSelection) => {
    if (next === current) return
    current = next
    for (const listener of listeners) listener()
  }
  return {
    getSnapshot: () => current,
    subscribe: (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    select: (pairs) => {
      set(linkedSelection(pairs))
    },
    clear: () => {
      set(EMPTY_SELECTION)
    },
  }
}

/** The store's current selection; the component re-renders when it changes. */
export function useLinkedSelection(store: SelectionStore): LinkedSelection {
  return useSyncExternalStore(store.subscribe, store.getSnapshot)
}
