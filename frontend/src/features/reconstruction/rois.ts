// The ROI Inspector's ROIs: square ROIs placed where the reference image is clicked, as the API
// places them (lauelab's `square_bounds`: never clipped or shifted), at most the API's 32, each with
// its own colour from a fixed order. They are kept in the browser per run and point: one Zustand
// store, each run's point under its own localStorage key, read when that point is first shown.
import { useMemo } from "react"
import { create } from "zustand"

/** The most ROIs one request takes (the API's `MAX_ROIS`). */
export const MAX_ROIS = 32

/**
 * The ROI colours, in their fixed order, one set for both themes, so an ROI's colour is the same on
 * the image, in the list and in the chart. All ROIs share one image and one chart, so every pair is
 * compared, not only neighbours (OKLab ΔE ×100; colour-vision deficiency by Machado 2009 at full
 * severity): at least 15 for normal vision and 10.5 for protanopia and deuteranopia, every colour at
 * least 3:1 on both themes' cards (white and oklch 0.205), OKLCH lightness 0.48-0.67 and chroma at
 * least 0.10. A dichromat's 10.5 is still close for small marks, so each ROI also has its number (on
 * its outline, in the list and in the chart's names). The first colours differ the most.
 */
export const ROI_COLORS = [
  "#5a50f0", // indigo
  "#c87800", // orange
  "#a078b9", // lilac
  "#be2d14", // red
  "#a52daf", // purple
  "#00a573", // green
  "#007355", // teal
  "#0096fa", // sky blue
] as const

/** A stored ROI: the clicked pixel (x across, y down), its side and its colour. */
export interface Roi {
  x: number
  y: number
  size: number
  color: string
}

/** Half-open `[y0, y1, x0, x1]`: rows y0 to y1 - 1, columns x0 to x1 - 1 (the API's `bounds`). */
export type RoiBounds = readonly [number, number, number, number]

/** A square's first pixel on an axis, `ceil(click - size / 2)` (+ 0: never -0). */
const firstPixel = (click: number, size: number) =>
  Math.ceil(click - size / 2) + 0

/**
 * The pixels a square of `size` at (x, y) covers in an image of `[rows, columns]`, or null when it
 * would extend outside the image (lauelab's `square_bounds`: the legal centre nearest the click, the
 * first pixel on each axis `ceil(click - size / 2)`; never clipped or shifted).
 */
export function roiBounds(
  { x, y, size }: Pick<Roi, "x" | "y" | "size">,
  [rows, columns]: readonly [number, number]
): RoiBounds | null {
  if (!Number.isInteger(size) || size < 1) return null
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null
  const x0 = firstPixel(x, size)
  const y0 = firstPixel(y, size)
  if (x0 < 0 || y0 < 0 || x0 + size > columns || y0 + size > rows) return null
  return [y0, y0 + size, x0, x0 + size]
}

/** The centre of a square's pixels (a half-integer for an even size). */
export function roiCenter({ x, y, size }: Pick<Roi, "x" | "y" | "size">): {
  x: number
  y: number
} {
  const x0 = firstPixel(x, size)
  const y0 = firstPixel(y, size)
  return { x: x0 + (size - 1) / 2, y: y0 + (size - 1) / 2 }
}

/**
 * The next ROI's colour: the first in the fixed order among those the ROIs use least, so the first
 * eight differ and a deleted ROI's colour is the next one given; the others keep theirs.
 */
export function nextRoiColor(rois: readonly Roi[]): string {
  const counts = ROI_COLORS.map(
    (color) => rois.filter((roi) => roi.color === color).length
  )
  const least = Math.min(...counts)
  return ROI_COLORS[counts.indexOf(least)] ?? ROI_COLORS[0]
}

/** Why a click added no ROI (a status). */
export type RoiRefusal = "outside" | "full"

export const REFUSAL_TEXT: Record<RoiRefusal, string> = {
  outside: "ROI outside the image",
  full: `At most ${String(MAX_ROIS)} ROIs`,
}

/**
 * The ROIs after a click at pixel (x, y) of an image of `[rows, columns]` adds a square of `size`, or
 * why it adds none: the square would extend outside the image, or there are 32 already.
 */
export function addRoi(
  rois: readonly Roi[],
  at: { x: number; y: number },
  size: number,
  shape: readonly [number, number]
): { rois: Roi[] } | { refused: RoiRefusal } {
  if (rois.length >= MAX_ROIS) return { refused: "full" }
  const roi = { x: at.x, y: at.y, size }
  if (!roiBounds(roi, shape)) return { refused: "outside" }
  return { rois: [...rois, { ...roi, color: nextRoiColor(rois) }] }
}

/** The localStorage key of a run's point's ROIs. */
export function roiStorageKey(reconstructionId: number, pointId: string) {
  return `laueportal:rois:reconstruction:${String(reconstructionId)}:${pointId}`
}

const COLOR = /^#[0-9a-f]{6}$/i

/** ROIs from a stored value: the readable ones (at most 32); none for anything else. */
export function readRois(value: unknown): Roi[] {
  if (!Array.isArray(value)) return []
  const rois: Roi[] = []
  for (const item of value as unknown[]) {
    if (typeof item !== "object" || item === null) continue
    const { x, y, size, color } = item as Record<string, unknown>
    if (
      typeof x === "number" &&
      Number.isFinite(x) &&
      typeof y === "number" &&
      Number.isFinite(y) &&
      typeof size === "number" &&
      Number.isInteger(size) &&
      size >= 1 &&
      typeof color === "string" &&
      COLOR.test(color)
    ) {
      rois.push({ x, y, size, color })
    }
  }
  return rois.slice(0, MAX_ROIS)
}

interface StorageLike {
  getItem: (key: string) => string | null
  setItem: (key: string, value: string) => void
}

function load(storage: StorageLike, key: string): Roi[] {
  try {
    const text = storage.getItem(key)
    return text === null ? [] : readRois(JSON.parse(text))
  } catch {
    return []
  }
}

export interface RoiState {
  /** The ROIs of each key read or changed so far. */
  byKey: Readonly<Record<string, readonly Roi[]>>
  /** A point's ROIs: those held, else those stored (read once). */
  roisOf: (key: string) => readonly Roi[]
  setRois: (key: string, rois: readonly Roi[]) => void
}

/** A store on `storage` (localStorage in the app); tests make their own. */
export function createRoiStore(storage: StorageLike = localStorage) {
  return create<RoiState>()((set, get) => ({
    byKey: {},
    roisOf: (key) => get().byKey[key] ?? load(storage, key),
    setRois: (key, rois) => {
      try {
        storage.setItem(key, JSON.stringify(rois))
      } catch {
        // Storage full or refused: the ROIs stay for this page.
      }
      set((state) => ({ byKey: { ...state.byKey, [key]: rois } }))
    },
  }))
}

export const useRoiStore = createRoiStore()

/** A run's point's ROIs and their setter (each change is stored at once). */
export function usePointRois(
  reconstructionId: number,
  pointId: string,
  store = useRoiStore
): [readonly Roi[], (rois: readonly Roi[]) => void] {
  const key = roiStorageKey(reconstructionId, pointId)
  const held = store((state) => state.byKey[key])
  const setRois = store((state) => state.setRois)
  const rois = useMemo(
    () => held ?? store.getState().roisOf(key),
    [held, key, store]
  )
  return [
    rois,
    (next) => {
      setRois(key, next)
    },
  ]
}
