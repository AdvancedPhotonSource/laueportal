import { readFileSync } from "node:fs"

import { describe, expect, it } from "vitest"

import {
  ApiError,
  type JobDetail,
  type RemoteObservation,
  type RemotePhase,
} from "@/api"
import { formatDateTime } from "@/lib/format"
import {
  emptyFailurePage,
  endedJob,
  failurePage,
  makeConfiguration,
  makeCruxJob,
  makeJobDetail,
  makeJobLive,
  makeObservation,
  makeRemoteJob,
  makeRunFile,
} from "@/test/jobs"
import {
  artifactLink,
  categoryTone,
  executionRows,
  failurePaging,
  failuresEmptyText,
  fileStateView,
  formatValue,
  historyRows,
  isCodeRow,
  jobRefetchInterval,
  jobRevision,
  liveRows,
  logTitle,
  noInputsText,
  parseJobId,
  partialResultsNote,
  phaseNote,
  progressNotes,
  remoteView,
  requestRows,
  runFiles,
  shortMessage,
  type Row,
} from "./job-page"
import {
  REMOTE_DELAYED_AFTER_SECONDS,
  remotePhaseView,
  type RemotePhaseView,
} from "./remote-status"

// The job page's logic: its polling, the notes on the phase, the heartbeat and a stop, the run
// files' states, the configuration's rows and the failure report's pages.

function withProgress(
  job: JobDetail,
  progress: Partial<JobDetail["progress"]>
): JobDetail {
  return { ...job, progress: { ...job.progress, ...progress } }
}

describe("the route's job and its polling", () => {
  it("takes a job ID of digits only", () => {
    expect(parseJobId("41")).toBe(41)
    expect(parseJobId("0")).toBe(0)
    for (const text of ["", "-1", "4.1", "41a", "9007199254740993"]) {
      expect(parseJobId(text)).toBeUndefined()
    }
  })

  it("polls every 2 s while the job is queued or running, never after it ended", () => {
    const running = makeJobDetail()
    expect(jobRefetchInterval({ ...running, status: "queued" }, null, 0)).toBe(
      2000
    )
    expect(jobRefetchInterval(running, null, 0)).toBe(2000)
    for (const status of ["finished", "failed", "cancelled"] as const) {
      expect(jobRefetchInterval({ ...running, status }, null, 0)).toBe(false)
    }
    expect(jobRefetchInterval(undefined, null, 0)).toBe(false)
  })

  it("polls a stale run every 30 s, backs off after failures and stops on a 404", () => {
    const running = makeJobDetail()
    const stale = { ...running, progress: { ...running.progress, stale: true } }
    expect(jobRefetchInterval(stale, null, 0)).toBe(30_000)
    const failed = new ApiError(503, "Service Unavailable")
    expect(jobRefetchInterval(running, failed, 1)).toBe(4000)
    expect(jobRefetchInterval(running, failed, 3)).toBe(16_000)
    expect(jobRefetchInterval(running, failed, 10)).toBe(60_000)
    expect(jobRefetchInterval(running, new ApiError(404, "Not Found"), 1)).toBe(
      false
    )
  })

  it("reads the log and the failures again when the status or the phase changes", () => {
    const running = makeJobDetail()
    expect(jobRevision(running)).toBe("running/running")
    expect(jobRevision({ ...running, phase: "finalizing" })).toBe(
      "running/finalizing"
    )
    expect(jobRevision({ status: "queued", phase: null })).toBe("queued/")
  })
})

describe("the phase note beside the status", () => {
  it("names the phase, _ as spaces", () => {
    expect(phaseNote("failed", "some_new_phase")).toBe("some new phase")
  })

  it("a run created or published, not on the queue yet: not yet queued", () => {
    expect(phaseNote("queued", "created")).toBe("not yet queued")
    expect(phaseNote("queued", "published")).toBe("not yet queued")
  })

  it("says nothing when the phase is the status, or unknown", () => {
    expect(phaseNote("running", "running")).toBeUndefined()
    expect(phaseNote("queued", "queued")).toBeUndefined()
    expect(phaseNote("finished", null)).toBeUndefined()
  })
})

