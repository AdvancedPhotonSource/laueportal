// The Pole Figure tab's logic: the HKL fields' Update rule, the pole figure's request, a clicked pole
// (the HSV centre and the misorientation reference, switching the map to Pole HSV and back), the
// centre tool's click (snap to a pole, or place the centre) and a selection's statistics request.
import { ApiError, MapColor, type GetIndexingPoleFigureParams } from "@/api"
import { selectionKey, type LinkedSelection } from "@/viz"
import { surfaceParam } from "./color-map"
import type { Hkl } from "./viewer-search"
import {
  isRestorableColor,
  type PolePattern,
  type PoleReference,
  type PoleSettings,
} from "./viewer-slices"
import type { ScopeParams } from "./viewer-store"

/** The HKL fields' text, h, k and l. */
export type HklText = readonly [string, string, string]

const INTEGER = /^[-+]?\d+$/

/** The HKL the fields name: three integers, not all zero; else null. */
export function parseHklText(text: HklText): Hkl | null {
  if (!text.every((value) => INTEGER.test(value.trim()))) return null
  const hkl = text.map((value) => Number(value.trim()))
  if (!hkl.every(Number.isSafeInteger) || hkl.every((n) => n === 0)) {
    return null
  }
  return [hkl[0] ?? 0, hkl[1] ?? 0, hkl[2] ?? 0]
}

/** The HKL Update applies, or null while the fields are invalid or name the applied HKL. */
export function hklUpdate(text: HklText, applied: Hkl): Hkl | null {
  const hkl = parseHklText(text)
  return hkl && hkl.some((n, i) => n !== applied[i]) ? hkl : null
}

export function hklText(hkl: Hkl): HklText {
  return [String(hkl[0]), String(hkl[1]), String(hkl[2])]
}

/**
 * The pole figure's request: the scope, the HKL, the surface and the scheme; the HSV scheme adds its
 * radius and the clicked pole's centre (the others read neither, so changing them refetches nothing).
 */
export function poleFigureRequest(
  pole: PoleSettings,
  reference: PoleReference | null,
  hkl: Hkl,
  scope: ScopeParams
): GetIndexingPoleFigureParams {
  const params: GetIndexingPoleFigureParams = {
    ...scope,
    hkl: hkl.join(","),
    surface: surfaceParam(pole.surface, pole.surfaceMatrix),
    scheme: pole.scheme,
  }
  if (pole.scheme === "hsv") {
    params.radius = pole.radius
    if (reference) params.center = reference.center.join(",")
  }
  return params
}

/** A clicked pole: its pattern and its position on the figure. */
export interface ClickedPole {
  step: number
  patternIndex: number
  center: readonly [number, number]
}

/** What a click or "Reset color center" changes: the run's centre and reference, and the map's mode. */
export interface PoleChange {
  reference: PoleReference | null
  color: MapColor
}

/**
 * A click on a pole: a pole of the reference pattern clears it and restores the mode the map had
 * before; another pole becomes the reference and the HSV centre, and the map shows Pole HSV, or
 * stays in misorientation (whose hint sends the user here to choose the reference). The mode to
 * restore is the map's mode when it can be restored, else the one kept from an earlier click (the
 * map is still on Pole HSV or misorientation), else cubic IPF.
 */
export function clickPole(
  indexingId: number,
  reference: PoleReference | null,
  clicked: ClickedPole,
  color: MapColor
): PoleChange {
  const pattern = reference?.pattern
  if (
    reference &&
    pattern?.step === clicked.step &&
    pattern.patternIndex === clicked.patternIndex
  ) {
    return resetPole(reference)
  }
  return snapTo(indexingId, reference, clicked, color)
}

/** The clicked pole as the centre and the reference; the map shows Pole HSV (or stays in misorientation). */
function snapTo(
  indexingId: number,
  reference: PoleReference | null,
  clicked: ClickedPole,
  color: MapColor
): PoleChange {
  return centred(
    indexingId,
    reference,
    { step: clicked.step, patternIndex: clicked.patternIndex },
    clicked.center,
    color
  )
}

