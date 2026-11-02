// The scan-log import page's rules: the rows of an index, which can be selected, the quick select
// (scans.service.quick_select's rule, on the rows the page holds), the upload's size check, and the rows'
// statuses after an import (from its results, never by indexing again).

import type {
  ScanImportDefaults,
  ScanImportItem,
  ScanImportReport,
  ScanImportSummary,
  ScanLogRow,
  ScanLogStatus,
} from "@/api"
import { formatBytes } from "@/components/PathPicker"
import type { FieldSpec, FormValues } from "@/features/program-form"
import { APERTURE_LABELS, APERTURES } from "./scan-detail"

/** A row's status: the index's, or what the import made of it. */
export type ImportStatus = ScanLogStatus | "imported" | "failed"

/** A row of the import table: the index's row, its status updated by the imports since. */
export interface ImportRow extends Omit<ScanLogRow, "status"> {
  status: ImportStatus
  /** Why the import failed (`failed`); null otherwise. */
  error: string | null
}

export const IMPORT_STATUS_LABELS: Record<ImportStatus, string> = {
  new: "New",
  exists: "Exists",
  duplicate: "Duplicate",
  invalid: "Invalid",
  imported: "Imported",
  failed: "Failed",
}

/** The statuses in the order the page counts them. */
export const IMPORT_STATUSES: readonly ImportStatus[] = [
  "new",
  "exists",
  "duplicate",
  "invalid",
  "imported",
  "failed",
]

/** The table's rows from an index answer. */
export function importRowsOf(rows: readonly ScanLogRow[]): ImportRow[] {
  return rows.map((row) => ({ ...row, error: null }))
}

/** The row's ID in the table (its position in the log: a scan ID can repeat). */
export function importRowId(row: Pick<ScanLogRow, "scan_index">): string {
  return String(row.scan_index)
}

/** Only a New row can be selected (and imported). */
export function isImportable(row: { status: ImportStatus }): boolean {
  return row.status === "new"
}

/** Quick select's criteria: each applies when given. */
export interface QuickSelectCriteria {
  scanIdGreaterThan?: number
  /** An instant: a row's start (UTC) at or after it. */
  createdAtOrAfter?: Date
}

/**
 * The rows quick select picks, in row order (`scans.service.quick_select`): the New rows whose scan ID
 * is greater than N and whose start is at or after the instant, each criterion when given.
 */
export function quickSelect<
  TRow extends {
    scan_number: number | null
    status: ImportStatus
    time: string | null
  },
>(rows: readonly TRow[], criteria: QuickSelectCriteria): TRow[] {
  const { scanIdGreaterThan, createdAtOrAfter } = criteria
  const after = createdAtOrAfter?.getTime()
  return rows.filter(
    (row) =>
      isImportable(row) &&
      row.scan_number !== null &&
      (scanIdGreaterThan === undefined ||
        row.scan_number > scanIdGreaterThan) &&
      (after === undefined ||
        (row.time !== null && Date.parse(row.time) >= after))
  )
}

/**
 * The instant of a `datetime-local` input's value (`2026-10-22T14:05`, with seconds or not): the
 * browser's local time; undefined for a blank or unreadable value.
 */
export function localInstant(value: string): Date | undefined {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(value)) {
    return undefined
  }
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? undefined : date
}

/** A whole number typed in "Scan ID greater than": undefined for blank or anything else. */
export function scanIdThreshold(value: string): number | undefined {
  const text = value.trim()
  return /^\d+$/.test(text) ? Number(text) : undefined
}

/** A size in MiB with one decimal, rounded up, so a file just over a limit does not read as the limit. */
function mebibytesUp(bytes: number): string {
  return `${(Math.ceil(bytes / 104857.6) / 10).toFixed(1)} MiB`
}

/**
 * The upload's size check against the published limit (`GET /files/roots`' `scan_logs.max_bytes`), before
 * anything is sent (a browser does not wait for the server's early 413, which then reaches the page as a
 * network error): the message for a file over the limit, else undefined.
 */
