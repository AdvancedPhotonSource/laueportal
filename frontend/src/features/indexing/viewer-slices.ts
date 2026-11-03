// The Color Map, Pole Figure and Detector View tabs' slices of the indexing viewer's store
// (viewer-store.ts): their shapes, defaults and readers. A reader takes a stored or linked value
// and checks each field, falling back to its default.
import { MapColor, OrientationSymmetry, Palette } from "@/api"
import {
  colorbarSettings,
  FRAME_AUTO_RANGE,
  FRAME_COLORMAPS,
  MAP_AUTO_RANGE,
  MAP_PALETTES,
  NON_INDEXED_COLORS,
  readColorbarSettings,
  type ColorbarSettings,
  type FrameColormap,
  type NonIndexedColor,
} from "@/viz"

/** The scalar colour modes: a palette and a range (the colorbar) rather than a colour per point. */
export const SCALAR_MODES = [
  MapColor.n_indexed,
  MapColor.goodness,
  MapColor.rms_error,
  MapColor.n_patterns,
] as const
export type ScalarMode = (typeof SCALAR_MODES)[number]

export function isScalarMode(color: MapColor): color is ScalarMode {
  return SCALAR_MODES.some((mode) => mode === color)
}

/** A scalar mode's colours: its palette, reversed or not, and the colorbar's range and scale. */
export interface ScalarColorSettings {
  palette: Palette
  reverse: boolean
  colorbar: ColorbarSettings
}

/** The sample surface of the IPF and pole colours: a preset, or a custom frame (`surfaceMatrix`). */
export const SURFACES = ["normal", "X", "H", "Y", "Z", "F", "custom"] as const
export type SurfaceChoice = (typeof SURFACES)[number]

export const SURFACE_LABELS: Record<SurfaceChoice, string> = {
  normal: "Normal",
  X: "X",
  H: "H",
  Y: "Y",
  Z: "Z",
  F: "F",
  custom: "Custom",
}

/** Nine numbers, row by row. */
export type Matrix3 = readonly number[]

const ROOT_HALF = Math.SQRT1_2
/** The custom frame: the rows tilt, roll and normal. */
export const DEFAULT_SURFACE_MATRIX: Matrix3 = [
  1,
  0,
  0,
  0,
  -ROOT_HALF,
  -ROOT_HALF,
  0,
  ROOT_HALF,
  -ROOT_HALF,
]

/** The Rodrigues colours' reference: the lab frame, a step's pattern 0, or a custom G_ref. */
export const RODRIGUES_REFERENCES = ["lab", "step", "custom"] as const
export type RodriguesReference = (typeof RODRIGUES_REFERENCES)[number]

export const DEFAULT_AXES_2D = ["X", "H"] as const
/** The lab frame's X, Y and Z (lauelab's "X lab", "Y lab", "Z lab"). */
export const DEFAULT_AXES_3D = ["Xlab", "Ylab", "Zlab"] as const
/**
 * The sample motors' X, Y and Z: the 3D axes of results without depths, which have no Zlab (`resolveAxes`), and the
 * 3D default that version 1 of the stored settings holds (`migrateStored`).
 */
export const MOTOR_AXES_3D = ["X", "Y", "Z"] as const

export const MARKER_SIZE_RANGE = [1, 75] as const

/** A Rodrigues reference step and the run it belongs to. */
export interface RodriguesStep {
  indexingId: number
  step: number
}

/** The Color Map tab's settings. The colour mode and 2D/3D are the URL's. */
export interface MapSettings {
  /** Per scalar mode. */
  scalar: Record<ScalarMode, ScalarColorSettings>
  /** Also the misorientation colours'. */
  rodriguesSymmetry: OrientationSymmetry
  rodriguesReference: RodriguesReference
  /**
   * The reference step (its pattern 0), with the run it was typed for: the store is shared by every
   * run, and another run's viewer ignores it (`rodriguesStepOf`).
   */
  rodriguesStep: RodriguesStep | null
  /** G_ref's rows a*, b*, c* (1/nm with 2π); null for an entry not given yet (blank at first). */
  rodriguesMatrix: readonly (number | null)[]
  surface: SurfaceChoice
  /** The custom surface's rows tilt, roll and normal. */
  surfaceMatrix: Matrix3
  axes2d: readonly [string, string]
  axes3d: readonly [string, string, string]
  /** CSS px, 1 to 75. */
  markerSize: number
  nonIndexed: NonIndexedColor
}

export const POLE_SCHEMES = ["hsv", "ipf", "uniform"] as const
export type PoleScheme = (typeof POLE_SCHEMES)[number]

/** A pole's pattern: the misorientation reference. */
export interface PolePattern {
  step: number
  patternIndex: number
}

/**
 * The pole figure's colour centre and misorientation reference. A clicked pole sets both: its
 * position is the HSV centre and its pattern the reference. The centre tool also places the centre
 * anywhere in the unit circle, keeping the pattern (none until a pole is clicked). It names its
 * run, so another run's viewer ignores it (the store is shared by every run).
 */
