import { describe, expect, it } from "vitest"

import { CUBIC_SETTINGS } from "@/test/indexing"

import {
  cameraBasis,
  cellEdges,
  cellFaces,
  crystalDirection,
  cross,
  dot,
  dragged,
  dualBasis,
  latticeParameters,
  mat3Of,
  multiply,
  nearestSetting,
  normalized,
  norm,
  rotationFromReference,
  scale,
  transpose,
  type Mat3,
  type Vec3,
} from "./geometry"

const RAD = Math.PI / 180
const CUBIC = CUBIC_SETTINGS.flatMap((rows) => {
  const m = mat3Of(rows)
  return m ? [m] : []
})

/** lauelab's native direct basis: c ∥ Z, b in the YZ plane. */
function direct(
  a: number,
  b: number,
  c: number,
  al: number,
  be: number,
  ga: number
): Mat3 {
  const az = a * Math.cos(be * RAD)
  const ay =
    (a * (Math.cos(ga * RAD) - Math.cos(be * RAD) * Math.cos(al * RAD))) /
    Math.sin(al * RAD)
  return [
    [Math.sqrt(a * a - ay * ay - az * az), ay, az],
    [0, b * Math.sin(al * RAD), b * Math.cos(al * RAD)],
    [0, 0, c],
  ]
}

