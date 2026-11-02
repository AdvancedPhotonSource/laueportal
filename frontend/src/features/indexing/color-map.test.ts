import { describe, expect, it } from "vitest"

import { MapColor } from "@/api"
import {
  G_REF_HINT,
  RODRIGUES_STEP_HINT,
  legendCaption,
  mapRequest,
  MISORIENTATION_HINT,
  resolveAxes,
  type MapRequestInput,
} from "./color-map"
import { DEFAULT_MAP, DEFAULT_POLE } from "./viewer-slices"

const SCOPE = { pattern0_only: true, min_peaks: 4 }
const REFERENCE = {
  indexingId: 1,
  pattern: { step: 3, patternIndex: 0 },
  center: [0.25, -0.5],
  previousColor: MapColor.cubic_ipf,
} as const

function input(change: Partial<MapRequestInput> = {}): MapRequestInput {
  return {
    indexingId: 1,
    color: MapColor.cubic_ipf,
    axes: ["X", "H"],
    map: DEFAULT_MAP,
    pole: DEFAULT_POLE,
    hkl: [1, 1, 0],
    scope: SCOPE,
    ...change,
  }
}

describe("the map's request", () => {
  it.each([
    [
      "cubic IPF: the surface",
      input({ map: { ...DEFAULT_MAP, surface: "H" } }),
      { color: "cubic_ipf", surface: "H" },
    ],
    [
      "cubic IPF: a custom surface's nine numbers",
      input({
        map: {
          ...DEFAULT_MAP,
          surface: "custom",
          surfaceMatrix: [1, 0, 0, 0, 1, 0, 0, 0, 1],
        },
      }),
      { color: "cubic_ipf", surface: "1,0,0,0,1,0,0,0,1" },
    ],
    [
      "Rodrigues: symmetry and the lab frame",
      input({ color: MapColor.rodrigues }),
      { color: "rodrigues", rodrigues_symmetry: "auto", rodrigues_ref: "lab" },
    ],
    [
      "Rodrigues: a reference step",
      input({
        color: MapColor.rodrigues,
        map: {
          ...DEFAULT_MAP,
          rodriguesSymmetry: "cubic",
          rodriguesReference: "step",
          rodriguesStep: { indexingId: 1, step: 12 },
        },
      }),
      { color: "rodrigues", rodrigues_symmetry: "cubic", rodrigues_ref: "12" },
    ],
    [
      "Rodrigues: a custom G_ref",
      input({
        color: MapColor.rodrigues,
        map: {
          ...DEFAULT_MAP,
          rodriguesReference: "custom",
          rodriguesMatrix: [1, 2, 3, 4, 5, 6, 7, 8, 9],
        },
      }),
      {
        color: "rodrigues",
        rodrigues_symmetry: "auto",
        rodrigues_ref: "1,2,3,4,5,6,7,8,9",
      },
    ],
    [
      "misorientation: this run's clicked pole and the symmetry",
      input({
        color: MapColor.misorientation,
        pole: { ...DEFAULT_POLE, reference: REFERENCE },
      }),
      {
        color: "misorientation",
        rodrigues_symmetry: "auto",
        ref_pattern: "3,0",
      },
    ],
    [
      "pole HSV: the pole figure's HKL, surface, centre and radius",
      input({
        color: MapColor.pole_hsv,
        // The map's own surface is not the one sent.
        map: { ...DEFAULT_MAP, surface: "X" },
        pole: {
          ...DEFAULT_POLE,
          surface: "F",
          radius: 30,
          reference: REFERENCE,
        },
      }),
      {
        color: "pole_hsv",
        surface: "F",
        pole_hkl: "1,1,0",
        pole_center: "0.25,-0.5",
        pole_radius: 30,
      },
    ],
    [
      "a scalar mode: neither palette, reverse nor range (the browser maps the values)",
      input({
        color: MapColor.goodness,
        axes: ["X", "Y", "Z"],
        map: {
          ...DEFAULT_MAP,
          scalar: {
            ...DEFAULT_MAP.scalar,
            goodness: {
              palette: "jet",
              reverse: true,
              colorbar: {
                ...DEFAULT_MAP.scalar.goodness.colorbar,
                min: 1,
                max: 5,
              },
            },
          },
        },
      }),
      { color: "goodness", axes: "X,Y,Z" },
    ],
  ])("%s", (_name, request, expected) => {
    expect(mapRequest(request)).toEqual({
      params: { ...SCOPE, axes: "X,H", ...expected },
    })
  })

  it("waits for what the mode needs: G_ref's nine entries, a reference step and a clicked pole of this run", () => {
    expect(
      mapRequest(
        input({
          color: MapColor.rodrigues,
          map: {
            ...DEFAULT_MAP,
            rodriguesReference: "custom",
            rodriguesMatrix: [1, 2, 3, 4, null, 6, 7, 8, 9],
          },
        })
      )
    ).toEqual({ blocked: G_REF_HINT })
    // No step typed, or another run's.
    for (const rodriguesStep of [null, { indexingId: 2, step: 12 }]) {
      expect(
        mapRequest(
          input({
            color: MapColor.rodrigues,
            map: { ...DEFAULT_MAP, rodriguesReference: "step", rodriguesStep },
          })
        )
      ).toEqual({ blocked: RODRIGUES_STEP_HINT })
    }
    expect(mapRequest(input({ color: MapColor.misorientation }))).toEqual({
      blocked: MISORIENTATION_HINT,
    })
    // A centre placed with the tool, before any pole was clicked: Pole HSV reads it, misorientation
    // still has no reference.
    const placed = {
      ...DEFAULT_POLE,
      reference: { ...REFERENCE, pattern: null },
    }
    expect(
      mapRequest(input({ color: MapColor.misorientation, pole: placed }))
    ).toEqual({ blocked: MISORIENTATION_HINT })
    expect(
      mapRequest(input({ color: MapColor.pole_hsv, pole: placed })).params
        ?.pole_center
    ).toBe("0.25,-0.5")
    // Another run's pole: neither the reference nor the centre.
    const other = {
      ...DEFAULT_POLE,
      reference: { ...REFERENCE, indexingId: 2 },
    }
    expect(
      mapRequest(input({ color: MapColor.misorientation, pole: other }))
    ).toEqual({ blocked: MISORIENTATION_HINT })
    expect(
      mapRequest(input({ color: MapColor.pole_hsv, pole: other })).params
        ?.pole_center
    ).toBe("0,0")
  })
})

describe("the plotted axes", () => {
  const choices = (...names: string[]) =>
    names.map((value) => ({ value, label: value }))

  it("are the stored ones, else the defaults, else the first the results have", () => {
    expect(resolveAxes(["Y", "F"], choices("X", "Y", "H", "F"), "2d")).toEqual([
      "Y",
      "F",
    ])
    expect(resolveAxes(["Y", "depth"], choices("X", "Y", "H"), "2d")).toEqual([
      "X",
      "H",
    ])
    expect(
      resolveAxes(["X", "Y", "Z"], choices("H", "F", "Xlab"), "3d")
    ).toEqual(["H", "F", "Xlab"])
    expect(resolveAxes(["X", "Y", "Z"], choices("H", "F"), "3d")).toBeNull()
  })
})

it("captions the legends", () => {
  expect(legendCaption(MapColor.cubic_ipf, "normal")).toBe(
    "Crystal direction ∥ sample normal"
  )
  expect(legendCaption(MapColor.pole_hsv, "custom")).toBe(
    "Custom surface direction in stereographic HSV"
  )
})
