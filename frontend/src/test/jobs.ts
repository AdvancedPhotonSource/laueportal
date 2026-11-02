// Jobs as the jobs API answers them, for tests (Vitest and Playwright): a run monitor row, a job page
// (a running local LaueGo run by default, with its variants: remote, migrated, failed, files missing),
// its failure report's pages and its log.
import type {
  FailurePage,
  FailureRow,
  JobDetail,
  JobLive,
  JobLog,
  JobSummary,
  RemoteObservation,
  RunConfiguration,
  RunFile,
  RunReference,
} from "@/api"

const RUNS = "/net/fileserver/export/data1/runs/scan_990012"

/** A run's directory under a runs root not on this host: `index_72`, `rec_12`. */
export function runDirectory(run: RunReference): string {
  return `${RUNS}/${run.kind === "indexing" ? "index" : "rec"}_${String(run.id)}`
}

const I72: RunReference = { kind: "indexing", id: 72, label: "I72" }
const RUN_DIRECTORY = runDirectory(I72)

/** A run monitor row: job `id` runs I(id + 31) of scan 990012, finished unless `overrides` say otherwise. */
export function makeJobSummary(
  id: number,
  overrides: Partial<JobSummary> = {}
): JobSummary {
  const started = Date.UTC(2026, 9, 1, 14, 0) + id * 3_600_000
  return {
    job_id: id,
    run: { kind: "indexing", id: id + 31, label: `I${String(id + 31)}` },
    author: ["jdoe", "mdoe", "rdoe"][id % 3] ?? null,
    status: "finished",
    phase: "finished",
    n_inputs: 441,
    n_succeeded: 441,
    n_failed: 0,
    n_not_run: 0,
    n_processed: 441,
    start_time: new Date(started).toISOString(),
    duration_seconds: 1_800,
    message: "Finished: 441 succeeded, 0 failed, 0 not run of 441",
    scan_number: 990_012,
    target: "local",
    execution: "APS",
    dm_state: null,
    observation: null,
    live: false,
    held: false,
    ...overrides,
  }
}

/** A run file in `directory` (I72's by default), available unless `overrides` say otherwise. */
export function makeRunFile(
  name: string,
  overrides: Partial<RunFile> = {},
  directory = RUN_DIRECTORY
): RunFile {
  return {
    name,
    path: `${directory}/${name}`,
    state: "available",
    detail: null,
    ...overrides,
  }
}

/** The request's parameters: a LaueGo indexing's, or a wire reconstruction's. */
function requestParameters(run: RunReference): Record<string, unknown> {
  const common = {
    scan_number: 990_012,
    filename_prefixes: ["Ni_%d.h5"],
    scan_points: "1-441",
  }
  return run.kind === "indexing"
    ? {
        ...common,
        input_path: `${RUNS}/rec_41/reconstruction`,
        box_size: 5,
        max_rfactor: 0.5,
        smooth: false,
        threshold: null,
        author: "jdoe",
      }
    : {
        ...common,
        input_path: "/net/fileserver/export/data1/2026/scan_990012",
        depth_start: -50,
        depth_end: 50,
        depth_resolution: 1,
        percent_brightest: 2,
        wire_edges: "leading",
        author: "jdoe",
      }
}

/** A running run's configuration (I72's by default): its request read, run.json still to come. */
export function makeConfiguration(
  overrides: Partial<RunConfiguration> = {},
  run: RunReference = I72
): RunConfiguration {
  const directory = runDirectory(run)
  return {
    kind: run.kind === "indexing" ? "lauego_indexing" : "wire_reconstruction",
    run_directory: { path: directory, state: "available", detail: null },
    files: [
      makeRunFile("request.json", {}, directory),
      makeRunFile("run.json", { state: "missing" }, directory),
    ],
    problems: [],
    validation_error: null,
    warnings: [],
    partial_results: false,
    request: {
      parameters: requestParameters(run),
      output:
        run.kind === "indexing"
          ? { directory, results: "output.h5" }
          : { directory, reconstruction: "reconstruction" },
      manifest: {
        path: "inputs.jsonl",
        n_inputs: 441,
        sha256:
          "0f3a9c5e2b7d4f1a8c6e0b9d2f4a7c1e3b5d8f0a2c4e6b8d0f1a3c5e7b9d1f2a",
      },
    },
    execution: null,
    history: null,
    artifacts: [],
    ...overrides,
  }
}