/** A rotation by `deg` about the unit `axis` (Rodrigues' formula). */
function rotation(axis: Vec3, deg: number): Mat3 {
  const [x, y, z] = axis
  const c = Math.cos(deg * RAD)
  const s = Math.sin(deg * RAD)
  const t = 1 - c
  return [
    [t * x * x + c, t * x * y - s * z, t * x * z + s * y],
    [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
    [t * x * z - s * y, t * y * z + s * x, t * z * z + c],
  ]
}

/** The rows of `basis` turned by U: v' = U · v, so R' = R · Uᵀ. */
const rotated = (basis: Mat3, u: Mat3) => multiply(basis, transpose(u))

function expectClose(
  actual: readonly number[],
  expected: readonly number[],
  digits = 9
) {
  expect(actual).toHaveLength(expected.length)
  actual.forEach((value, i) => {
    expect(value).toBeCloseTo(expected[i] ?? NaN, digits)
  })
}

describe("the bases", () => {
  it("the dual of a direct basis is its reciprocal, with 2π, and back", () => {
    const d = direct(0.5, 0.6, 0.7, 80, 95, 110)
    const r = dualBasis(d)
    for (const i of [0, 1, 2] as const)
      for (const j of [0, 1, 2] as const)
        expect(dot(d[i], r[j])).toBeCloseTo(i === j ? 2 * Math.PI : 0, 9)
    dualBasis(r).forEach((row, i) => {
      expectClose(row, d[i] ?? [])
    })
  })

  it("reads a triclinic cell's lengths and angles", () => {
    const parameters = latticeParameters(direct(0.5, 0.6, 0.7, 80, 95, 110))
    expectClose(parameters.lengths, [0.5, 0.6, 0.7])
    expectClose(parameters.angles, [80, 95, 110])
  })

  it("reads three finite rows of three, nothing else", () => {
    expect(
      mat3Of([
        [1, 0, 0],
        [0, 1, 0],
        [0, 0, 1],
      ])
    ).toEqual([
      [1, 0, 0],
      [0, 1, 0],
      [0, 0, 1],
    ])
    expect(
      mat3Of([
        [1, 0, 0],
        [0, 1, 0],
      ])
    ).toBeNull()
    expect(
      mat3Of([
        [1, 0],
        [0, 1, 0],
        [0, 0, 1],
      ])
    ).toBeNull()
    expect(
      mat3Of([
        [NaN, 0, 0],
        [0, 1, 0],
        [0, 0, 1],
      ])
    ).toBeNull()
  })

  it("scales the longest edge to 1", () => {
    expect(
      Math.max(...normalized(direct(0.5, 0.6, 0.7, 90, 90, 90)).map(norm))
    ).toBeCloseTo(1)
  })
})

describe("the rotation from the reference", () => {
  const reference = dualBasis(direct(0.35238, 0.35238, 0.35238, 90, 90, 90))
  const axis: Vec3 = [1 / Math.sqrt(3), 1 / Math.sqrt(3), 1 / Math.sqrt(3)]

  it("recovers the angle and the lab axis of a turned cell", () => {
    const turned = rotated(reference, rotation(axis, 25))
    const found = rotationFromReference(reference, turned)
    expect(found.angleDeg).toBeCloseTo(25, 6)
    expectClose(found.axis ?? [], axis, 6)
  })

  it("is the nearest rotation of a strained cell", () => {
    const strained: Mat3 = [
      scale(reference[0], 1.002),
      reference[1],
      reference[2],
    ]
    const found = rotationFromReference(
      reference,
      rotated(strained, rotation([0, 0, 1], 10))
    )
    expect(found.angleDeg).toBeCloseTo(10, 3)
    const u = found.matrix
    expect(dot(u[0], cross(u[1], u[2]))).toBeCloseTo(1, 9)
  })

  it("finds the axis of a half turn", () => {
    const found = rotationFromReference(
      reference,
      rotated(reference, rotation(axis, 180))
    )
    expect(found.angleDeg).toBeCloseTo(180, 6)
    expectClose(found.axis ?? [], axis, 6)
  })

  it("has no axis at zero", () => {
    expect(rotationFromReference(reference, reference)).toMatchObject({
      angleDeg: 0,
      axis: null,
    })
  })

  it("takes the setting nearest the reference: for a cube, 100° about Z is 10°", () => {
    const turned = rotated(reference, rotation([0, 0, 1], 100))
    expect(rotationFromReference(reference, turned).angleDeg).toBeCloseTo(
      100,
      6
    )
    const nearest = nearestSetting(reference, turned, CUBIC)
    expect(rotationFromReference(reference, nearest).angleDeg).toBeCloseTo(
      10,
      6
    )
    // the same lattice: its rows are the turned ones, permuted and signed
    for (const row of nearest)
      expect(
        turned.some(
          (other) =>
            Math.abs(Math.abs(dot(unit3(row), unit3(other))) - 1) < 1e-9
        )
      ).toBe(true)
  })

  it("keeps a cell as it is without settings", () => {
    const hexagonal = dualBasis(direct(0.295, 0.295, 0.468, 90, 90, 120))
    const turned = rotated(hexagonal, rotation([0, 0, 1], 100))
    expect(nearestSetting(hexagonal, turned, [])).toBe(turned)
  })
})

const unit3 = (v: Vec3) => scale(v, 1 / norm(v))

it("gives the crystal direction along a lab vector, its largest component 1", () => {
  const reference = dualBasis(direct(0.35238, 0.35238, 0.35238, 90, 90, 90))
  expectClose(crystalDirection(reference, [0, 0, 1]), [0, 0, 1])
  expectClose(crystalDirection(reference, [1, 1, 0]), [1, 1, 0])
})

describe("the camera", () => {
  it("is an orthonormal right-handed basis, the beam to the right at yaw 0", () => {
    for (const camera of [
      { yaw: 0, pitch: 0 },
      { yaw: 35, pitch: 22 },
      { yaw: -120, pitch: -80 },
    ]) {
      const { right, up, toward } = cameraBasis(camera)
      expect(norm(right)).toBeCloseTo(1)
      expect(dot(right, up)).toBeCloseTo(0)
      expectClose(cross(right, up), toward)
    }
    expectClose(cameraBasis({ yaw: 0, pitch: 0 }).right, [0, 0, 1])
  })

  it("keeps a drag's pitch within ±89°", () => {
    expect(dragged({ yaw: 0, pitch: 80 }, 10, 100)).toEqual({
      yaw: 6,
      pitch: 89,
    })
    expect(dragged({ yaw: 0, pitch: -80 }, 0, -100).pitch).toBe(-89)
  })
})

describe("the cell's faces and edges", () => {
  const cube = normalized(direct(1, 1, 1, 90, 90, 90))
  const eye = scale(cameraBasis({ yaw: 35, pitch: 22 }).toward, 3)

  it("seen from a corner's side: three faces and nine edges in front", () => {
    expect(cellFaces(cube, eye).filter((face) => face.front)).toHaveLength(3)
    const edges = cellEdges(cube, eye)
    expect(edges).toHaveLength(12)
    expect(edges.filter((edge) => edge.front)).toHaveLength(9)
    expect(
      edges.filter((edge) => edge.fromOrigin).map((edge) => edge.axis)
    ).toEqual([0, 1, 2])
  })

  it("has outward normals", () => {
    for (const face of cellFaces(cube, eye)) {
      const centre = scale(
        face.corners.reduce(
          (sum, p) => [sum[0] + p[0], sum[1] + p[1], sum[2] + p[2]] as Vec3,
          [0, 0, 0]
        ),
        0.25
      )
      expect(dot(face.normal, centre)).toBeGreaterThan(0)
    }
  })
})
