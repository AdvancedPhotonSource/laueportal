// The form's values and the request's: the form holds text (numbers as typed, the filename templates as
// a comma-separated Filename) and booleans; the request holds the program's values as the validate and
// create endpoints take them. The server is the one validator: a number that does not parse (or is not
// finite) is sent as typed, and the validate endpoint reports it on its field.
import type { FieldSpec, ProgramSpec } from "./program-spec"

export type FormValue = string | boolean
export type FormValues = Record<string, FormValue>
export type RequestValues = Record<string, unknown>

const NUMBER = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i

/** A field's value in the form, from a value of the program (a default, a prefill). */
export function formValueOf(field: FieldSpec, value: unknown): FormValue {
  if (field.type === "boolean") return value === true
  if (value === null || value === undefined) return ""
  if (Array.isArray(value)) return value.map(String).join(", ")
  if (typeof value === "string") return value
  if (typeof value === "number") return String(value)
  return JSON.stringify(value)
}

/** The form's values from the program's (prefill's `values`): every form field, blank where missing. */
export function formValuesOf(
  spec: Pick<ProgramSpec, "fields">,
  values: RequestValues = {}
): FormValues {
  return Object.fromEntries(
    Object.values(spec.fields).map((field) => [
      field.name,
      formValueOf(
        field,
        field.name in values ? values[field.name] : field.default
      ),
    ])
  )
}

/** The configured defaults of some fields, as form values (Restore Default). */
export function defaultsOf(fields: readonly FieldSpec[]): FormValues {
  return Object.fromEntries(
    fields.map((field) => [field.name, formValueOf(field, field.default)])
  )
}

/**
 * An output template's folder and its part from the first segment with `%d` on: `/runs/rec_%d/data` is
 * `/runs` and `rec_%d/data`; without `%d`, the whole is the folder.
 */
export function outputParts(template: string): {
  folder: string
  tail: string
} {
  const parts = template.split("/")
  const at = parts.findIndex((part) => part.includes("%d"))
  if (at < 0) return { folder: template, tail: "" }
  return {
    folder: parts.slice(0, at).join("/") || "/",
    tail: parts.slice(at).join("/"),
  }
}

/** A folder chosen for an output template: the folder with the template's part from its `%d` on. */
export function withOutputFolder(folder: string, tail: string): string {
  return tail ? `${folder.replace(/\/+$/, "")}/${tail}` : folder
}

/** A comma-separated Filename as the list of templates: each trimmed, blanks dropped. */
export function templatesOf(text: string): string[] {
  return text
    .split(",")
    .map((template) => template.trim())
    .filter(Boolean)
}

/** A field's value in the request, from the form's. */
export function requestValueOf(
  field: FieldSpec,
  value: FormValue | undefined
): unknown {
  if (field.type === "boolean") return value === true
  const text = typeof value === "string" ? value : ""
  if (field.type === "list") return templatesOf(text)
  if (text.trim() === "") return field.nullable ? null : ""
  if (field.type === "integer" || field.type === "number") {
    const trimmed = text.trim()
    const number = NUMBER.test(trimmed) ? Number(trimmed) : NaN
    // "1e400" is Infinity, which JSON would send as null: the text goes, and the server refuses it.
    return Number.isFinite(number) ? number : text
  }
  return text
}

/** The request's values: one per form field (the "Run on" choice included). */
export function requestOf(
  spec: ProgramSpec,
  values: FormValues
): RequestValues {
  return Object.fromEntries(
    Object.values(spec.fields).map((field) => [
      field.name,
      requestValueOf(field, values[field.name]),
    ])
  )
}

/**
 * A new prefill over the form's values (`current`): a field takes the prefill's value (`next`) when the
 * prefill derives it from its source (a value other than the configured default, `defaults`) or when the
 * user has not changed it since the last prefill (`previous`). Returns the fields the user changed whose
 * value stays (Author, Notes, parameters).
 */
export function keptByUser(
  defaults: FormValues,
  previous: FormValues,
  next: FormValues,
  current: FormValues
): FormValues {
  const kept: FormValues = {}
  for (const [name, value] of Object.entries(next)) {
    const derived = value !== defaults[name]
    const mine = current[name]
    if (!derived && mine !== undefined && mine !== previous[name]) {
      kept[name] = mine
    }
  }
  return kept
}

/**
 * What a new prefill fills: every field (a clone), or only some (`fields`: a scan's or a reconstruction's
 * data fields and source IDs, also when a source is cleared), every other field staying as it is on screen.
 * The source IDs (`ids`) are set only by a source, so they always take the prefill's value.
 */
export interface PrefillScope {
  fields?: ReadonlySet<string>
  ids: ReadonlySet<string>
}

/**
 * A new prefill (`next`) over the form (`current`; `previous` the last one applied): the values it
 * applies (the form's new baseline) and the user's values that stay over them. Inside the scope, the kit's
 * rule (`keptByUser`); outside it, everything on screen stays.
 */
export function applyPrefill(
  defaults: FormValues,
  previous: FormValues,
  next: FormValues,
  current: FormValues,
  scope: PrefillScope
): { values: FormValues; kept: FormValues } {
  const { fields, ids } = scope
  const values =
    fields === undefined
      ? next
      : {
          ...previous,
          ...Object.fromEntries(
            Object.entries(next).filter(([name]) => fields.has(name))
          ),
        }
  const mine = keptByUser(defaults, previous, values, current)
  const kept: FormValues = {}
  for (const [name, value] of Object.entries(values)) {
    const shown = current[name]
    if (ids.has(name) || shown === undefined || shown === value) continue
    if (fields !== undefined && !fields.has(name)) kept[name] = shown
    else if (name in mine) kept[name] = shown
  }
  return { values, kept }
}
