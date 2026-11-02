// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest"

import {
  createIndexingViewerStore,
  DEFAULT_SETTINGS,
  SCOPE_APPLY_DELAY_MS,
  scopeParams,
  VIEWER_STORE_KEY,
} from "./viewer-store"
import { DEFAULT_DETECTOR, DEFAULT_MAP, DEFAULT_POLE } from "./viewer-slices"

function memoryStorage(entries: Record<string, string> = {}) {
  const values = new Map(Object.entries(entries))
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value)
    },
    removeItem: (key: string) => {
      values.delete(key)
    },
    values,
  }
}

const stored = (state: unknown, version: number) =>
  JSON.stringify({ state, version })

afterEach(() => {
  vi.useRealTimers()
})

describe("the indexing viewer's store", () => {
  it("is kept under laueportal:view:indexing-viewer:v1 and read back", () => {
    expect(VIEWER_STORE_KEY).toBe("laueportal:view:indexing-viewer:v1")
    const storage = memoryStorage()
    const store = createIndexingViewerStore(storage)
    store.getState().setScope({ pattern0Only: false, skipPeaksAtMost: 0 })
    const again = createIndexingViewerStore(storage)
    expect(again.getState().scope).toEqual({
      pattern0Only: false,
      skipPeaksAtMost: 0,
    })
  })

  it("discards a stored value of another schema version", () => {
    const storage = memoryStorage({
      [VIEWER_STORE_KEY]: stored(
        { scope: { pattern0Only: false, skipPeaksAtMost: 9 } },
        0
      ),
    })
    expect(createIndexingViewerStore(storage).getState().scope).toEqual(
      DEFAULT_SETTINGS.scope
    )
  })

  it("falls back to the default of each stored field that is not valid", () => {
    const storage = memoryStorage({
      [VIEWER_STORE_KEY]: stored(
        { scope: { pattern0Only: false, skipPeaksAtMost: -2 } },
        1
      ),
    })
    expect(createIndexingViewerStore(storage).getState().scope).toEqual({
      pattern0Only: false,
      skipPeaksAtMost: 3,
    })
  })

  it("sends the scope as min_peaks = N + 1, and 0 when N is 0", () => {
    expect(scopeParams({ pattern0Only: true, skipPeaksAtMost: 3 })).toEqual({
      pattern0_only: true,
      min_peaks: 4,
    })
    expect(scopeParams({ pattern0Only: false, skipPeaksAtMost: 0 })).toEqual({
      pattern0_only: false,
      min_peaks: 0,
    })
  })

  it("saves a typed N at once and applies it after the pause, or at once on Enter or blur", () => {
    vi.useFakeTimers()
    const storage = memoryStorage()
    const store = createIndexingViewerStore(storage)
    const applied = () => store.getState().appliedScope.skipPeaksAtMost

    store.getState().draftScope({ skipPeaksAtMost: 1 })
    store.getState().draftScope({ skipPeaksAtMost: 12 })
    // saved at once (a reload or a copied link keeps it), not applied yet
    expect(
      createIndexingViewerStore(storage).getState().scope.skipPeaksAtMost
    ).toBe(12)
    expect(applied()).toBe(3)
    vi.advanceTimersByTime(SCOPE_APPLY_DELAY_MS - 1)
    expect(applied()).toBe(3)
    vi.advanceTimersByTime(1)
    expect(applied()).toBe(12)

    store.getState().draftScope({ skipPeaksAtMost: 120 })
    store.getState().applyScope()
    expect(applied()).toBe(120)

    // every other change applies at once, with a typed N still waiting
    store.getState().draftScope({ skipPeaksAtMost: 5 })
    store.getState().setScope({ pattern0Only: false })
    expect(store.getState().appliedScope).toEqual({
      pattern0Only: false,
      skipPeaksAtMost: 5,
    })
  })

  it("keeps the colour map's and the pole figure's settings, each scalar mode's colours apart", () => {
    // The default marker sizes: 10 on the orientation map, 6 on the pole figure
    expect([DEFAULT_MAP.markerSize, DEFAULT_POLE.markerSize]).toEqual([10, 6])
    const storage = memoryStorage()
    const store = createIndexingViewerStore(storage)
    store.getState().setMap({
      markerSize: 12,
      surface: "custom",
      axes2d: ["Y", "F"],
      rodriguesStep: { indexingId: 4, step: 3 },
    })
    store.getState().setScalar("goodness", {
      palette: "jet",
      colorbar: { auto: null, min: 2, max: 9, scale: "log", gamma: 2 },
    })
    store.getState().setPole({
      radius: 30,
      reference: {
        indexingId: 4,
        pattern: { step: 7, patternIndex: 1 },
        center: [0.1, 0.2],
        previousColor: "goodness",
      },
    })
    const again = createIndexingViewerStore(storage).getState()
    expect(again.map).toEqual({
      ...DEFAULT_MAP,
      markerSize: 12,
      surface: "custom",
      axes2d: ["Y", "F"],
      rodriguesStep: { indexingId: 4, step: 3 },
      scalar: {
        ...DEFAULT_MAP.scalar,
        goodness: {
          palette: "jet",
          reverse: false,
          colorbar: { auto: null, min: 2, max: 9, scale: "log", gamma: 2 },
        },
      },
    })
    expect(again.map.scalar.rms_error).toEqual(DEFAULT_MAP.scalar.rms_error)
    expect(again.pole).toEqual({
      ...DEFAULT_POLE,
      radius: 30,
      reference: {
        indexingId: 4,
        pattern: { step: 7, patternIndex: 1 },
        center: [0.1, 0.2],
        previousColor: "goodness",
      },
    })
  })

  it("checks each map and pole field it reads, keeping the valid ones", () => {
    const storage = memoryStorage({
      [VIEWER_STORE_KEY]: stored(
        {
          map: {
            markerSize: 76,
            nonIndexed: "red",
            surface: "diagonal",
            surfaceMatrix: [1, 2, 3],
            rodriguesMatrix: [1, null, 3, 4, 5, 6, 7, 8, 9],
            // A step without its run.
            rodriguesStep: 12,
            axes3d: ["X", "X", "Z"],
            scalar: { n_patterns: { palette: "Jet", reverse: true } },
          },
          pole: { radius: 0, scheme: "ipf", reference: { indexingId: 1 } },
        },
        1
      ),
    })
    const { map, pole } = createIndexingViewerStore(storage).getState()
    expect(map).toEqual({
      ...DEFAULT_MAP,
      nonIndexed: "red",
      rodriguesMatrix: [1, null, 3, 4, 5, 6, 7, 8, 9],
      scalar: {
        ...DEFAULT_MAP.scalar,
        n_patterns: { ...DEFAULT_MAP.scalar.n_patterns, reverse: true },
      },
    })
    expect(pole).toEqual({ ...DEFAULT_POLE, scheme: "ipf" })
  })

  it("keeps the detector view's settings, a typed range with them, and checks each field", () => {
    const storage = memoryStorage()
    const store = createIndexingViewerStore(storage)
    expect(store.getState().detector).toEqual(DEFAULT_DETECTOR)
    expect(DEFAULT_DETECTOR).toMatchObject({
      simulate: false,
      markerSize: 10,
      labelSize: 10,
      opacity: 0.8,
      colorbar: { auto: "p1-99.9" },
    })
    const kept = {
      showDetected: false,
      simulate: true,
      markerSize: 14,
      colormap: "inferno",
      colorbar: { auto: null, min: 10, max: 400, scale: "log", gamma: 2 },
      opacity: 0.35,
    } as const
    store.getState().setDetector(kept)
    expect(createIndexingViewerStore(storage).getState().detector).toEqual({
      ...DEFAULT_DETECTOR,
      ...kept,
    })

    const invalid = memoryStorage({
      [VIEWER_STORE_KEY]: stored(
        {
          detector: {
            showLabels: "no",
            markerSize: 0,
            colormap: "Viridis",
            colorbar: { auto: "median", min: null },
            showImage: false,
          },
        },
        1
      ),
    })
    expect(createIndexingViewerStore(invalid).getState().detector).toEqual({
      ...DEFAULT_DETECTOR,
      showImage: false,
    })
  })
})
