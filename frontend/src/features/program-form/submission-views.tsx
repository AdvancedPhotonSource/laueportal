// The form's validation summary, its Run on / Validate / Submit bar (the create page's header), a refused
// submission's message, and the submission's answer.
import { LoaderCircleIcon } from "lucide-react"
import type { Control } from "react-hook-form"
import { JobLink, RunLink } from "@/components/RunLinks"
import { RunStatusBadge } from "@/components/RunStatusBadge"
import { Button } from "@/components/ui/button"
import { DefinitionList } from "@/components/DefinitionList"
import { subheading, surface } from "@/lib/styles"
import { cn } from "@/lib/utils"
import { SelectField } from "./fields"
import type { FormValues } from "./form-values"
import { type FormIssue, type PlacedIssues, summaryText } from "./issues"
import type { FieldSpec } from "./program-spec"
import type { RunSubmission } from "./use-run-submission"
import type { SubmitState } from "./use-submit-run"

function plural(count: number, one: string): string {
  return `${String(count)} ${count === 1 ? one : `${one}s`}`
}

/** The counts of the last check (every issue, shown or not): "2 errors · 1 warning", "No issues". */
function issueCounts(issues: PlacedIssues): string {
  if (issues.all.length === 0) return "No issues"
  return [
    issues.errors > 0 && plural(issues.errors, "error"),
    issues.warnings > 0 && plural(issues.warnings, "warning"),
  ]
    .filter(Boolean)
    .join(" · ")
}

/**
 * The last check's counts (every issue) and the issues shown (`shown`: the touched fields', or all once
 * revealed), errors first; a field's issue names the field once and gives it the keyboard when clicked.
 */
export function ValidationSummary({
  issues,
  shown = issues,
  checking,
  failed,
  titleOf,
  onFocusField,
  className,
}: {
  issues: PlacedIssues
  shown?: PlacedIssues
  checking: boolean
  failed: boolean
  titleOf: (field: string | null) => string | undefined
  onFocusField: (field: string) => void
  className?: string
}) {
  return (
    <section
      aria-label="Validation"
      className={cn(surface, "flex flex-col gap-2 p-4", className)}
    >
      <div className="flex items-center gap-2">
        <h2 className={subheading}>Validation</h2>
        <span role="status" className="text-sm text-muted-foreground">
          {failed ? (
            <span className="text-destructive">Validation unavailable</span>
          ) : checking ? (
            "Checking…"
          ) : (
            issueCounts(issues)
          )}
        </span>
      </div>
      {shown.all.length > 0 && (
        <ul className="flex flex-col gap-1 text-sm">
          {shown.all.map((issue, index) => (
            <SummaryLine
              key={index}
              issue={issue}
              title={titleOf(issue.field)}
              onFocusField={onFocusField}
            />
          ))}
        </ul>
      )}
    </section>
  )
}

function SummaryLine({
  issue,
  title,
  onFocusField,
}: {
  issue: FormIssue
  title: string | undefined
  onFocusField: (field: string) => void
}) {
  const colour =
    issue.severity === "error" ? "text-destructive" : "text-warning"
  const { field } = issue
  const text = summaryText(issue, title)
  return (
    <li data-severity={issue.severity} className={colour}>
      {field === null ? (
        text
      ) : (
        <button
          type="button"
          className="text-left underline-offset-4 hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          onClick={() => {
            onFocusField(field)
          }}
        >
          {text}
        </button>
      )}
    </li>
  )
}

/** The "Run on" choice (the deployment's targets), Validate and Submit. */
export function SubmitBar({
  target,
  control,
  issuesOf,
  submission,
}: {
  target: FieldSpec | undefined
  control: Control<FormValues>
  issuesOf: (name: string) => readonly FormIssue[] | undefined
  submission: RunSubmission
}) {
  const submitting = submission.submitState.status === "submitting"
  return (
    <div className="flex flex-wrap items-center justify-end gap-3">
      {target && (
        <SelectField
          field={target}
          control={control}
          issues={issuesOf(target.name)}
          orientation="horizontal"
          className="w-auto *:data-[slot=field-label]:flex-none [&_[data-slot=select-trigger]]:w-36"
        />
      )}
      <Button
        type="button"
        variant="outline"
        onClick={() => {
          void submission.validateNow()
        }}
      >
        Validate
      </Button>
      <Button type="submit" disabled={!submission.canSubmit}>
        {submitting && <LoaderCircleIcon className="animate-spin" />}
        Submit
      </Button>
    </div>
  )
}

/**
 * Why the last submission was refused or failed, while its values are on screen; after a 409 (the key in use),
 * New Submission, beside the run the key created when it did. Nothing otherwise.
 */
export function SubmitMessage({ submission }: { submission: RunSubmission }) {
  if (submission.message === null) return null
  return (
    <div className="flex min-w-0 flex-wrap items-center justify-end gap-3">
      <p role="alert" className="min-w-0 text-sm text-destructive">
        {submission.message}
      </p>
      {submission.existing && (
        <span className="flex items-center gap-3 text-sm">
          <span>
            Run <RunLink run={submission.existing.run} />
          </span>
          <span>
            Job <JobLink id={submission.existing.job_id} />
          </span>
        </span>
      )}
      {submission.conflict && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={submission.newSession}
        >
          New Submission
        </Button>
      )}
    </div>
  )
}

/**
 * The created run's answer: the new run and job (or why it could not be queued), and New Submission. It
 * scrolls into view when it appears (Submit may have been clicked far below it).
 */
export function SubmitAnswer({
  state,
  targetLabels,
  onNewSession,
  className,
}: {
  state: SubmitState
  /** The targets' labels (`local` is "APS"). */
  targetLabels: Record<string, string>
  onNewSession: () => void
  className?: string
}) {
  if (state.status !== "created") return null
  const { created } = state
  return (
    <section
      ref={reveal}
      aria-label="Submitted"
      className={cn(surface, "flex flex-col gap-3 p-4", className)}
    >
      <div className="flex items-center gap-2">
        <h2 className={subheading}>Submitted</h2>
        <RunStatusBadge status={created.status} />
      </div>
      <DefinitionList
        rows={[
          { label: "Run", value: <RunLink run={created.run} /> },
          { label: "Job", value: <JobLink id={created.job_id} /> },
          {
            label: "Run on",
            value: targetLabels[created.target] ?? created.target,
          },
        ]}
      />
      {created.failure && (
        <p role="alert" className="text-sm text-destructive">
          {created.failure}
        </p>
      )}
      <div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={onNewSession}
        >
          New Submission
        </Button>
      </div>
    </section>
  )
}

function reveal(panel: HTMLElement | null) {
  panel?.scrollIntoView({ block: "nearest" })
}
