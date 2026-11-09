// The create forms' prefill parameters: `scan`, `reconstruction` and `indexing`. A new run has one source
// context: one ID per parameter (a scan and its run may come together). They are validated as strings, so
// `scan=5` arrives as "5" whether the URL gave a string (src/lib/search.ts) or a Link gave a number. A
// value that is not one ID is dropped (no error page for a bad parameter). The page consumes them once and
// removes them with replace (`useConsumedPrefill`).
import { useEffect, useRef, useState } from "react"

export interface CreatePrefill {
  scan?: string
  reconstruction?: string
  indexing?: string
  /** The indexing form's program (`/indexing/new`); no source. */
  program?: "lauego" | "laue_matching"
}

export const PREFILL_KEYS = ["scan", "reconstruction", "indexing"] as const

/** Whether a URL's `program` names an indexing program. */
export function isIndexingProgram(
  value: unknown
): value is "lauego" | "laue_matching" {
  return value === "lauego" || value === "laue_matching"
}

/** The link parameter that opens the indexing form on a run's method (LaueGo, the form's default, needs none). */
export function programSearch(method: string): { program?: "laue_matching" } {
  return method === "laue_matching" ? { program: "laue_matching" } : {}
}

/** The value as an ID string (trimmed), or undefined when it is not one. */
function idString(value: unknown): string | undefined {
  if (typeof value === "number") {
    return Number.isSafeInteger(value) && value >= 0 ? String(value) : undefined
  }
  return typeof value === "string" && /^\s*\d+\s*$/.test(value)
    ? value.trim()
    : undefined
}

/**
 * `validateSearch` of /reconstructions/new and /indexing/new. Every key is in the result, undefined
 * when the value is missing or invalid: the router lays the result over the URL's own values, so a
 * dropped value must be overwritten.
 */
export function validateCreatePrefill(
  search: Record<string, unknown>
): CreatePrefill {
  return {
    scan: idString(search.scan),
    reconstruction: idString(search.reconstruction),
    indexing: idString(search.indexing),
  }
}

/** The prefill's parameters that are set (none: an empty object). */
export function compactPrefill(prefill: CreatePrefill): CreatePrefill {
  const compact: CreatePrefill = {}
  for (const key of PREFILL_KEYS) {
    const value = prefill[key]
    if (value !== undefined) compact[key] = value
  }
  return compact
}

/** The ID a parameter names, or null when it names none. */
export function singleId(id: string | undefined): number | null {
  return id === undefined ? null : Number(id)
}

/** A source context's IDs: at most one per parameter; null or undefined where it has none. */
export interface SourceIds {
  scan?: number | null
  reconstruction?: number | null
  indexing?: number | null
}

/**
 * One source context as a create action's prefill (the lists' New Recon, New Index and New Wire
 * Recon): each ID given, as a string; a missing one left out. A button never names two sources.
 */
export function sourcePrefill(ids: SourceIds): CreatePrefill {
  const prefill: CreatePrefill = {}
  for (const key of PREFILL_KEYS) {
    const id = ids[key]
    if (id !== null && id !== undefined) prefill[key] = String(id)
  }
  return prefill
}

/**
 * The page's source context: the URL's prefill, taken once when it arrives (also a later link to the
 * page with another) and then removed from the URL with replace (`clear`); `choose` sets another (the
 * page's pickers).
 */
export function useConsumedPrefill(
  search: CreatePrefill,
  clear: () => void
): [CreatePrefill, (next: CreatePrefill) => void] {
  const incoming = compactPrefill(search)
  const key = JSON.stringify(incoming)
  const [source, setSource] = useState(incoming)
  const [taken, setTaken] = useState(key)
  if (key !== taken) {
    setTaken(key)
    if (key !== "{}") setSource(incoming)
  }
  const latestClear = useRef(clear)
  useEffect(() => {
    latestClear.current = clear
  })
  useEffect(() => {
    if (key !== "{}") latestClear.current()
  }, [key])
  return [source, setSource]
}
