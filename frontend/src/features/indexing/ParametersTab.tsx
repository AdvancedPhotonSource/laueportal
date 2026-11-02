import { InfoIcon } from "lucide-react"

import type { IndexingRunDetail } from "@/api"
import { DefinitionList } from "@/components/DefinitionList"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { parameterGroups } from "./parameters"

/**
 * The Parameters tab: the run's stored parameters (GET /indexing-runs/{id}, the database's),
 * read-only, grouped as the create form.
 */
export function ParametersTab({ run }: { run: IndexingRunDetail }) {
  const groups = parameterGroups(run)
  return (
    <div className="flex flex-col gap-3">
      {run.parameters === null && (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <InfoIcon aria-hidden className="size-4 shrink-0" />
          {`No LaueGo parameters stored (method ${run.method}).`}
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
              <DefinitionList rows={group.rows} />
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  )
}
