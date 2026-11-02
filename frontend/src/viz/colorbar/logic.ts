// The interactive colorbar's logic, in pure functions: the settings a page keeps, the range
// they resolve to for the data, the bar's axis, the handles' constraints and the histogram.
import { countBelow, quantile, type Distribution } from "../color/distribution"
import {
  DEFAULT_GAMMA,
  SCALE_KINDS,
  fromAxis,
  toAxis,
  type ColorScale,
  type ScaleKind,
} from "../color/scale"
import { formatTick, niceTicks } from "../scatter/view"

export type Range = readonly [number, number]

/** The automatic ranges: min–max, or a pair of percentiles. */
export const AUTO_RANGES = [
  { id: "minmax", label: "Min–max", low: 0, high: 100 },
  { id: "p1-99.9", label: "1–99.9 %", low: 1, high: 99.9 },
  { id: "p0.1-99.99", label: "0.1–99.99 %", low: 0.1, high: 99.99 },
  { id: "p5-95", label: "5–95 %", low: 5, high: 95 },
] as const
export type AutoRangeId = (typeof AUTO_RANGES)[number]["id"]

/** The detector and depth frames' default: the 1st to 99.9th percentile. */
export const FRAME_AUTO_RANGE: AutoRangeId = "p1-99.9"
/** The map's scalar modes' default: the data's range. */
export const MAP_AUTO_RANGE: AutoRangeId = "minmax"

/** What a page keeps for one colorbar (its viewer store may persist it). */
export interface ColorbarSettings {
  /** An automatic range, or null once the user set min and max. */
  auto: AutoRangeId | null
  /** The user's range (used while `auto` is null). */
  min: number
  max: number
  scale: ScaleKind
  gamma: number
}

export function colorbarSettings(auto: AutoRangeId): ColorbarSettings {
  return { auto, min: NaN, max: NaN, scale: "linear", gamma: DEFAULT_GAMMA }
}

/**
 * Settings from a stored or linked value (a viewer store's reader): each field checked, the
 * defaults of `defaultAuto` for the rest; a range not set is NaN (JSON stores it as null).
 */
export function readColorbarSettings(
  value: unknown,
  defaultAuto: AutoRangeId
): ColorbarSettings {
  const fallback = colorbarSettings(defaultAuto)
  const { auto, min, max, scale, gamma } =
    typeof value === "object" && value !== null
      ? (value as Record<string, unknown>)
      : {}
  const finite = (x: unknown): x is number =>
    typeof x === "number" && Number.isFinite(x)
  return {
    auto:
      auto === null
        ? null
        : (AUTO_RANGES.find((range) => range.id === auto)?.id ?? fallback.auto),
    min: finite(min) ? min : NaN,
    max: finite(max) ? max : NaN,
    scale: SCALE_KINDS.find((kind) => kind === scale) ?? fallback.scale,
    gamma: finite(gamma) && gamma > 0 ? gamma : fallback.gamma,
  }
}

export function colorScaleOf(settings: ColorbarSettings): ColorScale {
  return { kind: settings.scale, gamma: settings.gamma }
}

/**
 * The data's span (from its smallest positive value on the log scale, when it has one), at least
 * one step wide; null without data. Min–max, and the keys' Home and End.
 */
export function dataDomain(dist: Distribution, kind: ScaleKind): Range | null {
  if (dist.n === 0) return null
  const lo =
    kind === "log" && dist.minPositive > 0 ? dist.minPositive : dist.min
  const hi = Math.max(lo, dist.max)
  return hi > lo ? [lo, hi] : [lo, lo + stepOf(dist, lo)]
}

/** The smallest distance between the handles: 1 for integer data, else a millionth of `span`. */
export function minGap(dist: Distribution, span: Range): number {
  return dist.integer ? 1 : (span[1] - span[0]) * 1e-6
}

function stepOf(dist: Distribution, value: number): number {
  return dist.integer ? 1 : Math.max(Math.abs(value) * 1e-6, 1e-12)
}

/** An automatic range for the data (percentiles as numpy's), within the domain; null without data. */
export function autoRange(
  dist: Distribution,
  id: AutoRangeId,
  kind: ScaleKind
): Range | null {
  const domain = dataDomain(dist, kind)
  if (!domain) return null
  const spec = AUTO_RANGES.find((r) => r.id === id) ?? AUTO_RANGES[0]
  if (spec.id === "minmax") return domain
  const lo = clamp(quantile(dist, spec.low / 100), domain[0], domain[1])
  const hi = clamp(quantile(dist, spec.high / 100), domain[0], domain[1])
  // Percentiles that meet fall back to the data's range.
  return hi - lo >= minGap(dist, domain) ? [lo, hi] : domain
}

