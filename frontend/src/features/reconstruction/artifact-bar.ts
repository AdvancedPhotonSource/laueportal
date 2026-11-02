// The wire viewer's artifact bar: the selected point, "x of y points complete" or the artifact's
// state, and Refresh. The page reads the artifact every 5 s while the run is queued or running,
// revalidated by its ETag (the browser's cache: a 304 keeps the answer), backed off after failed
// reads and stopped by a 404 (src/lib/polling.ts).
import {
  ApiError,
  ReconstructionArtifactState,
  ReconstructionPointStatus,
  type ReconstructionArtifact,
  type ReconstructionPoint,
} from "@/api"
import { describeError } from "@/lib/errors"
import { backoff, isGone } from "@/lib/polling"
import { isActiveStatus } from "@/lib/run-status"

/** How often the page reads the artifact while the run is active. */
export const ARTIFACT_POLL_MS = 5000

/** Whether the run may still change (its job is queued or running). */
export function isRunActive(artifact: ReconstructionArtifact | undefined) {
  return isActiveStatus(artifact?.run_status)
}

/**
 * Poll every 5 s while the run is active or its run directory does not respond (a hung mount may
 * come back), doubled per consecutive failed read; never once the run is gone (a 404), nor before a
 * first answer (a failed first read shows Retry).
 */
export function artifactRefetchInterval(
  artifact: ReconstructionArtifact | undefined,
  error: unknown,
  failures: number
): number | false {
  const waiting =
    isRunActive(artifact) ||
    artifact?.state === ReconstructionArtifactState.not_responding
  if (isGone(error) || !waiting) return false
  return backoff(ARTIFACT_POLL_MS, failures)
}

/** What the bar says about the artifact: a count, or a state with its hint. */
export interface ArtifactStatus {
  text: string
  /** The status's tooltip. */
  hint?: string
  tone: "muted" | "warning" | "destructive"
}

/** What the tabs show for a run whose run directory holds no reconstruction (`empty`). */
export const NO_RECONSTRUCTION = "No reconstruction in the run directory"
/** … whose recorded run directory is absent or unreadable here (`not_on_host`). */
export const NOT_ON_HOST = "Run directory not on this host"
/** … whose run directory's file system did not answer (`not_responding`). */
export const NOT_RESPONDING = "Run directory not responding"

/**
 * What the tabs show instead of a point when the run has no reconstruction to show (empty, not on
 * this host, not responding); undefined while it has or may get one, or before the artifact is read.
 */
export function noReconstruction(
  artifact: ReconstructionArtifact | undefined
): string | undefined {
  switch (artifact?.state) {
    case ReconstructionArtifactState.empty:
      return NO_RECONSTRUCTION
    case ReconstructionArtifactState.not_on_host:
      return NOT_ON_HOST
    case ReconstructionArtifactState.not_responding:
      return NOT_RESPONDING
    default:
      return undefined
  }
}

/**
 * The artifact as one status: "x of y points complete", "Writing", "Empty", "Not on this host" or
 * "Not responding".
 */
export function artifactStatus(
  artifact: ReconstructionArtifact
): ArtifactStatus {
  switch (artifact.state) {
    case ReconstructionArtifactState.ready:
      return {
        text: `${String(artifact.points_complete ?? 0)} of ${String(artifact.points_total ?? 0)} points complete`,
        hint: artifact.path ?? undefined,
        tone: "muted",
      }
    case ReconstructionArtifactState.writing:
      return {
        text: "Writing",
        hint: "No reconstruction in the run directory yet",
        tone: "muted",
      }
    case ReconstructionArtifactState.empty:
      return { text: "Empty", hint: NO_RECONSTRUCTION, tone: "warning" }
    case ReconstructionArtifactState.not_on_host:
      return { text: "Not on this host", hint: NOT_ON_HOST, tone: "warning" }
    case ReconstructionArtifactState.not_responding:
      return { text: "Not responding", hint: NOT_RESPONDING, tone: "warning" }
  }
}

/** A failed read of the artifact as a status: the problem's title, its detail as the hint. */
export function errorStatus(error: unknown): ArtifactStatus {
  if (error instanceof ApiError && error.status === 404) {
    return {
      text: "Not found",
      hint: describeError(error).detail,
      tone: "destructive",
    }
  }
  const { title, detail } = describeError(error)
  return { text: title, hint: detail, tone: "destructive" }
}

/** Whether a point has images (only those can be viewed). */
export function isComplete(point: ReconstructionPoint): boolean {
  return point.status === ReconstructionPointStatus.complete
}

/**
 * The newest point, which "Follow newest point" selects: the last complete one in the list's order
 * (a live run's catalog is in arrival order).
 */
export function newestComplete(
  points: readonly ReconstructionPoint[]
): string | undefined {
  return points.findLast(isComplete)?.point_id
}

/** Whether the page offers "Follow newest point": an open live run (its points still arrive). */
export function canFollow(artifact: ReconstructionArtifact | undefined) {
  return artifact?.live_open === true
}

/** The first complete point (the default selection), in the list's order. */
export function firstComplete(
  points: readonly ReconstructionPoint[]
): string | undefined {
  return points.find(isComplete)?.point_id
}

/**
 * The point the page shows: the URL's when it is a complete point (or while the points load: the
 * artifact is `ready`), else the default (the first complete point). None when the run has no
 * points to load (the artifact empty, writing, not here or unreadable).
 */
export function selectedPoint(
  requested: string | undefined,
  points: readonly ReconstructionPoint[] | undefined,
  fallback: string | undefined,
  ready: boolean
): string | undefined {
  if (points === undefined) return ready ? (requested ?? fallback) : undefined
  if (
    requested !== undefined &&
    points.some((point) => point.point_id === requested && isComplete(point))
  ) {
    return requested
  }
  return fallback
}
