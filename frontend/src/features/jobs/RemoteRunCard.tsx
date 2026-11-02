import { ExternalLinkIcon } from "lucide-react"

import type { JobDetail } from "@/api"
import { DefinitionList, type Definition } from "@/components/DefinitionList"
import { remoteView } from "./job-page"
import { JobCard } from "./JobCard"
import { RemotePhaseBadge } from "./RemotePhaseBadge"
import type { RemotePhaseView } from "./remote-status"

/**
 * A remote run: where it executes, a crux run's phase, DM's state, a crux run's Globus run, and when
 * the remote poller last looked. Its counters update when the results return; the heartbeat check does
 * not apply.
 */
export function RemoteRunCard({ job }: { job: JobDetail }) {
  const remote = remoteView(job)
  if (!remote) return null
  const rows: Definition[] = [{ label: "Target", value: remote.target }]
  if (remote.phase) {
    rows.push({ label: "Phase", value: <PhaseValue view={remote.phase} /> })
  }
  rows.push({ label: "DM state", value: remote.dmState })
  if (remote.globus) {
    rows.push({
      label: "Globus run",
      value: remote.globus.url ? (
        <a
          href={remote.globus.url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-link underline-offset-4 hover:underline"
        >
          Globus run
          <ExternalLinkIcon className="size-3.5" aria-hidden />
          <span className="sr-only">(opens in a new tab)</span>
        </a>
      ) : (
        <span className="text-muted-foreground">Not reported by DM yet</span>
      ),
    })
  }
  rows.push({ label: "Last polled", value: remote.polled })
  return (
    <JobCard title="Remote Run">
      <div className="flex flex-col gap-2">
        <DefinitionList rows={rows} />
      </div>
    </JobCard>
  )
}

/** The phase badge, the detail, the PBS ID and "status delayed" (the observation time its tooltip). */
function PhaseValue({ view }: { view: RemotePhaseView }) {
  const notes = [view.detail, view.pbs].filter(Boolean).join(" · ")
  return (
    <span
      className="flex flex-wrap items-center gap-x-2 gap-y-1"
      data-testid="remote-phase"
    >
      <RemotePhaseBadge view={view} />
      {(notes || view.delayed) && (
        <span className="text-muted-foreground">
          {notes}
          {notes && view.delayed && " · "}
          {view.delayed && <span title={view.delayed}>status delayed</span>}
        </span>
      )}
    </span>
  )
}
