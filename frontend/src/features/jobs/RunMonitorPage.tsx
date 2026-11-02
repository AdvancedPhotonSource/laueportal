import { CircleStopIcon, RefreshCwIcon } from "lucide-react"
import { useRef, useState } from "react"

import { cancelJob, listJobs, moveJobToFront, type JobSummary } from "@/api"
import { ConfirmDialog } from "@/components/ConfirmDialog"
import {
  DataGrid,
  type DataGridHandle,
  type FetchRows,
} from "@/components/DataGrid"
import { GridPage } from "@/components/GridPage"
import { Button } from "@/components/ui/button"
import { JobActionList } from "./JobActionList"
import { toastReport } from "./report-toast"
import {
  isRemote,
  leftAlone,
  MOVE_EXPLANATION,
  moveOrder,
  moveReport,
  moveTitle,
  runEach,
  runMonitorActions,
  stopExplanation,
  stopReport,
  stopTitle,
  type JobRef,
  type RunMonitorActions,
} from "./run-monitor"
import { JOB_COLUMNS } from "./columns"
import type { RunMonitorRow } from "./remote-status"

// Each row keeps its page's as_of: when its observation was read, which the Status cell's "At ALCF"
// rule ages it to (remote-status.ts), as the job page's card ages its own.
const fetchJobs: FetchRows<RunMonitorRow> = async (query) => {
  const page = await listJobs(query)
  return {
    ...page,
    rows: page.rows.map((row) => ({ ...row, as_of: page.as_of })),
  }
}

const jobRowId = (job: JobSummary) => String(job.job_id)

const NO_SELECTION: RunMonitorActions = {
  count: 0,
  stoppable: [],
  movable: [],
}

/** An open confirm dialog: the action and the jobs it applies to (as selected when it opened). */
interface Pending {
  action: "stop" | "move"
  jobs: JobRef[]
  /** The selected jobs the action leaves alone, in words. */
  left?: string
}

/**
 * The run monitor: every job, live while one shows queued or running. Stop stops the selected
 * queued and running jobs, Move to Front puts the selected queued ones ahead of the other queued
 * runs; each asks first, then a toast says what happened to each job.
 */
export function RunMonitorPage() {
  const grid = useRef<DataGridHandle>(null)
  const [actions, setActions] = useState<RunMonitorActions>(NO_SELECTION)
  // Kept after the dialog closes, so its content stays while it fades out.
  const [pending, setPending] = useState<Pending | null>(null)
  const [open, setOpen] = useState(false)
  const { count, stoppable, movable } = actions

  const ask = (next: Pending) => {
    setPending(next)
    setOpen(true)
  }

  const confirm = async () => {
    if (!pending) return
    if (pending.action === "stop") {
      const outcomes = await runEach(pending.jobs, (jobId) => cancelJob(jobId))
      toastReport(stopReport(outcomes))
    } else {
      const outcomes = await runEach(moveOrder(pending.jobs), (jobId) =>
        moveJobToFront(jobId)
      )
      toastReport(moveReport(outcomes))
    }
    grid.current?.refresh()
  }

  const jobs = pending?.jobs ?? []
  const remote = jobs.some((job) => isRemote(job.target))
  const stop = stopExplanation(jobs)

  return (
    <GridPage
      title="Run Monitor"
      selected={count}
      onClearSelection={() => grid.current?.clearSelection()}
      actions={
        <>
          <Button
            variant="destructive"
            disabled={stoppable.length === 0}
            onClick={() => {
              ask({
                action: "stop",
                jobs: stoppable,
                left: leftAlone(count - stoppable.length, "ended"),
              })
            }}
          >
            <CircleStopIcon data-icon="inline-start" aria-hidden />
            Stop
          </Button>
          <Button
            disabled={movable.length === 0}
            onClick={() => {
              ask({
                action: "move",
                jobs: movable,
                left: leftAlone(count - movable.length, "not queued"),
              })
            }}
          >
            Move to Front
          </Button>
          <Button variant="outline" onClick={() => grid.current?.refresh()}>
            <RefreshCwIcon data-icon="inline-start" aria-hidden />
            Refresh
          </Button>
        </>
      }
    >
      <DataGrid
        ref={grid}
        gridId="run-monitor"
        columns={JOB_COLUMNS}
        fetchRows={fetchJobs}
        getRowId={jobRowId}
        selection="multiRow"
        onSelectionChange={(rows) => {
          setActions(runMonitorActions(rows))
        }}
        live
        changedSince
        noun="jobs"
        className="flex-1"
      />
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title={
          pending?.action === "move"
            ? moveTitle(jobs.length)
            : stopTitle(jobs.length)
        }
        description={
          pending?.action === "move" ? MOVE_EXPLANATION : stop.description
        }
        confirmLabel={
          pending?.action === "move"
            ? "Move to Front"
            : jobs.length === 1
              ? "Stop Run"
              : "Stop Runs"
        }
        destructive={pending?.action !== "move"}
        onConfirm={confirm}
      >
        <JobActionList
          jobs={jobs}
          notes={[
            remote
              ? pending?.action === "move"
                ? "Remote runs are ordered by their scheduler: they do not move."
                : "A remote run's stop is delivered to its scheduler by the remote poller."
              : undefined,
            pending?.action === "move" ? undefined : stop.note,
            pending?.left,
          ]}
        />
      </ConfirmDialog>
    </GridPage>
  )
}
