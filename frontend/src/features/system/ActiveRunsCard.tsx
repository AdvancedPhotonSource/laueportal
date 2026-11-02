import { HeartOffIcon, OctagonPauseIcon } from "lucide-react"

import type { SystemStatus } from "@/api"
import { JobLink, RunLink } from "@/components/RunLinks"
import { RunProgressBar } from "@/components/RunProgressBar"
import { RunStatusBadge } from "@/components/RunStatusBadge"
import { Badge } from "@/components/ui/badge"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { StatusCard } from "./StatusCard"
import { activeRunsView, type ActiveRunView } from "./system-status"

/**
 * The queued and running runs (from the database, the authority for run state): each with its job
 * and run links, status and progress, heartbeat age, the stale and stop-requested badges, and where
 * it runs (a remote run's target and DM's state).
 */
export function ActiveRunsCard({ status }: { status: SystemStatus }) {
  const view = activeRunsView(status)
  return (
    <StatusCard
      title="Active Runs"
      action={
        view.kind === "runs" && (
          <span className="text-xs text-muted-foreground">
            {view.runs.length} queued or running
          </span>
        )
      }
    >
      {view.kind === "unknown" && (
        <p className="text-muted-foreground">
          The runs cannot be read: the database does not answer.
        </p>
      )}
      {view.kind === "none" && (
        <p className="text-muted-foreground">No queued or running runs.</p>
      )}
      {view.kind === "runs" && (
        <div className="flex flex-col gap-2">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Job</TableHead>
                <TableHead>Run</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="w-48">Progress</TableHead>
                <TableHead>Heartbeat</TableHead>
                <TableHead>Target</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {view.runs.map((run) => (
                <ActiveRunRow key={run.jobId} run={run} />
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </StatusCard>
  )
}

function ActiveRunRow({ run }: { run: ActiveRunView }) {
  return (
    <TableRow data-job={run.jobId}>
      <TableCell>
        <JobLink id={run.jobId} />
      </TableCell>
      <TableCell>
        {run.run ? (
          <RunLink run={run.run} />
        ) : (
          <span className="text-muted-foreground">None</span>
        )}
      </TableCell>
      <TableCell>
        <div className="flex items-center gap-1">
          <RunStatusBadge
            status={run.status}
            processed={run.processed}
            total={run.counts.n_inputs}
          />
          {run.stopRequested && (
            <Badge
              variant="outline"
              className="border-warning/50 bg-warning/15 text-foreground"
            >
              <OctagonPauseIcon
                data-icon="inline-start"
                aria-hidden
                className="text-warning"
              />
              Stop requested
            </Badge>
          )}
        </div>
      </TableCell>
      <TableCell>
        <RunProgressBar counts={run.counts} status={run.status} />
      </TableCell>
      <TableCell>
        <div className="flex items-center gap-1">
          {run.stale && (
            <Badge
              variant="outline"
              title="No recent heartbeat: the worker marks the run interrupted once it finds its process gone"
              className="border-destructive/50 bg-destructive/15 text-foreground"
            >
              <HeartOffIcon
                data-icon="inline-start"
                aria-hidden
                className="text-destructive"
              />
              Stale
            </Badge>
          )}
          <span>{run.heartbeat}</span>
        </div>
      </TableCell>
      <TableCell>
        {run.remote ? (
          <span className="flex items-center gap-1.5">
            <span className="font-medium">{run.remote.target}</span>
            <span className="text-muted-foreground">
              DM: {run.remote.dmState}
            </span>
          </span>
        ) : (
          <span className="text-muted-foreground">Local</span>
        )}
      </TableCell>
    </TableRow>
  )
}
