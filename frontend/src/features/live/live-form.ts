// The Live page's form: the Scan card and the two program cards, their request (`POST /live-runs/validate`,
// `/live-runs`), the issues placed on them, the session's state and the values a session gives the form
// (shown read-only while it is open, the next session's prefill once it has ended). Pure functions: the page
// (LivePage.tsx) holds the forms and the queries.
import {
  ApiError,
  type LiveDaqEntry,
  type LiveRunsConflict,
  type LiveSession,
  type LiveSessionRun,
  type RunStatus,
} from "@/api"
import { formatDateTime } from "@/lib/format"
import {
  type FieldSpec,
  type FormIssue,
  type FormValues,
  formValuesOf,
  type IssueLike,
  outputParts,
  type PlacedIssues,
  placeIssues,
  type ProgramSpec,
  type RequestValues,
  requestOf,
  requestValueOf,
  type SectionSpec,
} from "@/features/program-form"

/** The form's parts, as the API names them (each issue's `loc[0]`). */
export type Part = "scan" | "reconstruction" | "indexing"

export const PART_TITLES: Record<Part, string> = {
  scan: "Scan",
  reconstruction: "Reconstruction",
  indexing: "Indexing",
}

/** The Source choice for `laueportal live notify` (else a DAQ's `daq:<id>`). */
export const NOTIFY = "notify"
const DAQ = "daq:"

/**
 * What the Scan card sets on each run (the API refuses a program part that sends one): the program
 * cards leave these out.
 */
export const SCAN_SETS: ReadonlySet<string> = new Set([
  "scan_number",
  "author",
  "input_path",
  "filename_prefixes",
  "output_path_template",
  "reconstruction_id",
])

/** The LaueGo run's depths: only while it follows the reconstruction. */
export const FOLLOWER_FIELD = "depth_range"

/** Which runs a session creates: the program cards' switches (one, the other, or both). */
export interface Switches {
  reconstruction: boolean
  indexing: boolean
}

/** The three forms' values. */
export interface LiveValues {
  scan: FormValues
  reconstruction: FormValues
  indexing: FormValues
}

/** The specs of the page's forms: the Scan card's fields and the two program cards. */
export interface LiveSpecs {
  scan: Record<string, FieldSpec>
  reconstruction: ProgramSpec
  indexing: ProgramSpec
}

function scanField(
  base: FieldSpec | undefined,
  field: Partial<FieldSpec> & Pick<FieldSpec, "name" | "title" | "hint">
): FieldSpec {
  return {
    type: "string",
    nullable: false,
    required: true,
    hasDefault: false,
    default: undefined,
    widget: "text",
    ...base,
    placeholder: undefined,
    extensions: undefined,
    options: undefined,
    ...field,
  }
}

/**
 * The Scan card's fields: the session's source and the identity and paths it gives each run. The scan
 * number and author are the runs' own fields; the paths are the session's.
 */
export function scanFields(wire: ProgramSpec): Record<string, FieldSpec> {
  const { scan_number, author, input_path, filename_prefixes } = wire.fields
  const output = wire.fields.output_path_template
  const fields = [
    scanField(undefined, {
      name: "source",
      title: "Source",
      hint: "Where new files are taken from: a DM DAQ's catalog, or a notification for each file",
      widget: "select",
    }),
    scanField(scan_number, {
      name: "scan_number",
      title: scan_number?.title ?? "Scan Number",
      hint: scan_number?.hint ?? "",
      type: "integer",
      nullable: true,
      required: false,
      widget: "integer",
      // Blank is a session of no scan (a warning), not an automatic value.
      placeholder: "",
    }),
    scanField(author, {
      name: "author",
      title: author?.title ?? "Author",
      hint: author?.hint ?? "",
    }),
    scanField(input_path, {
      name: "folder",
      title: "Folder",
      hint: "The folder the files arrive in",
      widget: "folder",
      hasDefault: false,
      default: undefined,
    }),
    scanField(undefined, {
      name: "filename_template",
      title: "Filename Template",
      hint: "The files' name, %d each file's number",
      placeholder: filename_prefixes?.placeholder,
    }),
    scanField(undefined, {
      name: "output_folder",
      title: "Output Folder",
      hint: "The folder the session's runs go in (rec_<ID>, index_<ID>)",
      widget: "folder",
      hasDefault: output?.hasDefault === true,
      default:
        typeof output?.default === "string"
          ? outputParts(output.default).folder
          : undefined,
    }),
  ]
  return Object.fromEntries(fields.map((field) => [field.name, field]))
}

