import { describe, expect, it } from "vitest"

import {
  ApiError,
  type LiveDaqEntry,
  type LiveSession,
  type LiveSessionRun,
} from "@/api"
import { formValuesOf } from "@/features/program-form"
import { specOf } from "@/test/programs"
import {
  blankScan,
  cardSpec,
  conflictJobs,
  daqChoice,
  daqFolder,
  daqListWait,
  EVERY_H5,
  followedOutput,
  type LiveSpecs,
  mirroredOutput,
  outputFollows,
  namedSessionEnded,
  NOTIFY,
  openRuns,
  placeSessionIssues,
  scanFields,
  scanValuesOf,
  sessionRequest,
  sessionState,
  shownSections,
  shownSession,
  waitingReason,
} from "./live-form"

// The Live page's rules: its request, the issues on its cards, the session's state, the DAQ's folder, the
// DAQ list's pace, and a session's values for the form.

const wire = specOf("wire-live")
const lauego = specOf("lauego-live")
const SPECS: LiveSpecs = {
  scan: scanFields(wire),
  reconstruction: cardSpec(wire),
  indexing: cardSpec(lauego),
}

function daq(values: Partial<LiveDaqEntry> = {}): LiveDaqEntry {
  return {
    id: "d1",
    experiment: "e1",
    status: "Running",
    data_directory: "/detector/e1/raw",
    storage_directory: "/data/e1",
    dest_directory: "34ide",
    destination: "/data/e1/34ide",
    ...values,
  }
}

const RUN: LiveSessionRun = {
  job_id: 7,
  run: { kind: "reconstruction", id: 3, label: "R3" },
  status: "running",
  n_inputs: 0,
  n_succeeded: 0,
  n_failed: 0,
  stop_requested: false,
  stopped_now: false,
  arrival: null,
}

function session(values: Partial<LiveSession> = {}): LiveSession {
  return {
    open: true,
    stopped: false,
    runs: [RUN],
    scan: {
      source: "daq",
      daq_id: "d1",
      folder: "/data/e1/34ide",
      filename_template: "wire_%d.h5",
      output_folder: "/data/out",
      scan_number: 12,
      author: "alice",
    },
    ...values,
  }
}

