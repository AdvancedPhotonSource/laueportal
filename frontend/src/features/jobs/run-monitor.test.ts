import { describe, expect, it, vi } from "vitest"

import { ApiError, NetworkError, type CancelResult } from "@/api"
import { makeJobSummary } from "@/test/jobs"
import {
  jobName,
  jobRef,
  leftAlone,
  LIVE_STOP_EXPLANATION,
  moveErrorLine,
  moveOrder,
  moveReport,
  runEach,
  runMonitorActions,
  stopErrorLine,
  stopExplanation,
  STOP_EXPLANATION,
  stopLine,
  stopReport,
  stopTitle,
  type JobRef,
} from "./run-monitor"

// The run monitor's rules and the toasts' lines: Stop for queued and running jobs, Move to Front
// for queued ones, and what each job's result or error says.

const QUEUED = makeJobSummary(7, { status: "queued", phase: "queued" })
const RUNNING = makeJobSummary(5, { status: "running", phase: "running" })
const FINISHED = makeJobSummary(3)
const REMOTE = makeJobSummary(9, {
  status: "running",
  target: "aps",
  dm_state: "running",
  run: { kind: "reconstruction", id: 12, label: "R12" },
})

const local: JobRef = jobRef(RUNNING)
const remote: JobRef = jobRef(REMOTE)

function problem(status: number, title: string, detail?: string): ApiError {
  return new ApiError(status, "", { status, title, detail })
}

describe("the buttons' rules", () => {
  it("enables Stop for queued and running jobs and Move to Front for queued ones only", () => {
    expect(runMonitorActions([])).toEqual({
      count: 0,
      stoppable: [],
      movable: [],
    })
    const actions = runMonitorActions([QUEUED, FINISHED, RUNNING])
    expect(actions.count).toBe(3)
    // By job ID, whatever the order of selection.
    expect(actions.stoppable.map((job) => job.jobId)).toEqual([5, 7])
    expect(actions.movable.map((job) => job.jobId)).toEqual([7])
  })

  it("leaves both off for ended jobs", () => {
    const ended = ["finished", "failed", "cancelled"] as const
    const actions = runMonitorActions(
      ended.map((status, index) => makeJobSummary(index + 1, { status }))
    )
    expect(actions).toMatchObject({ count: 3, stoppable: [], movable: [] })
  })

  it("Move to Front moves the newest first, so the jobs keep their order ahead of the others", () => {
    const jobs = runMonitorActions([
      makeJobSummary(8, { status: "queued" }),
      makeJobSummary(6, { status: "queued" }),
      makeJobSummary(7, { status: "queued" }),
    ]).movable
    expect(moveOrder(jobs).map((job) => job.jobId)).toEqual([8, 7, 6])
  })

  it("names the jobs and the selected ones an action leaves alone", () => {
    expect(jobName(local)).toBe("Job 5 (I36)")
    expect(jobName({ ...local, run: null })).toBe("Job 5")
    expect(leftAlone(0, "ended")).toBeUndefined()
    expect(leftAlone(1, "ended")).toBe(
      "1 selected run has ended and is left alone."
    )
    expect(leftAlone(2, "not queued")).toBe(
      "2 selected runs are not queued and are left alone."
    )
    expect(stopTitle(1)).toBe("Stop 1 run?")
    expect(stopTitle(3)).toBe("Stop 3 runs?")
  })
})

