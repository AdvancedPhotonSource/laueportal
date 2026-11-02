// The count of an integer range in the srange syntax (`laueportal.util.srange`): comma-separated
// parts, each a number, a range `lo-hi`, or a range with a stride `lo-hi:step`, in any order but not
// overlapping. Only for the field's preview: the server checks the range itself.

const PART = /^\s*(-?\d+)\s*(?:-\s*(-?\d+)\s*(?::\s*(\d+)\s*)?)?$/

interface Part {
  lo: number
  hi: number
  count: number
}

/** How many integers the range holds; 0 when blank, null when it is not a valid range. */
export function srangeCount(text: string): number | null {
  const trimmed = text.trim()
  if (trimmed === "" || trimmed.toLowerCase() === "none") return 0
  const parts: Part[] = []
  for (const part of trimmed.split(",")) {
    const match = PART.exec(part)
    if (!match) return null
    const lo = Number(match[1])
    const hi = match[2] === undefined ? lo : Number(match[2])
    const stride = match[3] === undefined ? 1 : Number(match[3])
    if (stride <= 0 || hi < lo) return null
    const count = Math.floor((hi - lo) / stride) + 1
    parts.push({ lo, hi: lo + (count - 1) * stride, count })
  }
  parts.sort((a, b) => a.lo - b.lo)
  let total = 0
  let last = -Infinity
  for (const { lo, hi, count } of parts) {
    if (lo <= last) return null
    last = hi
    total += count
  }
  return total
}
