// The Points tab's rows: every point of the run as GET /reconstruction-runs/{id}/points sends it,
// plus two values shown as text: the frame's shape (y × x) and the sample position (x, y, z in µm,
// one decimal).
import {
  OutputPathState,
  ReconstructionPointStatus,
  type ReconstructionPoint,
} from "@/api"

export interface PointRow extends ReconstructionPoint {
  /** One depth frame's shape, `y × x`; null when unknown. */
  frame: string | null
  /** The sample position, `x, y, z` (µm); null when unknown. */
  sample: string | null
}

export const POINT_STATUS_LABELS: Record<ReconstructionPointStatus, string> = {
  [ReconstructionPointStatus.pending]: "Pending",
  [ReconstructionPointStatus.writing]: "Writing",
  [ReconstructionPointStatus.complete]: "Complete",
  [ReconstructionPointStatus.failed]: "Failed",
  [ReconstructionPointStatus.interrupted]: "Interrupted",
  [ReconstructionPointStatus.unattempted]: "Unattempted",
  [ReconstructionPointStatus.no_frames]: "No frames",
}

/** A source path's state when it is not `available`. */
export const SOURCE_STATE_LABELS: Record<
  Exclude<OutputPathState, "available">,
  string
> = {
  [OutputPathState.unavailable]: "Unavailable",
  [OutputPathState.outside_roots]: "Outside the file roots",
  [OutputPathState.missing]: "Missing",
  [OutputPathState.not_responding]: "Not responding",
}

/** A length in µm as the grid shows it: at most three decimals. */
export function formatMicrons(value: number): string {
  return String(Math.round(value * 1000) / 1000)
}

function frameText(shape: number[] | null): string | null {
  return shape && shape.length === 2 ? shape.join(" × ") : null
}

function sampleText(point: ReconstructionPoint): string | null {
  const { sample_x_um: x, sample_y_um: y, sample_z_um: z } = point
  if (x === null || y === null || z === null) return null
  return [x, y, z].map((value) => value.toFixed(1)).join(", ")
}

/** The grid's rows. */
export function pointRows(points: readonly ReconstructionPoint[]): PointRow[] {
  return points.map((point) => ({
    ...point,
    frame: frameText(point.frame_shape),
    sample: sampleText(point),
  }))
}

/** A point's row ID in the grid: its point ID. */
export function pointRowId(row: ReconstructionPoint): string {
  return row.point_id
}
