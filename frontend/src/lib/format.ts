// Display formats shared by grids and cards: dates and durations.

function pad(value: number, width = 2): string {
  return String(value).padStart(width, "0")
}

/**
 * A timestamp from the API (ISO 8601 UTC) in the browser's local time: `YYYY-MM-DD HH:MM:SS`,
 * 24-hour. "" for no value; a value that is not a date is shown as it is.
 */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return ""
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const day = `${pad(date.getFullYear(), 4)}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  return `${day} ${time}`
}

/**
 * Seconds as `HH:MM:SS` (whole seconds; the hours keep counting past 99), followed by " (running)"
 * while the run is active. "" when there is no duration yet (a run that has not started).
 */
export function formatDuration(
  seconds: number | null | undefined,
  running = false
): string {
  if (seconds == null || !Number.isFinite(seconds)) return ""
  const total = Math.max(0, Math.floor(seconds))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const text = `${pad(hours)}:${pad(minutes)}:${pad(total % 60)}`
  return running ? `${text} (running)` : text
}

const AGE_UNITS: readonly [seconds: number, unit: string][] = [
  [86_400, "d"],
  [3600, "h"],
  [60, "min"],
  [1, "s"],
]

/**
 * An age in seconds in its two largest units, the second left out when it is 0: `42 s`, `3 min 20 s`,
 * `2 h`, `3 d 4 h` (a heartbeat's age on the status page). "" when there is no age.
 */
export function formatAge(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return ""
  const total = Math.max(0, Math.floor(seconds))
  const first = AGE_UNITS.findIndex(([size]) => total >= size)
  if (first === -1) return "0 s"
  const [size, unit] = AGE_UNITS[first] ?? [1, "s"]
  const parts = [`${String(Math.floor(total / size))} ${unit}`]
  const next = AGE_UNITS[first + 1]
  if (next) {
    const rest = Math.floor((total % size) / next[0])
    if (rest > 0) parts.push(`${String(rest)} ${next[1]}`)
  }
  return parts.join(" ")
}

const COUNT = new Intl.NumberFormat("en-US")

/** A count with thousands separators, as the indexing viewer shows them: 35743 → "35,743". */
export function formatCount(value: number): string {
  return COUNT.format(value)
}

/**
 * A measured value (a map's scalar, a stage or lab coordinate) to five significant digits, without trailing
 * zeros after the decimal point: 12000 → "12000", 123456 → "123460", 0.000012 → "0.000012", 1.5 → "1.5".
 * "—" for NaN or an infinity. The indexing viewer's readouts and cards use it, so they agree.
 */
export function formatSignificant(value: number): string {
  return Number.isFinite(value) ? String(Number(value.toPrecision(5))) : "—"
}