describe("the Run Progress notes", () => {
  it("a running local run: its heartbeat's age against the stale limit", () => {
    expect(progressNotes(makeJobDetail())).toEqual([
      { text: "Last heartbeat 4 s ago (stale after 5 min).", tone: "muted" },
    ])
  })

  it("a stale heartbeat: the run process is probably gone", () => {
    const stale = withProgress(makeJobDetail(), {
      heartbeat_age_seconds: 905,
      stale: true,
    })
    expect(progressNotes(stale)).toEqual([
      {
        text: "No heartbeat for 15 min 5 s (limit 5 min): the run process is probably gone.",
        tone: "alert",
      },
    ])
    const silent = withProgress(makeJobDetail(), {
      heartbeat_age_seconds: null,
      stale: true,
    })
    expect(progressNotes(silent)[0]?.text).toMatch(
      /^No heartbeat recorded: the run process is probably gone\./
    )
    const starting = withProgress(makeJobDetail(), {
      heartbeat_age_seconds: null,
    })
    expect(progressNotes(starting)).toEqual([
      { text: "No heartbeat recorded yet.", tone: "muted" },
    ])
  })

  it("a stop requested, at its time when one is recorded", () => {
    const stopping = withProgress(makeJobDetail(), {
      stop_requested: true,
      stop_requested_at: "2026-10-19T15:10:00Z",
    })
    const [, stop] = progressNotes(stopping)
    expect(stop?.tone).toBe("warning")
    expect(stop?.text).toMatch(/^Stop requested at 2026-10-19 \d\d:10:00\.$/)
    const untimed = withProgress(makeRemoteJob(), {
      stop_requested: true,
      stop_requested_at: null,
    })
    expect(progressNotes(untimed)).toEqual([
      { text: "Stop requested.", tone: "warning" },
    ])
  })

  it("no heartbeat check for a remote run, nothing for a queued one", () => {
    expect(progressNotes(makeRemoteJob())).toEqual([])
    expect(
      progressNotes(
        makeJobDetail({
          status: "queued",
          phase: "queued",
          progress: {
            ...makeJobDetail().progress,
            heartbeat_age_seconds: null,
          },
        })
      )
    ).toEqual([])
  })

  it("an ended run: what it processed", () => {
    const ended = endedJob(makeJobDetail(), "cancelled", {
      succeeded: 137,
      failed: 0,
      notRun: 263,
    })
    expect(progressNotes(withProgress(ended, { n_inputs: 400 }))).toEqual([
      {
        text: "Processed 137 of 400 inputs: 137 succeeded, 0 failed, 263 not run.",
        tone: "muted",
      },
    ])
  })
})

