import { describe, expect, it, vi } from "vitest"

import {
  createSelectionStore,
  selectionKey,
  selectionMask,
  selectionPairs,
} from "./linked-selection"

describe("linked selection", () => {
  it("keys distinct (step, pattern_index) pairs, frame-only records included", () => {
    const keys = [
      selectionKey(0, -1),
      selectionKey(0, 0),
      selectionKey(1, -1),
      selectionKey(2_000_000_000, 7),
    ]
    expect(new Set(keys).size).toBe(4)
    expect(keys.every(Number.isSafeInteger)).toBe(true)
  })

  it("reports a view's selected points as distinct pairs", () => {
    // A pole figure has one point per pole: three poles of pattern (5, 0), one of (6, 1).
    const step = Int32Array.of(5, 5, 6, 5)
    const pattern = Int32Array.of(0, 0, 1, 0)
    expect(selectionPairs(Int32Array.of(0, 1, 2, 3), step, pattern)).toEqual([
      [5, 0],
      [6, 1],
    ])
  })

  it("shares one selection: subscribers hear a change, and every view masks its own points", () => {
    const store = createSelectionStore()
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    store.select([
      [5, 0],
      [5, 0],
      [6, -1],
    ])
    expect(listener).toHaveBeenCalledTimes(1)
    expect(store.getSnapshot().pairs).toEqual([
      [5, 0],
      [6, -1],
    ])
    // The map (one record per pattern or frame-only step) and the pole figure (poles) both highlight.
    const mapMask = selectionMask(
      store.getSnapshot(),
      Int32Array.of(5, 6, 7),
      Int32Array.of(0, -1, 0)
    )
    const poleMask = selectionMask(
      store.getSnapshot(),
      Int32Array.of(5, 5, 7),
      Int32Array.of(0, 0, 0)
    )
    expect(Array.from(mapMask ?? [])).toEqual([1, 1, 0])
    expect(Array.from(poleMask ?? [])).toEqual([1, 1, 0])
    store.select([])
    expect(
      selectionMask(store.getSnapshot(), Int32Array.of(5), Int32Array.of(0))
    ).toBeNull()
    store.clear() // already empty: no change, no notification
    expect(listener).toHaveBeenCalledTimes(2)
    unsubscribe()
    store.select([[1, 0]])
    expect(listener).toHaveBeenCalledTimes(2)
  })
})