/** A program card's spec: the live program's fields but those the Scan card sets. */
export function cardSpec(spec: ProgramSpec): ProgramSpec {
  const fields = Object.fromEntries(
    Object.entries(spec.fields).filter(([name]) => !SCAN_SETS.has(name))
  )
  const sections: SectionSpec[] = spec.sections.flatMap((section) => {
    const shown = section.fields.filter((field) => !SCAN_SETS.has(field.name))
    return shown.length === 0 ? [] : [{ ...section, fields: shown }]
  })
  return { ...spec, fields, sections }
}

/** A card's sections as shown: the LaueGo card's depths only while it follows the reconstruction. */
export function shownSections(
  spec: ProgramSpec,
  follower: boolean
): SectionSpec[] {
  if (follower) return spec.sections
  return spec.sections.flatMap((section) => {
    const shown = section.fields.filter(
      (field) => field.name !== FOLLOWER_FIELD
    )
    return shown.length === 0 ? [] : [{ ...section, fields: shown }]
  })
}

/** The Source choice's value for a DAQ. */
export function daqChoice(id: string): string {
  return `${DAQ}${id}`
}

/** A DAQ's option label: its experiment, its data folder's last two names, its status. */
export function daqLabel(daq: LiveDaqEntry): string {
  const folder = (daq.data_directory ?? "")
    .split("/")
    .filter(Boolean)
    .slice(-2)
    .join("/")
  const status = daq.status ? ` (${daq.status})` : ""
  return `${daq.experiment}${folder ? ` · ${folder}` : ""}${status}`
}

/** The Source choices: Notifications, each listed DAQ, and a chosen DAQ the list does not hold (by its ID). */
export function sourceOptions(
  daqs: readonly LiveDaqEntry[],
  chosen: string
): { value: string; label: string }[] {
  const options = [
    { value: NOTIFY, label: "Notifications" },
    ...daqs.map((daq) => ({ value: daqChoice(daq.id), label: daqLabel(daq) })),
  ]
  if (chosen.startsWith(DAQ) && !options.some((o) => o.value === chosen)) {
    options.push({ value: chosen, label: chosen.slice(DAQ.length) })
  }
  return options
}

/** The DAQ a Source choice names, if the list holds it. */
export function chosenDaq(
  daqs: readonly LiveDaqEntry[],
  choice: string
): LiveDaqEntry | undefined {
  if (!choice.startsWith(DAQ)) return undefined
  const id = choice.slice(DAQ.length)
  return daqs.find((daq) => daq.id === id)
}

/** Whether `path` is `folder` or inside it, by its text. */
function inside(path: string, folder: string): boolean {
  const base = folder.replace(/\/+$/, "")
  return path === base || path.startsWith(`${base}/`)
}

/**
 * The Folder a DAQ picked fills in: the DAQ's destination, unless the folder is already inside it;
 * undefined when it changes nothing.
 */
export function daqFolder(
  daq: LiveDaqEntry,
  folder: string
): string | undefined {
  const destination = daq.destination
  if (!destination) return undefined
  const typed = folder.trim()
  return typed && inside(typed, destination) ? undefined : destination
}

/** The request of the Scan card's values. */
function scanRequest(
  fields: Record<string, FieldSpec>,
  values: FormValues
): RequestValues {
  const text = (name: string) => {
    const value = values[name]
    return typeof value === "string" ? value : ""
  }
  const source = text("source")
  const daq = source.startsWith(DAQ)
  return {
    source: daq ? "daq" : source,
    daq_id: daq ? source.slice(DAQ.length) : null,
    folder: text("folder"),
    filename_template: text("filename_template"),
    output_folder: text("output_folder"),
    scan_number: fields.scan_number
      ? requestValueOf(fields.scan_number, values.scan_number)
      : null,
    author: text("author"),
  }
}

/** The session's request (`POST /live-runs/validate`, `/live-runs`): the Scan card and each part switched on. */
export function sessionRequest(
  specs: LiveSpecs,
  values: LiveValues,
  on: Switches
): RequestValues {
  const indexing = on.indexing
    ? requestOf(specs.indexing, values.indexing)
    : null
  if (indexing && !on.reconstruction) indexing[FOLLOWER_FIELD] = null
  return {
    scan: scanRequest(specs.scan, values.scan),
    reconstruction: on.reconstruction
      ? requestOf(specs.reconstruction, values.reconstruction)
      : null,
    indexing,
  }
}

