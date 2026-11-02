// The scatter's public types.
import type { SelectionPair } from "../linked-selection"
import type { ScatterColors } from "./stamp"

/**
 * The points, as the map and pole-figure products' columns (typed arrays from Arrow): x and y,
 * the colours, and optionally `indexed` (1 or 0 per point) and the (step, pattern_index) pairs
 * that key the linked selection.
 */
export interface ScatterData {
  x: ArrayLike<number>
  y: ArrayLike<number>
  colors: ScatterColors
  indexed?: ArrayLike<number> | null
  step?: ArrayLike<number> | null
  patternIndex?: ArrayLike<number> | null
}

/**
 * What a drag does: pan the view, or select with a box or a lasso. The pole figure's "center" tool
 * drags nothing: a click there reports the point (and the pole under it, if any).
 */
export type ScatterTool = "pan" | "box" | "lasso" | "center"

/** A box or lasso selection: the points' indices and their distinct (step, pattern_index) pairs. */
export interface ScatterSelection {
  indices: Int32Array
  pairs: SelectionPair[]
}
