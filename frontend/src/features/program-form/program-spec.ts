// A program's form, read from its JSON Schema as GET /api/v1/programs serves it: the form fields with their
// titles, user hints (`x-hint`; the `description` is the API's, for developers), units, limits, defaults
// and widgets, and the layout (`x-layout`). The schema is typed `{[key: string]: unknown}` by the API, so
// it is read through a Zod schema of the JSON Schema keywords the portal writes
// (`jobs.programs.parameters_schema`), never cast.
import { z } from "zod"
import type { ListedProgramKind, Program, ProgramKind } from "@/api"

/** How a field's value is held and sent. */
export type ValueType = "integer" | "number" | "string" | "boolean" | "list"

/** A select's choice (`x-options`, in order). */
export interface FieldOption {
  value: string
  label: string
}

/** One form field (`x-source` `form`). */
export interface FieldSpec {
  name: string
  title: string
  /** The user's hint (`x-hint`): the label's tooltip with the limits (`fieldHint`). */
  hint: string
  type: ValueType
  /** The schema allows null (`anyOf` with `null`): a blank value is sent as null. */
  nullable: boolean
  /** Listed in the schema's `required`. */
  required: boolean
  minimum?: number
  maximum?: number
  exclusiveMinimum?: number
  exclusiveMaximum?: number
  /** Whether the schema gives a default (null included); Restore Default needs one. */
  hasDefault: boolean
  default: unknown
  unit?: string
  /** `x-widget`: number, integer, text, textarea, select, checkbox, hkl, indices, folder, file, ... */
  widget: string
  /** At most a terse value pattern (`Si_%d.h5`, `Automatic`). */
  placeholder?: string
  /** A file picker's filter (`x-extensions`, e.g. `[".xml"]`). */
  extensions?: string[]
  options?: FieldOption[]
}

/** A section of the form (an `x-layout` group), its fields in order. */
export interface SectionSpec {
  group: string
  fields: FieldSpec[]
  /** "Restore Default": a section of several fields that all have a default. */
  restorable: boolean
}

/** A program's form: a create form's (`ProgramSpec<ProgramKind>`) or one of the Live page's cards. */
export interface ProgramSpec<K extends ListedProgramKind = ListedProgramKind> {
  kind: K
  title: string
  schemaId: string
  /** Every form field, by name. */
  fields: Record<string, FieldSpec>
  /** The layout's sections, the Execution section (the "Run on" choice) left out. */
  sections: SectionSpec[]
  /** The "Run on" field (`x-widget` `target`): the deployment's execution targets. */
  target?: FieldSpec
}

const optionSchema = z.object({ value: z.string(), label: z.string() })

const branchSchema = z.object({
  type: z.string().optional(),
  minimum: z.number().optional(),
  maximum: z.number().optional(),
  exclusiveMinimum: z.number().optional(),
  exclusiveMaximum: z.number().optional(),
})

const propertySchema = branchSchema.extend({
  title: z.string().optional(),
  anyOf: z.array(branchSchema).optional(),
  enum: z.array(z.unknown()).optional(),
  default: z.unknown().optional(),
  "x-source": z.string().optional(),
  "x-unit": z.string().optional(),
  "x-widget": z.string().optional(),
  "x-hint": z.string().optional(),
  "x-placeholder": z.string().optional(),
  "x-extensions": z.array(z.string()).optional(),
  "x-options": z.array(optionSchema).optional(),
})

const schemaSchema = z.object({
  properties: z.record(z.string(), propertySchema),
  required: z.array(z.string()).default([]),
  "x-layout": z.array(
    z.object({ group: z.string(), fields: z.array(z.string()) })
  ),
})

type Property = z.infer<typeof propertySchema>

/** The group of the Execution section, whose only field is the "Run on" choice. */
const EXECUTION_WIDGET = "target"

function valueType(property: Property): ValueType {
  const types = [
    property.type,
    ...(property.anyOf ?? []).map((branch) => branch.type),
  ]
  if (types.includes("array")) return "list"
  if (types.includes("boolean")) return "boolean"
  if (types.includes("integer")) return "integer"
  if (types.includes("number")) return "number"
  return "string"
}

function fieldSpec(
  name: string,
  property: Property,
  required: boolean
): FieldSpec {
  // The limits are on the property, or on its non-null branch for a nullable field.
  const limits =
    property.anyOf?.find((branch) => branch.type !== "null") ?? property
  return {
    name,
    title: property.title ?? name,
    hint: property["x-hint"] ?? "",
    type: valueType(property),
    nullable: property.anyOf?.some((branch) => branch.type === "null") ?? false,
    required,
    minimum: limits.minimum,
    maximum: limits.maximum,
    exclusiveMinimum: limits.exclusiveMinimum,
    exclusiveMaximum: limits.exclusiveMaximum,
    hasDefault: "default" in property,
    default: property.default,
    unit: property["x-unit"],
    widget: property["x-widget"] ?? "text",
    placeholder: property["x-placeholder"],
    extensions: property["x-extensions"],
    options: property["x-options"],
  }
}