/** A field's name across the page's forms: `scan.folder`, `indexing.geometry_file`. */
export function qualified(part: Part, name: string): string {
  return `${part}.${name}`
}

/** The part and field of a qualified name. */
export function splitQualified(
  name: string
): { part: Part; field: string } | undefined {
  const [part, field] = name.split(".", 2)
  if (part !== "scan" && part !== "reconstruction" && part !== "indexing") {
    return undefined
  }
  return field ? { part, field } : undefined
}

/** The Scan card's field an issue of the Scan part is about (a DAQ's is the Source's). */
function scanName(name: unknown): unknown {
  return name === "daq_id" ? "source" : name
}

/**
 * The session's issues placed on the page's fields (`loc[0]` the part, `loc[1]` its field; a DAQ's on Source),
 * each field by its qualified name; an issue on no field is the form's own. The errors first.
 */
export function placeSessionIssues(
  specs: LiveSpecs,
  issues: readonly IssueLike[]
): PlacedIssues {
  const fieldsOf: Record<Part, Record<string, FieldSpec>> = {
    scan: specs.scan,
    reconstruction: specs.reconstruction.fields,
    indexing: specs.indexing.fields,
  }
  const placed: FormIssue[] = issues.flatMap((issue) => {
    const [part, ...rest] = issue.loc
    if (part !== "scan" && part !== "reconstruction" && part !== "indexing") {
      return placeIssues({ fields: {} }, [issue]).all
    }
    const loc = part === "scan" ? [scanName(rest[0]), ...rest.slice(1)] : rest
    return placeIssues({ fields: fieldsOf[part] }, [
      { ...issue, loc: loc as (string | number)[] },
    ]).all.map((found) => ({
      ...found,
      field: found.field === null ? null : qualified(part, found.field),
    }))
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

/** A qualified field's label in the summary: the Scan card's alone, a program card's with its card. */
export function summaryTitle(
  specs: LiveSpecs,
  name: string | null
): string | undefined {
  const at = name === null ? undefined : splitQualified(name)
  if (!at) return undefined
  if (at.part === "scan") return specs.scan[at.field]?.title
  const field = specs[at.part].fields[at.field]
  return field ? `${PART_TITLES[at.part]} · ${field.title}` : undefined
}

/** The session's state, for its badge: none yet, or its runs' together. */
export type SessionState = "none" | RunStatus | "stopping"

const OPEN_STATUSES: ReadonlySet<RunStatus> = new Set(["queued", "running"])

/**
 * The session's state: while open, its runs'; Stopping once a stop is asked or its first run has ended (a
 * follower drains after its wire run), until the session ends; ended, Failed when a run failed, Cancelled
 * when every run was, else Finished.
 */
export function sessionState(
  session: LiveSession | null | undefined
): SessionState {
  if (!session || session.runs.length === 0) return "none"
  const statuses = session.runs.map((run) => run.status)
  if (session.open) {
    const first = session.runs[0]
    const drains = first !== undefined && !OPEN_STATUSES.has(first.status)
    if (drains || session.runs.some((run) => run.stop_requested)) {
      return "stopping"
    }
    return statuses.includes("running") ? "running" : "queued"
  }
  if (statuses.includes("failed")) return "failed"
  if (statuses.every((status) => status === "cancelled")) return "cancelled"
  return "finished"
}

/**
 * The run a Stop stops (`POST /jobs/{id}/cancel`): the session's first, its wire run when it has one (a follower
 * drains after it). Once the session is Stopping, Stop is Stop now (`openRuns`).
 */
export function sourceRun(session: LiveSession): LiveSessionRun | undefined {
  return session.runs[0]
}

/**
 * The runs a Stop now reaches (`POST /jobs/{id}/stop-now`): every run of the session still open. One not stopped
 * yet is stopped (a follower seals and drains what it has); one already stopping is killed and ends Failed, keeping
 * what it published.
 */
export function openRuns(session: LiveSession): LiveSessionRun[] {
  return session.runs.filter((run) => OPEN_STATUSES.has(run.status))
}

/**
 * Why an open session waits: the waiting reason of an open run's arrival status (a follower held by a point it
 * cannot read, or a DAQ's catalog error), for the Stopping badge's tooltip; none otherwise.
 */
export function waitingReason(
  session: LiveSession | null | undefined
): string | undefined {
  if (!session?.open) return undefined
  for (const run of openRuns(session)) {
    const arrival = run.arrival
    if (arrival?.state === "available" && arrival.error) {
      return `${run.run?.label ?? `Job ${String(run.job_id)}`}: ${arrival.error}`
    }
  }
  return undefined
}

/**
 * The jobs of the session a Start's 409 names: the open session's run (`live-session-open`) or the runs its key
 * created (`key-created`); none for another answer.
 */
export function conflictJobs(error: unknown): number[] {
  if (!(error instanceof ApiError) || error.status !== 409) return []
  const problem = error.problem as LiveRunsConflict | undefined
  if (problem?.open_run) return [problem.open_run.job_id]
  return problem?.created?.runs.map((run) => run.job_id) ?? []
}

/**
 * Whether the session a 409 named (`conflictJobs`) has ended, as the latest session read says: it is that
 * session, closed, or a later one (its jobs are newer). A session read before the 409's is neither.
 */
export function namedSessionEnded(
  jobs: readonly number[],
  session: LiveSession | null | undefined
): boolean {
  const ids = session?.runs.map((run) => run.job_id) ?? []
  if (jobs.length === 0 || ids.length === 0) return false
  if (jobs.some((job) => ids.includes(job))) return session?.open === false
  return Math.min(...ids) > Math.max(...jobs)
}

/** Which program cards a session's runs switch on. */
export function sessionSwitches(session: LiveSession): Switches {
  return {
    reconstruction: session.runs.some(
      (run) => run.run?.kind === "reconstruction"
    ),
    indexing: session.runs.some((run) => run.run?.kind === "indexing"),
  }
}

/**
 * The Scan card's values from a session's (`LiveSession.scan`): its author while it is open (shown as it
 * runs); once it has ended, the configured one, as a clone resets it (a value the user typed stays).
 */
export function scanValuesOf(
  fields: Record<string, FieldSpec>,
  session: LiveSession
): FormValues {
  const blank = blankScan(fields)
  const scan = session.scan
  if (!scan) return blank
  return {
    ...blank,
    source:
      scan.source === "daq" && scan.daq_id ? daqChoice(scan.daq_id) : NOTIFY,
    scan_number: scan.scan_number === null ? "" : String(scan.scan_number),
    author: session.open ? (scan.author ?? "") : (blank.author ?? ""),
    folder: scan.folder,
    filename_template: scan.filename_template,
    output_folder: scan.output_folder ?? blank.output_folder ?? "",
  }
}

/** The Scan card's values before any session: Notifications, the configured defaults, blanks. */
export function blankScan(fields: Record<string, FieldSpec>): FormValues {
  return {
    ...formValuesOf({ fields }, {}),
    source: NOTIFY,
  }
}

/** A program card's values from its clone (`GET /programs/{kind}/prefill`'s `values`). */
export function cardValuesOf(
  spec: ProgramSpec,
  values: RequestValues | undefined
): FormValues {
  return formValuesOf(spec, values)
}

/** The wait before asking for the DAQ list again: no sooner than its answer is kept. */
export function daqListWait(
  kept: number | undefined,
  error: ApiError | null
): number {
  const seconds = error ? (error.retryAfter ?? 30) : (kept ?? 30)
  return (Math.max(1, seconds) + 1) * 1000
}

/** A run's arrival status as one line (its `arrival-status.json`): its error, else its last check; none: undefined. */
export function arrivalLine(
  run: LiveSessionRun
): { text: string; tone: "warning" | "muted"; hint?: string } | undefined {
  const arrival = run.arrival
  if (!arrival) return undefined
  if (arrival.state === "not_responding") {
    return { text: "Not responding", tone: "warning" }
  }
  if (arrival.state !== "available") {
    return { text: "Unreadable", tone: "warning" }
  }
  if (arrival.error) {
    return {
      text: arrival.error,
      tone: "warning",
      hint: arrival.error_at
        ? `Since ${formatDateTime(arrival.error_at)}`
        : undefined,
    }
  }
  return arrival.checked_at
    ? { text: `Checked ${formatDateTime(arrival.checked_at)}`, tone: "muted" }
    : undefined
}