describe("a live run's card", () => {
  const rows = (live = makeJobLive()) =>
    liveRows(live).map(({ label, text, warning }) =>
      warning ? [label, text, "warning"] : [label, text]
    )

  it("shows a notify run's source, folder, file name and arrivals, without an arrival status", () => {
    expect(rows()).toEqual([
      ["Source", "Notifications"],
      ["Folder", "/data/scan_1"],
      ["Filename Template", "frame_%d.h5"],
      ["Arrivals", "3"],
    ])
  })

  it("shows a DAQ run's catalog status: its last check, rows seen and error since when", () => {
    const live = makeJobLive({
      source: "daq",
      daq_id: "1a2b",
      arrival: {
        state: "available",
        checked_at: "2026-10-27T10:00:00+00:00",
        rows_seen: 1204,
        error: "1 catalog row(s) not used: x",
        error_at: "2026-10-27T09:59:00+00:00",
      },
    })
    expect(rows(live).slice(0, 1)).toEqual([["Source", "DAQ 1a2b"]])
    expect(rows(live).slice(4)).toEqual([
      ["Last Check", formatDateTime("2026-10-27T10:00:00+00:00")],
      ["Rows Seen", "1,204"],
      ["Error", "1 catalog row(s) not used: x", "warning"],
      ["Error Since", formatDateTime("2026-10-27T09:59:00+00:00")],
    ])
  })

  it("shows a follower's wire run, linked, and why it waits; none read is a state", () => {
    const follows = { kind: "reconstruction", id: 5, label: "R5" } as const
    const waiting = makeJobLive({
      source: "run",
      follows,
      follows_job_id: 40,
      arrival: {
        state: "available",
        checked_at: null,
        rows_seen: 2,
        error: "point 'wire_2' (line 2) not admitted yet: missing",
        error_at: null,
      },
    })
    expect(liveRows(waiting)[0]?.follows).toEqual({ run: follows, jobId: 40 })
    expect(rows(waiting).slice(4)).toEqual([
      ["Last Check", ""],
      ["Points Read", "2"],
      [
        "Waiting",
        "point 'wire_2' (line 2) not admitted yet: missing",
        "warning",
      ],
      ["Waiting Since", ""],
    ])
    const unread = makeJobLive({
      source: null,
      source_state: "not_responding",
      folder: null,
      filename_template: null,
      arrivals: null,
      arrivals_state: "unreadable",
      arrival: {
        state: "unreadable",
        checked_at: null,
        rows_seen: null,
        error: null,
        error_at: null,
      },
    })
    expect(rows(unread)).toEqual([
      ["Source", "Not responding", "warning"],
      ["Arrivals", "Unreadable", "warning"],
      ["Arrival Status", "Unreadable", "warning"],
    ])
  })

  it("shows a notify run's file without a number as its Error, with no catalog's rows", () => {
    const refused = makeJobLive({
      filename_template: null,
      arrival: {
        state: "available",
        checked_at: null,
        rows_seen: null,
        error: "Not taken: 'Si_dark.h5' has no number in its name",
        error_at: "2026-10-27T09:59:00+00:00",
      },
    })
    expect(rows(refused).slice(4)).toEqual([
      ["Error", "Not taken: 'Si_dark.h5' has no number in its name", "warning"],
      ["Error Since", formatDateTime("2026-10-27T09:59:00+00:00")],
    ])
  })

  it("shows a live run with no file name template's Filename Template as none", () => {
    const none = makeJobLive({ filename_template: null })
    expect(rows(none).slice(0, 3)).toEqual([
      ["Source", "Notifications"],
      ["Folder", none.folder],
      ["Filename Template", ""],
    ])
  })

  it("says a live run has no inputs yet, or none arrived, where a batch run selected none", () => {
    expect(noInputsText(makeJobDetail())).toBe("This job selected no inputs.")
    expect(noInputsText(makeJobDetail({ live: makeJobLive() }))).toBe(
      "No inputs yet."
    )
    expect(
      noInputsText(makeJobDetail({ live: makeJobLive(), status: "finished" }))
    ).toBe("No inputs arrived.")
  })
})