/**
 * The range the settings give for the data: the automatic one, or the user's as given (typed, or
 * kept across frames: never clamped to this frame's data, so frames compare by colour). On the log
 * scale a minimum at or below zero starts at the data's smallest positive value, when that lies
 * below the maximum. A user's range that is not min < max falls back to min–max.
 */
export function resolveRange(
  settings: ColorbarSettings,
  dist: Distribution
): Range | null {
  const { auto, min, max, scale } = settings
  if (auto) return autoRange(dist, auto, scale)
  if (!(min < max) || !Number.isFinite(min) || !Number.isFinite(max)) {
    return autoRange(dist, "minmax", scale)
  }
  if (scale === "log" && !(min > 0) && dist.minPositive < max) {
    return [dist.minPositive, max]
  }
  return [min, max]
}

/**
 * The scale the bar draws a range from `min` on, as the colours (`normalizer`) do: log needs a
 * positive minimum, so log on a range that starts at or below zero (data without positive values,
 * or none yet: NaN) is linear.
 */
export function effectiveScale(kind: ScaleKind, min: number): ScaleKind {
  return kind === "log" && !(min > 0) ? "linear" : kind
}

/**
 * The range after moving one handle to `value`, the minimum at least `minGap` below the maximum.
 * A gesture (a drag, the keys) passes `bounds`, what the bar shows: the value stays within it and
 * snaps to integers for integer data. A typed value (`bounds` null) is taken as given.
 */
export function moveHandle(
  which: "min" | "max",
  value: number,
  range: Range,
  dist: Distribution,
  bounds: Range | null
): Range {
  if (!Number.isFinite(value)) return range
  let v = value
  if (bounds) {
    const [b0, b1] = dist.integer
      ? [Math.ceil(bounds[0]), Math.floor(bounds[1])]
      : bounds
    v = clamp(dist.integer ? Math.round(v) : v, b0, b1)
  }
  const gap = minGap(dist, bounds ?? range)
  if (which === "min") return [Math.min(v, range[1] - gap), range[1]]
  return [range[0], Math.max(v, range[0] + gap)]
}

export type AxisMode = "fit" | "data"

/**
 * The span the bar shows, which always covers the range. "data": the data and the range. "fit": the
 * range in the middle half of the bar (in the axis's space), not beyond the data and the range; the
 * bar re-fits after a drag ends, not during it. A log axis leaves out data at or below zero.
 */
export function axisDomain(
  range: Range,
  domain: Range | null,
  kind: ScaleKind,
  mode: AxisMode
): Range {
  const data = domain && (kind !== "log" || domain[0] > 0) ? domain : range
  const lo = Math.min(data[0], range[0])
  const hi = Math.max(data[1], range[1])
  if (mode === "data") return [lo, hi]
  const a0 = toAxis(range[0], kind)
  const a1 = toAxis(range[1], kind)
  const pad = (a1 - a0) / 2
  return [
    Math.max(lo, fromAxis(a0 - pad, kind)),
    Math.min(hi, fromAxis(a1 + pad, kind)),
  ]
}

/** A value's position along a bar `width` pixels wide showing `axis` (log space on the log scale). */
export function valueToPosition(
  value: number,
  axis: Range,
  kind: ScaleKind,
  width: number
): number {
  const a0 = toAxis(axis[0], kind)
  const a1 = toAxis(axis[1], kind)
  return ((toAxis(value, kind) - a0) / (a1 - a0)) * width
}

export function positionToValue(
  position: number,
  axis: Range,
  kind: ScaleKind,
  width: number
): number {
  const a0 = toAxis(axis[0], kind)
  const a1 = toAxis(axis[1], kind)
  return fromAxis(a0 + (position / width) * (a1 - a0), kind)
}

/**
 * The histogram along the bar: at most `maxBins` bins evenly spaced in the axis's space (integer
 * data: the whole integers on the axis, per bin), each bar's height log(1 + density) over the
 * highest's. The density is the count per even bin of the axis's space, so integer and float data
 * look alike on a log axis too.
 */
