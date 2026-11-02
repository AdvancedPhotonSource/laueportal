import { RunStatusBadge } from "@/components/RunStatusBadge"
import { isRemote, targetName, type JobRef } from "./run-monitor"

/**
 * The jobs a confirm dialog acts on, one line each (its ID, its run, its status and a remote run's
 * target), then notes: on remote runs, and on the selected jobs the action leaves alone.
 */
export function JobActionList({
  jobs,
  notes,
}: {
  jobs: readonly JobRef[]
  notes: readonly (string | undefined)[]
}) {
  return (
    <div className="flex flex-col gap-2 text-sm">
      <ul
        aria-label="Jobs"
        className="flex max-h-48 flex-col gap-1 overflow-y-auto rounded-lg border p-2"
      >
        {jobs.map((job) => (
          <li
            key={job.jobId}
            data-job={job.jobId}
            className="flex items-center gap-2"
          >
            <span className="font-medium tabular-nums">Job {job.jobId}</span>
            {job.run && (
              <span className="text-muted-foreground">{job.run}</span>
            )}
            {isRemote(job.target) && (
              <span className="text-muted-foreground">
                on {targetName(job.target)}
              </span>
            )}
            <RunStatusBadge status={job.status} className="ml-auto" />
          </li>
        ))}
      </ul>
      {notes
        .filter((note): note is string => note !== undefined)
        .map((note) => (
          <p key={note} className="text-muted-foreground">
            {note}
          </p>
        ))}
    </div>
  )
}
