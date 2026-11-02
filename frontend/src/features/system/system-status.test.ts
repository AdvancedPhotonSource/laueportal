import { describe, expect, it } from "vitest"

import type { ActiveRun } from "@/api"
import { makeSystemStatus, makeThread, makeWorker } from "@/test/system"
import {
  activeRunsView,
  activeRunView,
  connectionView,
  heartbeatText,
  queueView,
  systemAlerts,
  threadView,
  workerView,
} from "./system-status"

const STATUS = makeSystemStatus()

function run(overrides: Partial<ActiveRun> = {}): ActiveRun {
  return {
    job_id: 12,
    run: { kind: "indexing", id: 70, label: "I70" },
    status: "running",
    phase: "running",
    n_inputs: 10,
    n_succeeded: 3,
    n_failed: 1,
    n_not_run: 0,
    heartbeat_age_seconds: 4,
    stale: false,
    stop_requested: false,
    remote: null,
    ...overrides,
  }
}

describe("connectionView", () => {
  it("shows the database with its file and the API's version", () => {
    expect(connectionView(STATUS)).toEqual([
      {
        name: "Database",
        tone: "ok",
        state: "OK",
        detail: "/data/laueportal.sqlite",
      },
      { name: "API", tone: "ok", state: "OK", detail: "Version 2.0.0.dev0" },
    ])
  })

  it("marks the database down", () => {
    const view = connectionView({
      ...STATUS,
      database: { status: "error", path: "/data/missing.sqlite" },
    })
    expect(view.map(({ name, tone, state }) => [name, tone, state])).toEqual([
      ["Database", "error", "Error"],
      ["API", "ok", "OK"],
    ])
    expect(view[0]?.detail).toBe("/data/missing.sqlite")
  })
})

describe("workerView", () => {
  it("shows a running worker's host, PID and start, and its threads", () => {
    const view = workerView(STATUS)
    expect(view).toMatchObject({ kind: "worker", tone: "ok", state: "Running" })
    if (view.kind !== "worker") return
    expect(view.rows.map((row) => [row.label, row.mono ?? false])).toEqual([
      ["Host", true],
      ["PID", false],
      ["Started", false],
    ])
    expect(view.rows[0]?.value).toBe("beamline-host")
    expect(view.rows[1]?.value).toBe("4321")
    expect(view.rows[2]?.value).toMatch(/^2026-10-2[45] \d\d:00:00$/)
    expect(view.threads).toEqual([
      {
        name: "Dispatcher",
        tone: "ok",
        state: "OK",
        mark: "4 s ago",
        hint: "Late after 45 s",
      },
      {
        name: "Reconciler",
        tone: "ok",
        state: "OK",
        mark: "30 s ago",
        hint: "Late after 3 min",
      },
      {
        name: "Remote poller",
        tone: "unknown",
        state: "Off",
        mark: "None",
      },
    ])
  })

  it.each([
    ["late", "warning", "Late"],
    ["not_running", "error", "Not running"],
  ] as const)("shows a %s worker", (state, tone, label) => {
    expect(
      workerView({ ...STATUS, worker: makeWorker({ state }) })
    ).toMatchObject({ kind: "worker", tone, state: label })
  })

  it("adds the stop once recorded, and nothing of a worker that never started", () => {
    const stopped = workerView({
      ...STATUS,
      worker: makeWorker({
        state: "not_running",
        stopped_at: "2026-10-25T09:00:00Z",
      }),
    })
    expect(
      stopped.kind === "worker" && stopped.rows.map((row) => row.label)
    ).toEqual(["Host", "PID", "Started", "Stopped"])
    const never = workerView({
      ...STATUS,
      worker: makeWorker({
        state: "not_running",
        host: null,
        pid: null,
        started_at: null,
      }),
    })
    expect(never).toMatchObject({ kind: "worker", rows: [] })
  })

  it("knows nothing of the worker while the database is down", () => {
    expect(workerView({ ...STATUS, worker: null })).toEqual({
      kind: "unknown",
    })
  })
})

describe("threadView", () => {
  it("shows a late thread with its limit", () => {
    expect(
      threadView(
        makeThread("poller", {
          state: "late",
          last_mark_at: "2026-10-25T09:58:00Z",
          last_mark_age_seconds: 125,
          late_after_seconds: 60,
        })
      )
    ).toEqual({
      name: "Remote poller",
      tone: "warning",
      state: "Late",
      mark: "2 min 5 s ago",
      hint: "Late after 1 min",
    })
  })
})