export function histogramBars(
  dist: Distribution,
  axis: Range,
  kind: ScaleKind,
  maxBins: number
): { edges: Float64Array; heights: Float64Array } {
  const a0 = toAxis(axis[0], kind)
  const a1 = toAxis(axis[1], kind)
  const bins = Math.max(1, Math.floor(maxBins))
  const nominal = (a1 - a0) / bins
  let edges = Float64Array.from({ length: bins + 1 }, (_, i) =>
    fromAxis(a0 + nominal * i, kind)
  )
  if (dist.integer) {
    // Half-integer edges around the integers on the axis (ceil(a0) to floor(a1)), so each bin
    // holds whole integers; drop the bins left empty of them.
    const snapped = [Math.ceil(axis[0]) - 0.5]
    for (let i = 1; i < bins; i++) {
      const e = Math.round(edges[i] as number) - 0.5
      if (e > (snapped[snapped.length - 1] as number)) snapped.push(e)
    }
    const last = Math.floor(axis[1]) + 0.5
    if (last > (snapped[snapped.length - 1] as number)) snapped.push(last)
    edges = Float64Array.from(snapped)
  }
  const n = edges.length - 1
  const heights = new Float64Array(Math.max(0, n))
  let top = 0
  for (let i = 0; i < n; i++) {
    const left = edges[i] as number
    const right = edges[i + 1] as number
    const count = countBelow(dist, right, i === n - 1) - countBelow(dist, left)
    // A snapped bin spans another width of the axis's space than the even ones.
    const density = dist.integer
      ? (count * nominal) / (toAxis(right, kind) - toAxis(left, kind))
      : count
    const h = Math.log1p(density)
    heights[i] = h
    if (h > top) top = h
  }
  if (top > 0)
    for (let i = 0; i < n; i++) heights[i] = (heights[i] as number) / top
  return { edges, heights }
}

/**
 * The ticks along the bar and their labels: decades on a log axis that spans two or more (each
 * labelled with its own magnitude), else nice numbers.
 */
export function axisTicks(
  axis: Range,
  kind: ScaleKind,
  count: number
): { values: number[]; labels: string[] } {
  if (kind === "log") {
    const first = Math.ceil(Math.log10(axis[0]) - 1e-9)
    const last = Math.floor(Math.log10(axis[1]) + 1e-9)
    if (last - first >= 1) {
      const every = Math.max(
        1,
        Math.ceil((last - first + 1) / Math.max(1, count))
      )
      const powers: number[] = []
      for (let p = first; p <= last; p += every) powers.push(p)
      return {
        values: powers.map((p) => 10 ** p),
        labels: powers.map(decadeLabel),
      }
    }
  }
  const { values, step } = niceTicks(axis[0], axis[1], count)
  return { values, labels: values.map((v) => formatTick(v, step)) }
}

/** A decade's label: plainly from 0.001 to 100000, else 1e±p. */
function decadeLabel(p: number): string {
  return p >= -3 && p <= 5 ? formatTick(10 ** p, 10 ** p) : `1e${String(p)}`
}

/** A value as the bar labels it: integers plainly, else with the digits the span needs. */
export function formatValue(
  value: number,
  span: number,
  integer: boolean
): string {
  if (integer) return String(Math.round(value))
  const step = 10 ** (Math.floor(Math.log10(Math.abs(span) || 1)) - 3)
  return formatTick(value, step)
}

/**
 * The left edges of the min and max value labels (`wMin`, `wMax` wide), centred on their handles
 * at `xMin` and `xMax`, kept apart by `gap` and inside [0, width].
 */
export function labelPositions(
  xMin: number,
  xMax: number,
  wMin: number,
  wMax: number,
  width: number,
  gap = 6
): [number, number] {
  let lMin = clamp(xMin - wMin / 2, 0, width - wMin)
  let lMax = clamp(xMax - wMax / 2, 0, width - wMax)
  if (lMin + wMin + gap > lMax) {
    const mid = (xMin + xMax) / 2
    lMin = mid - gap / 2 - wMin
    lMax = mid + gap / 2
    if (lMin < 0) {
      lMin = 0
      lMax = Math.max(lMax, wMin + gap)
    }
    if (lMax + wMax > width) {
      lMax = width - wMax
      lMin = Math.min(lMin, lMax - gap - wMin)
    }
  }
  return [lMin, lMax]
}

function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v
}
