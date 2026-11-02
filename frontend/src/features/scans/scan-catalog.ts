// The Catalog tab's form: its values from a stored catalog, its client rules, the whole-catalog
// PUT it sends (updateScanCatalog), and the server's per-field problems (a folder outside the
// roots is a 400 at `filefolder`).
import type { DeepPartial } from "react-hook-form"
import { z } from "zod"

import { ApiError, type ScanCatalog, type ScanCatalogUpdate } from "@/api"
import { APERTURES, apertureValue, type Aperture } from "./scan-detail"

export interface CatalogFormValues {
  /** Null until chosen: a new catalog, or a stored aperture the form does not offer. */
  aperture: Aperture | null
  sample_name: string
  filefolder: string
  /** One entry per prefix (React Hook Form's field arrays hold objects). */
  filename_prefix: { value: string }[]
  notes: string
}

/** The form's values for a stored catalog, or an empty form for a scan without one. */
export function catalogFormValues(
  catalog: ScanCatalog | null | undefined
): CatalogFormValues {
  return {
    aperture: apertureValue(catalog?.aperture),
    sample_name: catalog?.sample_name ?? "",
    filefolder: catalog?.filefolder ?? "",
    filename_prefix: (catalog?.filename_prefix ?? []).map((value) => ({
      value,
    })),
    notes: catalog?.notes ?? "",
  }
}

/** The form's values as one comparable text (a watched form's values may lack entries). */
function valuesKey(values: DeepPartial<CatalogFormValues>): string {
  return JSON.stringify([
    values.aperture ?? null,
    values.sample_name ?? "",
    values.filefolder ?? "",
    (values.filename_prefix ?? []).map((prefix) => prefix?.value ?? ""),
    values.notes ?? "",
  ])
}

/** Whether the form's (watched) values are those of a stored catalog's form: no edits. */
export function sameCatalogValues(
  values: DeepPartial<CatalogFormValues>,
  stored: CatalogFormValues
): boolean {
  return valuesKey(values) === valuesKey(stored)
}

/** The client's rules; the server checks the folder against the allowed roots. */
export const catalogFormSchema = z.object({
  aperture: z
    .enum(APERTURES)
    .nullable()
    .refine((aperture) => aperture !== null, { error: "Choose an aperture." }),
  sample_name: z.string(),
  filefolder: z
    .string()
    .trim()
    .min(1, { error: "Enter the data folder." })
    .refine((folder) => folder === "" || folder.startsWith("/"), {
      error: "Enter an absolute path (it starts with /).",
    }),
  filename_prefix: z.array(z.object({ value: z.string() })),
  notes: z.string(),
})

/** The values the form's rules let through: the aperture is chosen. */
export type CatalogFormOutput = z.output<typeof catalogFormSchema>

/**
 * The whole catalog to PUT: the folder trimmed, empty prefixes dropped, and a blank sample name or
 * notes as null; with the version of the stored catalog the edits started from (null for a scan
 * without one), so a catalog changed since is not overwritten (a 409).
 */
export function catalogUpdate(
  values: CatalogFormOutput,
  version: string | null
): ScanCatalogUpdate {
  const sample = values.sample_name.trim()
  return {
    version,
    aperture: values.aperture,
    sample_name: sample || null,
    filefolder: values.filefolder.trim(),
    filename_prefix: values.filename_prefix
      .map((prefix) => prefix.value.trim())
      .filter(Boolean),
    notes: values.notes.trim() ? values.notes : null,
  }
}

const FORM_FIELDS = ["aperture", "sample_name", "filefolder", "notes"] as const
export type CatalogField = (typeof FORM_FIELDS)[number]

export interface FieldProblem {
  field: CatalogField
  message: string
}

/**
 * The problems of a failed save that belong to a field of the form (`errors[].loc` names it, e.g.
 * the 400 for a folder outside the roots); empty for any other error.
 */
export function catalogFieldProblems(error: unknown): FieldProblem[] {
  if (!(error instanceof ApiError)) return []
  return (error.problem?.errors ?? []).flatMap((issue) => {
    const field = FORM_FIELDS.find((name) => name === issue.loc[0])
    return field ? [{ field, message: issue.message }] : []
  })
}

/** The problem type of a save over a catalog that changed since it was loaded (a 409). */
export const CATALOG_CHANGED = "urn:laueportal:problem:catalog-changed"

/** Whether a failed save was refused because the stored catalog changed since it was loaded. */
export function isCatalogChanged(error: unknown): boolean {
  return error instanceof ApiError && error.problem?.type === CATALOG_CHANGED
}

/**
 * Whether `appended` (a note append's answer) is `base` with only the note `text` appended (on a
 * new line, or the whole notes when they were empty). Then the edits started from `base` may carry
 * `appended`'s version: the form keeps that note, so a save drops nothing.
 */
export function onlyNoteAppended(
  base: ScanCatalog,
  text: string,
  appended: ScanCatalog
): boolean {
  return (
    appended.notes === (base.notes ? `${base.notes}\n${text}` : text) &&
    appended.filefolder === base.filefolder &&
    appended.aperture === base.aperture &&
    appended.sample_name === base.sample_name &&
    JSON.stringify(appended.filename_prefix) ===
      JSON.stringify(base.filename_prefix)
  )
}