describe("the Stop toast's line per job", () => {
  const result = (overrides: Partial<CancelResult>): CancelResult => ({
    job_id: 5,
    state: "cancelled",
    n_pending: 0,
    ...overrides,
  })

  it("a run that had not started is cancelled at once", () => {
    expect(stopLine(local, result({ state: "cancelled" }))).toEqual({
      jobId: 5,
      tone: "success",
      text: "Job 5 (I36) is cancelled; none of its inputs were processed.",
    })
  })

  it("a running run: stop requested, at most its pending inputs to be recorded as not run", () => {
    // n_pending is counted from counters that lag behind the run, and the inputs in flight still finish: the
    // inputs recorded as not run are at most that many (e.g. 400 counted, 381 recorded).
    expect(
      stopLine(local, result({ state: "requested", n_pending: 294 }))
    ).toEqual({
      jobId: 5,
      tone: "success",
      text: "Job 5 (I36): stop requested. The inputs in flight finish and completed results are kept; up to 294 pending inputs will be recorded as not run.",
    })
    expect(
      stopLine(local, result({ state: "requested", n_pending: 1 })).text
    ).toContain("; up to 1 pending input will be recorded as not run.")
    expect(
      stopLine(local, result({ state: "requested", n_pending: 0 })).text
    ).toBe(
      "Job 5 (I36): stop requested. The inputs in flight finish and completed results are kept; no pending input is left to record as not run."
    )
  })

  it("a remote run: the remote poller delivers the stop", () => {
    expect(
      stopLine(
        remote,
        result({ job_id: 9, state: "requested", n_pending: 441 })
      ).text
    ).toBe(
      "Job 9 (R12): stop requested; the remote poller delivers it to APS. Completed results are kept; up to 441 pending inputs will be recorded as not run."
    )
  })

  it("a live run's Stop says it is sealed, in the dialog (a note beside batch runs) and the report", () => {
    // No new files are taken, the taken ones are finished, and it ends Finished, not Cancelled with
    // inputs not run (the run monitor's rows say which runs are open live runs, JobSummary.live).
    const live = jobRef(
      makeJobSummary(4, { status: "running", phase: "running", live: true })
    )
    expect(stopExplanation([live])).toEqual({
      description: LIVE_STOP_EXPLANATION,
    })
    expect(stopExplanation([live, local])).toEqual({
      description: STOP_EXPLANATION,
      note: `Live runs: ${LIVE_STOP_EXPLANATION}`,
    })
    expect(stopExplanation([local])).toEqual({
      description: STOP_EXPLANATION,
    })
    expect(
      stopLine(live, result({ job_id: 4, state: "requested", n_pending: 3 }))
        .text
    ).toBe(`Job 4 (I35): stop requested. ${LIVE_STOP_EXPLANATION}`)
  })

  it("a run that had already ended, or that is gone", () => {
    expect(stopLine(local, result({ state: "already_terminal" }))).toEqual({
      jobId: 5,
      tone: "info",
      text: "Job 5 (I36) had already ended; nothing changed.",
    })
    expect(stopErrorLine(local, problem(404, "Job not found"))).toEqual({
      jobId: 5,
      tone: "error",
      text: "Job 5 (I36) no longer exists.",
    })
  })

  it("a request that failed says why", () => {
    expect(
      stopErrorLine(
        local,
        problem(500, "Internal Server Error", "Unexpected error")
      ).text
    ).toBe(
      "Job 5 (I36) was not stopped: Internal Server Error: Unexpected error."
    )
    expect(stopErrorLine(local, new NetworkError(new TypeError())).text).toBe(
      "Job 5 (I36) was not stopped: the server cannot be reached."
    )
  })

  it("reports every job, by job ID, in one toast whose tone is the worst line's", () => {
    const queued = jobRef(QUEUED)
    const report = stopReport([
      {
        job: queued,
        ok: true,
        value: result({ job_id: 7 }),
      },
      {
        job: local,
        ok: true,
        value: result({ state: "requested", n_pending: 3 }),
      },
    ])
    expect(report.title).toBe("Stop Runs")
    expect(report.tone).toBe("success")
    expect(report.lines.map((line) => line.jobId)).toEqual([5, 7])

    const partly = stopReport([
      {
        job: queued,
        ok: true,
        value: result({ job_id: 7 }),
      },
      { job: local, ok: false, error: problem(500, "Internal Server Error") },
    ])
    expect(partly.tone).toBe("warning")
    expect(
      stopReport([{ job: local, ok: false, error: new NetworkError(null) }])
        .tone
    ).toBe("error")
    expect(
      stopReport([
        { job: local, ok: true, value: result({ state: "already_terminal" }) },
      ]).tone
    ).toBe("info")
  })
})

describe("the Move to Front toast's line per job", () => {
  const queued = jobRef(QUEUED)
  const queuedRemote = jobRef({ ...REMOTE, status: "queued" })

  it("a run the worker took already is no longer queued (409)", () => {
    expect(
      moveErrorLine(queued, problem(409, "Job not queued", "prose"))
    ).toEqual({
      jobId: 7,
      tone: "error",
      text: "Job 7 (I38) was not moved: it is no longer waiting in the queue.",
    })
  })

  it("a remote run is ordered by its scheduler", () => {
    expect(
      moveErrorLine(queuedRemote, problem(409, "Job runs remotely")).text
    ).toBe(
      "Job 9 (R12) was not moved: it runs on APS, whose scheduler orders it."
    )
  })

  it("the database missing (503), a job that is gone (404), anything else", () => {
    const detail =
      "The database file /data/portal.db does not exist; create it with `laueportal db upgrade`."
    expect(
      moveErrorLine(queued, problem(503, "Database unavailable", detail)).text
    ).toBe(`Job 7 (I38) was not moved: Database unavailable: ${detail}.`)
    expect(moveErrorLine(queued, problem(404, "Job not found")).text).toBe(
      "Job 7 (I38) no longer exists."
    )
    expect(
      moveErrorLine(queued, problem(409, "Something new", "A new conflict"))
        .text
    ).toBe("Job 7 (I38) was not moved: Something new: A new conflict.")
    expect(moveErrorLine(queued, new NetworkError(null)).text).toBe(
      "Job 7 (I38) was not moved: the server cannot be reached."
    )
  })

  it("reports the moved jobs and the others", () => {
    const report = moveReport([
      { job: queued, ok: true, value: undefined },
      {
        job: queuedRemote,
        ok: false,
        error: problem(409, "Job runs remotely"),
      },
    ])
    expect(report).toEqual({
      title: "Move to Front",
      tone: "warning",
      lines: [
        {
          jobId: 7,
          tone: "success",
          text: "Job 7 (I38) moved to the front of the queue.",
        },
        {
          jobId: 9,
          tone: "error",
          text: "Job 9 (R12) was not moved: it runs on APS, whose scheduler orders it.",
        },
      ],
    })
  })
})

describe("runEach", () => {
  it("acts on one job at a time and goes on after a failure", async () => {
    const jobs = [jobRef(RUNNING), jobRef(QUEUED), jobRef(FINISHED)]
    let running = 0
    let most = 0
    const action = vi.fn(async (jobId: number) => {
      running += 1
      most = Math.max(most, running)
      await Promise.resolve()
      running -= 1
      if (jobId === 7) throw problem(409, "Job not queued")
      return jobId * 10
    })
    const outcomes = await runEach(jobs, action)
    expect(action.mock.calls.map(([jobId]) => jobId)).toEqual([5, 7, 3])
    expect(most).toBe(1)
    expect(outcomes.map((outcome) => outcome.ok)).toEqual([true, false, true])
    expect(outcomes[2]).toMatchObject({ ok: true, value: 30 })
  })
})
