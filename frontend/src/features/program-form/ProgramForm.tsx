// The program form scaffold: a page passes the program kind (and the prefill's values) and gets the
// sections from GET /programs' `x-layout`, each field drawn by its widget, the live validation and its
// summary, the "Run on" choice with Validate and Submit, and the submission's answer. A new prefill
// (another source) replaces the fields it derives from the source and keeps the ones the user changed; a
// scan's or a reconstruction's fills only its data fields and the source IDs. A run created and queued
// opens its page; one created that could not be queued stays in the Submitted panel.
import { useNavigate } from "@tanstack/react-router"
import { type ReactNode, useCallback, useEffect, useMemo, useRef } from "react"
import {
  type Control,
  useForm,
  useFormState,
  useWatch,
  type UseFormSetValue,
} from "react-hook-form"
import {
  type FileIndices,
  type FilePattern,
  type ProgramKind,
  useListPrograms,
} from "@/api"
import { ConfirmDialog } from "@/components/ConfirmDialog"
import { UnsavedChangesGuard } from "@/components/UnsavedChangesGuard"
import { ErrorState } from "@/components/ErrorState"
import { LoadingState } from "@/components/LoadingState"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardAction,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { cn } from "@/lib/utils"
import { FilenamePatternField, IndicesField } from "./file-helpers"
import {
  HklField,
  NumberField,
  OptionalNumberField,
  PathPickerField,
  SelectField,
  TextField,
  ToggleField,
} from "./fields"
import {
  applyPrefill,
  defaultsOf,
  formValuesOf,
  type FormValues,
  requestOf,
  type RequestValues,
  templatesOf,
} from "./form-values"
import type { FormIssue, PlacedIssues } from "./issues"
import {
  blocksOf,
  type FieldBlock,
  type FieldSpec,
  fieldWithWidget,
  isProgram,
  type ProgramSpec,
  programSpec,
  sectionAnchor,
  type SectionSpec,
  SOURCE_ID_WIDGETS,
  sourceIdsOf,
} from "./program-spec"
import { sectionIdentity } from "./section-identity"
import { SectionIcon } from "./SectionIcon"
import type { SourceOption } from "./source-search"
import { SourceField } from "./SourcePicker"
import { SubmitAnswer, SubmitBar, ValidationSummary } from "./submission-views"
import { useRunSubmission } from "./use-run-submission"

/** A source chosen in the form (its scan or reconstruction field): the page's prefill hears it. */
export type SourceChange = (
  field: string,
  id: number | null,
  option: SourceOption | null
) => void

/** What the page's source block gets: the form, its values and their issues. */
export interface SourceSlot {
  control: Control<FormValues>
  values: FormValues
  issuesOf: (name: string) => readonly FormIssue[] | undefined
}

/** The fields a new prefill fills besides the source IDs; undefined: every field (a clone). */
export type PrefillFills = readonly string[] | undefined

export interface ProgramFormProps {
  kind: ProgramKind
  /**
   * The prefill's `values` (GET /programs/{kind}/prefill); the configured defaults without. A new object
   * applies a new prefill (`applyPrefill`): keep it stable otherwise.
   */
  values?: RequestValues
  /** What `values` fills (default: every field). */
  fills?: PrefillFills
  onSourceChange?: SourceChange
  /**
   * The page's source block, drawn instead of the source ID fields (the scan, the reconstruction) in their
   * section: the create page's Source switch. Without it, each is a source picker.
   */
  source?: (slot: SourceSlot) => ReactNode
}

/** The program's form: loads GET /programs, then shows its sections. */
export function ProgramForm({
  kind,
  values,
  fills,
  onSourceChange,
  source,
}: ProgramFormProps) {
  const programs = useListPrograms()
  const spec = useMemo(() => {
    const program = programs.data?.programs.find(isProgram(kind))
    return program ? programSpec(program) : undefined
  }, [programs.data, kind])
  if (programs.isPending) return <LoadingState variant="cards" count={3} />
  if (programs.isError || !spec) {
    return (
      <ErrorState
        error={programs.error ?? new Error(`No ${kind} program`)}
        onRetry={() => void programs.refetch()}
        retrying={programs.isFetching}
      />
    )
  }
  return (
    <ProgramFormBody
      spec={spec}
      values={values}
      fills={fills}
      onSourceChange={onSourceChange}
      source={source}
    />
  )
}