/**
 * A listed program of `kind`: `GET /programs` lists the create forms' and, while live processing is on, the Live
 * page's.
 */
export type ProgramOf<K extends ListedProgramKind> = Program & { kind: K }

/** A create form's program (the batch ones). */
export type BatchProgram = ProgramOf<ProgramKind>

/** Whether `program` is the one of `kind` (a `find` over the listed programs). */
export function isProgram<K extends ListedProgramKind>(kind: K) {
  return (program: Program): program is ProgramOf<K> => program.kind === kind
}

/** Read a program's form from its served JSON Schema. Throws when the schema is not the expected shape. */
export function programSpec<K extends ListedProgramKind>(
  program: ProgramOf<K>
): ProgramSpec<K> {
  const schema = schemaSchema.parse(program.json_schema)
  const required = new Set(schema.required)
  const fields: Record<string, FieldSpec> = {}
  for (const [name, property] of Object.entries(schema.properties)) {
    if (property["x-source"] !== "form") continue
    fields[name] = fieldSpec(name, property, required.has(name))
  }
  let target: FieldSpec | undefined
  const sections: SectionSpec[] = []
  for (const { group, fields: names } of schema["x-layout"]) {
    const sectionFields = names.flatMap((name) => fields[name] ?? [])
    const runOn = sectionFields.find(
      (field) => field.widget === EXECUTION_WIDGET
    )
    if (runOn) target = runOn
    const shown = sectionFields.filter(
      (field) => field.widget !== EXECUTION_WIDGET
    )
    if (shown.length === 0) continue
    sections.push({
      group,
      fields: shown,
      restorable: shown.length > 1 && shown.every((field) => field.hasDefault),
    })
  }
  return {
    kind: program.kind,
    title: program.title,
    schemaId: program.schema_id,
    fields,
    sections,
    target,
  }
}

/** A field's limits as a short text for its tooltip: `> 0`, `0 – 1`, `≥ 0, ≤ 180`; empty without limits. */
export function limitText(field: FieldSpec): string {
  const lower =
    field.exclusiveMinimum !== undefined
      ? `> ${String(field.exclusiveMinimum)}`
      : field.minimum !== undefined
        ? `≥ ${String(field.minimum)}`
        : undefined
  const upper =
    field.exclusiveMaximum !== undefined
      ? `< ${String(field.exclusiveMaximum)}`
      : field.maximum !== undefined
        ? `≤ ${String(field.maximum)}`
        : undefined
  return [lower, upper].filter(Boolean).join(", ")
}

/** A field's tooltip: its hint and its limits. */
export function fieldHint(field: FieldSpec): string {
  const limits = limitText(field)
  return [field.hint, limits && `Limits: ${limits}`].filter(Boolean).join("\n")
}

/** An anchor for a section, for a page's sidebar (`#section-file-paths`). */
export function sectionAnchor(group: string): string {
  return `section-${group.toLowerCase().replaceAll(/[^a-z0-9]+/g, "-")}`
}

/** What a section shows: a field, or a row of fields shown as one (HKL; the index ranges with Find). */
export type FieldBlock =
  | { kind: "field"; field: FieldSpec }
  | { kind: "hkl"; fields: FieldSpec[] }
  | { kind: "indices"; fields: FieldSpec[] }

/** A section's fields as blocks: consecutive `hkl` fields, and consecutive `indices` fields, are one. */
export function blocksOf(fields: readonly FieldSpec[]): FieldBlock[] {
  const blocks: FieldBlock[] = []
  for (const field of fields) {
    const last = blocks.at(-1)
    if (field.widget === "hkl" || field.widget === "indices") {
      if (last?.kind === field.widget) last.fields.push(field)
      else blocks.push({ kind: field.widget, fields: [field] })
    } else {
      blocks.push({ kind: "field", field })
    }
  }
  return blocks
}

/** The first field with this widget (the folder, the filename templates), if the form has one. */
export function fieldWithWidget(
  spec: ProgramSpec,
  widget: string
): FieldSpec | undefined {
  return Object.values(spec.fields).find((field) => field.widget === widget)
}

/** The widgets of the source IDs: a scan's number, a reconstruction's ID. */
export const SOURCE_ID_WIDGETS: ReadonlySet<string> = new Set([
  "scan",
  "reconstruction",
])

/** The source ID fields (a scan's number, a reconstruction's ID): only a source sets them. */
export function sourceIdsOf(spec: ProgramSpec): ReadonlySet<string> {
  return new Set(
    Object.values(spec.fields)
      .filter((field) => SOURCE_ID_WIDGETS.has(field.widget))
      .map((field) => field.name)
  )
}