describe("queueView", () => {
  it("counts the queue and lists the next items in the API's order", () => {
    const view = queueView({
      ...STATUS,
      queue: {
        queued_runs: 3,
        running: 1,
        queued_conversions: 1,
        live_slots_in_use: 1,
        live_slots: 2,
      },
      next_up: [
        {
          kind: "conversion",
          job_id: null,
          run: { kind: "indexing", id: 70, label: "I70" },
          moved_to_front: false,
        },
        {
          kind: "run",
          job_id: 15,
          run: { kind: "reconstruction", id: 9, label: "R9" },
          moved_to_front: true,
        },
        { kind: "run", job_id: 14, run: null, moved_to_front: false },
      ],
    })
    expect(view).toEqual({
      kind: "queue",
      counts: [
        { label: "Queued runs", value: 3 },
        { label: "Running", value: 1 },
        { label: "Queued conversions", value: 1 },
        { label: "Live slots", value: "1/2" },
      ],
      next: [
        {
          key: "conversion-70",
          run: { kind: "indexing", id: 70, label: "I70" },
          jobId: null,
          conversion: true,
          movedToFront: false,
        },
        {
          key: "run-15",
          run: { kind: "reconstruction", id: 9, label: "R9" },
          jobId: 15,
          conversion: false,
          movedToFront: true,
        },
        {
          key: "run-14",
          run: null,
          jobId: 14,
          conversion: false,
          movedToFront: false,
        },
      ],
    })
  })

  it("knows nothing of the queue while the database is down", () => {
    expect(queueView({ ...STATUS, queue: null, next_up: null })).toEqual({
      kind: "unknown",
    })
  })
})

describe("systemAlerts", () => {
  it("has none while everything answers", () => {
    expect(systemAlerts(STATUS)).toEqual([])
    expect(
      systemAlerts({ ...STATUS, worker: makeWorker({ state: "late" }) })
    ).toEqual([])
  })

  it("says plainly that the database is down or the worker is not running", () => {
    const down = systemAlerts({
      ...STATUS,
      database: { status: "error", path: "/data/missing.sqlite" },
      worker: null,
      queue: null,
      next_up: null,
      active_runs: null,
    })
    expect(down.map((alert) => alert.title)).toEqual([
      "The database does not answer",
    ])
    expect(down[0]?.detail).toBe("/data/missing.sqlite cannot be read")
    expect(
      systemAlerts({ ...STATUS, worker: makeWorker({ state: "not_running" }) })
    ).toEqual([{ title: "The worker is not running" }])
  })
})

describe("activeRunsView", () => {
  it("says when nothing is queued or running, and when the runs cannot be read", () => {
    expect(activeRunsView(STATUS)).toEqual({ kind: "none" })
    expect(activeRunsView({ ...STATUS, active_runs: null })).toEqual({
      kind: "unknown",
    })
  })

  it("keeps the API's order (newest first) and notes a stale run", () => {
    const view = activeRunsView({
      ...STATUS,
      active_runs: [
        run({ job_id: 14, status: "queued", heartbeat_age_seconds: null }),
        run({ job_id: 13, stale: true, heartbeat_age_seconds: 400 }),
      ],
    })
    expect(view.kind).toBe("runs")
    if (view.kind !== "runs") return
    expect(view.runs.map((row) => row.jobId)).toEqual([14, 13])
    expect(view.stale).toBe(true)
  })
})

describe("activeRunView", () => {
  it("shows a local run's links, counters and heartbeat", () => {
    expect(activeRunView(run())).toEqual({
      jobId: 12,
      run: { kind: "indexing", id: 70, label: "I70" },
      status: "running",
      counts: { n_inputs: 10, n_succeeded: 3, n_failed: 1, n_not_run: 0 },
      processed: 4,
      heartbeat: "4 s ago",
      stale: false,
      stopRequested: false,
      remote: null,
    })
  })

  it("carries the stale and stop-requested flags", () => {
    const view = activeRunView(
      run({ stale: true, stop_requested: true, heartbeat_age_seconds: 905 })
    )
    expect(view).toMatchObject({
      stale: true,
      stopRequested: true,
      heartbeat: "15 min 5 s ago",
    })
  })

  it("shows a remote run's target and DM's state", () => {
    expect(
      activeRunView(run({ remote: { target: "aps", dm_state: "running" } }))
        .remote
    ).toEqual({ target: "aps", dmState: "running" })
    expect(
      activeRunView(run({ remote: { target: "crux", dm_state: null } })).remote
    ).toEqual({ target: "crux", dmState: "not submitted" })
  })
})

describe("heartbeatText", () => {
  it("says how long ago the heartbeat came, the poller's for a remote run", () => {
    expect(heartbeatText(run({ heartbeat_age_seconds: 75 }))).toBe(
      "1 min 15 s ago"
    )
    expect(
      heartbeatText(
        run({
          heartbeat_age_seconds: 12,
          remote: { target: "aps", dm_state: "running" },
        })
      )
    ).toBe("Polled 12 s ago")
  })

  it("says when there is none yet", () => {
    expect(
      heartbeatText(run({ status: "queued", heartbeat_age_seconds: null }))
    ).toBe("Not started")
    expect(
      heartbeatText(run({ status: "running", heartbeat_age_seconds: null }))
    ).toBe("None")
  })
})
