// The 3D view's public types.
import type { ScatterData } from "../scatter/types"

/**
 * The points, as the map product's columns with three axes (typed arrays from Arrow): x, y and z,
 * the colours, and optionally `indexed` and the (step, pattern_index) pairs that key the linked
 * selection, as for the scatter.
 */
export interface PointCloudData extends ScatterData {
  z: ArrayLike<number>
}

/** The axis titles (the selected axes' names). */
export interface PointCloudAxes {
  x: string
  y: string
  z: string
}