describe("the Live page's form", () => {
  it("the cards leave out what the Scan card sets; the LaueGo card's depths only while it follows", () => {
    for (const card of [SPECS.reconstruction, SPECS.indexing]) {
      for (const name of [
        "scan_number",
        "author",
        "input_path",
        "filename_prefixes",
        "output_path_template",
        "reconstruction_id",
      ]) {
        expect(card.fields).not.toHaveProperty(name)
      }
    }
    expect(SPECS.indexing.fields).toHaveProperty("output_xml")
    const names = (follower: boolean) =>
      shownSections(SPECS.indexing, follower).flatMap((section) =>
        section.fields.map((field) => field.name)
      )
    expect(names(true)).toContain("depth_range")
    expect(names(false)).not.toContain("depth_range")
    expect(names(false)).toEqual(
      expect.arrayContaining(["geometry_file", "crystal_file", "output_xml"])
    )
  })

  it("the request is the Scan card (a DAQ by ID) and each part on, a lone LaueGo run without depths", () => {
    const values = {
      scan: {
        ...blankScan(SPECS.scan),
        source: daqChoice("d1"),
        folder: "/data/e1/34ide",
        filename_template: "wire_%d.h5",
        output_folder: "/data/out",
        scan_number: "12",
        author: "alice",
      },
      reconstruction: formValuesOf(SPECS.reconstruction),
      indexing: { ...formValuesOf(SPECS.indexing), depth_range: "1-5" },
    }
    const both = sessionRequest(SPECS, values, {
      reconstruction: true,
      indexing: true,
    })
    expect(both.scan).toEqual({
      source: "daq",
      daq_id: "d1",
      folder: "/data/e1/34ide",
      filename_template: "wire_%d.h5",
      output_folder: "/data/out",
      scan_number: 12,
      author: "alice",
    })
    expect(Object.keys(both.reconstruction as object).sort()).toEqual(
      Object.keys(SPECS.reconstruction.fields).sort()
    )
    expect((both.indexing as Record<string, unknown>).depth_range).toBe("1-5")

    const alone = sessionRequest(
      SPECS,
      { ...values, scan: { ...values.scan, source: NOTIFY } },
      { reconstruction: false, indexing: true }
    )
    expect(alone.reconstruction).toBeNull()
    expect((alone.indexing as Record<string, unknown>).depth_range).toBeNull()
    expect(alone.scan).toMatchObject({ source: "notify", daq_id: null })
  })

  it("each issue goes on its card's field (a DAQ's on Source), an unplaced one on the form, errors first", () => {
    const placed = placeSessionIssues(SPECS, [
      {
        loc: ["indexing", "geometry_file"],
        severity: "warning",
        code: "w",
        message: "Geometry File: Unlinked",
      },
      { loc: ["scan", "daq_id"], code: "daq_refused", message: "Refused" },
      {
        loc: ["reconstruction", "geometry_file"],
        code: "does_not_load",
        message: "Does not load",
      },
      { loc: [], code: "x", message: "Whole form" },
    ])
    expect(
      placed.all.map((issue) => [issue.field, issue.severity, issue.message])
    ).toEqual([
      ["scan.source", "error", "Refused"],
      ["reconstruction.geometry_file", "error", "Does not load"],
      [null, "error", "Whole form"],
      ["indexing.geometry_file", "warning", "Unlinked"],
    ])
    expect([placed.errors, placed.warnings]).toEqual([3, 1])
    expect(Object.keys(placed.byField).sort()).toEqual([
      "indexing.geometry_file",
      "reconstruction.geometry_file",
      "scan.source",
    ])
  })

  it.each([
    ["none", null],
    ["queued", session({ runs: [{ ...RUN, status: "queued" }] })],
    ["running", session()],
    ["stopping", session({ runs: [{ ...RUN, stop_requested: true }] })],
    [
      "finished",
      session({
        open: false,
        runs: [
          { ...RUN, status: "finished" },
          { ...RUN, job_id: 8, status: "cancelled" },
        ],
      }),
    ],
    [
      "failed",
      session({
        open: false,
        runs: [
          { ...RUN, status: "finished" },
          { ...RUN, job_id: 8, status: "failed" },
        ],
      }),
    ],
    [
      "cancelled",
      session({
        open: false,
        runs: [{ ...RUN, status: "cancelled" }],
      }),
    ],
  ] as const)("a session's state is %s", (state, value) => {
    expect(sessionState(value)).toBe(state)
  })

  // A Both session: Stopping from its wire run's stop (or end) until its follower, which drains after it
  // has ended too.
  const follower: LiveSessionRun = {
    ...RUN,
    job_id: 8,
    run: { kind: "indexing", id: 4, label: "I4" },
  }
  it.each([
    ["both running", "running", [RUN, follower], true],
    [
      "the wire run asked to stop",
      "stopping",
      [{ ...RUN, stop_requested: true }, follower],
      true,
    ],
    [
      "the wire run finished, the follower running",
      "stopping",
      [{ ...RUN, status: "finished" }, follower],
      true,
    ],
    [
      "the wire run cancelled while queued, the follower queued",
      "stopping",
      [
        { ...RUN, status: "cancelled" },
        { ...follower, status: "queued" },
      ],
      true,
    ],
    [
      "both ended",
      "finished",
      [
        { ...RUN, status: "finished" },
        { ...follower, status: "finished" },
      ],
      false,
    ],
  ] as const)("a Both session, %s: %s", (_, state, runs, open) => {
    expect(sessionState(session({ open, runs: [...runs] }))).toBe(state)
  })

  it("a Start's 409 names its session by its jobs, which has ended once read closed or after a later one", () => {
    const conflict = (extra: object) =>
      new ApiError(409, "Conflict", {
        type: "about:blank",
        title: "Conflict",
        status: 409,
        ...extra,
      })
    const open = { job_id: 7, run: RUN.run, status: "running" }
    const created = {
      runs: [8, 9].map((job_id) => ({
        run: RUN.run,
        job_id,
        status: "queued",
        target: "local",
        queue_id: `live_${String(job_id)}`,
        failure: null,
      })),
    }
    expect(conflictJobs(conflict({ open_run: open }))).toEqual([7])
    expect(conflictJobs(conflict({ created }))).toEqual([8, 9])
    expect(conflictJobs(conflict({}))).toEqual([])
    expect(conflictJobs(new ApiError(503, "Unavailable"))).toEqual([])

    const read = (open: boolean, job_id: number) =>
      session({ open, runs: [{ ...RUN, job_id }] })
    expect(namedSessionEnded([7], read(true, 7))).toBe(false)
    expect(namedSessionEnded([7], read(false, 7))).toBe(true)
    expect(namedSessionEnded([7], read(true, 9))).toBe(true)
    // A read from before the 409 (an older session, or none) says nothing of it.
    expect(namedSessionEnded([7], read(false, 5))).toBe(false)
    expect(namedSessionEnded([7], null)).toBe(false)
    expect(namedSessionEnded([], read(false, 7))).toBe(false)
  })

  it("a DAQ picked fills the folder with where it writes, unless the folder is already inside it", () => {
    expect(daqFolder(daq(), "")).toBe("/data/e1/34ide")
    expect(daqFolder(daq(), "/elsewhere")).toBe("/data/e1/34ide")
    expect(daqFolder(daq(), "/data/e1/34ide/sub")).toBeUndefined()
    expect(daqFolder(daq(), "/data/e1/34ide")).toBeUndefined()
    expect(daqFolder(daq(), "/data/e1/34ide-other")).toBe("/data/e1/34ide")
    expect(daqFolder(daq({ destination: null }), "")).toBeUndefined()
  })

  it("the DAQ list is asked again no sooner than its answer is kept (or a failure's Retry-After)", () => {
    expect(daqListWait(30, null)).toBe(31_000)
    expect(daqListWait(12.5, null)).toBe(13_500)
    expect(daqListWait(0, null)).toBe(2_000)
    const failed = new ApiError(503, "DM unavailable", undefined, 8)
    expect(daqListWait(undefined, failed)).toBe(9_000)
  })

  it("Output Folder mirrors the Folder: its last component that is exactly data becomes analysis", () => {
    expect(mirroredOutput("/x/exp/data/scan_12")).toBe(
      "/x/exp/analysis/scan_12"
    )
    expect(mirroredOutput("/x/exp/data")).toBe("/x/exp/analysis")
    expect(mirroredOutput("/data/exp/data/scan_12")).toBe(
      "/data/exp/analysis/scan_12"
    )
    // The rest as typed: the server normalizes a trailing or a doubled slash, and refuses a `..`.
    expect(mirroredOutput("/x/exp/data/scan_12/")).toBe(
      "/x/exp/analysis/scan_12/"
    )
    expect(mirroredOutput("/x/exp//data/scan_12")).toBe(
      "/x/exp//analysis/scan_12"
    )
    expect(mirroredOutput("/x/exp/data/../data/scan_12")).toBe(
      "/x/exp/data/../analysis/scan_12"
    )
    // Only a whole component named exactly data.
    for (const folder of [
      "/x/exp/raw/scan_12",
      "/x/database/s",
      "/x/Data/s",
      "",
    ]) {
      expect(mirroredOutput(folder)).toBeUndefined()
    }
  })

  it("Output Folder follows the Folder's mirror, else the configured default (never an earlier Folder's mirror)", () => {
    expect(followedOutput("/x/exp/data/scan_12", "/runs/analysis")).toBe(
      "/x/exp/analysis/scan_12"
    )
    expect(followedOutput("", "/runs/analysis")).toBe("/runs/analysis")
    expect(followedOutput("  ", "/runs/analysis")).toBe("/runs/analysis")
    expect(followedOutput("/x/exp/raw/scan_12", "/runs/analysis")).toBe(
      "/runs/analysis"
    )
  })

  it("a form filled from a session follows its Folder only when its Output Folder is the one the Folder gives", () => {
    const follows = (folder: string, output: string) =>
      outputFollows(folder, output, "/runs/analysis")
    expect(follows("/x/exp/data/scan_12", "/x/exp/analysis/scan_12")).toBe(true)
    expect(follows("/x/exp/raw/scan_12", "/runs/analysis")).toBe(true)
    expect(follows("/x/exp/data/scan_12", "/x/exp/analysis/scan_11")).toBe(
      false
    )
    expect(follows("/x/exp/data/scan_12", "/runs/analysis")).toBe(false)
    expect(follows("/x/exp/raw/scan_12", "/y/out")).toBe(false)
    // The Folder recorded by its real path, the Output Folder as typed through a link: the names below the mirrored
    // component decide.
    expect(follows("/r/beamline/exp/data/s7", "/r/link/exp/analysis/s7")).toBe(
      true
    )
    expect(follows("/r/beamline/exp/data", "/r/link/exp/analysis")).toBe(true)
    expect(follows("/r/x/data/s7", "/r/y/analysis/s8")).toBe(false)
    expect(follows("/r/x/data/s7/a", "/r/y/analysis/s7")).toBe(false)
    expect(follows("/r/x/raw/s7", "/r/y/analysis/s7")).toBe(false)
  })

  it("Filename Template is optional: blank sends every .h5 file, and a session without one fills it blank", () => {
    expect(SPECS.scan.filename_template).toMatchObject({
      required: false,
      placeholder: EVERY_H5,
    })
    expect(SPECS.scan.filename_template?.hint).toContain(
      "Empty: every .h5 file"
    )
    const scan = session().scan
    const none = session({ scan: scan && { ...scan, filename_template: null } })
    expect(scanValuesOf(SPECS.scan, none).filename_template).toBe("")
  })

  it("the header shows no session once a session a user stopped has ended, one that ended by itself stays", () => {
    const ended = {
      open: false,
      runs: [{ ...RUN, status: "finished" as const }],
    }
    expect(shownSession(null)).toBeNull()
    expect(shownSession(undefined)).toBeNull()
    const stopping = session({
      stopped: true,
      runs: [{ ...RUN, stop_requested: true }],
    })
    expect(shownSession(stopping)).toBe(stopping)
    expect(shownSession(session({ ...ended, stopped: true }))).toBeNull()
    const failed = session({
      open: false,
      runs: [{ ...RUN, status: "failed" }],
    })
    expect(shownSession(failed)).toBe(failed)
    const finished = session(ended)
    expect(shownSession(finished)).toBe(finished)
    // Stopped, but a run Failed (a file failed in the drain, Stop now): it stays shown.
    const stoppedFailed = session({
      open: false,
      stopped: true,
      runs: [
        { ...RUN, status: "finished" },
        { ...RUN, job_id: 8, status: "failed" },
      ],
    })
    expect(shownSession(stoppedFailed)).toBe(stoppedFailed)
    // Its follower ended by its Stop now (killed, Failed): the Stop's end, hidden; with a failed file, a failure.
    const killed = {
      ...RUN,
      job_id: 8,
      status: "failed" as const,
      stopped_now: true,
    }
    const stoppedNow = session({
      open: false,
      stopped: true,
      runs: [{ ...RUN, status: "finished" }, killed],
    })
    expect(shownSession(stoppedNow)).toBeNull()
    const killedWithAFailure = session({
      open: false,
      stopped: true,
      runs: [
        { ...RUN, status: "finished" },
        { ...killed, n_failed: 1 },
      ],
    })
    expect(shownSession(killedWithAFailure)).toBe(killedWithAFailure)
    // Stop now on a session that ended without a Stop is not a stopped session.
    const unstopped = session({ ...stoppedNow, stopped: false })
    expect(shownSession(unstopped)).toBe(unstopped)
  })

  it("a session gives the Scan card its values, its author only while it is open (a clone resets it)", () => {
    const open = scanValuesOf(SPECS.scan, session())
    expect(open).toMatchObject({
      source: daqChoice("d1"),
      scan_number: "12",
      author: "alice",
      folder: "/data/e1/34ide",
      filename_template: "wire_%d.h5",
      output_folder: "/data/out",
    })
    const ended = scanValuesOf(SPECS.scan, session({ open: false }))
    expect(ended.author).toBe(blankScan(SPECS.scan).author)
    expect(scanValuesOf(SPECS.scan, session({ scan: null }))).toEqual(
      blankScan(SPECS.scan)
    )
  })
})

