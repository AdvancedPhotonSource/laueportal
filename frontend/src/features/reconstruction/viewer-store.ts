// The wire reconstruction viewer's settings: one Zustand store, persisted to localStorage under
// laueportal:view:wire-viewer:v<version>, as the indexing viewer's
// (src/features/indexing/viewer-store.ts) but its own. A stored value of another version is discarded
// (no migrations); a stored field that is not valid falls back to its default.
//
// Each tab adds its settings as a slice (the Depth View's `depth`: its colormap and contrast; the
// ROI Inspector's `roi`: its reference image, colormap and contrast, ROI size, trace axes, scale and
// values): a field of WireViewerSettings, its default in DEFAULT_SETTINGS, its reader in readSettings
// (so a stored or linked value is checked), and its actions. A field added with a default keeps the
// schema version. "Copy link to this view" packs the whole settings object (settingsOf). The ROIs are
// not here: they are stored per run and point (rois.ts). The selected point and the depth index are the
// URL's.
import { create } from "zustand"
import {
  createJSONStorage,
  persist,
  type StateStorage,
} from "zustand/middleware"

import { ReconstructionReferenceKind } from "@/api"
import {
  colorbarSettings,
  FRAME_AUTO_RANGE,
  FRAME_COLORMAPS,
  readColorbarSettings,
  type ColorbarSettings,
  type FrameColormap,
} from "@/viz"

/** The schema version of the stored settings and of copied links. */
export const WIRE_VIEWER_SCHEMA_VERSION = 1
export const WIRE_VIEWER_STORE_KEY = `laueportal:view:wire-viewer:v${String(WIRE_VIEWER_SCHEMA_VERSION)}`

/**
 * The Depth View's image: its colormap (the nine frame colormaps) and its contrast, the colorbar's
 * settings (automatic, the 1st to 99.9th percentile by default, or the user's min and max, kept
 * across depths and points so frames compare by colour; the scale).
 */
export interface DepthViewSettings {
  colormap: FrameColormap
  colorbar: ColorbarSettings
}

/** The reference images, in lauelab's `REFERENCE_OPTIONS` order. */
export const REFERENCE_KINDS = Object.values(ReconstructionReferenceKind)
/** The traces' x axis: depth (µm) or depth index (lauelab's `DEPTH_AXIS_OPTIONS`). */
export const TRACE_AXES = ["depth", "index"] as const
export type TraceAxis = (typeof TRACE_AXES)[number]
export const TRACE_SCALES = ["linear", "log"] as const
export type TraceScale = (typeof TRACE_SCALES)[number]
/** Each ROI's sums, or those divided by its maximum (lauelab's `INTENSITY_OPTIONS`). */
export const TRACE_VALUES = ["sum", "normalized"] as const
export type TraceValues = (typeof TRACE_VALUES)[number]

/** The largest ROI side the API takes. */
export const MAX_ROI_SIZE = 65_535

/**
 * The ROI Inspector's settings: the reference image (by default the sum of the reconstructed
 * frames), its colormap and contrast (its own: a sum's range is not a depth's), the size of the next
 * ROI (pixels; 9 by default), and the traces' x axis, y scale and values.
 */
export interface RoiViewSettings {
  reference: ReconstructionReferenceKind
  colormap: FrameColormap
  colorbar: ColorbarSettings
  size: number
  axis: TraceAxis
  scale: TraceScale
  values: TraceValues
}

/** Every tab's settings (the Points and Parameters tabs keep none). */
export interface WireViewerSettings {
  depth: DepthViewSettings
  roi: RoiViewSettings
}

export const DEFAULT_DEPTH_VIEW: DepthViewSettings = {
  colormap: "gray",
  colorbar: colorbarSettings(FRAME_AUTO_RANGE),
}

export const DEFAULT_ROI_VIEW: RoiViewSettings = {
  reference: ReconstructionReferenceKind.sum_reconstructed,
  colormap: "gray",
  colorbar: colorbarSettings(FRAME_AUTO_RANGE),
  size: 9,
  axis: "depth",
  scale: "linear",
  values: "sum",
}