/** The form for a program read already (ProgramForm loads it; tests pass it). */
export function ProgramFormBody({
  spec,
  values,
  fills,
  onSourceChange,
  source,
}: {
  spec: ProgramSpec<ProgramKind>
  values?: RequestValues
  fills?: PrefillFills
  onSourceChange?: SourceChange
  source?: (slot: SourceSlot) => ReactNode
}) {
  const initial = useMemo(() => formValuesOf(spec, values), [spec, values])
  const form = useForm<FormValues>({ defaultValues: initial })
  // A new prefill: the fields it fills from its source replace the form's, and what the user changed
  // otherwise stays (a scan's or a reconstruction's: every field outside `fills` stays).
  const seen = useRef(initial)
  const applied = useRef(initial)
  useEffect(() => {
    if (seen.current === initial) return
    seen.current = initial
    const ids = sourceIdsOf(spec)
    const { values: baseline, kept } = applyPrefill(
      formValuesOf(spec),
      applied.current,
      initial,
      form.getValues(),
      { ids, fields: fills && new Set([...ids, ...fills]) }
    )
    applied.current = baseline
    form.reset(baseline)
    for (const [name, value] of Object.entries(kept)) {
      form.setValue(name, value, { shouldDirty: true, shouldTouch: true })
    }
  }, [initial, spec, form, fills])
  const watched = useWatch({ control: form.control })
  const body = useMemo(
    () => requestOf(spec, { ...initial, ...watched } as FormValues),
    [spec, initial, watched]
  )
  // A field's issues show once it is changed or left (a prefilled value is neither), or after Submit.
  const { dirtyFields, touchedFields } = useFormState({ control: form.control })
  // React Hook Form updates these objects in place: the names, as one text, say when they change.
  const touchedNames = [
    ...new Set(
      [dirtyFields, touchedFields].flatMap((fields) =>
        Object.entries(fields).flatMap(([name, on]) => (on ? [name] : []))
      )
    ),
  ]
    .sort()
    .join("\n")
  const touched = useMemo(
    () => new Set(touchedNames ? touchedNames.split("\n") : []),
    [touchedNames]
  )
  const order = useMemo(
    () => [
      ...spec.sections.flatMap((section) =>
        section.fields.map((field) => field.name)
      ),
      ...(spec.target ? [spec.target.name] : []),
    ],
    [spec]
  )
  const { setFocus } = form
  const onBlocked = useCallback(
    (found: PlacedIssues) => {
      const first = order.find((name) =>
        found.byField[name]?.some((issue) => issue.severity === "error")
      )
      if (first !== undefined) setFocus(first)
    },
    [order, setFocus]
  )
  const submission = useRunSubmission(spec.kind, spec, body, {
    touched,
    onBlocked,
  })
  // Edits not yet a run: leaving the page asks first (a created run's values are kept by the run).
  const unsaved =
    Object.values(dirtyFields).some(Boolean) &&
    submission.submitState.status !== "created"
  // A run created and queued (a 201 without `failure`) opens its page, as its RunLink does; after the
  // render that ended the unsaved state, so the guard lets it go. A `failure` keeps the panel.
  const navigate = useNavigate()
  const { submitState } = submission
  useEffect(() => {
    if (submitState.status !== "created" || submitState.created.failure) return
    const { run } = submitState.created
    void (run.kind === "indexing"
      ? navigate({
          to: "/indexing/$indexingId",
          params: { indexingId: String(run.id) },
        })
      : navigate({
          to: "/reconstructions/$reconstructionId",
          params: { reconstructionId: String(run.id) },
        }))
  }, [submitState, navigate])
  const issuesOf = useCallback(
    (name: string) => submission.shown.byField[name],
    [submission.shown]
  )
  const targetLabels = useMemo(
    () =>
      Object.fromEntries(
        (spec.target?.options ?? []).map((option) => [
          option.value,
          option.label,
        ])
      ),
    [spec.target]
  )
  const titleOf = (name: string | null) =>
    name === null ? undefined : spec.fields[name]?.title

  return (
    <form
      noValidate
      className="flex flex-col gap-4"
      onSubmit={(event) => {
        event.preventDefault()
        void submission.requestSubmit()
      }}
    >
      {spec.sections.map((section) => (
        <ProgramSection
          key={section.group}
          spec={spec}
          section={section}
          control={form.control}
          setValue={form.setValue}
          watched={{ ...initial, ...watched } as FormValues}
          issuesOf={issuesOf}
          onSourceChange={onSourceChange}
          source={source}
        />
      ))}
      <ValidationSummary
        issues={submission.issues}
        shown={submission.shown}
        checking={submission.checking}
        failed={submission.checkFailed}
        titleOf={titleOf}
        onFocusField={(name) => {
          form.setFocus(name)
        }}
      />
      <SubmitBar
        target={spec.target}
        control={form.control}
        issuesOf={issuesOf}
        submission={submission}
      />
      <UnsavedChangesGuard when={unsaved} />
      <SubmitAnswer
        state={submission.submitState}
        targetLabels={targetLabels}
        onNewSession={submission.newSession}
      />
      <ConfirmDialog
        open={submission.confirming}
        onOpenChange={(open) => {
          if (!open) submission.cancelConfirm()
        }}
        title="Submit with warnings?"
        description={`The run is created despite ${String(submission.issues.warnings)} ${submission.issues.warnings === 1 ? "warning" : "warnings"}.`}
        confirmLabel="Submit"
        onConfirm={submission.confirm}
      >
        <ul className="flex list-disc flex-col gap-1 pl-5 text-sm text-warning">
          {submission.issues.all
            .filter((issue) => issue.severity === "warning")
            .map((issue, index) => (
              <li key={index}>{issueLine(issue, titleOf(issue.field))}</li>
            ))}
        </ul>
      </ConfirmDialog>
    </form>
  )
}