describe("Stop now and the waiting reason", () => {
  const arrival: NonNullable<LiveSessionRun["arrival"]> = {
    state: "available",
    checked_at: "2026-10-27T10:00:00Z",
    rows_seen: 3,
    error:
      "point 'wire_3' (line 3 of the followed run's completed.jsonl) not admitted yet",
    error_at: "2026-10-27T09:59:00Z",
  }
  const follower: LiveSessionRun = {
    ...RUN,
    job_id: 8,
    run: { kind: "indexing", id: 4, label: "I4" },
    arrival,
  }

  it("Stop now reaches every open run of the session", () => {
    const ended = { ...RUN, status: "finished" as const }
    expect(
      openRuns(session({ runs: [ended, follower] })).map((run) => run.job_id)
    ).toEqual([8])
    expect(
      openRuns(session({ runs: [RUN, follower] })).map((run) => run.job_id)
    ).toEqual([7, 8])
  })

  it("the session waits for an open run's arrival error, named by its run", () => {
    const ended = { ...RUN, status: "finished" as const }
    expect(waitingReason(session({ runs: [ended, follower] }))).toBe(
      "I4: point 'wire_3' (line 3 of the followed run's completed.jsonl) not admitted yet"
    )
    expect(waitingReason(session({ runs: [ended] }))).toBeUndefined()
    expect(
      waitingReason(session({ open: false, runs: [ended, follower] }))
    ).toBeUndefined()
    const unread = {
      ...follower,
      arrival: { ...arrival, state: "not_responding" as const },
    }
    expect(waitingReason(session({ runs: [ended, unread] }))).toBeUndefined()
  })

  it("a DAQ's or a notify run's arrival error is never what a Stopping session waits for", () => {
    const refused: NonNullable<LiveSessionRun["arrival"]> = {
      ...arrival,
      error:
        "0 catalogued file(s) not admitted yet, 1 refused: 'Si_dark.h5' has no number in its name",
    }
    // An Indexing session alone, its run draining with a file refused for good.
    const alone = { ...follower, stop_requested: true, arrival: refused }
    expect(waitingReason(session({ runs: [alone] }))).toBeUndefined()
    // A Both session: the wire run's error (still open) is not; the follower's held point is.
    const wire = { ...RUN, stop_requested: true, arrival: refused }
    expect(
      waitingReason(session({ runs: [wire, { ...follower, arrival: null }] }))
    ).toBeUndefined()
    expect(waitingReason(session({ runs: [wire, follower] }))).toBe(
      "I4: point 'wire_3' (line 3 of the followed run's completed.jsonl) not admitted yet"
    )
  })
})
