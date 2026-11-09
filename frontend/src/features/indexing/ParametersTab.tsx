import { InfoIcon } from "lucide-react"

import type { IndexingRunDetail } from "@/api"
import { DefinitionList } from "@/components/DefinitionList"
import { SectionCard } from "@/features/program-form"
import { parameterGroups } from "./parameters"

/**
 * The Parameters tab: the run's stored parameters (GET /indexing-runs/{id}, the database's),
 * read-only, in the create form's section cards and order, one column.
 */
export function ParametersTab({ run }: { run: IndexingRunDetail }) {
  const groups = parameterGroups(run)
  return (
    <div className="flex flex-col gap-4">
      {run.parameters === null && run.laue_matching_parameters === null && (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <InfoIcon aria-hidden className="size-4 shrink-0" />
          {`No parameters stored (method ${run.method}).`}
        </p>
      )}
      {groups.map((group) => (
        <SectionCard key={group.title} title={group.title}>
          <DefinitionList rows={group.rows} />
        </SectionCard>
      ))}
    </div>
  )
}