function issueLine(issue: FormIssue, title: string | undefined): string {
  return title ? `${title}: ${issue.message}` : issue.message
}

/** The widgets drawn across the section's whole width. */
const WIDE = new Set([
  "folder",
  "file",
  "output-folder",
  "filename-templates",
  "textarea",
])

function blockKey(block: FieldBlock): string {
  return block.kind === "field"
    ? block.field.name
    : (block.fields[0]?.name ?? block.kind)
}

function ProgramSection({
  spec,
  section,
  control,
  setValue,
  watched,
  issuesOf,
  onSourceChange,
  source,
}: {
  spec: ProgramSpec<ProgramKind>
  section: SectionSpec
  control: Control<FormValues>
  setValue: UseFormSetValue<FormValues>
  watched: FormValues
  issuesOf: (name: string) => readonly FormIssue[] | undefined
  onSourceChange?: SourceChange
  source?: (slot: SourceSlot) => ReactNode
}) {
  const set = (name: string, value: string) => {
    if (name in spec.fields) setValue(name, value, { shouldDirty: true })
  }
  // The page's source block stands for the section's source ID fields.
  const sourceBlock =
    source !== undefined &&
    section.fields.some((field) => SOURCE_ID_WIDGETS.has(field.widget))
  const fields = sourceBlock
    ? section.fields.filter((field) => !SOURCE_ID_WIDGETS.has(field.widget))
    : section.fields
  const restore = () => {
    for (const [name, value] of Object.entries(defaultsOf(section.fields))) {
      setValue(name, value, { shouldDirty: true })
    }
  }
  const folder = fieldWithWidget(spec, "folder")
  const templates = fieldWithWidget(spec, "filename-templates")
  const folderValue = folder ? String(watched[folder.name] ?? "") : ""
  const firstTemplate = templates
    ? (templatesOf(String(watched[templates.name] ?? ""))[0] ?? "")
    : ""
  // A suggestion or Find Indices fills the index ranges (a missing depth range: none).
  const fillIndices = (
    found: Pick<FilePattern, "scan_points" | "depth_range">
  ) => {
    if (found.scan_points !== null) set("scan_points", found.scan_points)
    set("depth_range", found.depth_range ?? "")
  }

  const render = (block: FieldBlock) => {
    if (block.kind === "hkl") {
      return (
        <HklField fields={block.fields} control={control} issuesOf={issuesOf} />
      )
    }
    if (block.kind === "indices") {
      return (
        <IndicesField
          fields={block.fields}
          control={control}
          issuesOf={issuesOf}
          kind={spec.kind}
          folder={folderValue}
          template={firstTemplate}
          onFound={(found: FileIndices) => {
            fillIndices(found)
          }}
        />
      )
    }
    const { field } = block
    const props = { field, control, issues: issuesOf(field.name) }
    return renderField(props, {
      kind: spec.kind,
      folder: folderValue,
      onPick: fillIndices,
      onSourceChange,
    })
  }

  return (
    // Below the sticky navbar (h-12) when the page's sidebar scrolls to it, and focused (tabIndex -1).
    <Card
      id={sectionAnchor(section.group)}
      tabIndex={-1}
      className={cn("scroll-mt-16", sectionIdentity(section.group).stripe)}
    >
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <SectionIcon title={section.group} className="size-4.5" />
          {section.group}
        </CardTitle>
        {section.restorable && (
          <CardAction>
            <Button type="button" variant="outline" size="sm" onClick={restore}>
              Restore Default
            </Button>
          </CardAction>
        )}
      </CardHeader>
      <CardContent className="grid items-start gap-4 md:grid-cols-3">
        {sourceBlock && (
          <div className="md:col-span-2">
            {source({ control, values: watched, issuesOf })}
          </div>
        )}
        {blocksOf(fields).map((block) => (
          <div
            key={blockKey(block)}
            className={cn(
              (block.kind === "indices" ||
                (block.kind === "field" && WIDE.has(block.field.widget))) &&
                "md:col-span-3",
              block.kind === "field" &&
                block.field.widget === "checkbox" &&
                "md:self-end"
            )}
          >
            {render(block)}
          </div>
        ))}
      </CardContent>
    </Card>
  )
}