describe("the remote run", () => {
  it("shows the target via DM, DM's state and when the poller looked", () => {
    expect(remoteView(makeJobDetail())).toBeUndefined()
    expect(remoteView(makeRemoteJob())).toEqual({
      target: "APS via DM",
      phase: null,
      dmState: "running",
      globus: null,
      polled: "25 s ago",
    })
    // No DM state yet: "pending".
    const unsubmitted = makeRemoteJob({
      remote: {
        target: "aps",
        execution: "APS",
        dm_state: null,
        globus_url: null,
        observation: null,
      },
      progress: { ...makeRemoteJob().progress, heartbeat_age_seconds: null },
    })
    expect(remoteView(unsubmitted)).toMatchObject({
      dmState: "pending",
      polled: undefined,
    })
  })

  it("shows the Globus run for a crux run only, or that DM has not reported it", () => {
    // An APS run's Globus link, were DM to report one, is not shown.
    expect(makeRemoteJob().remote?.globus_url).not.toBeNull()
    expect(remoteView(makeRemoteJob())?.globus).toBeNull()
    expect(remoteView(makeCruxJob())?.globus).toEqual({
      url: "https://app.globus.org/runs/0c5e7a12-1d3b-4f6a-8e2c-9b7d5f3a1e60",
    })
    const unreported = makeCruxJob()
    if (unreported.remote) unreported.remote.globus_url = null
    expect(remoteView(unreported)?.globus).toEqual({ url: null })
  })

  it("shows each golden observation's phase text, At ALCF where the launcher cannot see the PBS job", () => {
    // Each read 30 s after it was observed, through the job API's shape (jobs.service.remote_observation).
    const shown = Object.fromEntries(
      Object.entries(goldenObservations()).map(([name, stored]) => {
        const asOf = new Date(
          Date.parse(String(stored.observed_at)) + 30_000
        ).toISOString()
        return [name, phaseText(remotePhaseView(apiView(stored, asOf), asOf))]
      })
    )
    // at-alcf and at-alcf-late (the launcher's stale: no PBS view for 90 s) both show At ALCF, not "status
    // delayed".
    expect(shown).toEqual({
      "at-alcf": "Queued · At ALCF",
      "at-alcf-late": "Queued · At ALCF",
      downlink: "Downlink · Returning results",
      finishing: "Running · Finishing processing · PBS 123456",
      "flow-succeeded": "Downlink · Returning results",
      "observer-stale": "Running · Processing · status delayed",
      "pbs-finished": "Downlink · Awaiting result return · PBS 123456",
      "pbs-held": "Queued · Held in PBS · PBS 123456",
      "pbs-queued": "Queued · Waiting for PBS allocation · PBS 123456",
      running: "Running · Processing · PBS 123456",
      "scheduler-unavailable":
        "Running · PBS status unavailable · PBS 123456 · status delayed",
      submitting: "Queued · Submitting to PBS · PBS 123456",
      uplink: "Uplink · Transferring inputs",
      "workflow-unavailable":
        "Running · Workflow status unavailable · status delayed",
      publishing: "Downlink · Publishing results",
      // Login needed
      "login-required": "Login needed · Sign in to Globus and resume this run",
      "login-required-on-the-run":
        "Login needed · Sign in to Globus and resume this run",
      "login-resumed": "Queued · Awaiting workflow",
      "inactive-without-login": "Uplink · Transferring inputs",
    })
  })

  it("delays At ALCF only when its launcher stopped: observed more than 90 s before the page", () => {
    const alcf = makeObservation({
      phase: "queued",
      detail: "Waiting for status",
      pbs_id: null,
      stale: true,
      observed_at: "2026-10-25T12:00:00Z",
    })
    expect(remotePhaseView(alcf, "2026-10-25T12:01:30Z")).toMatchObject({
      detail: "At ALCF",
      delayed: null,
    })
    expect(remotePhaseView(alcf, "2026-10-25T12:01:31Z")).toMatchObject({
      detail: "At ALCF",
      delayed: `Observed ${formatDateTime("2026-10-25T12:00:00Z")}`,
    })
    // Another Flow step, or a PBS ID known: the launcher's own words and its stale flag.
    expect(
      remotePhaseView(
        { ...alcf, flow_state: "InputTransfer" },
        alcf.observed_at
      )
    ).toMatchObject({
      detail: "Waiting for status",
      delayed: `Observed ${formatDateTime("2026-10-25T12:00:00Z")}`,
    })
    expect(
      remotePhaseView(
        { ...alcf, pbs_id: "123456.crux-pbs-01" },
        alcf.observed_at
      )
    ).toMatchObject({ detail: "Waiting for status", pbs: "PBS 123456" })
  })
})

// Read at run time, as scan-import.test.ts reads its shared cases (the base is a variable because Vite
// rewrites `new URL("…", import.meta.url)`).
const here = import.meta.url

/** The golden observations (the launcher's printed ones, and the poller's "Publishing results"). */
function goldenObservations(): Record<string, Record<string, unknown>> {
  const goldens = JSON.parse(
    readFileSync(
      new URL(
        "../../../../backend/tests/fixtures/observation_goldens/goldens.json",
        here
      ),
      "utf8"
    )
  ) as {
    observations: Record<string, string>
    publishing: { after: Record<string, unknown> }
  }
  const printed = Object.entries(goldens.observations).map(
    ([name, line]) =>
      [name, JSON.parse(line) as Record<string, unknown>] as const
  )
  return {
    ...Object.fromEntries(printed),
    publishing: goldens.publishing.after,
  }
}

