// The viewer's sidebar controls, shared by the tabs.
import type { ReactNode } from "react"

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { subheading } from "@/lib/styles"

export interface Option<T extends string> {
  value: T
  label: string
  /** Why it cannot be chosen; set for a disabled option. */
  reason?: string | null
}

/** A labelled select of string values. */
export function Choice<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: T
  options: readonly Option<T>[]
  onChange: (value: T) => void
}) {
  return (
    <Control label={label}>
      <Select
        items={Object.fromEntries(options.map((o) => [o.value, o.label]))}
        value={value}
        onValueChange={(next) => {
          const option = options.find((o) => o.value === next)
          if (option) onChange(option.value)
        }}
      >
        <SelectTrigger size="sm" aria-label={label} className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent className="w-auto">
          {options.map((option) => (
            <SelectItem
              key={option.value}
              value={option.value}
              disabled={Boolean(option.reason)}
            >
              {option.label}
              {option.reason && (
                <span className="text-muted-foreground">
                  {" "}
                  ({option.reason})
                </span>
              )}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </Control>
  )
}

export function Control({
  label,
  children,
}: {
  label: string
  children: ReactNode
}) {
  return (
    <div className="grid grid-cols-[5.5rem_minmax(0,1fr)] items-center gap-2 text-sm">
      <span className="text-muted-foreground">{label}</span>
      {children}
    </div>
  )
}

export function Section({
  title,
  children,
}: {
  title: string
  children: ReactNode
}) {
  return (
    <section aria-label={title} className="flex flex-col gap-2">
      <h3 className={subheading}>{title}</h3>
      {children}
    </section>
  )
}

export function Note({ children }: { children: ReactNode }) {
  return <p className="text-xs text-muted-foreground">{children}</p>
}
