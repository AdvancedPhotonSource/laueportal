// The source pickers: a search-as-you-type combobox over scans, reconstructions or indexing runs
// (source-search.ts), each match with its ID, sample (a run's: its scan's) and date. One source per
// picker: choosing another replaces it. A value set from outside (a prefill, a reset, the URL) shows
// as its ID.
import { useQuery } from "@tanstack/react-query"
import { type Ref, useEffect, useId, useMemo, useState } from "react"
import { type Control, useController } from "react-hook-form"
import type { ProgramKind } from "@/api"
import {
  Combobox,
  ComboboxContent,
  ComboboxEmpty,
  ComboboxInput,
  ComboboxItem,
  ComboboxList,
} from "@/components/ui/combobox"
import { formatDateTime } from "@/lib/format"
import { FieldShell } from "./FieldShell"
import type { FormValues } from "./form-values"
import type { FormIssue } from "./issues"
import { fieldHint, type FieldSpec } from "./program-spec"
import {
  CLONE_SOURCE,
  SOURCE_SEARCH_DELAY_MS,
  searchSources,
  sourceLabel,
  type SourceKind,
  type SourceOption,
} from "./source-search"

/** The prompt when empty: the label names the kind (and a third of a card's width at 900 px fits it). */
const PLACEHOLDER = "ID or sample"

/** A source picker: `value` is the chosen ID (null for none); `onChange` gives the new one and its match. */
export function SourcePicker({
  kind,
  method,
  value,
  onChange,
  id,
  inputRef,
  onBlur,
  invalid,
  describedBy,
}: {
  kind: SourceKind
  /** Only the indexing runs of this method. */
  method?: string
  value: number | null
  onChange: (id: number | null, option: SourceOption | null) => void
  id?: string
  inputRef?: Ref<HTMLInputElement>
  onBlur?: () => void
  invalid?: boolean
  describedBy?: string
}) {
  const [open, setOpen] = useState(false)
  const labelOf = (chosen: number | null) =>
    chosen === null ? "" : sourceLabel(kind, chosen)
  const [text, setText] = useState(() => labelOf(value))
  // The value the text was last set from: another one (a pick, or a prefill, a reset) shows its ID; what
  // the user types stays until then.
  const [shownValue, setShownValue] = useState(value)
  if (shownValue !== value) {
    setShownValue(value)
    setText(labelOf(value))
  }
  const [query, setQuery] = useState("")
  useEffect(() => {
    const timer = setTimeout(() => {
      setQuery(text)
    }, SOURCE_SEARCH_DELAY_MS)
    return () => {
      clearTimeout(timer)
    }
  }, [text])

  // While a match's label is shown (the chosen one), the list shows the latest sources.
  const typed =
    value !== null && query === sourceLabel(kind, value) ? "" : query
  const matches = useQuery({
    queryKey: ["program-form", "sources", kind, typed, method],
    queryFn: ({ signal }) => searchSources(kind, typed, signal, method),
    enabled: open,
    staleTime: 10_000,
  })

  const selected = useMemo<SourceOption | null>(
    () =>
      value === null
        ? null
        : (matches.data?.find((option) => option.id === value) ?? {
            id: value,
            label: sourceLabel(kind, value),
            detail: null,
            date: "",
          }),
    [kind, value, matches.data]
  )

  return (
    <Combobox<SourceOption>
      items={matches.data ?? []}
      filter={null}
      value={selected}
      onValueChange={(option) => {
        onChange(option?.id ?? null, option)
      }}
      inputValue={text}
      onInputValueChange={setText}
      open={open}
      onOpenChange={setOpen}
      itemToStringLabel={(option) => option.label}
      isItemEqualToValue={(a, b) => a.id === b.id}
    >
      <ComboboxInput
        id={id}
        ref={inputRef}
        onBlur={onBlur}
        className="w-full"
        placeholder={PLACEHOLDER}
        showClear={value !== null}
        aria-invalid={invalid || undefined}
        aria-describedby={describedBy}
      />
      <ComboboxContent className="w-max min-w-(--anchor-width)">
        <ComboboxEmpty>
          {matches.isError
            ? "Search failed"
            : matches.isPending
              ? "Searching…"
              : "No matches"}
        </ComboboxEmpty>
        <ComboboxList>
          {(option: SourceOption) => (
            <ComboboxItem key={option.id} value={option}>
              <span className="font-medium tabular-nums">{option.label}</span>
              <span className="min-w-0 truncate text-muted-foreground">
                {option.detail}
              </span>
              <span className="ml-auto shrink-0 text-muted-foreground tabular-nums">
                {formatDateTime(option.date)}
              </span>
            </ComboboxItem>
          )}
        </ComboboxList>
      </ComboboxContent>
    </Combobox>
  )
}

/**
 * A form field holding a source's ID (the scan's number, the reconstruction's ID), chosen with a source
 * picker. `onSourceChange` hears each choice (the page's prefill).
 */
export function SourceField({
  field,
  control,
  issues,
  kind,
  onSourceChange,
  className,
}: {
  field: FieldSpec
  control: Control<FormValues>
  issues?: readonly FormIssue[]
  kind: SourceKind
  onSourceChange?: (id: number | null, option: SourceOption | null) => void
  className?: string
}) {
  const id = useId()
  const {
    field: { ref, value, onChange, onBlur },
  } = useController({ name: field.name, control })
  const text = typeof value === "string" ? value.trim() : ""
  const chosen = /^\d+$/.test(text) ? Number(text) : null
  return (
    <FieldShell
      id={id}
      label={field.title}
      hint={fieldHint(field)}
      issues={issues}
      className={className}
    >
      <SourcePicker
        id={id}
        kind={kind}
        value={chosen}
        inputRef={ref}
        onBlur={onBlur}
        invalid={issues?.some((issue) => issue.severity === "error")}
        describedBy={issues?.length ? `${id}-issues` : undefined}
        onChange={(next, option) => {
          onChange(next === null ? "" : String(next))
          onSourceChange?.(next, option)
        }}
      />
    </FieldShell>
  )
}

/**
 * The run a create form clones, labelled: an indexing run on the LaueGo form, a reconstruction on the wire
 * form (the prefill's `indexing` or `reconstruction`). `onChange` gives the chosen ID (the page prefills).
 */
export function CloneField({
  program,
  value,
  onChange,
  className,
}: {
  program: ProgramKind
  value: number | null
  onChange: (id: number | null, option: SourceOption | null) => void
  className?: string
}) {
  const id = useId()
  return (
    <FieldShell
      id={id}
      label="Clone of"
      hint="The run whose parameters the form copies."
      className={className}
    >
      <SourcePicker
        id={id}
        kind={CLONE_SOURCE[program]}
        method={program === "wire" ? undefined : program}
        value={value}
        onChange={onChange}
      />
    </FieldShell>
  )
}
