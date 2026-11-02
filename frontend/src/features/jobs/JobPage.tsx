import { keepPreviousData, useQueryClient } from "@tanstack/react-query"
import { CircleStopIcon, RefreshCwIcon } from "lucide-react"
import { useRef, useState } from "react"

import {
  cancelJob,
  getGetJobLogQueryKey,
  getGetJobQueryKey,
  getListJobFailuresQueryKey,
  useGetJob,
  useGetJobLog,
  type JobDetail,
} from "@/api"
import { ConfirmDialog } from "@/components/ConfirmDialog"
import { ErrorState } from "@/components/ErrorState"
import { LoadingState } from "@/components/LoadingState"
import { RunLink, ScanLink } from "@/components/RunLinks"
import { Button } from "@/components/ui/button"
import { failureStreak } from "@/lib/polling"
import { useRetryFocus } from "@/lib/retry-focus"
import { isActiveStatus } from "@/lib/run-status"
import { cn } from "@/lib/utils"
import { FailedInputsCard } from "./FailedInputsCard"
import {
  detailRef,
  JOB_POLL_MS,
  jobRefetchInterval,
  jobRevision,
  LOG_TAIL_LINES,
} from "./job-page"
import { JobDetailsCard } from "./JobDetailsCard"
import { LiveRunCard } from "./LiveRunCard"
import { RemoteRunCard } from "./RemoteRunCard"
import { toastReport } from "./report-toast"
import { RunConfigurationCard } from "./RunConfigurationCard"
import { RunProgressCard } from "./RunProgressCard"
import {
  LIVE_STOP_EXPLANATION,
  runEach,
  STOP_EXPLANATION,
  stopReport,
  targetName,
} from "./run-monitor"

const LOG_PARAMS = { tail: LOG_TAIL_LINES }

/**
 * A job's page: its run and scan, Job Details, Run Progress, a live run's card, a remote run's card,
 * Run Configuration with the log's last lines, and the failed inputs; Refresh and Stop Run (a live
 * run's dialog: `LIVE_STOP_EXPLANATION`). It asks for the job every 2 s while it is queued or
 * running (not while the tab is hidden), slowly for a stale run, backed off after failures, and no
 * more once the job is gone (jobRefetchInterval).
 */
export function JobPage({ jobId }: { jobId: number }) {
  const queryClient = useQueryClient()
  // The job's consecutive failed polls, for the back-off.
  const [streak] = useState(failureStreak)
  const job = useGetJob(jobId, {
    query: {
      // The route's loader has just read it: no second read when the page shows; polls still run.
      staleTime: JOB_POLL_MS,
      refetchInterval: (query) =>
        jobRefetchInterval(
          query.state.data,
          query.state.error,
          streak(query.state)
        ),
      refetchIntervalInBackground: false,
    },
  })
  // A Retry that loads takes its alert away: the heading gets the keyboard.
  const headingRef = useRef<HTMLHeadingElement>(null)
  const markRetry = useRetryFocus(
    job.error !== null,
    job.isFetching,
    headingRef
  )
  const [stopping, setStopping] = useState(false)

  const refresh = () => {
    for (const queryKey of [
      getGetJobQueryKey(jobId),
      getGetJobLogQueryKey(jobId),
      getListJobFailuresQueryKey(jobId),
    ]) {
      void queryClient.invalidateQueries({ queryKey })
    }
  }

  const stop = async (detail: JobDetail) => {
    const outcomes = await runEach([detailRef(detail)], (id) => cancelJob(id))
    toastReport(stopReport(outcomes, "Stop Run"))
    await queryClient.invalidateQueries({ queryKey: getGetJobQueryKey(jobId) })
  }

  const data = job.data
  const active = data !== undefined && isActiveStatus(data.status)

  return (
    <section
      className="flex flex-col gap-4"
      aria-busy={data === undefined && job.error === null}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <h1
          ref={headingRef}
          tabIndex={-1}
          className="text-xl font-semibold outline-none"
        >
          Job {jobId}
        </h1>
        {data && <JobLinks job={data} />}
        <div className="ml-auto flex items-center gap-2">
          <Button variant="outline" onClick={refresh}>
            <RefreshCwIcon
              data-icon="inline-start"
              aria-hidden
              className={cn(job.isFetching && "motion-safe:animate-spin")}
            />
            Refresh
          </Button>
          <Button
            variant="destructive"
            disabled={!active}
            onClick={() => {
              setStopping(true)
            }}
          >
            <CircleStopIcon data-icon="inline-start" aria-hidden />
            Stop Run
          </Button>
        </div>
      </div>

      {job.error !== null && (
        <ErrorState
          error={job.error}
          onRetry={() => {
            markRetry()
            void job.refetch({ cancelRefetch: false })
          }}
          refreshing={job.isFetching}
          stale={data !== undefined}
        />
      )}
      {data ? (
        <JobCards job={data} />
      ) : (
        job.error === null && <LoadingState label="Loading the job" />
      )}

      {data && (
        <ConfirmDialog
          open={stopping}
          onOpenChange={setStopping}
          title={`Stop job ${String(jobId)}?`}
          description={data.live ? LIVE_STOP_EXPLANATION : STOP_EXPLANATION}
          confirmLabel="Stop Run"
          destructive
          onConfirm={() => stop(data)}
        >
          {data.remote && (
            <p className="text-sm text-muted-foreground">
              It runs on {targetName(data.remote.target)}: the remote poller
              delivers the stop to its scheduler.
            </p>
          )}
        </ConfirmDialog>
      )}
    </section>
  )
}

/** The job's run (`I12`, `R3`) and its scan, beside the heading. */
function JobLinks({ job }: { job: JobDetail }) {
  if (!job.run && job.scan_number === null) return null
  return (
    <p className="flex flex-wrap items-center gap-x-3 text-sm">
      {job.run && (
        <span>
          <span className="text-muted-foreground">
            {job.run.kind === "indexing" ? "Indexing" : "Reconstruction"}{" "}
          </span>
          <RunLink run={job.run} />
        </span>
      )}
      {job.scan_number !== null && (
        <span>
          <span className="text-muted-foreground">Scan </span>
          <ScanLink scan={job.scan_number} />
        </span>
      )}
    </p>
  )
}

function JobCards({ job }: { job: JobDetail }) {
  const log = useGetJobLog(job.job_id, LOG_PARAMS, {
    query: {
      // Read again when the job's status or phase changes: run.json is written when the run ends.
      queryKey: [
        ...getGetJobLogQueryKey(job.job_id, LOG_PARAMS),
        jobRevision(job),
      ],
      // It changes only when the job does (its revision is in the key) or on Refresh.
      staleTime: Infinity,
      placeholderData: keepPreviousData,
    },
  })
  return (
    <>
      <JobDetailsCard job={job} />
      <RunProgressCard job={job} />
      <LiveRunCard job={job} />
      {job.remote && <RemoteRunCard job={job} />}
      <RunConfigurationCard
        job={job}
        log={{
          data: log.data,
          error: log.error,
          isFetching: log.isFetching,
          retry: () => void log.refetch({ cancelRefetch: false }),
        }}
      />
      <FailedInputsCard job={job} />
    </>
  )
}
