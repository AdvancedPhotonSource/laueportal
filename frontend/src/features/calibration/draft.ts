// The Calibration page's draft: what the user typed and chose, kept in the browser (localStorage) so a
// reload loses nothing. Nothing of it is on the server, which keeps no calibration (each request
// carries the inputs). Per frame: the peaks added by hand and the found peaks removed, the measured
// energies with their sources and the harmonic orders, the starting detector of Match Again, and the
// peak search and indexing settings when changed from the defaults. Peaks are named by their position
// (`peakKey`), so a measurement stays on its peak when the peak list changes around it.
import { create } from "zustand"
import { persist } from "zustand/middleware"

import type {
  CalibrationDetectorValues,
  CalibrationIndexSettings,
  CalibrationMeasurement,
  CalibrationPeak,
  CalibrationPeakSettings,
} from "@/api"

export interface CalibrationFiles {
  frame: string
  geometry: string
  crystal: string
}

export interface Position {
  x: number
  y: number
}

/** A user's value for one peak. */
export interface MeasurementDraft {
  /** keV; null for none. */
  energyKev: number | null
  /** Where the energy was measured, such as a scan number. */
  source: string
  /** The harmonic: the reflection is this multiple of its first-order hkl; null keeps the indexing's. */
  order: number | null
}

export interface FrameDraft {
  /** Peaks added by hand (fitted where the user clicked), in the order added. */
  added: Position[]
  /** The found peaks removed, by `peakKey`. */
  removed: string[]
  /** By `peakKey`. */
  measurements: Record<string, MeasurementDraft>
  /** Match Again's starting detector (a fit's refined values); null: the geometry file's. */
  start: CalibrationDetectorValues | null
  /** null: the setup's defaults. */
  peakSettings: CalibrationPeakSettings | null
  indexSettings: CalibrationIndexSettings | null
}

/** A detector-0 calibration's orientation, for the side detectors' fits with the sample rotation fixed. */
export interface Detector0Orientation {
  frame: string
  reciprocal: number[][]
}

export const EMPTY_DRAFT: FrameDraft = {
  added: [],
  removed: [],
  measurements: {},
  start: null,
  peakSettings: null,
  indexSettings: null,
}

/** A peak's name: its position to 0.01 pixel. */
export function peakKey({ x, y }: Position): string {
  return `${x.toFixed(2)},${y.toFixed(2)}`
}

/** A peak of the list the page indexes: found by peak search, or added by hand. */
export interface ListedPeak extends Position {
  key: string
  origin: "found" | "added"
}

/** The peaks indexed, in order: the found ones not removed (brightest first), then the added ones. */
export function listedPeaks(
  found: readonly CalibrationPeak[],
  draft: FrameDraft
): ListedPeak[] {
  const removed = new Set(draft.removed)
  const peaks: ListedPeak[] = []
  const seen = new Set<string>()
  for (const peak of found) {
    const key = peakKey(peak)
    if (removed.has(key) || seen.has(key)) continue
    seen.add(key)
    peaks.push({ x: peak.x, y: peak.y, key, origin: "found" })
  }
  for (const peak of draft.added) {
    const key = peakKey(peak)
    if (seen.has(key)) continue
    seen.add(key)
    peaks.push({ x: peak.x, y: peak.y, key, origin: "added" })
  }
  return peaks
}

/** The draft's measurements as the API takes them, by index in `peaks`; peaks without a value are left out. */
export function measurementsFor(
  peaks: readonly ListedPeak[],
  draft: FrameDraft
): CalibrationMeasurement[] {
  const measured: CalibrationMeasurement[] = []
  peaks.forEach((peak, index) => {
    const value = draft.measurements[peak.key]
    if (!value || (value.energyKev === null && value.order === null)) return
    measured.push({
      peak: index,
      energy_kev: value.energyKev,
      source: value.source,
      order: value.order,
    })
  })
  return measured
}

/** The listed peak within `radius` pixels of (x, y) nearest to it, or null. */
export function nearestPeak(
  peaks: readonly ListedPeak[],
  x: number,
  y: number,
  radius: number
): ListedPeak | null {
  let best: ListedPeak | null = null
  let distance = radius
  for (const peak of peaks) {
    const d = Math.hypot(peak.x - x, peak.y - y)
    if (d <= distance) {
      best = peak
      distance = d
    }
  }
  return best
}

interface CalibrationDraftState {
  files: CalibrationFiles
  /** The folder Save writes in; "" for the geometry folder. */
  saveFolder: string
  /** Whether the frame's marks show their hkl, and the labels' size (CSS px, 6 to 24). */
  showLabels: boolean
  labelSize: number
  frames: Record<string, FrameDraft>
  detector0: Detector0Orientation | null
  setFiles: (files: Partial<CalibrationFiles>) => void
  updateFrame: (
    frame: string,
    change: (draft: FrameDraft) => FrameDraft
  ) => void
  /** Forget the frame's draft (Start Over). */
  clearFrame: (frame: string) => void
  setDetector0: (orientation: Detector0Orientation | null) => void
  setSaveFolder: (folder: string) => void
  setLabels: (labels: { showLabels?: boolean; labelSize?: number }) => void
}

export const CALIBRATION_STORE_KEY = "laueportal:calibration:v1"

/** The labels' size (CSS px), as the Detector View's default. */
export const DEFAULT_LABEL_SIZE = 10

export const useCalibrationDraftStore = create<CalibrationDraftState>()(
  persist(
    (set) => ({
      files: { frame: "", geometry: "", crystal: "" },
      frames: {},
      detector0: null,
      saveFolder: "",
      showLabels: true,
      labelSize: DEFAULT_LABEL_SIZE,
      setSaveFolder: (saveFolder) => {
        set({ saveFolder })
      },
      setLabels: (labels) => {
        set(labels)
      },
      setFiles: (files) => {
        set((state) => ({ files: { ...state.files, ...files } }))
      },
      updateFrame: (frame, change) => {
        set((state) => ({
          frames: {
            ...state.frames,
            [frame]: change(state.frames[frame] ?? EMPTY_DRAFT),
          },
        }))
      },
      clearFrame: (frame) => {
        set((state) => {
          const frames = { ...state.frames }
          // eslint-disable-next-line @typescript-eslint/no-dynamic-delete -- a record keyed by path
          delete frames[frame]
          return { frames }
        })
      },
      setDetector0: (orientation) => {
        set({ detector0: orientation })
      },
    }),
    {
      name: CALIBRATION_STORE_KEY,
      version: 1,
      partialize: ({
        files,
        frames,
        detector0,
        saveFolder,
        showLabels,
        labelSize,
      }) => ({
        files,
        frames,
        detector0,
        saveFolder,
        showLabels,
        labelSize,
      }),
    }
  )
)

/** Back to an empty draft (tests). */
export function resetCalibrationDraftStore(): void {
  useCalibrationDraftStore.setState({
    files: { frame: "", geometry: "", crystal: "" },
    frames: {},
    detector0: null,
    saveFolder: "",
    showLabels: true,
    labelSize: DEFAULT_LABEL_SIZE,
  })
}
