// The File Paths helpers ("Find Matching Files" and "Find Indices", the API's files/patterns and
// files/indices): a filename field with suggested templates, and the index ranges with Find.
import { LoaderCircleIcon } from "lucide-react"
import { useId, useState } from "react"
import { type Control, useController } from "react-hook-form"
import {
  type FileIndices,
  findFileIndices,
  findFilePatterns,
  type FilePattern,
  type FilePatterns,
  type ProgramKind,
} from "@/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { describeError } from "@/lib/errors"
import { formatCount } from "@/lib/format"
import { surface } from "@/lib/styles"
import { cn } from "@/lib/utils"
import { FieldShell } from "./FieldShell"
import type { FormValues } from "./form-values"
import { RangeField } from "./fields"
import type { FormIssue } from "./issues"
import { fieldHint, type FieldSpec } from "./program-spec"

type Lookup<T> =
  | { state: "idle" }
  | { state: "busy" }
  | { state: "done"; answer: T }
  | { state: "failed"; message: string }

function messageOf(error: unknown): string {
  const { title, detail } = describeError(error)
  return detail ? `${title}: ${detail}` : title
}

function isError(issues: readonly FormIssue[] | undefined): boolean {
  return issues?.some((issue) => issue.severity === "error") ?? false
}

/**
 * Whether the input path names an HDF5 file (a reconstruction's scan.h5) rather than a folder of frames:
 * the helpers read folders only (the API refuses a file).
 */
function isFileInput(path: string): boolean {
  const name = path.trim().replace(/\/+$/, "").split("/").pop() ?? ""
  return /\.(h5|hdf5|hdf)$/i.test(name)
}

const FILE_INPUT_REASON =
  "The input is a file (a reconstruction's scan.h5), not a folder"

function FindButton({
  busy,
  disabled,
  title,
  onClick,
  children,
}: {
  busy: boolean
  /** Why the button is off (its tooltip), or false. */
  disabled: string | boolean
  title: string
  onClick: () => void
  children: string
}) {
  const reason = typeof disabled === "string" ? disabled : undefined
  const button = (
    <Button
      type="button"
      variant="outline"
      disabled={disabled !== false || busy}
      title={reason === undefined ? title : undefined}
      onClick={onClick}
      className="shrink-0 text-foreground"
    >
      {busy && <LoaderCircleIcon className="animate-spin" />}
      {children}
    </Button>
  )
  // A disabled button takes no pointer events, so its reason sits on a wrapper.
  return reason === undefined ? (
    button
  ) : (
    <span title={reason} className="inline-flex shrink-0">
      {button}
    </span>
  )
}

/** Why Find is off for this folder value: a file input's reason, true when empty, else false. */
function findDisabled(folder: string): string | boolean {
  if (isFileInput(folder)) return FILE_INPUT_REASON
  return folder.trim() === ""
}

/** A suggestion's index ranges by the form's labels: "Scan Indices 2,745 · Depth Indices 0-90:30". */
function indicesText(pattern: FilePattern): string {
  return [
    pattern.scan_points !== null && `Scan Indices ${pattern.scan_points}`,
    pattern.depth_range !== null && `Depth Indices ${pattern.depth_range}`,
  ]
    .filter(Boolean)
    .join(" · ")
}

/** The suggestions of a Find Matching Files: each template with its index ranges, to pick one. */
function PatternSuggestions({
  answer,
  onPick,
}: {
  answer: FilePatterns
  onPick: (pattern: FilePattern) => void
}) {
  if (answer.patterns.length === 0) {
    return <p className="text-sm text-muted-foreground">No HDF files</p>
  }
  return (
    <div className={cn(surface, "flex flex-col p-1")}>
      <ul aria-label="Matching files" className="flex flex-col">
        {answer.patterns.map((pattern) => (
          <li key={pattern.pattern}>
            <button
              type="button"
              className="flex w-full flex-wrap items-baseline gap-x-3 rounded-md px-2 py-1 text-left text-sm hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
              onClick={() => {
                onPick(pattern)
              }}
            >
              <span className="font-mono">{pattern.pattern}</span>
              <span className="text-muted-foreground">
                {indicesText(pattern)}
              </span>
            </button>
          </li>
        ))}
      </ul>
      {answer.truncated && (
        <p className="px-2 py-1 text-sm text-muted-foreground">
          Truncated: the first 200,000 files
        </p>
      )}
    </div>
  )
}

/**
 * The filename templates (a comma-separated Filename) with Find Matching Files: the folder's
 * suggestions; picking one sets the field to its template and gives it to `onPick` (its index ranges).
 */
