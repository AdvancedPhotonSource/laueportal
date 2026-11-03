import { InfoIcon } from "lucide-react"

import type { ReconstructionRunDetail } from "@/api"
import { DefinitionList, Value } from "@/components/DefinitionList"
import { SectionCard } from "@/features/program-form"
import { parameterGroups, type ParameterRow } from "./parameters"

/**
 * The Parameters tab: the run's stored parameters (GET /reconstruction-runs/{id}, the database's),
 * read-only, in the wire form's section cards and order, one column; a path inside the file roots
 * also shows its root-relative form.
 */
export function ParametersTab({ run }: { run: ReconstructionRunDetail }) {
  const groups = parameterGroups(run)
  return (
    <div className="flex flex-col gap-4">
      {run.parameters === null && (
        <p
          role="status"
          className="flex items-center gap-2 text-sm text-muted-foreground"
        >
          <InfoIcon aria-hidden className="size-4 shrink-0" />
          {`No wire parameters stored (method ${run.method})`}
        </p>
      )}
      {groups.map((group) => (
        <SectionCard key={group.title} title={group.title}>
          <DefinitionList rows={group.rows.map(definitionOf)} />
        </SectionCard>
      ))}
    </div>
  )
}

function definitionOf(row: ParameterRow) {
  if (row.relative === undefined) return row
  return {
    label: row.label,
    mono: row.mono,
    value: (
      <span className="flex flex-col">
        <Value text={row.value} mono />
        <span
          className="wrap-anywhere text-muted-foreground"
          title="Relative to its file root"
        >
          {row.relative}
        </span>
      </span>
    ),
  }
}