export interface PoleReference {
  indexingId: number
  /** The clicked pole's pattern; null while the centre was only placed. */
  pattern: PolePattern | null
  /** The HSV centre, stereographic x and y. */
  center: readonly [number, number]
  /** The map's colour mode before the centre was set, which "Reset color center" restores. */
  previousColor: RestorableColor
}

/** The colour modes a clicked pole can return to: all but the two the pole itself sets. */
export type RestorableColor = Exclude<
  MapColor,
  typeof MapColor.misorientation | typeof MapColor.pole_hsv
>

export function isRestorableColor(value: unknown): value is RestorableColor {
  return Object.values(MapColor).some(
    (color) =>
      color === value &&
      color !== MapColor.misorientation &&
      color !== MapColor.pole_hsv
  )
}

/**
 * The Pole Figure tab's settings, which the map's Pole HSV and misorientation modes read. The HKL is
 * the URL's `hkl`.
 */
export interface PoleSettings {
  surface: SurfaceChoice
  surfaceMatrix: Matrix3
  scheme: PoleScheme
  /** The HSV colour radius, in degrees. */
  radius: number
  markerSize: number
  reference: PoleReference | null
}

/** The detector view's marker and label sizes' bounds, in CSS px. */
export const DETECTOR_MARKER_RANGE = [1, 40] as const
export const DETECTOR_LABEL_RANGE = [6, 24] as const

/**
 * The Detector View tab's settings: the overlays, their sizes, and the image's colormap, colorbar
 * (its range is kept across steps, so frames compare by colour) and opacity. The step is the
 * URL's; the pattern checklist is the step's own (every pattern).
 */
export interface DetectorSettings {
  showDetected: boolean
  showIndexed: boolean
  /** "Show simulated missing peaks": a simulation of each pattern (about 4 s a pattern), off by default. */
  simulate: boolean
  showLabels: boolean
  markerSize: number
  labelSize: number
  showImage: boolean
  colormap: FrameColormap
  colorbar: ColorbarSettings
  /** 0 to 1. */
  opacity: number
}

function scalarDefaults(): ScalarColorSettings {
  return {
    palette: Palette.viridis,
    reverse: false,
    colorbar: colorbarSettings(MAP_AUTO_RANGE),
  }
}

export const DEFAULT_MAP: MapSettings = {
  scalar: {
    n_indexed: scalarDefaults(),
    goodness: scalarDefaults(),
    rms_error: scalarDefaults(),
    n_patterns: scalarDefaults(),
  },
  rodriguesSymmetry: OrientationSymmetry.auto,
  rodriguesReference: "lab",
  rodriguesStep: null,
  rodriguesMatrix: Array.from({ length: 9 }, () => null),
  surface: "normal",
  surfaceMatrix: DEFAULT_SURFACE_MATRIX,
  axes2d: DEFAULT_AXES_2D,
  axes3d: DEFAULT_AXES_3D,
  markerSize: 10,
  nonIndexed: "gray",
}

export const DEFAULT_POLE: PoleSettings = {
  surface: "normal",
  surfaceMatrix: DEFAULT_SURFACE_MATRIX,
  scheme: "hsv",
  radius: 22.5,
  markerSize: 6,
  reference: null,
}

export const DEFAULT_DETECTOR: DetectorSettings = {
  showDetected: true,
  showIndexed: true,
  simulate: false,
  showLabels: true,
  markerSize: 10,
  labelSize: 10,
  showImage: true,
  colormap: "gray",
  colorbar: colorbarSettings(FRAME_AUTO_RANGE),
  opacity: 0.8,
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : {}
}

function oneOf<T>(choices: readonly T[], value: unknown, fallback: T): T {
  return choices.find((choice) => choice === value) ?? fallback
}

const finite = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value)

export function isMarkerSize(value: unknown): value is number {
  return (
    finite(value) &&
    Number.isInteger(value) &&
    value >= MARKER_SIZE_RANGE[0] &&
    value <= MARKER_SIZE_RANGE[1]
  )
}

export function isStep(value: unknown): value is number {
  return finite(value) && Number.isSafeInteger(value) && value >= 0
}

function readRodriguesStep(value: unknown): RodriguesStep | null {
  if (typeof value !== "object" || value === null) return null
  const { indexingId, step } = value as Record<string, unknown>
  return isStep(indexingId) && indexingId > 0 && isStep(step)
    ? { indexingId, step }
    : null
}

function readMatrix(value: unknown, fallback: Matrix3): Matrix3 {
  return Array.isArray(value) && value.length === 9 && value.every(finite)
    ? value
    : fallback
}

function readAxes<T extends readonly string[]>(value: unknown, fallback: T): T {
  return Array.isArray(value) &&
    value.length === fallback.length &&
    value.every((axis) => typeof axis === "string" && axis !== "") &&
    new Set(value).size === value.length
    ? (value as unknown as T)
    : fallback
}

const inRange = (value: unknown, [lo, hi]: readonly [number, number]) =>
  finite(value) && Number.isInteger(value) && value >= lo && value <= hi

export function isDetectorMarkerSize(value: unknown): value is number {
  return inRange(value, DETECTOR_MARKER_RANGE)
}

