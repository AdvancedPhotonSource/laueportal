import { useState, type ComponentProps } from "react"

import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"

const NUMBER = /^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/

/** A typed number, or null for an empty field; undefined when the text is not a number. */
function parse(text: string): number | null | undefined {
  const trimmed = text.trim()
  if (trimmed === "") return null
  return NUMBER.test(trimmed) ? Number(trimmed) : undefined
}

const show = (value: number | null) => (value === null ? "" : String(value))

/**
 * A number that applies on Enter or on leaving the field, never per keystroke (it may change a
 * product request). A value `isValid` refuses is marked invalid while typed and restored on leaving;
 * Escape restores it at once. `value` null is an empty field.
 */
export function NumberField({
  value,
  onCommit,
  onClear,
  isValid = Number.isFinite,
  className,
  ...props
}: {
  value: number | null
  onCommit: (value: number) => void
  /** Called when the field is emptied and applied; without it an emptied field restores its value. */
  onClear?: () => void
  isValid?: (value: number) => boolean
} & Omit<
  ComponentProps<typeof Input>,
  "value" | "onChange" | "type" | "onBlur" | "onKeyDown"
>) {
  const [draft, setDraft] = useState(show(value))
  // Another value arrived (a reset, a link): the field shows it.
  const [shown, setShown] = useState(value)
  if (shown !== value) {
    setShown(value)
    setDraft(show(value))
  }
  const parsed = parse(draft)
  const valid =
    parsed === null
      ? value === null || onClear !== undefined
      : parsed !== undefined && isValid(parsed)

  const commit = () => {
    if (parsed === null && value !== null && onClear) {
      onClear()
    } else if (parsed !== null && parsed !== undefined && isValid(parsed)) {
      if (parsed !== value) onCommit(parsed)
    } else {
      setDraft(show(value))
    }
  }

  return (
    <Input
      type="text"
      inputMode="decimal"
      value={draft}
      aria-invalid={!valid || undefined}
      className={cn("h-7 px-2 font-mono text-xs tabular-nums", className)}
      onChange={(event) => {
        setDraft(event.target.value)
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter") commit()
        if (event.key === "Escape") setDraft(show(value))
      }}
      onBlur={commit}
      {...props}
    />
  )
}

/**
 * A 3×3 matrix by rows (a compact grid: the custom surface's tilt, roll and normal; G_ref's a*,
 * b*, c*). Each entry applies on Enter or on leaving it; a null entry is an empty field.
 */
export function MatrixField({
  label,
  rows,
  columns,
  values,
  onChange,
  title,
}: {
  label: string
  rows: readonly [string, string, string]
  columns: readonly [string, string, string]
  values: readonly (number | null)[]
  onChange: (values: (number | null)[]) => void
  /** The matrix's tooltip (its units, say). */
  title?: string
}) {
  return (
    <div
      role="group"
      aria-label={label}
      title={title}
      className="grid grid-cols-[auto_1fr_1fr_1fr] items-center gap-1"
    >
      {rows.map((row, r) => (
        <div key={row} className="contents">
          <span className="pr-1 text-xs text-muted-foreground">{row}</span>
          {columns.map((column, c) => (
            <NumberField
              key={column}
              aria-label={`${label} ${row} ${column}`}
              value={values[3 * r + c] ?? null}
              onCommit={(value) => {
                const next = [...values]
                next[3 * r + c] = value
                onChange(next)
              }}
            />
          ))}
        </div>
      ))}
    </div>
  )
}
