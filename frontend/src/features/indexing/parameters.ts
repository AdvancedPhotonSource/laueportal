// The Parameters tab: the run's stored parameters (GET /indexing-runs/{id}: the database's, so a
// migrated run has them too), grouped and labelled as the create form: Identity, File Paths, Peak
// Search Parameters, Indexing Parameters, Notes. Stored values the groups do not hold (the detector
// crop, the units, the cosmic filter, the beamline) follow under "Other recorded values". Paths and
// file names are monospace (`mono`); the other values are not.
import type { IndexingRunDetail } from "@/api"
import type { Definition } from "@/components/DefinitionList"

/** A row: its label, its value as text, and `mono` for a path or a file name. */
export interface ParameterRow extends Definition {
  value: string
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
      ["reconstruction_id", "Reconstruction"],
      ["author", "Author"],
    ],
  },
  {
    title: "File Paths",
    fields: [
      ["input_path", "Folder Path"],
      ["filename_prefixes", "Filename"],
      ["scan_points", "Scan Indices"],
      ["depth_range", "Depth Indices"],
      ["geometry_file", "Geometry File"],
      ["crystal_file", "Structures File"],
      ["output_path", "Output Path"],
      ["output_xml", "Output XML"],
    ],
  },
  {
    title: "Peak Search Parameters",
    fields: [
      ["box_size", "Box Size [px]"],
      ["max_rfactor", "Max R-factor"],
      ["threshold", "Threshold", "auto"],
      ["threshold_ratio", "Threshold Ratio", "auto"],
      ["min_size", "Min Spot Size [px]"],
      ["min_separation", "Min Spot Sep. [px]"],
      ["max_number", "Max detected spots", "all"],
      ["peak_shape", "Peak Shape"],
      ["mask_file", "Mask File", "none"],
      ["smooth", "Smooth peak before fitting"],
    ],
  },
  {
    title: "Indexing Parameters",
    fields: [
      ["index_kev_max_calc", "Max Calc Energy [keV]"],
      ["index_kev_max_test", "Max Test Energy [keV]"],
      ["index_angle_tolerance", "Angle Tolerance [deg]"],
      ["index_hkl", "Central HKL"],
      ["index_cone", "Cone Angle [deg]"],
      ["max_peaks", "Max spots to index"],
      ["depth", "Depth [um]", "auto"],
    ],
  },
  { title: "Notes", fields: [["notes", "Notes"]] },
]

/** A LaueMatching run's groups: the create form's sections and order. */
const MATCHING_GROUPS: readonly { title: string; fields: readonly Field[] }[] =
  [
    {
      title: "Identity",
      fields: [
        ["scan_number", "Scan"],
        ["reconstruction_id", "Reconstruction"],
        ["author", "Author"],
      ],
    },
    {
      title: "File Paths",
      fields: [
        ["input_path", "Folder Path"],
        ["filename_prefixes", "Filename"],
        ["scan_points", "Scan Indices"],
        ["depth_range", "Depth Indices"],
        ["geometry_file", "Geometry File"],
        ["crystal_file", "Structures File"],
        ["mask_file", "Mask File", "none"],
        ["output_path", "Output Path"],
      ],
    },
    {
      title: "Forward Cache",
      fields: [
        ["forward_cache", "Forward Cache"],
        ["create_forward_cache", "Built by this run"],
      ],
    },
    {
      title: "Spot Finding",
      fields: [
        ["threshold_method", "Threshold Method"],
        ["threshold_percentile", "Threshold Percentile [%]"],
        ["threshold_value", "Threshold [counts]"],
        ["min_area", "Min Spot Area [px]"],
        ["watershed_image", "Split Overlapping Spots"],
        ["background_file", "Background File", "first frame's median"],
      ],
    },
    {
      title: "Indexing Parameters",
      fields: [
        ["elo", "Min Energy [keV]"],
        ["ehi", "Max Energy [keV]"],
        ["max_nr_laue_spots", "Max Spots per Orientation"],
        ["min_nr_spots", "Min Matching Spots"],
        ["min_good_spots", "Min Exclusive Spots"],
        ["min_intensity", "Min Intensity [counts]"],
        ["max_angle", "Min Misorientation [deg]"],
        ["robust_filter", "Keep Twins"],
      ],
    },
    { title: "Notes", fields: [["notes", "Notes"]] },
  ]

/** The paths and file names, shown in a monospace font. */
const FILE_KEYS: ReadonlySet<string> = new Set([
  "input_path",
  "filename_prefixes",
  "geometry_file",
  "crystal_file",
  "output_path",
  "output_xml",
  "mask_file",
  "background_file",
  "forward_cache",
])

/** Keys shown as one row: the central HKL. */
const HKL_KEYS = ["index_h", "index_k", "index_l"] as const

/** The stored values the groups do not hold, in this order. */
const OTHER_FIELDS: readonly Field[] = [
  ["detector_crop", "Detector Crop [px]"],
  ["energy_unit", "Energy Unit"],
  ["exposure_unit", "Exposure Unit"],
  ["reciprocal_lattice_unit", "Reciprocal Lattice Unit"],
  ["lattice_parameters_unit", "Lattice Parameters Unit"],
  ["cosmic_filter", "Cosmic Filter"],
  ["beamline", "Beamline"],
]

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

function value(raw: unknown, empty?: string): string {
  const text = formatValue(raw)
  return text === "—" && empty ? empty : text
}

function rowsOf(
  fields: readonly Field[],
  values: Record<string, unknown>
): ParameterRow[] {
  return fields
    .filter(([key]) => key in values)
    .map(([key, label, empty]) => {
      const row: ParameterRow = { label, value: value(values[key], empty) }
      // A placeholder ("none" for no mask file) is not a file name.
      if (FILE_KEYS.has(key) && formatValue(values[key]) !== "—")
        row.mono = true
      return row
    })
}

function groupsOf(
  definitions: readonly { title: string; fields: readonly Field[] }[],
  values: Record<string, unknown>
): ParameterGroup[] {
  const groups: ParameterGroup[] = []
  for (const { title, fields } of definitions) {
    const rows = rowsOf(fields, values)
    if (rows.length > 0) groups.push({ title, rows })
  }
  return groups
}

/**
 * The groups of a run's parameters; a group with none of its values is left out. Without stored
 * parameters (another method, or a run recorded without them) only the run's own values are shown.
 */
export function parameterGroups(run: IndexingRunDetail): ParameterGroup[] {
  const values: Record<string, unknown> = {
    scan_number: run.scan_number,
    reconstruction_id: run.reconstruction_id,
    author: run.author,
    input_path: run.input_path,
    output_path: run.output_path,
    notes: run.notes,
  }
  if (run.laue_matching_parameters) {
    Object.assign(values, run.laue_matching_parameters)
    return groupsOf(MATCHING_GROUPS, values)
  }
  const stored = run.parameters
  if (stored) {
    Object.assign(values, stored)
    values.index_hkl = HKL_KEYS.map((key) => formatValue(stored[key])).join(" ")
    values.detector_crop = `x ${String(stored.detector_crop_x1)}–${String(stored.detector_crop_x2)}, y ${String(stored.detector_crop_y1)}–${String(stored.detector_crop_y2)}`
  }
  const groups = groupsOf(GROUPS, values)
  const other = rowsOf(OTHER_FIELDS, values)
  if (other.length > 0) {
    groups.push({ title: "Other recorded values", rows: other })
  }
  return groups
}
