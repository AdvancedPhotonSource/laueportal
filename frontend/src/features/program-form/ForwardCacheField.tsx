// The LaueMatching form's Forward Cache: a choice among the named caches that exactly fit the form's geometry,
// crystal, energies and spot count, or New… with a name to type. Which of the two it is, the field
// `create_forward_cache`, is set here (`forward-cache-new` draws nothing).
import { useEffect, useId, useRef, useState } from "react"
import { useController, useWatch } from "react-hook-form"

import { useListForwardCaches } from "@/api"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { FieldShell } from "./FieldShell"
import type { KitFieldProps } from "./fields"
import { fieldHint } from "./program-spec"

const NEW = "__new__"
const FLAG = "create_forward_cache"
const DEBOUNCE_MS = 400
const INPUTS = [
  "geometry_file",
  "crystal_file",
  "elo",
  "ehi",
  "max_nr_laue_spots",
] as const

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : ""
}

function positive(value: unknown): number | undefined {
  const number = Number(text(value))
  return text(value) !== "" && Number.isFinite(number) && number > 0
    ? number
    : undefined
}

/** The list's query for the form's values, or undefined until every one is usable. */
function queryOf(values: readonly unknown[]) {
  const [geometry_file, crystal_file] = [text(values[0]), text(values[1])]
  const [elo, ehi, max_nr_laue_spots] = [
    positive(values[2]),
    positive(values[3]),
    positive(values[4]),
  ]
  if (!geometry_file || !crystal_file || !elo || !ehi || !max_nr_laue_spots)
    return undefined
  return { geometry_file, crystal_file, elo, ehi, max_nr_laue_spots }
}

const EMPTY_QUERY = {
  geometry_file: "",
  crystal_file: "",
  elo: 1,
  ehi: 1,
  max_nr_laue_spots: 1,
}

export function ForwardCacheField({
  field,
  control,
  issues,
  className,
}: KitFieldProps) {
  const id = useId()
  const {
    field: { value: nameValue, onChange: onName, onBlur: onNameBlur, ref },
  } = useController({ name: field.name, control })
  const {
    field: { value: flagValue, onChange: onFlag },
  } = useController({ name: FLAG, control })
  const creating = flagValue === true
  const current = text(nameValue)
  const watched = useWatch({ control, name: [...INPUTS] })
  const latest = JSON.stringify(queryOf(watched) ?? null)
  const [settled, setSettled] = useState(latest)
  useEffect(() => {
    const timer = setTimeout(() => {
      setSettled(latest)
    }, DEBOUNCE_MS)
    return () => {
      clearTimeout(timer)
    }
  }, [latest])
  const query = JSON.parse(settled) as ReturnType<typeof queryOf> | null
  const list = useListForwardCaches(query ?? EMPTY_QUERY, {
    query: { enabled: query !== null, staleTime: 5_000 },
  })
  const caches = list.data?.caches ?? []
  const suggested = list.data?.suggested_name ?? ""
  // The choice follows the list until the user makes one: the one cache that fits, or New… when none does.
  const chosen = useRef(current !== "")
  useEffect(() => {
    if (chosen.current || list.data === undefined || list.data.problem) return
    if (list.data.caches.length === 1) {
      onFlag(false)
      onName(list.data.caches[0])
    } else if (list.data.caches.length === 0 && list.data.suggested_name) {
      onFlag(true)
      onName(list.data.suggested_name)
    }
  }, [list.data, onFlag, onName])

  const options = [
    ...new Set([...caches, ...(!creating && current ? [current] : [])]),
  ]
  const labels: Record<string, string> = { [NEW]: "New…" }
  for (const option of options) labels[option] = option
  const problem = list.data?.problem
  return (
    <FieldShell
      id={id}
      label={field.title}
      hint={fieldHint(field)}
      issues={issues}
      className={className}
    >
      <Select
        items={labels}
        value={creating ? NEW : current}
        onValueChange={(next) => {
          chosen.current = true
          if (next === NEW) {
            onFlag(true)
            onName(suggested)
          } else {
            onFlag(false)
            onName(next ?? "")
          }
        }}
      >
        <SelectTrigger id={id} ref={ref} className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {options.map((option) => (
            <SelectItem key={option} value={option}>
              {option}
            </SelectItem>
          ))}
          <SelectItem value={NEW}>New…</SelectItem>
        </SelectContent>
      </Select>
      {creating && (
        <Input
          aria-label="New cache name"
          value={current}
          onChange={(event) => {
            chosen.current = true
            onName(event.target.value)
          }}
          onBlur={onNameBlur}
        />
      )}
      {problem && <p className="text-sm text-muted-foreground">{problem}</p>}
    </FieldShell>
  )
}