/** A stored observation as the job API gives it at `asOf` (jobs.service.remote_observation). */
function apiView(
  stored: Record<string, unknown>,
  asOf: string
): RemoteObservation {
  const text = (value: unknown) => (typeof value === "string" ? value : null)
  const observedAt = String(stored.observed_at)
  const pbs = stored.pbs as { job_id?: unknown } | undefined
  const age = (Date.parse(asOf) - Date.parse(observedAt)) / 1000
  return {
    phase: stored.phase as RemotePhase,
    detail: text(stored.detail),
    pbs_id: text(pbs?.job_id),
    stale: Boolean(stored.stale) || age > REMOTE_DELAYED_AFTER_SECONDS,
    observed_at: observedAt,
    phase_since: text(stored.phase_since),
    flow_state: text(stored.flow_state),
  }
}

/** The card's phase row as text: "Running · Processing · PBS 123456 · status delayed". */
function phaseText(view: RemotePhaseView): string {
  return [view.label, view.detail, view.pbs, view.delayed && "status delayed"]
    .filter(Boolean)
    .join(" · ")
}

describe("the run files' states", () => {
  it("available, not recorded, unreadable with the reason", () => {
    expect(fileStateView(makeRunFile("run.json"), false)).toEqual({
      label: "Available",
      tone: "ok",
    })
    expect(
      fileStateView(
        makeRunFile("request.json", { state: "not_recorded", path: null }),
        false
      )
    ).toEqual({ label: "Not recorded", tone: "muted" })
    expect(
      fileStateView(
        makeRunFile("run.json", {
          state: "unreadable",
          detail: "larger than 4 MB",
        }),
        false
      )
    ).toEqual({
      label: "Unreadable",
      tone: "error",
      detail: "larger than 4 MB",
    })
  })

  it("missing: not available on this host, unless the active run writes it at its end", () => {
    const missing = (name: string) => makeRunFile(name, { state: "missing" })
    expect(fileStateView(missing("run.json"), false)).toEqual({
      label: "Not available on this host",
      tone: "warning",
    })
    expect(fileStateView(missing("run.json"), true)).toEqual({
      label: "Not written yet",
      tone: "muted",
    })
    // The request is written when the run is published: missing is missing, active or not.
    expect(fileStateView(missing("request.json"), true).label).toBe(
      "Not available on this host"
    )
    expect(fileStateView(missing("pbs-output.log"), true).label).toBe(
      "Not written yet"
    )
  })

  it("lists the run directory first, then the files read", () => {
    const configuration = makeConfiguration({
      run_directory: {
        path: "/net/fileserver/runs/index_9",
        state: "missing",
        detail: null,
      },
    })
    expect(
      runFiles(configuration).map((file) => [file.name, file.state])
    ).toEqual([
      ["run directory", "missing"],
      ["request.json", "available"],
      ["run.json", "missing"],
    ])
  })
})

