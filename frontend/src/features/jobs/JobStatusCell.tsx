import { RunStatusValue } from "@/components/DataGrid"
import { RemotePhaseBadge } from "./RemotePhaseBadge"
import {
  remotePhaseNotes,
  remoteStatus,
  type RunMonitorRow,
} from "./remote-status"

/**
 * The run monitor's Status cell: a queued or running crux run's remote phase (the job page's badge), its
 * notes the tooltip ("Processing · PBS 123456 · status delayed"); else the run's status.
 */
export function JobStatusCell({ row }: { row: RunMonitorRow }) {
  const remote = remoteStatus(row)
  if (!remote) {
    return (
      <RunStatusValue
        status={row.status}
        processed={row.n_processed}
        total={row.n_inputs}
        held={row.held}
      />
    )
  }
  const notes = remotePhaseNotes(remote)
  return (
    <span className="inline-flex" data-remote-status title={notes || undefined}>
      <RemotePhaseBadge view={remote} />
    </span>
  )
}
