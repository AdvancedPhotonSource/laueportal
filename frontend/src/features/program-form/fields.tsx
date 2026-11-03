// The form kit's field components: shadcn Field with React Hook Form's Controller, labelled, limited and
// hinted from GET /programs (program-spec.ts). The form holds text and booleans (form-values.ts); what is
// sent is the server's to check.
import { type ComponentProps, type ReactNode, useId } from "react"
import { type Control, useController } from "react-hook-form"
import { PathField, type PickerMode } from "@/components/PathPicker"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldLabel, FieldTitle } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { formatCount } from "@/lib/format"
import { cn } from "@/lib/utils"
import { FieldShell, IssueMessages } from "./FieldShell"
import { type FormValues, outputParts, withOutputFolder } from "./form-values"
import type { FormIssue } from "./issues"
import { fieldHint, type FieldSpec } from "./program-spec"
import { srangeCount } from "./srange"

export interface KitFieldProps {
  field: FieldSpec
  control: Control<FormValues>
  issues?: readonly FormIssue[]
  className?: string
}

function isError(issues: readonly FormIssue[] | undefined): boolean {
  return issues?.some((issue) => issue.severity === "error") ?? false
}

function textOf(value: unknown): string {
  return typeof value === "string" ? value : ""
}

/** A text input with the field's unit at its end. */
function UnitInput({
  unit,
  className,
  ...props
}: ComponentProps<typeof Input> & { unit?: string }) {
  if (!unit) return <Input className={className} {...props} />
  return (
    <div className={cn("relative", className)}>
      <Input className="pr-12" {...props} />
      <span className="pointer-events-none absolute inset-y-0 right-2.5 flex items-center text-sm text-muted-foreground">
        {unit}
      </span>
    </div>
  )
}

/** A number with its unit (an integer or a decimal, typed as text: the server checks it). */
export function NumberField({
  field,
  control,
  issues,
  className,
  placeholder,
}: KitFieldProps & { placeholder?: string }) {
  const id = useId()
  const {
    field: { name, ref, value, onChange, onBlur },
  } = useController({ name: field.name, control })
  return (
    <FieldShell
      id={id}
      label={field.title}
      hint={fieldHint(field)}
      issues={issues}
      className={className}
    >
      <UnitInput
        id={id}
        name={name}
        ref={ref}
        value={textOf(value)}
        onChange={(event) => {
          onChange(event.target.value)
        }}
        onBlur={onBlur}
        inputMode={field.type === "integer" ? "numeric" : "decimal"}
        autoComplete="off"
        unit={field.unit}
        placeholder={placeholder ?? field.placeholder}
        aria-invalid={isError(issues) || undefined}
        aria-describedby={issues?.length ? `${id}-issues` : undefined}
        className="tabular-nums"
      />
    </FieldShell>
  )
}

/** A number left empty for "automatic" (a nullable number: blank is sent as null). */
export function OptionalNumberField(props: KitFieldProps) {
  return (
    <NumberField
      {...props}
      placeholder={props.field.placeholder ?? "Automatic"}
    />
  )
}

/** A text (or, `multiline`, a text area), with the field's unit when it has one. */
export function TextField({
  field,
  control,
  issues,
  className,
  multiline = false,
  mono = false,
}: KitFieldProps & { multiline?: boolean; mono?: boolean }) {
  const id = useId()
  const {
    field: { name, ref, value, onChange, onBlur },
  } = useController({ name: field.name, control })
  const common = {
    id,
    name: name,
    value: textOf(value),
    onBlur: onBlur,
    placeholder: field.placeholder,
    autoComplete: "off",
    spellCheck: false,
    "aria-invalid": isError(issues) || undefined,
    "aria-describedby": issues?.length ? `${id}-issues` : undefined,
  }
  return (
    <FieldShell
      id={id}
      label={field.title}
      hint={fieldHint(field)}
      issues={issues}
      className={className}
    >
      {multiline ? (
        <Textarea
          {...common}
          ref={ref}
          rows={3}
          onChange={(event) => {
            onChange(event.target.value)
          }}
        />
      ) : (
        <UnitInput
          {...common}
          ref={ref}
          unit={field.unit}
          className={cn(mono && "font-mono")}
          onChange={(event) => {
            onChange(event.target.value)
          }}
        />
      )}
    </FieldShell>
  )
}

/** The count a range holds, beside it: "11 points"; nothing while blank or not a range. */
function RangeCount({ text }: { text: string }) {
  const count = srangeCount(text)
  if (!count) return null
  return (
    <span className="shrink-0 text-sm text-muted-foreground tabular-nums">
      {formatCount(count)} {count === 1 ? "point" : "points"}
    </span>
  )
}

/** An integer range in the srange syntax (`1-10,15,20-30:2`), with the count it holds. */
export function RangeField({
  field,
  control,
  issues,
  className,
}: KitFieldProps) {
  const id = useId()
  const {
    field: { name, ref, value, onChange, onBlur },
  } = useController({ name: field.name, control })
  const text = textOf(value)
  return (
    <FieldShell
      id={id}
      label={field.title}
      hint={fieldHint(field)}
      issues={issues}
      className={className}
    >
      <div className="flex items-center gap-2">
        <Input
          id={id}
          name={name}
          ref={ref}
          value={text}
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
        <RangeCount text={text} />
      </div>
    </FieldShell>
  )
}

