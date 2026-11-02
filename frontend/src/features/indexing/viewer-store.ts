// The indexing viewer's settings: one Zustand store, persisted to localStorage under
// laueportal:view:indexing-viewer:v<version>. A stored value of another version is discarded (no
// migrations); a stored field that is not valid falls back to its default.
//
// Each tab adds its settings as a slice: a field of IndexingViewerSettings, its default in
// DEFAULT_SETTINGS, its reader in readSettings (so a stored or linked value is checked), and its
// actions. "Copy link to this view" packs the whole settings object (settingsOf).
//
// The scope is saved at once, but the products read the applied scope (`useScopeParams`): a typed N
// applies after a pause or on Enter/blur (a debounced input), every other change at once.
import { useMemo } from "react"
import { create } from "zustand"
import {
  createJSONStorage,
  persist,
  type StateStorage,
} from "zustand/middleware"

import {
  DEFAULT_DETECTOR,
  DEFAULT_MAP,
  DEFAULT_POLE,
  readDetector,
  readMap,
  readPole,
  type DetectorSettings,
  type MapSettings,
  type PoleSettings,
  type ScalarColorSettings,
  type ScalarMode,
} from "./viewer-slices"

/** The schema version of the stored settings and of copied links. */
export const VIEWER_SCHEMA_VERSION = 1
export const VIEWER_STORE_KEY = `laueportal:view:indexing-viewer:v${String(VIEWER_SCHEMA_VERSION)}`

/** The scope bar: which records every tab reads. */
export interface ScopeSettings {
  /** "Load only pattern 0". */
  pattern0Only: boolean
  /** "Skip steps with peaks ≤ N": N; 0 keeps every step. */
  skipPeaksAtMost: number
}

export interface IndexingViewerSettings {
  scope: ScopeSettings
  /** The Color Map tab (viewer-slices.ts). */
  map: MapSettings
  /** The Pole Figure tab's settings, which the map's Pole HSV and misorientation modes read. */
  pole: PoleSettings
  /** The Detector View tab's overlays, sizes and image (viewer-slices.ts). */
  detector: DetectorSettings
}

/** The largest N: the API takes min_peaks = N + 1 up to 100,000. */
export const MAX_SKIP_PEAKS = 99_999

export const DEFAULT_SCOPE: ScopeSettings = {
  pattern0Only: true,
  skipPeaksAtMost: 3,
}

export const DEFAULT_SETTINGS: IndexingViewerSettings = {
  scope: DEFAULT_SCOPE,
  map: DEFAULT_MAP,
  pole: DEFAULT_POLE,
  detector: DEFAULT_DETECTOR,
}

/** How long a typed N waits before the products read it (unless Enter or leaving the field applies it). */
export const SCOPE_APPLY_DELAY_MS = 300

export interface ScopeParams {
  pattern0_only: boolean
  min_peaks: number
}

/** The API's scope parameters: min_peaks = N + 1 ("peaks ≤ N" skipped), and N = 0 sends 0 (off). */
export function scopeParams(scope: ScopeSettings): ScopeParams {
  return {
    pattern0_only: scope.pattern0Only,
    min_peaks: scope.skipPeaksAtMost === 0 ? 0 : scope.skipPeaksAtMost + 1,
  }
}

export function isSkipPeaks(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isInteger(value) &&
    value >= 0 &&
    value <= MAX_SKIP_PEAKS
  )
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : {}
}

function readScope(value: unknown): ScopeSettings {
  const { pattern0Only, skipPeaksAtMost } = record(value)
  return {
    pattern0Only:
      typeof pattern0Only === "boolean"
        ? pattern0Only
        : DEFAULT_SCOPE.pattern0Only,
    skipPeaksAtMost: isSkipPeaks(skipPeaksAtMost)
      ? skipPeaksAtMost
      : DEFAULT_SCOPE.skipPeaksAtMost,
  }
}

/** Settings from a stored or linked value: each field checked, defaults for the rest. */
export function readSettings(value: unknown): IndexingViewerSettings {
  const { scope, map, pole, detector } = record(value)
  return {
    scope: readScope(scope),
    map: readMap(map),
    pole: readPole(pole),
    detector: readDetector(detector),
  }
}

