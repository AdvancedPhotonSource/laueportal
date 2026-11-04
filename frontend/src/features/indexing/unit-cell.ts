// The Color Map tab's crystal-orientation card: what it shows of a clicked point's pattern (its
// measured cell, the nominal one, the rotation from the reference cell, the crystal directions along
// the beam and up), read from the point's details.

import type { PointDetails } from "@/api"
import {
  crystalDirection,
  dualBasis,
  latticeParameters,
  mat3Of,
  nearestSetting,
  rotationFromReference,
  type LatticeParameters,
  type Mat3,
  type Rotation,
  type Vec3,
} from "@/viz"

export interface UnitCellReading {
  patternIndex: number
  /** Rows a, b, c (nm) in beamline coordinates, in the equivalent setting nearest the reference. */
  direct: Mat3
  /** The pattern's cell, from its reciprocal rows. */
  measured: LatticeParameters
  nominal: LatticeParameters | null
  /**
   * The pattern's cell differs from the nominal one (a refined lattice). Orientation indexing keeps
   * the nominal cell, so it usually does not.
   */
  refined: boolean
  /** From the reference cell; null without a crystal. */
  rotation: Rotation | null
  /** The symmetry that chose the setting (`nearestSetting`); "none" without a crystal. */
  symmetry: "cubic" | "hexagonal" | "none"
  /** The crystal directions [u v w] along the beam (Z) and up (Y). */
  beam: Vec3
  up: Vec3
}

/** The patterns of the point that have an orientation, in the results' order. */
export function orientedPatterns(point: PointDetails): number[] {
  return point.patterns
    .filter((pattern) => pattern.reciprocal && mat3Of(pattern.reciprocal))
    .map((pattern) => pattern.pattern_index)
}

/**
 * The pattern the card shows: `wanted` (the clicked or chosen one) when it has an orientation, else
 * the first that does; null when none has.
 */
export function shownPattern(
  point: PointDetails,
  wanted: number | null
): number | null {
  const oriented = orientedPatterns(point)
  if (wanted !== null && oriented.includes(wanted)) return wanted
  return oriented[0] ?? null
}

export function readUnitCell(
  point: PointDetails,
  patternIndex: number
): UnitCellReading | null {
  const pattern = point.patterns.find((p) => p.pattern_index === patternIndex)
  const measured = pattern?.reciprocal ? mat3Of(pattern.reciprocal) : null
  if (!measured) return null
  const crystal = point.crystal ?? null
  const reference = crystal ? mat3Of(crystal.reference_reciprocal) : null
  const settings = (crystal?.equivalent_settings ?? []).flatMap((setting) => {
    const matrix = mat3Of(setting)
    return matrix ? [matrix] : []
  })
  const reciprocal = reference
    ? nearestSetting(reference, measured, settings)
    : measured
  const direct = dualBasis(reciprocal)
  const parameters = latticeParameters(direct)
  const [a, b, c] = crystal?.lengths_nm ?? []
  const [alpha, beta, gamma] = crystal?.angles_deg ?? []
  const nominal: LatticeParameters | null =
    a !== undefined &&
    b !== undefined &&
    c !== undefined &&
    alpha !== undefined &&
    beta !== undefined &&
    gamma !== undefined
      ? { lengths: [a, b, c], angles: [alpha, beta, gamma] }
      : null
  return {
    patternIndex,
    direct,
    measured: parameters,
    nominal,
    refined: nominal !== null && differs(parameters, nominal),
    rotation: reference ? rotationFromReference(reference, reciprocal) : null,
    symmetry: reference ? (crystal?.symmetry ?? "none") : "none",
    beam: crystalDirection(reciprocal, [0, 0, 1]),
    up: crystalDirection(reciprocal, [0, 1, 0]),
  }
}

/**
 * Whether two cells differ by more than the results' text precision: 10⁻⁵ of a length, 10⁻³° of
 * an angle.
 */
function differs(a: LatticeParameters, b: LatticeParameters): boolean {
  return (
    a.lengths.some(
      (length, i) => Math.abs(length / (b.lengths[i] ?? length) - 1) > 1e-5
    ) ||
    a.angles.some((angle, i) => Math.abs(angle - (b.angles[i] ?? angle)) > 1e-3)
  )
}

/** Three values with `digits` decimals, separated by spaces. */
export function triple(values: Vec3, digits: number): string {
  return values.map((value) => value.toFixed(digits)).join(" ")
}

/** A direction [u v w] with two decimals, no "-0.00". */
export function direction(values: Vec3): string {
  return `[${values
    .map((value) => (Math.abs(value) < 0.005 ? 0 : value).toFixed(2))
    .join(" ")}]`
}
