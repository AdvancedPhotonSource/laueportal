// The Calibration page's pure helpers: the overlay layers drawn on the frame, the energy error's
// colour, and the display of a detector's values.
import type {
  CalibrationIndexing,
  CalibrationResult,
  CalibrationSurface,
} from "@/api"
import { hklLabel, type OverlayLayer } from "@/viz"

import type { ListedPeak } from "./draft"

/** The energy error (eV) at which the fitted marks reach their full colour. */
export const ENERGY_ERROR_FULL_EV = 10

/**
 * A fitted mark's colour by its energy error: blue for a measured energy below the calculated one,
 * red above, grey without a measured energy (as LaueGo's calibration graph, RedWhiteBlue).
 */
export function energyErrorColor(errorEv: number | null | undefined): string {
  if (errorEv === null || errorEv === undefined || !Number.isFinite(errorEv)) {
    return "rgb(40, 160, 70)"
  }
  const t = Math.min(1, Math.abs(errorEv) / ENERGY_ERROR_FULL_EV)
  const light = Math.round(200 - 140 * t)
  return errorEv >= 0
    ? `rgb(220, ${String(light)}, ${String(light)})`
    : `rgb(${String(light)}, ${String(light)}, 230)`
}

export interface CalibrationOverlayOptions {
  labels: boolean
}

/**
 * The layers on the frame: the found peaks, the added ones, the indexing's predicted positions with
 * their hkl, and the fitted detector's predictions coloured by their energy error.
 */
export function calibrationLayers(
  peaks: readonly ListedPeak[],
  indexing: CalibrationIndexing | undefined,
  result: CalibrationResult | undefined,
  options: CalibrationOverlayOptions
): OverlayLayer[] {
  const found = peaks.filter((peak) => peak.origin === "found")
  const added = peaks.filter((peak) => peak.origin === "added")
  const layers: OverlayLayer[] = [
    marks("detected", found, null),
    marks("added", added, null),
  ]
  if (indexing?.found && !result) {
    const shown = indexing.rows.filter(
      (row) => row.predicted_x !== null && row.predicted_y !== null
    )
    layers.push({
      kind: "indexed",
      x: Float64Array.from(shown, (row) => row.predicted_x ?? NaN),
      y: Float64Array.from(shown, (row) => row.predicted_y ?? NaN),
      labels: options.labels
        ? shown.map((row) => hklLabel(row.h, row.k, row.l))
        : null,
    })
  }
  if (result?.status === "ok") {
    const shown = result.rows.filter(
      (row) => row.predicted_x !== null && row.predicted_y !== null
    )
    layers.push({
      kind: "fitted",
      x: Float64Array.from(shown, (row) => row.predicted_x ?? NaN),
      y: Float64Array.from(shown, (row) => row.predicted_y ?? NaN),
      labels: options.labels
        ? shown.map((row) => hklLabel(row.h, row.k, row.l))
        : null,
      colors: shown.map((row) => energyErrorColor(row.energy_error_ev)),
    })
  }
  return layers
}

function marks(
  kind: OverlayLayer["kind"],
  peaks: readonly ListedPeak[],
  labels: readonly string[] | null
): OverlayLayer {
  return {
    kind,
    x: Float64Array.from(peaks, (peak) => peak.x),
    y: Float64Array.from(peaks, (peak) => peak.y),
    labels,
  }
}

/** Whether the surface check warns: the surface reflection is further than `limit` from the expected normal. */
export function surfaceWarns(
  surface: CalibrationSurface | null | undefined,
  limit: number
): boolean {
  return (
    surface !== null &&
    surface !== undefined &&
    (surface.angle_to_expected_deg > limit || !surface.between)
  )
}

/** A measured energy far from its row's calculated one: most likely another harmonic (the API refuses > 25 %). */
export function energyMismatch(
  measured: number | null | undefined,
  calculated: number | null | undefined
): boolean {
  if (measured === null || measured === undefined) return false
  if (calculated === null || calculated === undefined || calculated <= 0)
    return false
  return Math.abs(measured / calculated - 1) > 0.25
}

/** Three values as "a, b, c" with `digits` decimals ("0.0", never "-0.0"). */
export function vector(
  values: readonly (number | null)[],
  digits: number,
  scale = 1
): string {
  return values
    .map((value) => {
      if (value === null) return "—"
      const text = (value * scale).toFixed(digits)
      return Number(text) === 0 ? (0).toFixed(digits) : text
    })
    .join(", ")
}
