// The Live page's header strip: the session's state, Validate, Start and Stop, and each run of the latest
// session with its counters (arrived, done, failed), its arrival status and its links.
import {
  CircleDashedIcon,
  CircleStopIcon,
  LoaderCircleIcon,
  OctagonPauseIcon,
  PlayIcon,
  RefreshCwIcon,
} from "lucide-react"
import type { CreatedLiveRuns, LiveSession, LiveSessionRun } from "@/api"
import { JobLink, RunLink } from "@/components/RunLinks"
import { RUN_STATUS_STYLES, RunStatusBadge } from "@/components/RunStatusBadge"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardAction,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { describeError } from "@/lib/errors"
import { formatCount } from "@/lib/format"
import { cn } from "@/lib/utils"
import { arrivalLine, type SessionState } from "./live-form"

/**
 * The session's state as a badge: a run status, Stopping, or Idle before the first session; `hint`, its tooltip
 * (why an open session waits: `waitingReason`).
 */
export function SessionBadge({
  state,
  hint,
}: {
  state: SessionState
  hint?: string | undefined
}) {
  if (state === "none") {
    return (
      <Badge
        variant="outline"
        data-status="idle"
        className="border-border bg-muted text-foreground"
      >
        <CircleDashedIcon
          data-icon="inline-start"
          aria-hidden
          className="text-muted-foreground"
        />
        Idle
      </Badge>
    )
  }
  if (state === "stopping") {
    const style = RUN_STATUS_STYLES.running
    return (
      <Badge
        variant="outline"
        data-status="stopping"
        title={hint}
        className={cn("text-foreground", style.tint)}
      >
        <OctagonPauseIcon
          data-icon="inline-start"
          aria-hidden
          className={style.iconColor}
        />
        Stopping
      </Badge>
    )
  }
  return hint ? (
    <span title={hint}>
      <RunStatusBadge status={state} />
    </span>
  ) : (
    <RunStatusBadge status={state} />
  )
}

export function LiveHeader({
  session,
  state,
  started,
  message,
  readError,
  onReadAgain,
  reading,
  canValidate,
  canStart,
  starting,
  canStop,
  stopNow,
  waiting,
  onValidate,
  onStart,
  onStop,
}: {
  /** The latest session; null before the first. */
  session: LiveSession | null
  state: SessionState
  /** The last Start's answer while its runs are not the session shown (a run that could not be created). */
  started: CreatedLiveRuns | null
  /** Why the last Start was refused or failed. */
  message: string | null
  /** Why the session's last read again failed (the last answer stays shown); null when it did not. */
  readError: unknown
  onReadAgain: () => void
  reading: boolean
  canValidate: boolean
  canStart: boolean
  starting: boolean
  canStop: boolean
  /** The session is Stopping: Stop is Stop now. */
  stopNow: boolean
  /** Why the open session waits (`waitingReason`): the badge's tooltip. */
  waiting?: string | undefined
  onValidate: () => void
  onStart: () => void
  onStop: () => void
}) {
  const failures = (started?.runs ?? []).flatMap((run) =>
    run.failure ? [`${run.run.label}: ${run.failure}`] : []
  )
  return (
    <Card role="region" aria-label="Session">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          Session
          <SessionBadge state={state} hint={waiting} />
        </CardTitle>
        <CardAction className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            variant="outline"
            disabled={!canValidate}
            onClick={onValidate}
          >
            Validate
          </Button>
          <Button type="button" disabled={!canStart} onClick={onStart}>
            {starting ? (
              <LoaderCircleIcon
                data-icon="inline-start"
                aria-hidden
                className="animate-spin"
              />
            ) : (
              <PlayIcon data-icon="inline-start" aria-hidden />
            )}
            Start
          </Button>
          <Button
            type="button"
            variant="destructive"
            disabled={!canStop}
            onClick={onStop}
          >
            <CircleStopIcon data-icon="inline-start" aria-hidden />
            {stopNow ? "Stop now" : "Stop"}
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {readError !== null && (
          <div className="flex items-center gap-2">
            <p role="status" className="text-sm text-warning">
              Session not refreshed:{" "}
              {describeError(readError).detail ??
                describeError(readError).title}
            </p>
            <Button
              type="button"
              variant="outline"
              size="xs"
              disabled={reading}
              onClick={onReadAgain}
            >
              <RefreshCwIcon
                data-icon="inline-start"
                aria-hidden
                className={cn(reading && "animate-spin")}
              />
              Retry
            </Button>
          </div>
        )}
        {message !== null && (
          <p role="alert" className="text-sm text-destructive">
            {message}
          </p>
        )}
        {failures.map((failure) => (
          <p key={failure} role="alert" className="text-sm text-destructive">
            {failure}
          </p>
        ))}
        {session ? (
          <SessionRuns runs={session.runs} />
        ) : (
          <p className="text-sm text-muted-foreground">No live session</p>
        )}
      </CardContent>
    </Card>
  )
}

function SessionRuns({ runs }: { runs: readonly LiveSessionRun[] }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Run</TableHead>
          <TableHead>Job</TableHead>
          <TableHead>Status</TableHead>
          <TableHead className="text-right">Arrived</TableHead>
          <TableHead className="text-right">Done</TableHead>
          <TableHead className="text-right">Failed</TableHead>
          <TableHead className="pl-6">Arrivals</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {runs.map((run) => (
          <SessionRun key={run.job_id} run={run} />
        ))}
      </TableBody>
    </Table>
  )
}

function SessionRun({ run }: { run: LiveSessionRun }) {
  const arrival = arrivalLine(run)
  return (
    <TableRow data-job={run.job_id}>
      <TableCell>
        <span className="text-muted-foreground">
          {run.run?.kind === "indexing" ? "Indexing " : "Reconstruction "}
        </span>
        <RunLink run={run.run} />
      </TableCell>
      <TableCell>
        <JobLink id={run.job_id} />
      </TableCell>
      <TableCell>
        <div className="flex items-center gap-1">
          <RunStatusBadge status={run.status} />
          {run.stop_requested && (
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
      <TableCell className="text-right tabular-nums" data-count="arrived">
        {formatCount(run.n_inputs)}
      </TableCell>
      <TableCell className="text-right tabular-nums" data-count="done">
        {formatCount(run.n_succeeded)}
      </TableCell>
      <TableCell className="text-right tabular-nums" data-count="failed">
        {formatCount(run.n_failed)}
      </TableCell>
      <TableCell className="max-w-96 pl-6 whitespace-normal" data-arrival>
        {arrival ? (
          <span
            title={arrival.hint}
            className={cn(
              "text-sm",
              arrival.tone === "warning"
                ? "text-warning"
                : "text-muted-foreground"
            )}
          >
            {arrival.text}
          </span>
        ) : (
          <span className="text-muted-foreground">–</span>
        )}
      </TableCell>
    </TableRow>
  )
}
