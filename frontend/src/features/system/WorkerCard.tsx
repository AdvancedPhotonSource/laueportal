import type { SystemStatus } from "@/api"
import { DefinitionList } from "@/components/DefinitionList"
import { subheading } from "@/lib/styles"
import { StatusCard, ToneBadge } from "./StatusCard"
import { workerView } from "./system-status"

/**
 * The worker (its row, `jobs.worker`): its state, host, PID and start, and its three threads, each
 * with its state and its last mark's age (the late limit in the badge's tooltip; an Off thread has
 * none).
 */
export function WorkerCard({ status }: { status: SystemStatus }) {
  const view = workerView(status)
  return (
    <StatusCard
      title="Worker"
      action={
        view.kind === "worker" && (
          <ToneBadge tone={view.tone}>{view.state}</ToneBadge>
        )
      }
    >
      {view.kind === "unknown" ? (
        <p className="text-muted-foreground">Unknown</p>
      ) : (
        <div className="flex flex-col gap-3">
          {view.rows.length > 0 ? (
            <DefinitionList aria-label="Worker" rows={view.rows} />
          ) : (
            <p className="text-muted-foreground">Never started</p>
          )}
          <div className="flex flex-col gap-1">
            <h3 className={subheading}>Threads</h3>
            <DefinitionList
              aria-label="Threads"
              rows={view.threads.map((thread) => ({
                label: thread.name,
                value: (
                  <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <ToneBadge tone={thread.tone} title={thread.hint}>
                      {thread.state}
                    </ToneBadge>
                    <span className="text-xs text-muted-foreground">
                      {thread.mark}
                    </span>
                  </span>
                ),
              }))}
            />
          </div>
        </div>
      )}
    </StatusCard>
  )
}