export interface IndexingViewerState extends IndexingViewerSettings {
  /** The scope the products read (`useScopeParams`): `scope`, once a typed N has applied. Not stored. */
  appliedScope: ScopeSettings
  /** Change the scope; it applies at once. */
  setScope: (change: Partial<ScopeSettings>) => void
  /** Change the scope as it is typed: saved at once, applied after `SCOPE_APPLY_DELAY_MS`. */
  draftScope: (change: Partial<ScopeSettings>) => void
  /** Apply a typed change now (Enter, leaving the field). */
  applyScope: () => void
  resetScope: () => void
  /** Change the Color Map tab's settings (each checked; an invalid value keeps the field's default). */
  setMap: (change: Partial<MapSettings>) => void
  /** Change a scalar mode's palette, reverse or colorbar. */
  setScalar: (mode: ScalarMode, change: Partial<ScalarColorSettings>) => void
  /** Change the pole figure's settings. */
  setPole: (change: Partial<PoleSettings>) => void
  /** Change the detector view's settings (each checked, as the others). */
  setDetector: (change: Partial<DetectorSettings>) => void
  /** Replace every setting (a copied link's); they apply at once. */
  applySettings: (settings: IndexingViewerSettings) => void
}

/** The settings alone (what is stored and what a link packs). */
export function settingsOf(
  state: IndexingViewerSettings
): IndexingViewerSettings {
  return {
    scope: state.scope,
    map: state.map,
    pole: state.pole,
    detector: state.detector,
  }
}

/** A store on `storage` (localStorage in the app); tests make their own. */
export function createIndexingViewerStore(
  storage: StateStorage = localStorage
) {
  // A typed scope change waiting to apply.
  let pending: ReturnType<typeof setTimeout> | undefined
  const settle = () => {
    clearTimeout(pending)
    pending = undefined
  }
  return create<IndexingViewerState>()(
    persist<IndexingViewerState, [], [], IndexingViewerSettings>(
      (set) => ({
        ...DEFAULT_SETTINGS,
        appliedScope: DEFAULT_SETTINGS.scope,
        setScope: (change) => {
          settle()
          set((state) => {
            const scope = readScope({ ...state.scope, ...change })
            return { scope, appliedScope: scope }
          })
        },
        draftScope: (change) => {
          settle()
          set((state) => ({ scope: readScope({ ...state.scope, ...change }) }))
          pending = setTimeout(() => {
            pending = undefined
            set((state) => ({ appliedScope: state.scope }))
          }, SCOPE_APPLY_DELAY_MS)
        },
        applyScope: () => {
          settle()
          set((state) =>
            state.appliedScope === state.scope
              ? state
              : { appliedScope: state.scope }
          )
        },
        resetScope: () => {
          settle()
          set({ scope: DEFAULT_SCOPE, appliedScope: DEFAULT_SCOPE })
        },
        setMap: (change) => {
          set((state) => ({ map: readMap({ ...state.map, ...change }) }))
        },
        setScalar: (mode, change) => {
          set((state) => ({
            map: readMap({
              ...state.map,
              scalar: {
                ...state.map.scalar,
                [mode]: { ...state.map.scalar[mode], ...change },
              },
            }),
          }))
        },
        setPole: (change) => {
          set((state) => ({ pole: readPole({ ...state.pole, ...change }) }))
        },
        setDetector: (change) => {
          set((state) => ({
            detector: readDetector({ ...state.detector, ...change }),
          }))
        },
        applySettings: (settings) => {
          settle()
          const read = readSettings(settings)
          set({ ...read, appliedScope: read.scope })
        },
      }),
      {
        name: VIEWER_STORE_KEY,
        version: VIEWER_SCHEMA_VERSION,
        storage: createJSONStorage(() => storage),
        partialize: settingsOf,
        // Another version's value is discarded, not migrated.
        migrate: () => DEFAULT_SETTINGS,
        merge: (stored, current) => {
          const read = readSettings(stored)
          return { ...current, ...read, appliedScope: read.scope }
        },
      }
    )
  )
}

export const useIndexingViewerStore = createIndexingViewerStore()

/**
 * The scope's API parameters for every product query (`pattern0_only`, `min_peaks`): the applied
 * scope, so a typed N re-reads the products once, after the pause or on Enter/blur.
 */
export function useScopeParams(): ScopeParams {
  const applied = useIndexingViewerStore((state) => state.appliedScope)
  return useMemo(() => scopeParams(applied), [applied])
}
