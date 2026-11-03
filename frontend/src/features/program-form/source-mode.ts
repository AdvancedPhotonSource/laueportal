// The create form's source: one kind of source per form, chosen with the "Source" switch. A scan is direct
// (an empty Scan picker: an unlinked run); a reconstruction (LaueGo) sets its scan; a clone sets its scan,
// its reconstruction and its values. A scan or a reconstruction fills only its data fields (`SOURCE_FIELDS`,
// as backend jobs/prefill.py derives them), a clone the form; a kind change resets the data fields
// (`CLEARED_FIELDS`). The Structures File, the Output XML (and the Geometry File on a kind change) stay like
// the parameters.
import type { ProgramKind } from "@/api"
import type { CreatePrefill } from "@/lib/prefill"
import { CLONE_SOURCE } from "./source-search"

export type SourceMode = "scan" | "reconstruction" | "clone"

/** The kinds each form offers, Scan (the default) first. */
export const SOURCE_MODES: Record<ProgramKind, readonly SourceMode[]> = {
  wire: ["scan", "clone"],
  lauego: ["scan", "reconstruction", "clone"],
}

export const SOURCE_MODE_LABELS: Record<SourceMode, string> = {
  scan: "Scan",
  reconstruction: "Reconstruction",
  clone: "Clone",
}

/**
 * The kind a prefill opens (the URL's, a list's create button): the run to clone when it names one
 * (LaueGo: an indexing run; wire: a reconstruction), else a LaueGo form's reconstruction, else the
 * scan.
 */
export function sourceModeOf(
  program: ProgramKind,
  prefill: CreatePrefill
): SourceMode {
  if (prefill[CLONE_SOURCE[program]] !== undefined) return "clone"
  if (program === "lauego" && prefill.reconstruction !== undefined)
    return "reconstruction"
  return "scan"
}

/** The prefill of a source picked in `mode`. */
export function modePrefill(
  program: ProgramKind,
  mode: SourceMode,
  id: number
): CreatePrefill {
  const key = mode === "clone" ? CLONE_SOURCE[program] : mode
  return { [key]: String(id) }
}

/** The data fields a scan or a reconstruction sets (besides the source IDs); a clone sets every field. */
export const SOURCE_FIELDS: Record<
  Exclude<SourceMode, "clone">,
  readonly string[]
> = {
  scan: ["input_path", "filename_prefixes", "output_path_template"],
  reconstruction: [
    "input_path",
    "filename_prefixes",
    "output_path_template",
    "scan_points",
    "depth_range",
    "geometry_file",
  ],
}

/** The data fields a kind change (or a cleared or refused source) resets to the defaults, with the IDs. */
export const CLEARED_FIELDS: readonly string[] = [
  "input_path",
  "filename_prefixes",
  "scan_points",
  "depth_range",
  "output_path_template",
]

/**
 * The fields a source of `mode` fills (undefined: every field, a clone's), the cleared ones too when the
 * form shows another kind's source (`shown`): a kind set from outside clears like the switch.
 */
export function fillFields(
  mode: SourceMode,
  shown: SourceMode
): readonly string[] | undefined {
  if (mode === "clone") return undefined
  return mode === shown
    ? SOURCE_FIELDS[mode]
    : [...new Set([...SOURCE_FIELDS[mode], ...CLEARED_FIELDS])]
}