export function scanLogSizeProblem(
  size: number,
  limit: number
): string | undefined {
  if (size <= limit) return undefined
  return `${mebibytesUp(size)}, over the ${formatBytes(limit)} limit`
}

/**
 * The rows after an import: each row sent (by its ID) takes its scan's result (Imported, Exists or
 * Failed, with the error); the others are kept as they are.
 */
export function applyImportResults(
  rows: readonly ImportRow[],
  sent: ReadonlySet<string>,
  results: readonly ScanImportItem[]
): ImportRow[] {
  const byScan = new Map(results.map((item) => [item.scan_number, item]))
  return rows.map((row) => {
    if (!sent.has(importRowId(row)) || row.scan_number === null) return row
    const item = byScan.get(row.scan_number)
    if (!item) return row
    return { ...row, status: item.outcome, error: item.error }
  })
}

/** The rows of each status, for the log's counts. */
export function statusCounts(
  rows: readonly { status: ImportStatus }[]
): Map<ImportStatus, number> {
  const counts = new Map<ImportStatus, number>()
  for (const row of rows)
    counts.set(row.status, (counts.get(row.status) ?? 0) + 1)
  return counts
}

/** The import's summary: "Imported 4 · Exists 1 · Failed 0". */
export function summaryText(summary: ScanImportSummary): string {
  return [
    `Imported ${String(summary.imported)}`,
    `Exists ${String(summary.exists)}`,
    `Failed ${String(summary.failed)}`,
  ].join(" · ")
}

/** The failed scans of a report, with their errors. */
export function failuresOf(report: ScanImportReport): ScanImportItem[] {
  return report.results.filter((item) => item.outcome === "failed")
}

function defaultsField(
  name: string,
  title: string,
  hint: string,
  more: Partial<FieldSpec> = {}
): FieldSpec {
  return {
    name,
    title,
    hint,
    type: "string",
    nullable: false,
    required: false,
    hasDefault: true,
    default: "",
    widget: "text",
    ...more,
  }
}

/** The catalog defaults' fields (`ScanImportDefaults`), for the form kit. */
export const CATALOG_DEFAULT_FIELDS = {
  aperture: defaultsField(
    "aperture",
    "Aperture",
    "The aperture of every scan imported.",
    {
      widget: "select",
      default: "wire",
      options: APERTURES.map((value) => ({
        value,
        label: APERTURE_LABELS[value],
      })),
    }
  ),
  sample_name: defaultsField(
    "sample_name",
    "Sample Name",
    "The sample of every scan imported."
  ),
  filefolder: defaultsField(
    "filefolder",
    "Files Path",
    "The folder of the scans' data files, on the server.",
    { widget: "folder" }
  ),
  filename_prefix: defaultsField(
    "filename_prefix",
    "Filename Prefix",
    "The data files' name patterns, separated by commas.",
    { placeholder: "Ni_%d.h5" }
  ),
  notes: defaultsField("notes", "Notes", "A note on every scan imported."),
} satisfies Record<keyof ScanImportDefaults, FieldSpec>

/** The catalog defaults' starting values: Wire, the rest blank. */
export const CATALOG_DEFAULT_VALUES: FormValues = {
  aperture: "wire",
  sample_name: "",
  filefolder: "",
  filename_prefix: "",
  notes: "",
}

function text(values: FormValues, name: keyof ScanImportDefaults): string {
  const value = values[name]
  return typeof value === "string" ? value : ""
}

/** What the import sends as `catalog_defaults`: the prefixes split at commas, blanks dropped. */
export function catalogDefaultsOf(values: FormValues): ScanImportDefaults {
  const aperture = text(values, "aperture")
  return {
    aperture: APERTURES.find((value) => value === aperture) ?? "wire",
    sample_name: text(values, "sample_name").trim(),
    filefolder: text(values, "filefolder").trim(),
    filename_prefix: text(values, "filename_prefix")
      .split(",")
      .map((prefix) => prefix.trim())
      .filter(Boolean),
    notes: text(values, "notes"),
  }
}
