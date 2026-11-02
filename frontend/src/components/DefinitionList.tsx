import type { ReactNode } from "react"

import { cn } from "@/lib/utils"

/** A row of a DefinitionList: a label and its value. */
export interface Definition {
  label: string
  /** Text (none, "" or "—": a dash, read as "none"), or other content (a badge, a link). */
  value: ReactNode
  /** A path, a file name or a code-like value: monospace. */
  mono?: boolean
}

/**
 * Label and value rows, the app's one definition list: the labels muted and smaller, the values in
 * the foreground in tabular figures, monospace only for paths, file names and code-like values, and
 * a hairline between rows.
 */
export function DefinitionList({
  rows,
  className,
  "aria-label": label,
}: {
  rows: readonly Definition[]
  className?: string
  "aria-label"?: string
}) {
  return (
    <dl
      aria-label={label}
      className={cn(
        "grid h-fit grid-cols-[max-content_minmax(0,1fr)] text-sm [&>div:not(:last-child)>*]:border-b",
        className
      )}
    >
      {rows.map((row, index) => {
        const code = row.mono ?? false
        return (
          <div key={`${row.label}-${String(index)}`} className="contents">
            <dt className="border-border/70 py-1.5 pr-4 text-xs leading-5 text-muted-foreground">
              {row.label}
            </dt>
            <dd
              className={cn(
                "min-w-0 border-border/70 py-1.5 leading-5 whitespace-pre-wrap tabular-nums",
                code && "font-mono text-xs"
              )}
            >
              {isText(row.value) ? (
                <Value text={row.value} mono={code} />
              ) : (
                row.value
              )}
            </dd>
          </div>
        )
      })}
    </dl>
  )
}

function isText(value: ReactNode): value is string | null | undefined {
  return value === null || value === undefined || typeof value === "string"
}

/** A value, or a dash (read as "none") when there is none ("", "—"). */
export function Value({
  text,
  mono = false,
}: {
  text: string | null | undefined
  mono?: boolean
}) {
  if (!text || text === "—") {
    return (
      <span className="text-muted-foreground">
        <span aria-hidden>—</span>
        <span className="sr-only">none</span>
      </span>
    )
  }
  return (
    <span className={mono ? "font-mono wrap-anywhere" : "wrap-break-word"}>
      {text}
    </span>
  )
}