describe("the configuration's rows", () => {
  it("formats each kind of value", () => {
    expect(formatValue(null)).toBe("—")
    expect(formatValue("")).toBe("—")
    expect(formatValue(true)).toBe("yes")
    expect(formatValue(false)).toBe("no")
    expect(formatValue(["a.h5", "b.h5"])).toBe("a.h5, b.h5")
    expect(formatValue([])).toBe("—")
    expect(formatValue(0.5)).toBe("0.5")
    expect(formatValue({ cone_deg: 72 })).toBe('{"cone_deg":72}')
  })

  it("the submitted request: its parameters, its output and its manifest", () => {
    const { request } = makeConfiguration()
    if (!request) throw new Error("no request")
    expect(requestRows(request)).toEqual([
      ["scan number", "990012"],
      ["filename prefixes", "Ni_%d.h5"],
      ["scan points", "1-441"],
      [
        "input path",
        "/net/fileserver/export/data1/runs/scan_990012/rec_41/reconstruction",
      ],
      ["box size", "5"],
      ["max rfactor", "0.5"],
      ["smooth", "no"],
      ["threshold", "—"],
      ["author", "jdoe"],
      [
        "output directory",
        "/net/fileserver/export/data1/runs/scan_990012/index_72",
      ],
      ["output results", "output.h5"],
      ["manifest", "inputs.jsonl (441 inputs)"],
      [
        "manifest sha256",
        "0f3a9c5e2b7d4f1a8c6e0b9d2f4a7c1e3b5d8f0a2c4e6b8d0f1a3c5e7b9d1f2a",
      ],
    ])
  })

  it("execution and versions, and a migrated run's history", () => {
    expect(
      executionRows({
        provenance: { lauelab_version: "0.1.0" },
        policy: { max_in_flight: 8 },
        host: "compute-host",
        started_at: "2026-10-19T10:00:00",
        finished_at: null,
        n_indexed: 0,
      })
    ).toEqual([
      ["lauelab version", "0.1.0"],
      ["max in flight", "8"],
      ["host", "compute-host"],
      ["executed", "2026-10-19T10:00:00 to —"],
      ["frames indexed", "0"],
    ])
    expect(
      historyRows({
        original_status_name: "Finished",
        first_input_at: "2025-03-02 10:00:00",
        last_input_at: null,
        message_classes: [
          { message: "No peaks found", count: 12 },
          { message: "Out of memory", count: 1 },
        ],
      })
    ).toEqual([
      ["recorded status", "Finished"],
      ["per-input work", "2025-03-02 10:00:00 to —"],
      ["12 inputs", "No peaks found"],
      ["1 input", "Out of memory"],
    ])
  })

  it("sets paths, file names, hashes and recorded objects in monospace, not numbers or words", () => {
    const code = (rows: Row[]) => rows.map(isCodeRow)
    expect(
      code([
        ["input path", "/data/scan_1"],
        ["filename prefixes", "Ni_%d.h5"],
        ["output results", "output.h5"],
        ["manifest sha256", "0f3a"],
        ["lauelab commit", "ab12cd"],
        ["results summary", '{"n":1}'],
      ])
    ).toEqual([true, true, true, true, true, true])
    expect(
      code([
        ["box size", "5"],
        ["smooth", "no"],
        ["host", "compute-host"],
        ["mask file", "—"],
      ])
    ).toEqual([false, false, false, false])
  })

  it("the partial results note, by what was published", () => {
    const failed = endedJob(makeJobDetail(), "failed", {
      succeeded: 100,
      failed: 341,
      notRun: 0,
    }).configuration
    expect(partialResultsNote(failed)).toMatch(
      /^Partial results: the inputs that succeeded\.$/
    )
    expect(
      partialResultsNote({
        ...failed,
        artifacts: [
          {
            name: "reconstruction",
            path: "/x/reconstruction",
            viewer: "reconstruction",
          },
        ],
      })
    ).toBe("Partial results: the completed point files.")
    expect(
      partialResultsNote({ ...failed, partial_results: false })
    ).toBeUndefined()
  })

  it("links an artifact to its viewer, the job's own run", () => {
    const run = { kind: "indexing", id: 72, label: "I72" } as const
    expect(
      artifactLink(
        { name: "results", path: "/r/output.h5", viewer: "indexing_results" },
        run
      )
    ).toEqual({ to: "/indexing/$indexingId", params: { indexingId: "72" } })
    expect(
      artifactLink({ name: "xml", path: "/r/output.xml", viewer: null }, run)
    ).toBeUndefined()
    expect(
      artifactLink(
        {
          name: "reconstruction",
          path: "/r/reconstruction",
          viewer: "reconstruction",
        },
        { kind: "reconstruction", id: 3, label: "R3" }
      )
    ).toEqual({
      to: "/reconstructions/$reconstructionId",
      params: { reconstructionId: "3" },
    })
  })

  it("titles a log by what it shows", () => {
    expect(logTitle("Run log", 3, 3, false)).toBe("Run log")
    expect(logTitle("Run log", 50, 120, true)).toBe(
      "Run log (last 50 of 120 lines)"
    )
    expect(logTitle("PBS output", 50, null, true)).toBe(
      "PBS output (last 50 lines)"
    )
  })
})

