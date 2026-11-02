// The validate endpoint's issues (and a refused create's), placed on the form: `loc[0]` names the field
// (`["box_size"]`; a filename template `["filename_prefixes", 1]`), `[]` or a name that is not a form
// field is about the whole form, shown only in the summary. The server's messages do not repeat the
// field's label ("Required", "Must be greater than 0"); the summary adds it once (`summaryText`).
import type { ProblemIssue } from "@/api"
import type { ProgramSpec } from "./program-spec"

export type IssueSeverity = "error" | "warning"

export interface FormIssue {
  /** The form field it belongs to; null for the whole form. */
  field: string | null
  severity: IssueSeverity
  code: string
  /** As shown under its field: without a leading "<label>: ". */
  message: string
}

export interface PlacedIssues {
  /** Every issue, the errors first (each in the server's order), each with its field (or null). */
  all: FormIssue[]
  /** The issues of each field. */
  byField: Record<string, FormIssue[]>
  errors: number
  warnings: number
}

/** An issue as the API gives it (ProblemItem's severity is optional, an error by default). */
export interface IssueLike {
  loc: (string | number)[]
  message: string
  code: string
  severity?: ProblemIssue["severity"]
}

/** A message that quotes a value (`Filename 'x_%d.h5' has …`) names its template itself. */
const QUOTES = /'[^']*'|"[^"]*"/

function fieldMessage(
  message: string,
  title: string | undefined,
  index: unknown
): string {
  const own =
    title && message.startsWith(`${title}: `)
      ? message.slice(title.length + 2)
      : message
  // A template's issue names it by its position (the Filename field holds them all), unless it quotes it.
  return typeof index === "number" && !QUOTES.test(own)
    ? `Template ${String(index + 1)}: ${own}`
    : own
}

/** Place each issue on its field (one of `spec.fields`), or on the whole form; the errors first. */
export function placeIssues(
  spec: Pick<ProgramSpec, "fields">,
  issues: readonly IssueLike[]
): PlacedIssues {
  const placed: FormIssue[] = issues.map((issue) => {
    const [head, index] = issue.loc
    const field = typeof head === "string" && head in spec.fields ? head : null
    return {
      field,
      severity: issue.severity ?? "error",
      code: issue.code,
      message:
        field === null
          ? issue.message
          : fieldMessage(issue.message, spec.fields[field]?.title, index),
    }
  })
  const all = [
    ...placed.filter((issue) => issue.severity === "error"),
    ...placed.filter((issue) => issue.severity !== "error"),
  ]
  const byField: Record<string, FormIssue[]> = {}
  for (const issue of all) {
    if (issue.field !== null) (byField[issue.field] ??= []).push(issue)
  }
  const errors = all.filter((issue) => issue.severity === "error").length
  return { all, byField, errors, warnings: all.length - errors }
}

/** Only the issues `shown` lets through (the touched fields'; the form's own when it says so). */
export function shownIssues(
  issues: PlacedIssues,
  shown: (field: string | null) => boolean
): PlacedIssues {
  const all = issues.all.filter((issue) => shown(issue.field))
  const byField: Record<string, FormIssue[]> = {}
  for (const issue of all) {
    if (issue.field !== null) (byField[issue.field] ??= []).push(issue)
  }
  const errors = all.filter((issue) => issue.severity === "error").length
  return { all, byField, errors, warnings: all.length - errors }
}

/** An issue's line in the summary: the field's label once ("Box Size: Required"), unless the message names it. */
export function summaryText(issue: FormIssue, title: string | undefined) {
  return title && !issue.message.includes(title)
    ? `${title}: ${issue.message}`
    : issue.message
}

export const NO_ISSUES: PlacedIssues = {
  all: [],
  byField: {},
  errors: 0,
  warnings: 0,
}
