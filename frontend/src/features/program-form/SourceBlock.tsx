// The create form's source: the "Source" switch, the one picker of the chosen kind (the existing source
// pickers), and the IDs that source sets, read-only (a reconstruction's scan; a clone's scan and
// reconstruction).
import { useId } from "react"

import type { ProgramKind } from "@/api"
import { ReconstructionRunLink, ScanLink } from "@/components/RunLinks"
import { FieldTitle } from "@/components/ui/field"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { IssueMessages } from "./FieldShell"
import type { SourceSlot } from "./ProgramForm"
import {
  fieldWithWidget,
  type FieldSpec,
  type ProgramSpec,
} from "./program-spec"
import {
  SOURCE_MODE_LABELS,
  SOURCE_MODES,
  type SourceMode,
} from "./source-mode"
import { CloneField, SourceField } from "./SourcePicker"

const SWITCH_HINT: Record<ProgramKind, string> = {
  wire: "Scan: its files (none: paths by hand). Clone: a reconstruction's values and scan.",
  lauego:
    "Scan: its files (none: paths by hand). Reconstruction: its scan.h5 and scan. Clone: an indexing run's values, scan and reconstruction.",
}

/** A form value as a source's ID, or null. */
function idOf(value: unknown): number | null {
  const text = typeof value === "string" ? value.trim() : ""
  return /^\d+$/.test(text) ? Number(text) : null
}

export function SourceBlock({
  program,
  spec,
  slot,
  mode,
  onModeChange,
  clone,
  onPick,
}: {
  program: ProgramKind
  spec: ProgramSpec
  slot: SourceSlot
  mode: SourceMode
  onModeChange: (mode: SourceMode) => void
  /** The run cloned (the Clone picker's value). */
  clone: number | null
  /** A source picked (null: the picker cleared). */
  onPick: (mode: SourceMode, id: number | null) => void
}) {
  const labelId = useId()
  const scan = fieldWithWidget(spec, "scan")
  const reconstruction = fieldWithWidget(spec, "reconstruction")
  const derived = (
    mode === "reconstruction"
      ? [scan]
      : mode === "clone"
        ? [scan, reconstruction]
        : []
  ).filter(
    (field): field is FieldSpec =>
      field !== undefined && idOf(slot.values[field.name]) !== null
  )
  const picker =
    mode === "clone" ? (
      <CloneField
        program={program}
        value={clone}
        onChange={(id) => {
          onPick("clone", id)
        }}
      />
    ) : (
      (() => {
        const field = mode === "scan" ? scan : reconstruction
        return field === undefined ? null : (
          <SourceField
            field={field}
            control={slot.control}
            issues={slot.issuesOf(field.name)}
            kind={mode}
            onSourceChange={(id) => {
              onPick(mode, id)
            }}
          />
        )
      })()
    )

  return (
    <div className="flex flex-wrap items-start gap-4">
      <div className="flex flex-col gap-1.5">
        <FieldTitle id={labelId} title={SWITCH_HINT[program]}>
          Source
        </FieldTitle>
        <ToggleGroup
          aria-labelledby={labelId}
          variant="outline"
          spacing={0}
          value={[mode]}
          onValueChange={(value) => {
            const next = value[0] as SourceMode | undefined
            if (next !== undefined && next !== mode) onModeChange(next)
          }}
        >
          {SOURCE_MODES[program].map((item) => (
            <ToggleGroupItem
              key={item}
              value={item}
              className="aria-pressed:bg-primary aria-pressed:text-primary-foreground aria-pressed:hover:bg-primary aria-pressed:hover:text-primary-foreground"
            >
              {SOURCE_MODE_LABELS[item]}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
      </div>
      <div className="min-w-48 flex-1">{picker}</div>
      {derived.length > 0 && (
        <dl aria-label="Source IDs" className="flex gap-4">
          {derived.map((field) => {
            const id = idOf(slot.values[field.name]) ?? 0
            return (
              <div key={field.name} className="flex flex-col gap-1.5">
                <dt className="text-sm leading-snug font-medium">
                  {field.title}
                </dt>
                <dd className="flex flex-col gap-1.5">
                  <span className="flex h-8 items-center text-sm tabular-nums">
                    {field.widget === "scan" ? (
                      <ScanLink scan={id}>SN{id}</ScanLink>
                    ) : (
                      <ReconstructionRunLink id={id} />
                    )}
                  </span>
                  <IssueMessages
                    id={`${labelId}-${field.name}-issues`}
                    issues={slot.issuesOf(field.name)}
                  />
                </dd>
              </div>
            )
          })}
        </dl>
      )}
    </div>
  )
}
