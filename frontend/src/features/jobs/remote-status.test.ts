import { describe, expect, it } from "vitest"

import { RemotePhase } from "@/api"
import { makeCruxJob, makeJobSummary, makeObservation } from "@/test/jobs"
import { remoteView } from "./job-page"
import {
  remotePhaseNotes,
  remotePhaseView,
  remoteStatus,
  type RunMonitorRow,
} from "./remote-status"

// The remote phase the Remote Run card and the run monitor's Status cell share.

const AS_OF = "2026-10-25T12:00:30Z"

function cruxRow(overrides: Partial<RunMonitorRow> = {}): RunMonitorRow {
  return {
    ...makeJobSummary(40, {
      status: "running",
      phase: "running",
      target: "crux",
      execution: "ALCF",
      dm_state: "running",
      observation: makeObservation(),
    }),
    as_of: AS_OF,
    ...overrides,
  }
}

describe("a crux run's remote status", () => {
  it("is warning-toned for Queued and Login needed and info-toned otherwise", () => {
    const tones = Object.fromEntries(
      Object.values(RemotePhase).map((phase) => {
        const view = remotePhaseView(makeObservation({ phase }), AS_OF)
        return [view.label, view.tone]
      })
    )
    expect(tones).toEqual({
      Uplink: "info",
      Queued: "warning",
      Running: "info",
      Downlink: "info",
      "Login needed": "warning",
    })
  })

  it("never reads At ALCF for Login needed, and a kept one printed stale reads delayed", () => {
    const paused = makeObservation({
      phase: "login_required",
      detail: "Sign in to Globus and resume this run",
      pbs_id: null,
      stale: true,
    })
    const view = remotePhaseView(paused, AS_OF)
    expect(remotePhaseNotes(view)).toBe(
      "Sign in to Globus and resume this run · status delayed"
    )
  })

  it("shows in the Status cell what the job page's card shows, for a queued or running crux run only", () => {
    const job = makeCruxJob()
    const row = cruxRow({ observation: job.remote?.observation ?? null })
    const shown = remoteStatus(row)
    expect(shown).toEqual(remoteView(job)?.phase)
    expect(shown && remotePhaseNotes(shown)).toBe("Processing · PBS 123456")
    // An ended run, another target or no observation: the run's status instead.
    expect(remoteStatus(cruxRow({ status: "finished" }))).toBeNull()
    expect(remoteStatus(cruxRow({ target: "aps" }))).toBeNull()
    expect(remoteStatus(cruxRow({ observation: null }))).toBeNull()
  })
})