/**
 * Job 41, running I72 of scan 990012 locally: 123 of 441 inputs processed, 3 failed. Another `run`
 * gets its own configuration (unless `overrides` give one).
 */
export function makeJobDetail(overrides: Partial<JobDetail> = {}): JobDetail {
  const run = overrides.run ?? I72
  return {
    job_id: 41,
    run,
    scan_number: 990_012,
    status: "running",
    phase: "running",
    computer_name: "compute-host",
    submit_time: "2026-10-19T14:58:20Z",
    start_time: "2026-10-19T15:00:00Z",
    finish_time: null,
    duration_seconds: 754.2,
    messages: "Queued on laue-run-1\nStarted on laue-run-1",
    progress: {
      n_inputs: 441,
      n_processed: 123,
      n_succeeded: 120,
      n_failed: 3,
      n_not_run: 0,
      n_pending: 318,
      heartbeat_age_seconds: 4.2,
      stale_after_seconds: 300,
      stale: false,
      stop_requested: false,
      stop_requested_at: null,
    },
    remote: null,
    held: false,
    live: null,
    configuration: makeConfiguration({}, run),
    as_of: "2026-10-19T15:12:34Z",
    ...overrides,
  }
}

/** A live run's block: a `notify` run with 3 arrivals and no arrival status. */
export function makeJobLive(overrides: Partial<JobLive> = {}): JobLive {
  return {
    source: "notify",
    source_state: "available",
    daq_id: null,
    folder: "/data/scan_1",
    filename_template: "frame_%d.h5",
    follows: null,
    follows_job_id: null,
    arrivals: 3,
    arrivals_state: "available",
    arrival: null,
    ...overrides,
  }
}

/** The same job after it ended: `status` with its counters, run.json written, the artifacts published. */
export function endedJob(
  job: JobDetail,
  status: "finished" | "failed" | "cancelled",
  counters: { succeeded: number; failed: number; notRun: number }
): JobDetail {
  const { succeeded, failed, notRun } = counters
  return {
    ...job,
    status,
    phase: status,
    finish_time: "2026-10-19T15:40:00Z",
    duration_seconds: 2400,
    messages: `${job.messages ?? ""}\n${status === "finished" ? "Finished" : status === "failed" ? "Failed" : "Cancelled by request"}: ${String(succeeded)} succeeded, ${String(failed)} failed, ${String(notRun)} not run of ${String(job.progress.n_inputs)}`,
    progress: {
      ...job.progress,
      n_processed: succeeded + failed,
      n_succeeded: succeeded,
      n_failed: failed,
      n_not_run: notRun,
      n_pending: job.progress.n_inputs - succeeded - failed - notRun,
      stale: false,
      stop_requested: false,
    },
    configuration: endedConfiguration(job, status, succeeded),
  }
}

/** What an ended run's directory holds: run.json written, and its outputs when some input succeeded. */
function endedConfiguration(
  job: JobDetail,
  status: "finished" | "failed" | "cancelled",
  succeeded: number
): RunConfiguration {
  const directory = job.configuration.run_directory.path ?? RUN_DIRECTORY
  const indexing = job.run?.kind !== "reconstruction"
  const artifacts: RunConfiguration["artifacts"] = indexing
    ? [
        {
          name: "results",
          path: `${directory}/output.h5`,
          viewer: "indexing_results",
        },
        { name: "xml", path: `${directory}/output.xml`, viewer: null },
      ]
    : [
        {
          name: "reconstruction",
          path: `${directory}/reconstruction`,
          viewer: "reconstruction",
        },
      ]
  return {
    ...job.configuration,
    files: [
      makeRunFile("request.json", {}, directory),
      makeRunFile("run.json", {}, directory),
    ],
    execution: {
      provenance: { lauelab_version: "0.1.0", engine: "lauelab liblaue" },
      policy: { workers: 4, max_in_flight: 8 },
      host: job.computer_name,
      started_at: "2026-10-19T10:00:00",
      finished_at: "2026-10-19T10:40:00",
      n_indexed: indexing ? succeeded : null,
    },
    artifacts: succeeded > 0 ? artifacts : [],
    partial_results: status !== "finished" && succeeded > 0,
  }
}