export function FilenamePatternField({
  field,
  control,
  issues,
  kind,
  folder,
  onPick,
  className,
}: {
  field: FieldSpec
  control: Control<FormValues>
  issues?: readonly FormIssue[]
  kind: ProgramKind
  /** The folder field's value. */
  folder: string
  onPick?: (pattern: FilePattern) => void
  className?: string
}) {
  const id = useId()
  const {
    field: { name, ref, value, onChange, onBlur },
  } = useController({ name: field.name, control })
  const [lookup, setLookup] = useState<Lookup<FilePatterns>>({ state: "idle" })
  const find = async () => {
    setLookup({ state: "busy" })
    try {
      const answer = await findFilePatterns({
        folder: folder.trim(),
        program: kind === "wire" ? "wire" : "lauego",
      })
      setLookup({ state: "done", answer })
    } catch (error) {
      setLookup({ state: "failed", message: messageOf(error) })
    }
  }
  return (
    <FieldShell
      id={id}
      label={field.title}
      hint={fieldHint(field)}
      issues={issues}
      className={className}
    >
      <div className="flex gap-2">
        <Input
          id={id}
          name={name}
          ref={ref}
          value={typeof value === "string" ? value : ""}
          onChange={(event) => {
            onChange(event.target.value)
          }}
          onBlur={onBlur}
          placeholder={field.placeholder}
          autoComplete="off"
          spellCheck={false}
          aria-invalid={isError(issues) || undefined}
          aria-describedby={issues?.length ? `${id}-issues` : undefined}
          className="font-mono"
        />
        <FindButton
          busy={lookup.state === "busy"}
          disabled={findDisabled(folder)}
          title="Suggest templates from the folder's files"
          onClick={() => {
            void find()
          }}
        >
          Find Matching Files
        </FindButton>
      </div>
      {lookup.state === "failed" && (
        <p role="alert" className="text-sm text-destructive">
          {lookup.message}
        </p>
      )}
      {lookup.state === "done" && (
        <PatternSuggestions
          answer={lookup.answer}
          onPick={(pattern) => {
            onChange(pattern.pattern)
            setLookup({ state: "idle" })
            onPick?.(pattern)
          }}
        />
      )}
    </FieldShell>
  )
}

/**
 * The index ranges of the File Paths (Scan Indices, and Depth Indices on the LaueGo form) with Find
 * Indices: the indices of the folder's files that match the first template, given to `onFound`.
 */
export function IndicesField({
  fields,
  control,
  issuesOf,
  kind,
  folder,
  template,
  onFound,
  className,
}: {
  fields: readonly FieldSpec[]
  control: Control<FormValues>
  issuesOf: (name: string) => readonly FormIssue[] | undefined
  kind: ProgramKind
  folder: string
  /** The first filename template (LaueGo matches it; the wire form reads every HDF file). */
  template: string
  onFound?: (indices: FileIndices) => void
  className?: string
}) {
  const [lookup, setLookup] = useState<Lookup<FileIndices>>({ state: "idle" })
  const find = async () => {
    setLookup({ state: "busy" })
    try {
      const answer = await findFileIndices({
        folder: folder.trim(),
        pattern: template || null,
        program: kind === "wire" ? "wire" : "lauego",
      })
      setLookup({ state: "done", answer })
      onFound?.(answer)
    } catch (error) {
      setLookup({ state: "failed", message: messageOf(error) })
    }
  }
  const needsTemplate = kind !== "wire" && template === ""
  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      <div className="flex flex-col gap-4 md:flex-row md:items-start">
        {fields.map((field) => (
          <RangeField
            key={field.name}
            field={field}
            control={control}
            issues={issuesOf(field.name)}
            className="min-w-0 md:flex-1"
          />
        ))}
        <div className="flex shrink-0 flex-col gap-1.5 md:pt-[1.375rem]">
          <FindButton
            busy={lookup.state === "busy"}
            disabled={findDisabled(folder) || needsTemplate}
            title="Read the indices of the matching files"
            onClick={() => {
              void find()
            }}
          >
            Find Indices
          </FindButton>
        </div>
      </div>
      {lookup.state === "failed" && (
        <p role="alert" className="text-sm text-destructive">
          {lookup.message}
        </p>
      )}
      {lookup.state === "done" && (
        <p role="status" className="text-sm text-muted-foreground tabular-nums">
          {formatCount(lookup.answer.files)}{" "}
          {lookup.answer.files === 1 ? "file" : "files"}
          {lookup.answer.truncated && " (truncated)"}
        </p>
      )}
    </div>
  )
}