export function isDetectorLabelSize(value: unknown): value is number {
  return inRange(value, DETECTOR_LABEL_RANGE)
}

export function isOpacity(value: unknown): value is number {
  return finite(value) && value >= 0 && value <= 1
}

function readScalar(value: unknown): ScalarColorSettings {
  const fallback = scalarDefaults()
  const { palette, reverse, colorbar } = record(value)
  return {
    palette: oneOf(MAP_PALETTES, palette, fallback.palette),
    reverse: typeof reverse === "boolean" ? reverse : fallback.reverse,
    colorbar: readColorbarSettings(colorbar, MAP_AUTO_RANGE),
  }
}

export function readMap(value: unknown): MapSettings {
  const v = record(value)
  const scalar = record(v.scalar)
  const matrix = v.rodriguesMatrix
  return {
    scalar: {
      n_indexed: readScalar(scalar.n_indexed),
      goodness: readScalar(scalar.goodness),
      rms_error: readScalar(scalar.rms_error),
      n_patterns: readScalar(scalar.n_patterns),
    },
    rodriguesSymmetry: oneOf(
      Object.values(OrientationSymmetry),
      v.rodriguesSymmetry,
      DEFAULT_MAP.rodriguesSymmetry
    ),
    rodriguesReference: oneOf(
      RODRIGUES_REFERENCES,
      v.rodriguesReference,
      DEFAULT_MAP.rodriguesReference
    ),
    rodriguesStep: readRodriguesStep(v.rodriguesStep),
    rodriguesMatrix:
      Array.isArray(matrix) &&
      matrix.length === 9 &&
      matrix.every((entry) => entry === null || finite(entry))
        ? (matrix as (number | null)[])
        : DEFAULT_MAP.rodriguesMatrix,
    surface: oneOf(SURFACES, v.surface, DEFAULT_MAP.surface),
    surfaceMatrix: readMatrix(v.surfaceMatrix, DEFAULT_MAP.surfaceMatrix),
    axes2d: readAxes(v.axes2d, DEFAULT_MAP.axes2d),
    axes3d: readAxes(v.axes3d, DEFAULT_MAP.axes3d),
    markerSize: isMarkerSize(v.markerSize)
      ? v.markerSize
      : DEFAULT_MAP.markerSize,
    nonIndexed: oneOf(NON_INDEXED_COLORS, v.nonIndexed, DEFAULT_MAP.nonIndexed),
  }
}

function readPattern(value: unknown): PolePattern | null {
  const { step, patternIndex } = record(value)
  return isStep(step) && isStep(patternIndex) ? { step, patternIndex } : null
}

function readReference(value: unknown): PoleReference | null {
  const v = record(value)
  const { indexingId, center, previousColor } = v
  if (
    !isStep(indexingId) ||
    !Array.isArray(center) ||
    center.length !== 2 ||
    !center.every(finite)
  ) {
    return null
  }
  return {
    indexingId,
    // The flat shape: the reference held the pattern's step and index itself.
    pattern: "pattern" in v ? readPattern(v.pattern) : readPattern(v),
    center: [center[0] as number, center[1] as number],
    // A reference stored without one returns to the default mode.
    previousColor: isRestorableColor(previousColor)
      ? previousColor
      : MapColor.cubic_ipf,
  }
}

export function readPole(value: unknown): PoleSettings {
  const v = record(value)
  return {
    surface: oneOf(SURFACES, v.surface, DEFAULT_POLE.surface),
    surfaceMatrix: readMatrix(v.surfaceMatrix, DEFAULT_POLE.surfaceMatrix),
    scheme: oneOf(POLE_SCHEMES, v.scheme, DEFAULT_POLE.scheme),
    radius:
      finite(v.radius) && v.radius > 0 && v.radius <= 180
        ? v.radius
        : DEFAULT_POLE.radius,
    markerSize: isMarkerSize(v.markerSize)
      ? v.markerSize
      : DEFAULT_POLE.markerSize,
    reference: readReference(v.reference),
  }
}

export function readDetector(value: unknown): DetectorSettings {
  const v = record(value)
  const flag = (
    key:
      "showDetected" | "showIndexed" | "simulate" | "showLabels" | "showImage"
  ) => {
    const stored = v[key]
    return typeof stored === "boolean" ? stored : DEFAULT_DETECTOR[key]
  }
  return {
    showDetected: flag("showDetected"),
    showIndexed: flag("showIndexed"),
    simulate: flag("simulate"),
    showLabels: flag("showLabels"),
    markerSize: isDetectorMarkerSize(v.markerSize)
      ? v.markerSize
      : DEFAULT_DETECTOR.markerSize,
    labelSize: isDetectorLabelSize(v.labelSize)
      ? v.labelSize
      : DEFAULT_DETECTOR.labelSize,
    showImage: flag("showImage"),
    colormap: oneOf(FRAME_COLORMAPS, v.colormap, DEFAULT_DETECTOR.colormap),
    colorbar: readColorbarSettings(v.colorbar, FRAME_AUTO_RANGE),
    opacity: isOpacity(v.opacity) ? v.opacity : DEFAULT_DETECTOR.opacity,
  }
}