export const DEFAULT_SETTINGS: WireViewerSettings = {
  depth: DEFAULT_DEPTH_VIEW,
  roi: DEFAULT_ROI_VIEW,
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : {}
}

function readDepthView(value: unknown): DepthViewSettings {
  const { colormap, colorbar } = record(value)
  return {
    colormap:
      FRAME_COLORMAPS.find((name) => name === colormap) ??
      DEFAULT_DEPTH_VIEW.colormap,
    colorbar: readColorbarSettings(colorbar, FRAME_AUTO_RANGE),
  }
}

function oneOf<T>(values: readonly T[], value: unknown, fallback: T): T {
  return values.find((option) => option === value) ?? fallback
}

/** A whole ROI size the API takes (1 to 65,535 pixels). */
export function isRoiSize(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isInteger(value) &&
    value >= 1 &&
    value <= MAX_ROI_SIZE
  )
}

function readRoiView(value: unknown): RoiViewSettings {
  const { reference, colormap, colorbar, size, axis, scale, values } =
    record(value)
  const fallback = DEFAULT_ROI_VIEW
  return {
    reference: oneOf(REFERENCE_KINDS, reference, fallback.reference),
    colormap: oneOf(FRAME_COLORMAPS, colormap, fallback.colormap),
    colorbar: readColorbarSettings(colorbar, FRAME_AUTO_RANGE),
    size: isRoiSize(size) ? size : fallback.size,
    axis: oneOf(TRACE_AXES, axis, fallback.axis),
    scale: oneOf(TRACE_SCALES, scale, fallback.scale),
    values: oneOf(TRACE_VALUES, values, fallback.values),
  }
}

/** Settings from a stored or linked value: each field checked, defaults for the rest. */
export function readSettings(value: unknown): WireViewerSettings {
  const { depth, roi } = record(value)
  return { depth: readDepthView(depth), roi: readRoiView(roi) }
}

/** The settings alone (what is stored and what a link packs). */
export function settingsOf(state: WireViewerSettings): WireViewerSettings {
  return { depth: state.depth, roi: state.roi }
}

export type WireViewerState = WireViewerSettings & {
  /** Replace every setting (a copied link's); they apply at once. */
  applySettings: (settings: WireViewerSettings) => void
  setDepthView: (patch: Partial<DepthViewSettings>) => void
  setRoiView: (patch: Partial<RoiViewSettings>) => void
}

/** A store on `storage` (localStorage in the app); tests make their own. */
export function createWireViewerStore(storage: StateStorage = localStorage) {
  return create<WireViewerState>()(
    persist<WireViewerState, [], [], WireViewerSettings>(
      (set) => ({
        ...DEFAULT_SETTINGS,
        applySettings: (settings) => {
          set(readSettings(settings))
        },
        setDepthView: (patch) => {
          set((state) => ({ depth: { ...state.depth, ...patch } }))
        },
        setRoiView: (patch) => {
          set((state) => {
            const roi = { ...state.roi, ...patch }
            // A manual range is one reference kind's (a raw frame's tops at 65,535, a raw sum's
            // reaches 10^7): another kind starts on the automatic range.
            if (
              patch.reference !== undefined &&
              patch.reference !== state.roi.reference &&
              patch.colorbar === undefined &&
              roi.colorbar.auto === null
            ) {
              roi.colorbar = {
                ...roi.colorbar,
                auto: FRAME_AUTO_RANGE,
                min: NaN,
                max: NaN,
              }
            }
            return { roi }
          })
        },
      }),
      {
        name: WIRE_VIEWER_STORE_KEY,
        version: WIRE_VIEWER_SCHEMA_VERSION,
        storage: createJSONStorage(() => storage),
        partialize: settingsOf,
        // Another version's value is discarded, not migrated.
        migrate: () => DEFAULT_SETTINGS,
        merge: (stored, current) => ({ ...current, ...readSettings(stored) }),
      }
    )
  )
}

export const useWireViewerStore = createWireViewerStore()
