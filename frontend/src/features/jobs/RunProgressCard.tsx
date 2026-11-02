import {
  HeartOffIcon,
  InfoIcon,
  OctagonPauseIcon,
  type LucideIcon,
} from "lucide-react"

import type { JobDetail } from "@/api"
import { RunProgressBar } from "@/components/RunProgressBar"
import { cn } from "@/lib/utils"
import {
  noInputsText,
  progressCounters,
  progressNotes,
  type ProgressNote,
} from "./job-page"
import { JobCard } from "./JobCard"

const NOTE_STYLE: Record<
  ProgressNote["tone"],
  { Icon: LucideIcon; icon: string }
> = {
  muted: { Icon: InfoIcon, icon: "text-muted-foreground" },
  warning: { Icon: OctagonPauseIcon, icon: "text-warning" },
  alert: { Icon: HeartOffIcon, icon: "text-destructive" },
}

/** A remote run's counters stay at zero until DM returns its results. */
const REMOTE_COUNTERS = "Progress counters update after the results return"

/**
 * Run Progress: the six counters, the segmented bar, and notes on the heartbeat (a running local
 * run's age against the stale limit), a stop requested, and what an ended run processed. A remote
 * run's counters say in their tooltip when they update.
 */
export function RunProgressCard({ job }: { job: JobDetail }) {
  const { progress } = job
  return (
    <JobCard title="Run Progress">
      {progress.n_inputs === 0 ? (
        <p className="text-muted-foreground">{noInputsText(job)}</p>
      ) : (
        <div className="flex flex-col gap-3">
          <dl
            className="grid grid-cols-3 gap-2 text-center sm:grid-cols-6"
            title={job.remote ? REMOTE_COUNTERS : undefined}
          >
            {progressCounters(job).map(({ label, value }) => (
              <div key={label} className="flex flex-col-reverse">
                <dt className="text-xs text-muted-foreground">{label}</dt>
                <dd className="text-2xl font-semibold tabular-nums">{value}</dd>
              </div>
            ))}
          </dl>
          <RunProgressBar counts={progress} status={job.status} size="md" />
          <ProgressNotes notes={progressNotes(job)} />
        </div>
      )}
    </JobCard>
  )
}

function ProgressNotes({ notes }: { notes: readonly ProgressNote[] }) {
  if (notes.length === 0) return null
  return (
    <ul className="flex flex-col gap-1">
      {notes.map(({ text, tone }) => {
        const { Icon, icon } = NOTE_STYLE[tone]
        return (
          <li
            key={text}
            data-tone={tone}
            className={cn(
              "flex items-start gap-2",
              tone === "muted" && "text-muted-foreground"
            )}
          >
            <Icon className={cn("mt-0.5 size-4 shrink-0", icon)} aria-hidden />
            <span>{text}</span>
          </li>
        )
      })}
    </ul>
  )
}
