// The ROI traces chart's data (lauelab's plot_roi_traces): one line per shown ROI against depth (µm)
// or depth index, sums or normalized (the API's `normalize`), on a linear or a log axis. A log axis
// leaves out the samples that are not positive (gaps, counted in a status); a trace that cannot be
// normalized has no line and its reason as a status.
import type { RoiTracesAnswer } from "@/api"
import { formatSignificant } from "@/lib/format"
import { roiCenter, type Roi } from "./rois"
import type { TraceAxis, TraceScale } from "./viewer-store"

/** An ROI's line: its chart key (a CSS-safe name), its label and its colour. */
export interface TraceLine {
  key: string
  label: string
  color: string
}

export interface TraceChart {
  /** One row per depth: `depth_um`, `depth_index` and each line's value (null for a gap). */
  rows: Record<string, number | null>[]
  x: { key: "depth_um" | "depth_index"; label: string }
  y: { label: string; scale: TraceScale }
  lines: TraceLine[]
  /** Statuses: traces without values (their reasons) and the samples a log axis leaves out. */
  notes: string[]
}

export const AXIS_LABELS: Record<TraceAxis, string> = {
  depth: "Depth (µm)",
  index: "Depth index",
}

/** An ROI's name: its number (its place in the list, as on its outline) and its centre. */
export function roiLabel(
  roi: Pick<Roi, "x" | "y" | "size">,
  index: number
): string {
  const { x, y } = roiCenter(roi)
  return `ROI ${String(index + 1)} (Y ${formatSignificant(y)}, X ${formatSignificant(x)})`
}

const same = (a: Pick<Roi, "x" | "y" | "size">, b: typeof a) =>
  a.x === b.x && a.y === b.y && a.size === b.size

/**
 * The chart of the ROIs (the list, in its order) not `hidden`, from an answer (its own ROIs, which may
 * be an earlier list's while another loads: an ROI it lacks has no line yet).
 */
export function traceChart(
  answer: RoiTracesAnswer | undefined,
  rois: readonly Roi[],
  {
    axis,
    scale,
    hidden,
  }: { axis: TraceAxis; scale: TraceScale; hidden?: ReadonlySet<Roi> }
): TraceChart {
  const depths = answer?.answer.depth_um ?? []
  const rows: Record<string, number | null>[] = depths.map((depth, i) => ({
    depth_um: depth,
    depth_index: answer?.answer.depth_index[i] ?? i,
  }))
  const lines: TraceLine[] = []
  const notes: string[] = []
  let omitted = 0
  let total = 0
  rois.forEach((roi, n) => {
    if (hidden?.has(roi)) return
    const at = answer?.rois.findIndex((asked) => same(asked, roi)) ?? -1
    const trace = at < 0 ? undefined : answer?.answer.traces[at]
    if (!trace) return
    const label = roiLabel(roi, n)
    if (trace.values === null) {
      notes.push(`${label}: ${trace.reason ?? "no values"}`)
      return
    }
    const key = `roi${String(n)}`
    lines.push({ key, label, color: roi.color })
    trace.values.forEach((value, i) => {
      const row = rows[i]
      if (!row) return
      if (scale === "log") {
        total += 1
        if (value === null || value <= 0) {
          omitted += 1
          row[key] = null
          return
        }
      }
      row[key] = value
    })
  })
  if (omitted > 0) {
    notes.push(
      `Log axis: ${String(omitted)} of ${String(total)} samples ≤ 0 not shown`
    )
  }
  return {
    rows,
    x: {
      key: axis === "depth" ? "depth_um" : "depth_index",
      label: AXIS_LABELS[axis],
    },
    y: {
      label: answer?.answer.normalize
        ? "Normalized intensity"
        : "Stored intensity",
      scale,
    },
    lines,
    notes,
  }
}
