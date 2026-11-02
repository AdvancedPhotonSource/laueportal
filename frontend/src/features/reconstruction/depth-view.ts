// The Depth View's logic: which depth a point opens at and the depth readout. A busy trace is
// retried as every capped route's answer is (`@/lib/busy`).
import type { ReconstructionPoint, ReconstructionTrace } from "@/api"
import { formatSignificant } from "@/lib/format"

/** The pause after the last move of the depth slider before its depth is asked for. */
export const DEPTH_DEBOUNCE_MS = 150

/** A point's depth count: its trace's, else the points list's; null while neither is known. */
export function depthCount(
  point: ReconstructionPoint,
  trace: ReconstructionTrace | undefined
): number | null {
  return trace?.depth_um.length ?? point.n_depths
}

/**
 * The depth shown: the URL's index while it is one of the point's, else the point's brightest depth
 * (the trace's `brightest_index`), undefined until that is known.
 */
export function shownDepth(
  urlDepth: number | undefined,
  count: number | null,
  trace: ReconstructionTrace | undefined
): number | undefined {
  if (urlDepth !== undefined && count !== null && urlDepth < count) {
    return urlDepth
  }
  return trace?.brightest_index
}

const um = (value: number) => formatSignificant(value)

/**
 * "depth µm (first … last)": the depth (the frame's `X-Depth-Um`, or the trace's axis at the index
 * before the frame arrives) and the point's depth range; "—" for what is not known yet.
 */
export function depthReadout(
  depthUm: number | null,
  range: readonly [number, number] | null
): string {
  const depth = depthUm === null ? "—" : `${um(depthUm)} µm`
  return range === null ? depth : `${depth} (${um(range[0])} … ${um(range[1])})`
}

/** The point's depth range: its trace's axis, else the points list's first and last depth. */
export function depthRange(
  point: ReconstructionPoint,
  axis: readonly number[] | undefined
): readonly [number, number] | null {
  const first = axis?.[0] ?? point.depth_first_um
  const last = axis?.[axis.length - 1] ?? point.depth_last_um
  return first === null || last === null ? null : [first, last]
}
