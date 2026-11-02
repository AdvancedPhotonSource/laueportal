import type { JobDetail } from "@/api"
import { DefinitionList } from "@/components/DefinitionList"
import { RunStatusBadge } from "@/components/RunStatusBadge"
import { formatDateTime, formatDuration } from "@/lib/format"
import { HELD_HINT, isActiveStatus } from "@/lib/run-status"
import { subheading } from "@/lib/styles"
import { phaseNote } from "./job-page"
import { JobCard } from "./JobCard"

/**
 * Job Details: the status with a note on the finer phase, the computer it was submitted from, its
 * times and duration, and its messages. The priority is not shown.
 */
export function JobDetailsCard({ job }: { job: JobDetail }) {
  const note = phaseNote(job.status, job.phase)
  const active = isActiveStatus(job.status)
  const status = (
    <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
      <RunStatusBadge
        status={job.status}
        processed={job.progress.n_processed}
        total={job.progress.n_inputs}
      />
      {note && (
        <span className="text-muted-foreground" data-testid="phase-note">
          {note}
        </span>
      )}
      {job.held && (
        <span
          className="text-muted-foreground"
          data-testid="held"
          title={HELD_HINT}
        >
          Held by the live session
        </span>
      )}
    </span>
  )
  return (
    <JobCard title="Job Details">
      <div className="flex flex-col gap-3">
        <div className="grid gap-x-8 gap-y-1 md:grid-cols-2">
          <DefinitionList
            rows={[
              { label: "Status", value: status },
              { label: "Computer", value: job.computer_name },
            ]}
          />
          <DefinitionList
            rows={[
              { label: "Submit Time", value: formatDateTime(job.submit_time) },
              { label: "Start Time", value: formatDateTime(job.start_time) },
              { label: "Finish Time", value: formatDateTime(job.finish_time) },
              {
                label: "Duration",
                value: formatDuration(job.duration_seconds, active),
              },
            ]}
          />
        </div>
        <div className="flex flex-col gap-1">
          <h3 className={subheading}>Messages</h3>
          {job.messages ? (
            <pre
              aria-label="Messages"
              tabIndex={0}
              className="max-h-40 overflow-auto rounded-lg border bg-muted/40 p-2 font-mono text-xs/5 whitespace-pre-wrap"
            >
              {job.messages}
            </pre>
          ) : (
            <p className="text-muted-foreground">No messages.</p>
          )}
        </div>
      </div>
    </JobCard>
  )
}
