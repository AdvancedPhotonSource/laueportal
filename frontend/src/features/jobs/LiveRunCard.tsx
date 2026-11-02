import type { JobDetail } from "@/api"
import { DefinitionList } from "@/components/DefinitionList"
import { JobLink, RunLink } from "@/components/RunLinks"
import { liveRows, type LiveRow } from "./job-page"
import { JobCard } from "./JobCard"

/**
 * Live Run: a live run's source (a DAQ, Notifications, or the wire run it follows, linked), its
 * folder and file name, its arrivals and its arrival status, as labels and values.
 */
export function LiveRunCard({ job }: { job: JobDetail }) {
  if (!job.live) return null
  return (
    <JobCard title="Live Run">
      <DefinitionList
        aria-label="Live run"
        rows={liveRows(job.live).map((row) => ({
          label: row.label,
          value: <LiveValue row={row} />,
          mono: row.mono,
        }))}
      />
    </JobCard>
  )
}

function LiveValue({ row }: { row: LiveRow }) {
  if (row.follows) {
    return (
      <span className="flex flex-wrap gap-x-3">
        <span>
          <span className="text-muted-foreground">Reconstruction </span>
          <RunLink run={row.follows.run} />
        </span>
        {row.follows.jobId !== null && (
          <span>
            <span className="text-muted-foreground">Job </span>
            <JobLink id={row.follows.jobId} />
          </span>
        )}
      </span>
    )
  }
  if (!row.text) {
    return (
      <span className="text-muted-foreground">
        <span aria-hidden>—</span>
        <span className="sr-only">none</span>
      </span>
    )
  }
  return (
    <span
      title={row.hint}
      data-warning={row.warning ? "" : undefined}
      className={
        row.mono
          ? "font-mono wrap-anywhere"
          : row.warning
            ? "wrap-break-word text-warning"
            : "wrap-break-word"
      }
    >
      {row.text}
    </span>
  )
}
