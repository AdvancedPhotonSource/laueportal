import { ArrowUpToLineIcon } from "lucide-react"

import type { SystemStatus } from "@/api"
import { JobLink, RunLink } from "@/components/RunLinks"
import { Badge } from "@/components/ui/badge"
import { Separator } from "@/components/ui/separator"
import { subheading } from "@/lib/styles"
import { StatusCard } from "./StatusCard"
import { queueView, type NextItemView } from "./system-status"

/**
 * The queue (the job and conversion tables): its counts, the live slots in use, and the next
 * items the worker's dispatcher takes, in its order (the live runs, the conversions, then the runs
 * moved to the front, then the rest).
 */
export function QueueCard({ status }: { status: SystemStatus }) {
  const view = queueView(status)
  return (
    <StatusCard title="Queue">
      {view.kind === "unknown" ? (
        <p className="text-muted-foreground">Unknown</p>
      ) : (
        <div className="flex flex-col gap-3">
          <dl className="grid grid-cols-4 gap-2 text-center">
            {view.counts.map((count) => (
              <div
                key={count.label}
                className="flex flex-col-reverse justify-end"
              >
                <dt className="text-xs text-muted-foreground">{count.label}</dt>
                <dd className="text-2xl font-semibold tabular-nums">
                  {count.value}
                </dd>
              </div>
            ))}
          </dl>
          <Separator />
          <div className="flex flex-col gap-1">
            <h3 className={subheading}>Next up</h3>
            {view.next.length === 0 ? (
              <p className="text-muted-foreground">Queue empty</p>
            ) : (
              <ol aria-label="Next up" className="flex flex-col text-sm">
                {view.next.map((item, index) => (
                  <NextItem key={item.key} item={item} position={index + 1} />
                ))}
              </ol>
            )}
          </div>
        </div>
      )}
    </StatusCard>
  )
}

function NextItem({
  item,
  position,
}: {
  item: NextItemView
  position: number
}) {
  return (
    <li
      data-item={item.key}
      className="flex min-h-7 flex-wrap items-center gap-x-2 gap-y-1 border-b border-border/70 py-1 last:border-b-0"
    >
      <span className="w-4 text-xs text-muted-foreground tabular-nums">
        {position}
      </span>
      {item.run ? (
        <RunLink run={item.run} />
      ) : (
        item.jobId !== null && (
          <span>
            Job <JobLink id={item.jobId} />
          </span>
        )
      )}
      {item.conversion && <Badge variant="outline">Conversion</Badge>}
      {item.movedToFront && (
        <Badge variant="outline">
          <ArrowUpToLineIcon data-icon="inline-start" aria-hidden />
          Moved to front
        </Badge>
      )}
    </li>
  )
}
