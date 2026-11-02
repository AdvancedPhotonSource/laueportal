import { InfoIcon } from "lucide-react"

import type { ReconstructionRunDetail } from "@/api"
import { DefinitionList, Value } from "@/components/DefinitionList"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { parameterGroups, type ParameterRow } from "./parameters"

/**
 * The Parameters tab: the run's stored parameters (GET /reconstruction-runs/{id}, the database's),
 * read-only, grouped as the wire form; a path inside the file roots also shows its root-relative
 * form.
 */
export function ParametersTab({ run }: { run: ReconstructionRunDetail }) {
  const groups = parameterGroups(run)
  return (
    <div className="flex flex-col gap-3">
      {run.parameters === null && (
        <p
          role="status"
          className="flex items-center gap-2 text-sm text-muted-foreground"
        >
          <InfoIcon aria-hidden className="size-4 shrink-0" />
          {`No wire parameters stored (method ${run.method})`}
        </p>
      )}
      <div className="grid items-start gap-3 lg:grid-cols-2">
        {groups.map((group) => (
          <Card key={group.title} size="sm">
            <CardHeader>
              <CardTitle>
                <h2>{group.title}</h2>
              </CardTitle>
            </CardHeader>
            <CardContent>
              <DefinitionList rows={group.rows.map(definitionOf)} />
            </CardContent>
          </Card>
        ))}
      </div>
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
          className="text-muted-foreground"
          title="Relative to its file root"
        >
          {row.relative}
        </span>
      </span>
    ),
  }
}