/** Job 40, running R12 on APS through DM: its counters are the submission's until the results return. */
export function makeRemoteJob(overrides: Partial<JobDetail> = {}): JobDetail {
  return makeJobDetail({
    job_id: 40,
    run: { kind: "reconstruction", id: 12, label: "R12" },
    progress: {
      ...makeJobDetail().progress,
      n_processed: 0,
      n_succeeded: 0,
      n_failed: 0,
      n_pending: 441,
      heartbeat_age_seconds: 25,
      stale_after_seconds: null,
    },
    remote: {
      target: "aps",
      execution: "APS",
      dm_state: "running",
      globus_url:
        "https://app.globus.org/runs/7d3c1f0e-5b2a-4c8e-9f61-2a4b6c8d0e1f",
      observation: null,
    },
    ...overrides,
  })
}

/**
 * A `crux` run's remote observation as the job API gives it: the golden `running`
 * (`backend/tests/fixtures/observation_goldens/goldens.json`), not stale.
 */
export function makeObservation(
  overrides: Partial<RemoteObservation> = {}
): RemoteObservation {
  return {
    phase: "running",
    detail: "Processing",
    pbs_id: "123456.crux-pbs-01",
    stale: false,
    observed_at: "2026-10-25T12:00:00Z",
    phase_since: "2026-10-25T12:00:00Z",
    flow_state: "RunPbsJob",
    ...overrides,
  }
}

/** Job 40 (R12) running on CRUX through DM, read 30 s after `observation` (none: DM's state only). */
export function makeCruxJob(
  observation: RemoteObservation | null = makeObservation()
): JobDetail {
  return makeRemoteJob({
    as_of: "2026-10-25T12:00:30Z",
    remote: {
      target: "crux",
      execution: "ALCF",
      dm_state: "running",
      globus_url:
        "https://app.globus.org/runs/0c5e7a12-1d3b-4f6a-8e2c-9b7d5f3a1e60",
      observation,
    },
  })
}

/** A failed or unattempted input, at position `index`. */
export function makeFailure(
  index: number,
  overrides: Partial<FailureRow> = {}
): FailureRow {
  return {
    index,
    input_id: `Ni_${String(index + 1)}`,
    source: `${RUNS}/rec_41/reconstruction/Ni_${String(index + 1)}.h5`,
    category: "numerical",
    error_type: "IndexingError",
    message: `No pattern indexed within the angle tolerance (${String(index)})`,
    recorded_at: "2026-10-19T10:12:00",
    ...overrides,
  }
}

/** A page of a failure report of `total` records (`make(index)` each), as the server pages it. */
export function failurePage(
  total: number,
  offset: number,
  limit: number,
  make: (index: number) => FailureRow = (index) => makeFailure(index),
  report: RunFile = makeRunFile("failures.jsonl")
): FailurePage {
  const end = Math.min(offset + limit, total)
  const rows = Array.from({ length: Math.max(end - offset, 0) }, (_, i) =>
    make(offset + i)
  )
  return { rows, total, has_more: end < total, report }
}

/** An empty failure page whose report is in `report`'s state (not recorded, missing, unreadable). */
export function emptyFailurePage(total: number, report: RunFile): FailurePage {
  return { rows: [], total, has_more: false, report }
}

/** A job's logs: the run log's `lines` (of `nLines`) and, for a remote run, the PBS output. */
export function makeJobLog(
  jobId: number,
  lines: string[] = [],
  overrides: Partial<JobLog> = {}
): JobLog {
  return {
    job_id: jobId,
    run_log: {
      lines,
      truncated: false,
      n_lines: lines.length,
      file: makeRunFile("run.json"),
    },
    pbs_output: null,
    ...overrides,
  }
}