function centred(
  indexingId: number,
  reference: PoleReference | null,
  pattern: PolePattern | null,
  center: readonly [number, number],
  color: MapColor
): PoleChange {
  return {
    reference: {
      indexingId,
      pattern,
      center: [center[0], center[1]],
      previousColor: isRestorableColor(color)
        ? color
        : (reference?.previousColor ?? MapColor.cubic_ipf),
    },
    color:
      color === MapColor.misorientation
        ? MapColor.misorientation
        : MapColor.pole_hsv,
  }
}

/** The colour centre as the sidebar shows it: "(x, y)" to 3 decimals, the default (0, 0) when none is set. */
export function centerText(center: readonly [number, number] | null): string {
  const [x, y] = (center ?? [0, 0]).map((v) => Number(v.toFixed(3)) || 0)
  return `(${String(x)}, ${String(y)})`
}

/** The reference pattern as the sidebar shows it, on one line ("step 3 / 0": step / pattern), or a dash. */
export function patternText(pattern: PolePattern | null): string {
  return pattern
    ? `step ${String(pattern.step)} / ${String(pattern.patternIndex)}`
    : "—"
}

/** A point on the pole figure, moved onto the unit circle when it lies outside. */
export function clampToCircle(
  point: readonly [number, number]
): [number, number] {
  const [x, y] = point
  const r = Math.hypot(x, y)
  return r > 1 ? [x / r, y / r] : [x, y]
}

/**
 * A click of the centre tool: on a pole (`pole`, within the view's hover pick radius) it snaps
 * there, the pole's position the centre and its pattern the reference, as a click on the pole (but
 * a second click keeps it); anywhere else the centre goes to the point, clamped to the unit circle,
 * and the reference stays. The map's mode changes as for a click on a pole.
 */
export function setCenter(
  indexingId: number,
  reference: PoleReference | null,
  click: { pole: ClickedPole | null; point: readonly [number, number] },
  color: MapColor
): PoleChange {
  if (click.pole) return snapTo(indexingId, reference, click.pole, color)
  return centred(
    indexingId,
    reference,
    reference?.pattern ?? null,
    clampToCircle(click.point),
    color
  )
}

/** "Reset color center": no reference, and the map's mode before the click. */
export function resetPole(reference: PoleReference): PoleChange {
  return { reference: null, color: reference.previousColor }
}

/** The bound on the patterns whose pairwise misorientation the card computes (the API's too). */
export const MAX_STATS_PATTERNS = 700

/** The selection's statistics: what the card shows, and the request when there is one. */
export type StatsRequest =
  | { kind: "empty" }
  | {
      kind: "summary"
      /** The selected poles on the figure, and their distinct patterns. */
      poles: number
      patterns: number
      /**
       * The distinct patterns as [step, pattern_index], sorted: null when there is nothing to ask
       * (fewer than two, or above MAX_STATS_PATTERNS: the skip notice, and no request).
       */
      request: [number, number][] | null
    }

/**
 * The statistics request of a selection (from the pole figure or the map, through the page's store):
 * the selected patterns that have a pole on the figure, so indexed patterns only, never a frame-only
 * record (pattern_index -1), each sent once (at most 700 entries, far below the API's 33,600).
 */
export function statsRequest(
  selection: LinkedSelection,
  poles: {
    length: number
    step: ArrayLike<number>
    patternIndex: ArrayLike<number>
  }
): StatsRequest {
  if (selection.pairs.length === 0) return { kind: "empty" }
  let count = 0
  const patterns = new Map<number, [number, number]>()
  for (let i = 0; i < poles.length; i++) {
    const step = poles.step[i] ?? -1
    const index = poles.patternIndex[i] ?? -1
    if (index < 0) continue
    const key = selectionKey(step, index)
    if (!selection.keys.has(key)) continue
    count++
    if (!patterns.has(key)) patterns.set(key, [step, index])
  }
  const n = patterns.size
  return {
    kind: "summary",
    poles: count,
    patterns: n,
    request:
      n >= 2 && n <= MAX_STATS_PATTERNS
        ? [...patterns.values()].sort((a, b) => a[0] - b[0] || a[1] - b[1])
        : null,
  }
}

/** The API's refusal of a selection above its bound (422 `too_many_patterns`): the skip notice too. */
export function isTooManyPatterns(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status === 422 &&
    (error.problem?.errors ?? []).some(
      (issue) => issue.code === "too_many_patterns"
    )
  )
}
