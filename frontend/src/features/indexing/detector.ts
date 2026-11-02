// The Detector View tab's rules: which step it shows, what its overlays request asks, and which
// patterns the checklist keeps.
import {
  ApiError,
  type DetectorPattern,
  type GetIndexingDetectorOverlaysParams,
  type IndexingResultsSummary,
} from "@/api"
import type { ScopeParams } from "./viewer-store"

/**
 * The step the tab shows: the URL's when the scope keeps it, else the default it showed last for the
 * same scope while that step is still there (`kept`: a live run's results read again keep the step
 * shown, though their default moves to the first step with a pattern), else the summary's default
 * (the first step in scope with a pattern); null when the scope keeps no step. The URL keeps a step
 * the scope drops (a wider scope shows it again), as it keeps any value it can read.
 */
export function detectorStep(
  requested: number | undefined,
  summary: Pick<IndexingResultsSummary, "steps" | "default_step">,
  kept?: number
): number | null {
  if (requested !== undefined && summary.steps.includes(requested)) {
    return requested
  }
  if (kept !== undefined && summary.steps.includes(kept)) return kept
  return summary.default_step
}

/** The step in scope before (-1) or after (+1) `step`, or null at either end. */
export function adjacentStep(
  steps: readonly number[],
  step: number,
  direction: -1 | 1
): number | null {
  const at = steps.indexOf(step)
  if (at < 0) return null
  return steps[at + direction] ?? null
}

/**
 * The overlays' requests: the scope's, without a simulation (`simulate` defaults to false), and with
 * one only when "Simulated missing peaks" is on. The fast request runs then too, so the detected and
 * indexed peaks show while the simulation (about 4 s a pattern) runs. The energy window is the
 * beamline's (`kev` not sent).
 */
export function overlayRequests(
  scope: ScopeParams,
  simulate: boolean
): {
  plain: GetIndexingDetectorOverlaysParams
  simulated: GetIndexingDetectorOverlaysParams | null
} {
  return {
    plain: { ...scope },
    simulated: simulate ? { ...scope, simulate: true } : null,
  }
}

/** The image endpoint's 404 for a frame that is not on this host (its detail says why). */
export function isImageNotFound(error: unknown): error is ApiError {
  return (
    error instanceof ApiError &&
    error.status === 404 &&
    error.problem?.title === "Detector image not found"
  )
}

/** The patterns whose indexed peaks and simulated reflections show: every one not unchecked. */
export function checkedPatterns(
  patterns: readonly DetectorPattern[],
  unchecked: ReadonlySet<number>
): Set<number> {
  return new Set(
    patterns
      .map((pattern) => pattern.pattern_index)
      .filter((index) => !unchecked.has(index))
  )
}