describe("the failed inputs' pages", () => {
  it("pages the report 50 at a time, with its range text", () => {
    expect(failurePaging(failurePage(263, 0, 50), 0)).toEqual({
      text: "Showing 1–50 of 263",
      previous: null,
      next: 50,
    })
    expect(failurePaging(failurePage(263, 50, 50), 50)).toEqual({
      text: "Showing 51–100 of 263",
      previous: 0,
      next: 100,
    })
    expect(failurePaging(failurePage(263, 250, 50), 250)).toEqual({
      text: "Showing 251–263 of 263",
      previous: 200,
      next: null,
    })
    // A report longer than the counters say (a migrated run's): no total.
    const longer = { ...failurePage(80, 50, 50), total: 60 }
    expect(failurePaging(longer, 50).text).toBe("Showing 51–80")
  })

  it("says why a page of an ended run's report is empty", () => {
    const report = makeRunFile("failures.jsonl")
    expect(failuresEmptyText(failurePage(3, 0, 50), 0)).toBeUndefined()
    expect(failuresEmptyText(emptyFailurePage(0, report), 0)).toBe(
      "No failed or unattempted inputs."
    )
    const notRecorded = makeRunFile("failures.jsonl", {
      state: "not_recorded",
      path: null,
    })
    expect(failuresEmptyText(emptyFailurePage(3, notRecorded), 0)).toBe(
      "3 inputs did not succeed, but the run recorded no failure report."
    )
    const missing = makeRunFile("failures.jsonl", { state: "missing" })
    expect(failuresEmptyText(emptyFailurePage(1, missing), 0)).toBe(
      "1 input did not succeed, but the failure report is not available on this host."
    )
    const unreadable = makeRunFile("failures.jsonl", {
      state: "unreadable",
      detail: "record 3 is not valid JSON",
    })
    expect(failuresEmptyText(emptyFailurePage(5, unreadable), 0)).toBe(
      "5 inputs did not succeed, but the failure report cannot be read: record 3 is not valid JSON."
    )
    expect(failuresEmptyText(emptyFailurePage(5, report), 50)).toBe(
      "No more records."
    )
  })

  it("counts an active run's failed inputs from its live counter, not the page's", () => {
    const notRecorded = makeRunFile("failures.jsonl", {
      state: "not_recorded",
      path: null,
    })
    // The page was read with 3 failed; the counter says 7 now.
    expect(failuresEmptyText(emptyFailurePage(3, notRecorded), 0, 7)).toBe(
      "7 inputs failed so far: the failure report is written when the run ends."
    )
    expect(failuresEmptyText(emptyFailurePage(0, notRecorded), 0, 1)).toBe(
      "1 input failed so far: the failure report is written when the run ends."
    )
    expect(failuresEmptyText(emptyFailurePage(0, notRecorded), 0, 0)).toBe(
      "No failed inputs so far."
    )
    const unreadable = makeRunFile("failures.jsonl", {
      state: "unreadable",
      detail: "not a regular file",
    })
    expect(failuresEmptyText(emptyFailurePage(2, unreadable), 0, 4)).toBe(
      "4 inputs did not succeed, but the failure report cannot be read: not a regular file."
    )
  })

  it("colours the categories and shortens long messages", () => {
    expect(categoryTone("input")).toBe("warning")
    for (const category of ["numerical", "memory", "error"]) {
      expect(categoryTone(category)).toBe("error")
    }
    for (const category of ["cancelled", "interrupted", "not_run", "new"]) {
      expect(categoryTone(category)).toBe("muted")
    }
    expect(shortMessage("short")).toBe("short")
    const long = shortMessage("x".repeat(400))
    expect(long).toHaveLength(300)
    expect(long.endsWith("…")).toBe(true)
  })
})
