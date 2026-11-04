// Test data for the indexing viewer: a run (its job and stored parameters), its artifact and its
// summary, as the API sends them; and the viewer store's reset.
import type {
  IndexingArtifact,
  IndexingResultsSummary,
  IndexingRunDetail,
  IndexingRunPage,
  LaueGoParameters,
  PatternDetails,
  PointDetails,
} from "@/api"
import {
  DEFAULT_SETTINGS,
  useIndexingViewerStore,
} from "@/features/indexing/viewer-store"

/**
 * The viewer's store as a fresh page has it: every setting at its default, nothing waiting to apply,
 * nothing stored. The store is a module singleton: call this after each test that renders the viewer.
 */
export function resetIndexingViewerStore() {
  useIndexingViewerStore.getState().applySettings(DEFAULT_SETTINGS)
  localStorage.clear()
}

export function makeLaueGoParameters(
  overrides: Partial<LaueGoParameters> = {}
): LaueGoParameters {
  return {
    filename_prefixes: ["Ni_%d.h5"],
    threshold: null,
    threshold_ratio: null,
    max_rfactor: 0.5,
    box_size: 5,
    max_number: 50,
    min_separation: 40,
    peak_shape: "Lorentzian",
    scan_points: "1-4",
    scan_points_len: 4,
    depth_range: null,
    depth_range_len: null,
    detector_crop_x1: 0,
    detector_crop_x2: 2047,
    detector_crop_y1: 0,
    detector_crop_y2: 2047,
    min_size: 1.13,
    max_peaks: 250,
    smooth: false,
    mask_file: null,
    index_kev_max_calc: 30,
    index_kev_max_test: 35,
    index_angle_tolerance: 0.1,
    index_h: 0,
    index_k: 0,
    index_l: 1,
    index_cone: 72,
    energy_unit: "keV",
    exposure_unit: "sec",
    cosmic_filter: false,
    reciprocal_lattice_unit: "1/nm",
    lattice_parameters_unit: "nm",
    output_xml: "output.xml",
    geometry_file: "/data/geoN.xml",
    crystal_file: "/data/Ni.xml",
    depth: null,
    beamline: "34ID-E",
    ...overrides,
  }
}

export function makeIndexingRunDetail(
  overrides: Partial<IndexingRunDetail> = {}
): IndexingRunDetail {
  return {
    id: 1,
    method: "lauego",
    scan_number: 990001,
    reconstruction_id: null,
    job_id: 7,
    input_path: "/data/scan_990001",
    output_path: "/runs/index_1",
    author: "demo",
    notes: null,
    created_at: "2026-10-01T08:00:00Z",
    parameters: makeLaueGoParameters(),
    ...overrides,
  }
}

export function makeArtifact(
  overrides: Partial<IndexingArtifact> = {}
): IndexingArtifact {
  return {
    indexing_id: 1,
    state: "ready",
    results_path: "/runs/index_1/output.h5",
    xml_path: null,
    xml_source: null,
    xml_candidates: [],
    conversion_job_id: null,
    conversion: null,
    problems: [],
    results_version: "v1",
    live_open: false,
    ...overrides,
  }
}

export function makeSummary(
  overrides: Partial<IndexingResultsSummary> = {}
): IndexingResultsSummary {
  return {
    indexing_id: 1,
    path: "/runs/index_1/output.h5",
    source: "results",
    counts: { frames: 4, patterns: 3, peaks: 120 },
    axes: [],
    color_modes: [],
    default_step: 0,
    steps: [0, 1, 3],
    structure: "Ni",
    space_group: 225,
    geometry: { source: "embedded", named: null, recorded: null },
    ...overrides,
  }
}

/**
 * The indexing endpoints' answers for a path (the run, the artifact, the summary; the indexations list
 * empty), or undefined for another path. `runs` answers a run: undefined is a 404.
 */
export function answerIndexing(
  url: URL,
  artifact: IndexingArtifact = makeArtifact(),
  runs: (id: number) => IndexingRunDetail | undefined = (id) =>
    makeIndexingRunDetail({ id })
): unknown {
  const path = url.pathname
  if (path === "/api/v1/indexing-runs") {
    const page: IndexingRunPage = {
      rows: [],
      total: 0,
      as_of: "2026-10-19T12:00:00Z",
    }
    return page
  }
  const run = /^\/api\/v1\/indexing-runs\/(\d+)$/.exec(path)
  if (run) {
    const id = Number(run[1])
    return (
      runs(id) ??
      Response.json(
        {
          title: "Indexing run not found",
          status: 404,
          detail: `No indexing run I${String(id)}`,
        },
        { status: 404, headers: { "content-type": "application/problem+json" } }
      )
    )
  }
  if (/^\/api\/v1\/indexing-runs\/\d+\/artifact$/.test(path)) return artifact
  if (/^\/api\/v1\/indexing-runs\/\d+\/summary$/.test(path)) {
    return makeSummary()
  }
  return undefined
}

/**
 * The 24 proper rotations of the cube as signed permutation matrices: a cubic crystal's
 * `equivalent_settings`, as the API sends them.
 */
export const CUBIC_SETTINGS: number[][][] = (() => {
  const out: number[][][] = []
  const permutations = [
    [0, 1, 2],
    [1, 2, 0],
    [2, 0, 1],
    [0, 2, 1],
    [2, 1, 0],
    [1, 0, 2],
  ]
  for (const [index, permutation] of permutations.entries())
    for (const signs of [
      [1, 1, 1],
      [1, -1, -1],
      [-1, 1, -1],
      [-1, -1, 1],
    ]) {
      // an odd permutation needs an odd number of minus signs for determinant +1
      const flip = index >= 3 ? -1 : 1
      out.push(
        permutation.map((column, row) =>
          [0, 1, 2].map((i) => (i === column ? (signs[row] ?? 1) * flip : 0))
        )
      )
    }
  return out
})()

/** 2π / a of Ni (1/nm): its reference cell's reciprocal rows are this times the lab axes. */
export const NI_RECIPROCAL = (2 * Math.PI) / 0.35238

/** A point's pattern; `reciprocal` null for one without an orientation. */
export function makePatternDetails(
  index: number,
  reciprocal: number[][] | null
): PatternDetails {
  return {
    pattern_index: index,
    n_indexed: 20,
    goodness: 100,
    rms_error_deg: 0.001,
    indexed: reciprocal !== null,
    in_scope: true,
    reciprocal,
  }
}

/** Step 3's details with `patterns`, and Ni's cell unless `crystal` is false. */
export function makePointDetails(
  patterns: PatternDetails[],
  crystal = true
): PointDetails {
  const k = NI_RECIPROCAL
  return {
    step: 3,
    frame_id: 3,
    sample_position: [0, 0, 0],
    depth: null,
    n_peaks: 24,
    in_scope: true,
    patterns,
    coordinates: [],
    crystal: crystal
      ? {
          name: "Ni",
          space_group: 225,
          lengths_nm: [0.35238, 0.35238, 0.35238],
          angles_deg: [90, 90, 90],
          reference_reciprocal: [
            [k, 0, 0],
            [0, k, 0],
            [0, 0, k],
          ],
          symmetry: "cubic",
          equivalent_settings: CUBIC_SETTINGS,
        }
      : null,
  }
}
