// The Color Map tab's logic: the map's request parameters for the colour mode and settings, the
// axes the results offer, and the legends' captions. A scalar mode's palette, reverse and range
// are not sent: the browser maps the values, so changing them never refetches the map.
import {
  LegendColor,
  MapColor,
  type AxisChoice,
  type GetIndexingMapParams,
} from "@/api"
import type { Hkl, ViewMode } from "./viewer-search"
import {
  DEFAULT_AXES_2D,
  DEFAULT_AXES_3D,
  type MapSettings,
  type Matrix3,
  MOTOR_AXES_3D,
  type PoleReference,
  type PoleSettings,
  type SurfaceChoice,
} from "./viewer-slices"
import type { ScopeParams } from "./viewer-store"

/** The parameters to request, or why there is nothing to request yet. */
export type MapRequest =
  | { params: GetIndexingMapParams; blocked?: undefined }
  | { blocked: string; params?: undefined }

// What a mode waits for, one short line each; the fields' tooltips say the rest.
export const MISORIENTATION_HINT =
  "No reference pattern: click a pole on the Pole Figure tab."
export const G_REF_HINT = "Enter all nine entries of G_ref."

export const RODRIGUES_STEP_HINT = "Enter the reference step."

export function surfaceParam(surface: SurfaceChoice, matrix: Matrix3): string {
  return surface === "custom" ? matrix.join(",") : surface
}

/** The colour centre and reference of this run, if set (the store is shared by every run). */
export function poleReferenceOf(
  pole: PoleSettings,
  indexingId: number
): PoleReference | null {
  return pole.reference?.indexingId === indexingId ? pole.reference : null
}

/** The Rodrigues reference step typed for this run, if any (the store is shared by every run). */
export function rodriguesStepOf(
  map: MapSettings,
  indexingId: number
): number | null {
  return map.rodriguesStep?.indexingId === indexingId
    ? map.rodriguesStep.step
    : null
}

/**
 * The axes to plot: the stored ones when the results have them all, else the first default they have
 * (2D: X × H; 3D: the lab X, Y, Z, else, for results without depths, which have no Zlab, the motors'
 * X, Y, Z), else the first ones they have; null when they have too few.
 */
export function resolveAxes(
  stored: readonly string[],
  available: readonly AxisChoice[],
  mode: ViewMode
): string[] | null {
  const names = available.map((axis) => axis.value)
  const has = (axes: readonly string[]) =>
    axes.every((axis) => names.includes(axis))
  const defaults: readonly (readonly string[])[] =
    mode === "3d" ? [DEFAULT_AXES_3D, MOTOR_AXES_3D] : [DEFAULT_AXES_2D]
  if (has(stored)) return [...stored]
  const found = defaults.find(has)
  if (found) return [...found]
  const count = mode === "3d" ? DEFAULT_AXES_3D.length : DEFAULT_AXES_2D.length
  return names.length >= count ? names.slice(0, count) : null
}

export interface MapRequestInput {
  indexingId: number
  color: MapColor
  /** The resolved axes (resolveAxes). */
  axes: readonly string[]
  map: MapSettings
  pole: PoleSettings
  /** The pole figure's HKL (the URL's `hkl`). */
  hkl: Hkl
  scope: ScopeParams
}

/** The map's request for the colour mode: only the parameters that mode reads, so others never refetch it. */
export function mapRequest({
  indexingId,
  color,
  axes,
  map,
  pole,
  hkl,
  scope,
}: MapRequestInput): MapRequest {
  const params: GetIndexingMapParams = {
    ...scope,
    color,
    axes: axes.join(","),
  }
  switch (color) {
    case MapColor.cubic_ipf:
      params.surface = surfaceParam(map.surface, map.surfaceMatrix)
      break
    case MapColor.rodrigues: {
      params.rodrigues_symmetry = map.rodriguesSymmetry
      if (map.rodriguesReference === "custom") {
        const matrix = map.rodriguesMatrix
        if (matrix.some((entry) => entry === null))
          return { blocked: G_REF_HINT }
        params.rodrigues_ref = matrix.join(",")
      } else if (map.rodriguesReference === "step") {
        const step = rodriguesStepOf(map, indexingId)
        if (step === null) return { blocked: RODRIGUES_STEP_HINT }
        params.rodrigues_ref = String(step)
      } else {
        params.rodrigues_ref = "lab"
      }
      break
    }
    case MapColor.misorientation: {
      const pattern = poleReferenceOf(pole, indexingId)?.pattern
      if (!pattern) return { blocked: MISORIENTATION_HINT }
      params.rodrigues_symmetry = map.rodriguesSymmetry
      params.ref_pattern = `${String(pattern.step)},${String(pattern.patternIndex)}`
      break
    }
    case MapColor.pole_hsv: {
      const reference = poleReferenceOf(pole, indexingId)
      params.surface = surfaceParam(pole.surface, pole.surfaceMatrix)
      params.pole_hkl = hkl.join(",")
      params.pole_center = (reference?.center ?? [0, 0]).join(",")
      params.pole_radius = pole.radius
      break
    }
    default:
      // The scalar modes: the values only; the colorbar maps them in the browser.
      break
  }
  return { params }
}

/** The legend image a colour mode has (the IPF triangle, the HSV wheel), else null. */
export function legendOf(color: MapColor): LegendColor | null {
  if (color === MapColor.cubic_ipf) return LegendColor.cubic_ipf
  if (color === MapColor.pole_hsv) return LegendColor.pole_hsv
  return null
}

const SURFACE_NAMES: Record<SurfaceChoice, string> = {
  normal: "sample normal",
  X: "sample X",
  H: "sample H",
  Y: "sample Y",
  Z: "sample Z",
  F: "sample F",
  custom: "custom surface",
}

/** The legend captions. */
export function legendCaption(color: MapColor, surface: SurfaceChoice): string {
  const name = SURFACE_NAMES[surface]
  return color === MapColor.pole_hsv
    ? `${name.charAt(0).toUpperCase()}${name.slice(1)} direction in stereographic HSV`
    : `Crystal direction ∥ ${name}`
}
