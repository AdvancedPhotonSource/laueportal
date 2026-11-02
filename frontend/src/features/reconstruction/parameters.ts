// The wire viewer's Parameters tab: the run's stored parameters (GET /reconstruction-runs/{id}: the
// database's, so a migrated run has them too), grouped and labelled as the wire form (`x-layout`:
// Identity, File Paths, Wire Reconstruction Parameters, Notes). Stored values the form does not show
// (threads, memory, verbosity) follow under "Other recorded values". Paths and file names are
// monospace (`mono`), and the run's folder, output and geometry file also show their form relative to
// the file root that holds them.
import type {
  ReconstructionRelativePaths,
  ReconstructionRunDetail,
  RootRelativePath,
} from "@/api"
import type { Definition } from "@/components/DefinitionList"

/** A row: its label, its value as text, `mono` for a path, and a path's root-relative form. */
export interface ParameterRow extends Definition {
  value: string
  /** `<root>/<path in it>` (just the root for the root itself). */
  relative?: string
}

export interface ParameterGroup {
  title: string
  rows: ParameterRow[]
}

type Field = readonly [key: string, label: string, empty?: string]

const GROUPS: readonly { title: string; fields: readonly Field[] }[] = [
  {
    title: "Identity",
    fields: [
      ["scan_number", "Scan"],
      ["author", "Author"],
    ],
  },
  {
    title: "File Paths",
    fields: [
      ["input_path", "Folder Path"],
      ["filename_prefixes", "Filename"],
      ["scan_points", "Scan Indices"],
      ["output_path", "Output Path"],
      ["geometry_file", "Geometry File"],
    ],
  },
  {
    title: "Wire Reconstruction Parameters",
    fields: [
      ["depth_start", "Depth Start [µm]"],
      ["depth_end", "Depth End [µm]"],
      ["depth_resolution", "Depth Resolution [µm]"],
      ["wire_edges", "Wire Edges"],
      ["percent_brightest", "Intensity Percentile [%]"],
      ["frame_skip", "Frame Skip [slices]", "default"],
      ["wire_skip", "Wire Skip [entries]", "default"],
    ],
  },
  { title: "Notes", fields: [["notes", "Notes"]] },
]

/** The stored values the wire form does not show, in this order. */
const OTHER_FIELDS: readonly Field[] = [
  ["scan_points_len", "Scan Points"],
  ["num_threads", "Threads"],
  ["memory_limit_mb", "Memory Limit [MB]"],
  ["verbose", "Verbose"],
  ["method", "Method"],
]

/** The paths and file names, shown in a monospace font. */
const FILE_KEYS: ReadonlySet<string> = new Set([
  "input_path",
  "filename_prefixes",
  "output_path",
  "geometry_file",
])

/** The paths the API also gives relative to their file root. */
const RELATIVE_KEYS: ReadonlySet<string> = new Set<
  keyof ReconstructionRelativePaths
>(["input_path", "output_path", "geometry_file"])

function formatValue(raw: unknown): string {
  if (raw === null || raw === undefined || raw === "") return "—"
  if (typeof raw === "boolean") return raw ? "yes" : "no"
  if (typeof raw === "string" || typeof raw === "number") return String(raw)
  if (Array.isArray(raw)) {
    return raw.length === 0
      ? "—"
      : raw.map((item) => formatValue(item)).join(", ")
  }
  return JSON.stringify(raw)
}

/** A root-relative path as one value: `<root>/<path>`, or the root alone. */
export function relativeText(path: RootRelativePath): string {
  return path.relative === "" ? path.root : `${path.root}/${path.relative}`
}

function rowsOf(
  fields: readonly Field[],
  values: Record<string, unknown>,
  relative: ReconstructionRelativePaths
): ParameterRow[] {
  return fields
    .filter(([key]) => key in values)
    .map(([key, label, empty]) => {
      const text = formatValue(values[key])
      const row: ParameterRow = {
        label,
        value: text === "—" && empty ? empty : text,
      }
      if (FILE_KEYS.has(key) && text !== "—") row.mono = true
      const root = RELATIVE_KEYS.has(key)
        ? relative[key as keyof ReconstructionRelativePaths]
        : null
      if (root) row.relative = relativeText(root)
      return row
    })
}

/**
 * The groups of a run's parameters; a group with none of its values is left out. Without stored
 * parameters (a run recorded without them) only the run's own values are shown.
 */
export function parameterGroups(
  run: ReconstructionRunDetail
): ParameterGroup[] {
  const values: Record<string, unknown> = {
    scan_number: run.scan_number,
    author: run.author,
    input_path: run.input_path,
    output_path: run.output_path,
    notes: run.notes,
    method: run.method,
  }
  if (run.parameters) Object.assign(values, run.parameters)
  const groups: ParameterGroup[] = []
  for (const { title, fields } of GROUPS) {
    const rows = rowsOf(fields, values, run.relative_paths)
    if (rows.length > 0) groups.push({ title, rows })
  }
  const other = rowsOf(OTHER_FIELDS, values, run.relative_paths)
  if (other.length > 0) {
    groups.push({ title: "Other recorded values", rows: other })
  }
  return groups
}