function HklInput({
  field,
  control,
  issues,
}: {
  field: FieldSpec
  control: Control<FormValues>
  issues: readonly FormIssue[] | undefined
}) {
  const {
    field: { name, ref, value, onChange, onBlur },
  } = useController({ name: field.name, control })
  const letter = field.title.split(":").at(-1)?.trim() ?? field.name
  return (
    <Input
      name={name}
      ref={ref}
      value={textOf(value)}
      onChange={(event) => {
        onChange(event.target.value)
      }}
      onBlur={onBlur}
      inputMode="numeric"
      autoComplete="off"
      aria-label={letter}
      title={fieldHint(field) || undefined}
      aria-invalid={isError(issues) || undefined}
      className="tabular-nums"
    />
  )
}

/** The label of the HKL fields' group: their titles' common part ("Central HKL: H" → "Central HKL"). */
function hklLabel(fields: readonly FieldSpec[]): string {
  return fields[0]?.title.split(":")[0]?.trim() ?? "HKL"
}

/** HKL: three integers (three fields of the program, e.g. index_h, index_k, index_l) in one row. */
export function HklField({
  fields,
  control,
  issuesOf,
  className,
}: {
  fields: readonly FieldSpec[]
  control: Control<FormValues>
  issuesOf: (name: string) => readonly FormIssue[] | undefined
  className?: string
}) {
  const id = useId()
  const issues = fields.flatMap((field) => issuesOf(field.name) ?? [])
  return (
    <Field
      aria-labelledby={`${id}-label`}
      data-invalid={isError(issues) || undefined}
      className={cn("gap-1.5", className)}
    >
      <FieldTitle
        id={`${id}-label`}
        title={(fields[0] && fieldHint(fields[0])) || undefined}
      >
        {hklLabel(fields)}
      </FieldTitle>
      <div className="grid grid-cols-3 gap-2">
        {fields.map((field) => (
          <HklInput
            key={field.name}
            field={field}
            control={control}
            issues={issuesOf(field.name)}
          />
        ))}
      </div>
      <IssueMessages id={`${id}-issues`} issues={issues} />
    </Field>
  )
}

/** A choice among the field's `x-options`, in their order. */
export function SelectField({
  field,
  control,
  issues,
  className,
  orientation,
  onPick,
}: KitFieldProps & {
  /** "horizontal": the label beside the control (the create page's header). */
  orientation?: "vertical" | "horizontal"
  /** The user picked an option (its value already set); a value set from outside is no pick. */
  onPick?: (value: string) => void
}) {
  const id = useId()
  const {
    field: { ref, value, onChange, onBlur },
  } = useController({ name: field.name, control })
  const options = field.options ?? []
  const labels = Object.fromEntries(
    options.map((option) => [option.value, option.label])
  )
  return (
    <FieldShell
      id={id}
      label={field.title}
      hint={fieldHint(field)}
      issues={issues}
      className={className}
      orientation={orientation}
    >
      <Select
        items={labels}
        value={textOf(value)}
        onValueChange={(next) => {
          onChange(next ?? "")
          onPick?.(next ?? "")
        }}
      >
        <SelectTrigger
          id={id}
          ref={ref}
          onBlur={onBlur}
          aria-invalid={isError(issues) || undefined}
          aria-describedby={issues?.length ? `${id}-issues` : undefined}
          className="w-full"
        >
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {options.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </FieldShell>
  )
}

/** A yes/no field, as a checkbox with its label beside it. */
export function ToggleField({
  field,
  control,
  issues,
  className,
  disabled = false,
}: KitFieldProps & {
  /** Base UI's checkbox is not a native control: a disabled fieldset around it does not reach it. */
  disabled?: boolean
}) {
  const id = useId()
  const {
    field: { name, ref, value, onChange, onBlur },
  } = useController({ name: field.name, control })
  return (
    <Field
      orientation="horizontal"
      data-invalid={isError(issues) || undefined}
      className={cn("flex-wrap gap-x-2 gap-y-1 self-end", className)}
    >
      <Checkbox
        id={id}
        name={name}
        inputRef={ref}
        checked={value === true}
        onCheckedChange={(checked) => {
          onChange(checked)
        }}
        onBlur={onBlur}
        disabled={disabled}
        aria-invalid={isError(issues) || undefined}
      />
      <FieldLabel htmlFor={id} title={fieldHint(field) || undefined}>
        {field.title}
      </FieldLabel>
      <IssueMessages id={`${id}-issues`} issues={issues} />
    </Field>
  )
}

/**
 * A path on the server, typed or chosen in the roots browser: a folder, a file (of the field's
 * `x-extensions`), or an output template's folder (`template`: Browse picks the folder and keeps the
 * template's part from its `%d` on, the run ID's place).
 */
export function PathPickerField({
  field,
  control,
  issues,
  className,
  mode,
  template = false,
  actions,
}: KitFieldProps & {
  mode: PickerMode
  template?: boolean
  /** More controls after Browse, in its row (`PathField`'s). */
  actions?: ReactNode
}) {
  const id = useId()
  const {
    field: { name, ref, value, onChange, onBlur },
  } = useController({ name: field.name, control })
  const parts = template ? outputParts(textOf(value)) : undefined
  return (
    <FieldShell
      id={id}
      label={field.title}
      hint={fieldHint(field)}
      issues={issues}
      className={className}
    >
      <PathField
        id={id}
        name={name}
        value={textOf(value)}
        onChange={onChange}
        onBlur={onBlur}
        inputRef={ref}
        mode={mode}
        extensions={mode === "file" ? field.extensions : undefined}
        browsePath={parts?.folder}
        fromBrowse={parts && ((folder) => withOutputFolder(folder, parts.tail))}
        invalid={isError(issues)}
        describedBy={issues?.length ? `${id}-issues` : undefined}
        placeholder={field.placeholder}
        dialogTitle={`Choose the ${field.title}`}
        actions={actions}
      />
    </FieldShell>
  )
}
