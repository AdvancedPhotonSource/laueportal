import type { ReactNode } from "react"
import { Field, FieldLabel } from "@/components/ui/field"
import { cn } from "@/lib/utils"
import type { FormIssue } from "./issues"

/** A field's issues under it: an error in red, a warning in amber. */
export function IssueMessages({
  id,
  issues,
}: {
  id: string
  issues: readonly FormIssue[] | undefined
}) {
  if (!issues?.length) return null
  return (
    <ul id={id} className="flex flex-col gap-0.5 text-sm">
      {issues.map((issue, index) => (
        <li
          key={index}
          data-severity={issue.severity}
          className={
            issue.severity === "error" ? "text-destructive" : "text-warning"
          }
        >
          {issue.message}
        </li>
      ))}
    </ul>
  )
}

/**
 * A form field's frame (shadcn Field): its label, whose tooltip is the hint, the control, and the
 * field's issues. `invalid` when an issue is an error (the control's red ring and the label's colour).
 */
export function FieldShell({
  id,
  label,
  hint,
  issues,
  className,
  children,
}: {
  /** The control's ID (the label points at it); the issues are `${id}-issues`. */
  id: string
  label: ReactNode
  hint?: string
  issues?: readonly FormIssue[]
  className?: string
  children: ReactNode
}) {
  return (
    <Field
      data-invalid={hasError(issues) || undefined}
      className={cn("gap-1.5", className)}
    >
      <FieldLabel htmlFor={id} title={hint || undefined}>
        {label}
      </FieldLabel>
      {children}
      <IssueMessages id={`${id}-issues`} issues={issues} />
    </Field>
  )
}

function hasError(issues: readonly FormIssue[] | undefined): boolean {
  return issues?.some((issue) => issue.severity === "error") ?? false
}
