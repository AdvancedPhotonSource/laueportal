// The data's distribution for the colorbar: its histogram and percentiles come from data the browser
// already has. A uint16 frame is counted once into 65,536 integer bins; any other data is sorted
// once. Either answers "how many values lie below x" in O(1) or O(log n), so the histogram at any
// binning and every percentile cost nothing proportional to the data afterwards.

/** The values: a frame (uint16 or float32) or a map's scalars. NaN and infinities are left out. */
export type DistributionInput =
  Uint16Array | Float32Array | Float64Array | readonly number[]

interface Summary {
  /** The number of finite values. */
  n: number
  /** NaN when there are none. */
  min: number
  max: number
  /** The smallest value above 0 (the log scale's floor), NaN when there is none. */
  minPositive: number
  /** Every value is an integer (the handles then snap to integers). */
  integer: boolean
}

export type Distribution = Summary &
  (
    | {
        kind: "counts"
        /** cumulative[v] = the number of values below v, for v in 0..65536. */
        cumulative: Float64Array
      }
    | {
        kind: "sorted"
        /** The finite values, ascending. */
        values: Float32Array | Float64Array
      }
  )

export function distributionOf(data: DistributionInput): Distribution {
  if (data instanceof Uint16Array) return countUint16(data)
  const values =
    data instanceof Float32Array
      ? Float32Array.from(data)
      : Float64Array.from(data)
  values.sort()
  // Sorted typed arrays put -Infinity first and +Infinity, then NaN, last.
  let start = 0
  while (start < values.length && values[start] === -Infinity) start++
  let end = start
  while (end < values.length && Number.isFinite(values[end])) end++
  const finite = values.subarray(start, end)
  const n = finite.length
  let integer = true
  for (let i = 0; i < n && integer; i++) {
    integer = Number.isInteger(finite[i])
  }
  const firstPositive = upperBound(finite, 0)
  return {
    kind: "sorted",
    values: finite,
    n,
    min: n ? (finite[0] as number) : NaN,
    max: n ? (finite[n - 1] as number) : NaN,
    minPositive: firstPositive < n ? (finite[firstPositive] as number) : NaN,
    integer,
  }
}

function countUint16(data: Uint16Array): Distribution {
  const cumulative = new Float64Array(65537)
  for (let i = 0; i < data.length; i++) {
    const bin = (data[i] as number) + 1
    cumulative[bin] = (cumulative[bin] as number) + 1
  }
  let min = NaN
  let max = NaN
  let minPositive = NaN
  for (let v = 0; v < 65536; v++) {
    const count = cumulative[v + 1] as number
    if (count > 0) {
      if (min !== min) min = v
      if (v > 0 && minPositive !== minPositive) minPositive = v
      max = v
    }
    cumulative[v + 1] = (cumulative[v] as number) + count
  }
  return {
    kind: "counts",
    cumulative,
    n: data.length,
    min,
    max,
    minPositive,
    integer: true,
  }
}

/** The number of values below x (or at most x, `inclusive`). */
export function countBelow(
  dist: Distribution,
  x: number,
  inclusive = false
): number {
  if (x !== x) return 0
  if (dist.kind === "sorted") {
    return inclusive ? upperBound(dist.values, x) : lowerBound(dist.values, x)
  }
  // Integer values: v < x ⟺ v < ceil(x), and v ≤ x ⟺ v < floor(x) + 1.
  const v = inclusive ? Math.floor(x) + 1 : Math.ceil(x)
  return dist.cumulative[Math.min(65536, Math.max(0, v))] as number
}

/** The k-th smallest value (0-based). */
export function kth(dist: Distribution, k: number): number {
  if (dist.kind === "sorted") return dist.values[k] as number
  // The smallest v with more than k values at most v: cumulative[v + 1] > k.
  const c = dist.cumulative
  let lo = 0
  let hi = 65535
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if ((c[mid + 1] as number) > k) hi = mid
    else lo = mid + 1
  }
  return lo
}

/** The q-quantile (q in [0, 1]) with numpy's default linear interpolation; NaN without data. */
export function quantile(dist: Distribution, q: number): number {
  if (dist.n === 0) return NaN
  const position = Math.min(1, Math.max(0, q)) * (dist.n - 1)
  const i = Math.floor(position)
  const frac = position - i
  const low = kth(dist, i)
  if (frac === 0) return low
  return low + (kth(dist, i + 1) - low) * frac
}

function lowerBound(values: ArrayLike<number>, x: number): number {
  let lo = 0
  let hi = values.length
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if ((values[mid] as number) < x) lo = mid + 1
    else hi = mid
  }
  return lo
}

function upperBound(values: ArrayLike<number>, x: number): number {
  let lo = 0
  let hi = values.length
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if ((values[mid] as number) <= x) lo = mid + 1
    else hi = mid
  }
  return lo
}
