// Test data for the wire reconstruction viewer: a run (its job and stored parameters), its artifact
// and its points, as the API sends them.
import type {
  ReconstructionArtifact,
  ReconstructionPoint,
  ReconstructionRunDetail,
  WireParameters,
} from "@/api"
import { useRoiStore } from "@/features/reconstruction/rois"
import {
  DEFAULT_SETTINGS,
  useWireViewerStore,
} from "@/features/reconstruction/viewer-store"

/**
 * The wire viewer's stores (its settings, the ROIs) are module singletons persisted to localStorage:
 * reset them and it after a test.
 */
export function resetWireViewerStore() {
  useWireViewerStore.getState().applySettings(DEFAULT_SETTINGS)
  useRoiStore.setState({ byKey: {} })
  localStorage.clear()
}

export function makeWireParameters(
  overrides: Partial<WireParameters> = {}
): WireParameters {
  return {
    depth_start: -50,
    depth_end: 50,
    depth_resolution: 1,
    filename_prefixes: ["wire_%d.h5"],
    frame_skip: null,
    wire_skip: null,
    geometry_file: "/data/demo/geometry.xml",
    memory_limit_mb: 2048,
    num_threads: 4,
    percent_brightest: 100,
    scan_points: "1",
    scan_points_len: 1,
    verbose: 1,
    wire_edges: "leading",
    ...overrides,
  }
}

export function makeReconstructionRunDetail(
  overrides: Partial<ReconstructionRunDetail> = {}
): ReconstructionRunDetail {
  return {
    id: 1,
    job_id: 3,
    method: "wire",
    scan_number: 990001,
    author: "demo",
    notes: null,
    created_at: "2026-10-19T12:00:00Z",
    input_path: "/data/demo/synthetic-reconstruction",
    output_path: "/data/runs/rec_1",
    parameters: makeWireParameters(),
    relative_paths: {
      input_path: { root: "data", relative: "demo/synthetic-reconstruction" },
      output_path: { root: "data", relative: "runs/rec_1" },
      geometry_file: null,
    },
    ...overrides,
  }
}

export function makeReconstructionArtifact(
  overrides: Partial<ReconstructionArtifact> = {}
): ReconstructionArtifact {
  return {
    reconstruction_id: 1,
    state: "ready",
    format: "scan",
    path: "/data/runs/rec_1/reconstruction/scan.h5",
    catalog_status: "finished",
    points_complete: 2,
    points_total: 3,
    run_status: "finished",
    live_open: false,
    ...overrides,
  }
}

/** A complete point (`overrides` make it another). */
export function makePoint(
  overrides: Partial<ReconstructionPoint> = {}
): ReconstructionPoint {
  return {
    point_id: "wire_1",
    index: 0,
    status: "complete",
    explanation: null,
    n_depths: 101,
    depth_first_um: -50,
    depth_last_um: 50,
    frame_shape: [64, 48],
    dtype: "uint16",
    sample_x_um: 1,
    sample_y_um: 2.25,
    sample_z_um: -3,
    source: "/data/demo/wire_1.h5",
    source_path: "/data/demo/wire_1.h5",
    source_state: "available",
    error: null,
    ...overrides,
  }
}

/** Three points: two complete (`wire_1`, `wire_3`) and a pending one between them. */
export function makePoints(): ReconstructionPoint[] {
  return [
    makePoint(),
    makePoint({
      point_id: "wire_2",
      index: 1,
      status: "pending",
      explanation:
        "Point wire_2 has not been reconstructed yet; press Refresh after it completes. It has no images.",
      n_depths: null,
      depth_first_um: null,
      depth_last_um: null,
      frame_shape: null,
      dtype: null,
    }),
    makePoint({ point_id: "wire_3", index: 2 }),
  ]
}

function notFound(title: string, detail: string): Response {
  return Response.json(
    { title, status: 404, detail },
    { status: 404, headers: { "content-type": "application/problem+json" } }
  )
}

/**
 * The reconstruction viewer's answers for a path (the run, the artifact, the points), or undefined for
 * another path. `runs` answers a run: undefined is a 404.
 */
export function answerReconstruction(
  url: URL,
  {
    artifact = () => makeReconstructionArtifact(),
    points = makePoints,
    runs = (id) => makeReconstructionRunDetail({ id }),
  }: {
    artifact?: () => ReconstructionArtifact | Response
    points?: () => ReconstructionPoint[] | Response
    runs?: (id: number) => ReconstructionRunDetail | undefined
  } = {}
): unknown {
  const match =
    /^\/api\/v1\/reconstruction-runs\/(\d+)(\/artifact|\/points)?$/.exec(
      url.pathname
    )
  if (!match) return undefined
  const id = Number(match[1])
  const run = runs(id)
  if (!run) {
    return notFound(
      "Reconstruction run not found",
      `No reconstruction run R${String(id)}`
    )
  }
  if (match[2] === "/artifact") return artifact()
  if (match[2] === "/points") {
    const rows = points()
    return rows instanceof Response
      ? rows
      : { reconstruction_id: id, state: "ready", rows }
  }
  return run
}
