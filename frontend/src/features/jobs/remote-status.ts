// A crux run's remote phase as the job page's Remote Run card and the run monitor's Status cell show it: the phase's
// label and tone, the detail (but "At ALCF"), the PBS ID and "status delayed". No React here.
import { RemotePhase, type JobSummary, type RemoteObservation } from "@/api"
import { formatDateTime } from "@/lib/format"
import { isActiveStatus } from "@/lib/run-status"
import { remotePhaseText } from "./run-monitor"

/**
 * The "status delayed" age (the API's `stale` uses the same, `jobs.service` `observation_view`).
 */
export const REMOTE_DELAYED_AFTER_SECONDS = 90

/** The detail shown for a launcher that does not list. */
export const AT_ALCF = "At ALCF"

/** The badge colour: `warning` for Queued and Login needed, `info` for the others. */
export type RemoteTone = "warning" | "info"

export interface RemotePhaseView {
  phase: RemotePhase
  /** "Uplink", "Queued", "Running", "Downlink", "Login needed". */
  label: string
  tone: RemoteTone
  /** The launcher's detail ("Processing"), or "At ALCF"; null when it gave none. */
  detail: string | null
  /** "PBS 123456" (the part of the ID before its first dot); null until the launcher knows it. */
  pbs: string | null
  /** "status delayed"'s tooltip, when it was observed ("Observed 2026-10-25 07:00:00"); null when not delayed. */
  delayed: string | null
}

const WARNING_PHASES: ReadonlySet<RemotePhase> = new Set([
  RemotePhase.queued,
  RemotePhase.login_required,
])

/**
 * The launcher while the Flow runs the PBS job and it cannot see the job: the Flow-only launcher (no observer, no
 * listing), stale once that lasts 90 s (the golden `at-alcf-late`), and the observer launcher before its observer's
 * first answer, or stale when that answer is over 90 s old as the Flow enters RunPbsJob. The launcher says "Waiting
 * for status" there; the phase shows "At ALCF", delayed only when the launcher itself stopped. Never Login needed (its
 * phase is not Queued).
 */
function atAlcf(observation: RemoteObservation): boolean {
  return (
    observation.flow_state === "RunPbsJob" &&
    observation.phase === RemotePhase.queued &&
    observation.detail === "Waiting for status" &&
    observation.pbs_id === null
  )
}

/** A crux run's phase at the answer's `as_of`. */
export function remotePhaseView(
  observation: RemoteObservation,
  asOf: string
): RemotePhaseView {
  const alcf = atAlcf(observation)
  const age = (Date.parse(asOf) - Date.parse(observation.observed_at)) / 1000
  const delayed = alcf ? age > REMOTE_DELAYED_AFTER_SECONDS : observation.stale
  return {
    phase: observation.phase,
    label: remotePhaseText(observation.phase),
    tone: WARNING_PHASES.has(observation.phase) ? "warning" : "info",
    detail: alcf ? AT_ALCF : observation.detail,
    pbs: observation.pbs_id
      ? `PBS ${observation.pbs_id.split(".")[0] ?? ""}`
      : null,
    delayed: delayed
      ? `Observed ${formatDateTime(observation.observed_at)}`
      : null,
  }
}

/** The phase's notes in one line: "Processing · PBS 123456 · status delayed". */
export function remotePhaseNotes(view: RemotePhaseView): string {
  return [view.detail, view.pbs, view.delayed && "status delayed"]
    .filter(Boolean)
    .join(" · ")
}

/** A run monitor row with its page's `as_of`, when its observation was read (`RunMonitorPage`). */
export type RunMonitorRow = JobSummary & { as_of: string }

/**
 * The run monitor's Status cell: a queued or running crux run's phase in place of its status, when the row has an
 * observation (the job page's, `JobSummary.observation`); null otherwise, and the cell shows the run's status.
 */
export function remoteStatus(row: RunMonitorRow): RemotePhaseView | null {
  if (row.target !== "crux" || !isActiveStatus(row.status) || !row.observation)
    return null
  return remotePhaseView(row.observation, row.as_of)
}
