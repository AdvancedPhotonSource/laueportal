// The source pickers' search: scans, reconstructions and indexing runs through their list endpoints with
// the grid protocol's filter and sort, the most recent first. A number (with or without its `SN`, `R` or
// `I` prefix) finds that ID; other text finds a sample (a run's: its scan's, `sample_name` on the run
// lists).
import {
  listIndexingRuns,
  listReconstructionRuns,
  listScans,
  type ListScansParams,
  type ProgramKind,
} from "@/api"

export type SourceKind = "scan" | "reconstruction" | "indexing"

/** The source a program's clone is made from: an indexing run (LaueGo), a reconstruction (wire). */
export const CLONE_SOURCE: Record<ProgramKind, SourceKind> = {
  lauego: "indexing",
  wire: "reconstruction",
}

export interface SourceOption {
  id: number
  /** The ID with its prefix: `SN276990`, `R3`, `I12`. */
  label: string
  /** The sample (a run's: its scan's); null when it has none. */
  detail: string | null
  /** The scan's time or the run's creation (ISO). */
  date: string
}

/** The pause after typing before the search asks the server. */
export const SOURCE_SEARCH_DELAY_MS = 250

/** How many matches a search shows. */
export const SOURCE_LIMIT = 20

const PREFIX: Record<SourceKind, string> = {
  scan: "SN",
  reconstruction: "R",
  indexing: "I",
}

interface SourceFields {
  id: string
  text: string
  sort: string
}

const FIELDS: Record<SourceKind, SourceFields> = {
  scan: { id: "scan_number", text: "sample_name", sort: "-time" },
  reconstruction: { id: "id", text: "sample_name", sort: "-created_at" },
  indexing: { id: "id", text: "sample_name", sort: "-created_at" },
}

/** The ID with its prefix. */
export function sourceLabel(kind: SourceKind, id: number): string {
  return `${PREFIX[kind]}${String(id)}`
}

/** The list request for what was typed. */
export function sourceQuery(kind: SourceKind, text: string): ListScansParams {
  const fields = FIELDS[kind]
  const params: ListScansParams = { sort: fields.sort, limit: SOURCE_LIMIT }
  const typed = text.trim()
  if (typed === "") return params
  const id = new RegExp(`^(?:${PREFIX[kind]})?\\s*(\\d+)$`, "i").exec(typed)
  const filter = id
    ? {
        [fields.id]: {
          filterType: "number",
          type: "equals",
          filter: Number(id[1]),
        },
      }
    : { [fields.text]: { filterType: "text", type: "contains", filter: typed } }
  return { ...params, filter: JSON.stringify(filter) }
}

/** The matches for what was typed. */
export async function searchSources(
  kind: SourceKind,
  text: string,
  signal?: AbortSignal
): Promise<SourceOption[]> {
  const params = sourceQuery(kind, text)
  if (kind === "scan") {
    const page = await listScans(params, { signal })
    return page.rows.map((row) => ({
      id: row.scan_number,
      label: sourceLabel(kind, row.scan_number),
      detail: row.sample_name,
      date: row.time,
    }))
  }
  const page =
    kind === "reconstruction"
      ? await listReconstructionRuns(params, { signal })
      : await listIndexingRuns(params, { signal })
  return page.rows.map((row) => ({
    id: row.id,
    label: sourceLabel(kind, row.id),
    detail: row.sample_name,
    date: row.created_at,
  }))
}