function renderField(
  props: {
    field: FieldSpec
    control: Control<FormValues>
    issues: readonly FormIssue[] | undefined
  },
  context: {
    kind: ProgramKind
    folder: string
    onPick: (pattern: FilePattern) => void
    onSourceChange?: SourceChange
  }
) {
  const { field } = props
  switch (field.widget) {
    case "scan":
    case "reconstruction":
      return (
        <SourceField
          {...props}
          kind={field.widget}
          onSourceChange={(id, option) => {
            context.onSourceChange?.(field.name, id, option)
          }}
        />
      )
    case "folder":
      return <PathPickerField {...props} mode="directory" />
    case "output-folder":
      return <PathPickerField {...props} mode="directory" template />
    case "file":
      return <PathPickerField {...props} mode="file" />
    case "filename-templates":
      return (
        <FilenamePatternField
          {...props}
          kind={context.kind}
          folder={context.folder}
          onPick={context.onPick}
        />
      )
    case "select":
      return <SelectField {...props} />
    case "checkbox":
      return <ToggleField {...props} />
    case "textarea":
      return <TextField {...props} multiline />
    case "integer":
    case "number":
      return field.nullable ? (
        <OptionalNumberField {...props} />
      ) : (
        <NumberField {...props} />
      )
    default:
      return <TextField {...props} />
  }
}
