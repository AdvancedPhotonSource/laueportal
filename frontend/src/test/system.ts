// Test data for the status page and the navbar's Worker indicator (GET /api/v1/system/status and
// /health).
import type { SystemStatus, WorkerStatus, WorkerThread } from "@/api"

/** A thread as a running worker without remote execution reports it. */
export function makeThread(
  name: WorkerThread["name"],
  overrides: Partial<WorkerThread> = {}
): WorkerThread {
  const poller = name === "poller"
  return {
    name,
    state: poller ? "off" : "ok",
    last_mark_at: poller ? null : "2026-10-25T09:59:56Z",
    last_mark_age_seconds: poller ? null : 4,
    late_after_seconds: poller ? null : name === "reconciler" ? 180 : 45,
    ...overrides,
  }
}

/** A running worker on `beamline-host`, its poller off (no remote execution). */
export function makeWorker(
  overrides: Partial<WorkerStatus> = {}
): WorkerStatus {
  return {
    state: "running",
    host: "beamline-host",
    pid: 4321,
    started_at: "2026-10-25T08:00:00Z",
    stopped_at: null,
    threads: [
      makeThread("dispatcher"),
      makeThread("reconciler", { last_mark_age_seconds: 30 }),
      makeThread("poller"),
    ],
    ...overrides,
  }
}

/** The status of a healthy portal with an empty queue and no active run. */
export function makeSystemStatus(
  overrides: Partial<SystemStatus> = {}
): SystemStatus {
  return {
    database: { status: "ok", path: "/data/laueportal.sqlite" },
    worker: makeWorker(),
    queue: {
      queued_runs: 0,
      running: 0,
      queued_conversions: 0,
      live_slots_in_use: 0,
      live_slots: 2,
    },
    next_up: [],
    version: "2.0.0.dev0",
    active_runs: [],
    ...overrides,
  }
}
