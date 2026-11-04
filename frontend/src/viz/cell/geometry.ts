// The geometry of the unit-cell view: a cell's direct and reciprocal bases, its lattice parameters,
// its rotation from a reference cell, and the camera that draws it. Vectors are in beamline
// coordinates (X, Y up, Z along the beam), as lauelab's reciprocal rows a*, b*, c* are; a basis is
// three rows.

export type Vec3 = readonly [number, number, number]
export type Mat3 = readonly [Vec3, Vec3, Vec3]

const DEG = 180 / Math.PI

export const dot = (a: Vec3, b: Vec3) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
export const cross = (a: Vec3, b: Vec3): Vec3 => [
  a[1] * b[2] - a[2] * b[1],
  a[2] * b[0] - a[0] * b[2],
  a[0] * b[1] - a[1] * b[0],
]
export const scale = (a: Vec3, s: number): Vec3 => [
  a[0] * s,
  a[1] * s,
  a[2] * s,
]
export const add = (a: Vec3, b: Vec3): Vec3 => [
  a[0] + b[0],
  a[1] + b[1],
  a[2] + b[2],
]
export const sub = (a: Vec3, b: Vec3): Vec3 => [
  a[0] - b[0],
  a[1] - b[1],
  a[2] - b[2],
]
export const norm = (a: Vec3) => Math.hypot(a[0], a[1], a[2])
export const unit = (a: Vec3): Vec3 => scale(a, 1 / (norm(a) || 1))

const mapRows = (m: Mat3, f: (row: Vec3, i: number) => Vec3): Mat3 => [
  f(m[0], 0),
  f(m[1], 1),
  f(m[2], 2),
]

export const transpose = (m: Mat3): Mat3 => [
  [m[0][0], m[1][0], m[2][0]],
  [m[0][1], m[1][1], m[2][1]],
  [m[0][2], m[1][2], m[2][2]],
]

export function multiply(a: Mat3, b: Mat3): Mat3 {
  const t = transpose(b)
  return mapRows(a, (row) => [dot(row, t[0]), dot(row, t[1]), dot(row, t[2])])
}

/** A basis from JSON rows; null unless it is three finite rows of three. */
export function mat3Of(rows: readonly (readonly number[])[]): Mat3 | null {
  if (rows.length !== 3) return null
  const out: Vec3[] = []
  for (const row of rows) {
    const [x, y, z] = row
    if (
      row.length !== 3 ||
      x === undefined ||
      y === undefined ||
      z === undefined ||
      ![x, y, z].every(Number.isFinite)
    )
      return null
    out.push([x, y, z])
  }
  const [a, b, c] = out
  return a && b && c ? [a, b, c] : null
}

const determinant = (m: Mat3) => dot(m[0], cross(m[1], m[2]))

function inverse(m: Mat3): Mat3 {
  const det = determinant(m)
  // the adjugate's columns are the rows' cross products
  return transpose([
    scale(cross(m[1], m[2]), 1 / det),
    scale(cross(m[2], m[0]), 1 / det),
    scale(cross(m[0], m[1]), 1 / det),
  ])
}

/**
 * The dual basis, with 2π: a reciprocal basis (rows a*, b*, c*, 1/nm) gives the direct one (rows
 * a, b, c, nm), with a_i · b*_j = 2π δ_ij, and a direct basis its reciprocal.
 */
export function dualBasis(m: Mat3): Mat3 {
  const k = (2 * Math.PI) / determinant(m)
  return [
    scale(cross(m[1], m[2]), k),
    scale(cross(m[2], m[0]), k),
    scale(cross(m[0], m[1]), k),
  ]
}

export interface LatticeParameters {
  /** a, b, c in the basis's unit. */
  lengths: Vec3
  /** α, β, γ in degrees. */
  angles: Vec3
}

export function latticeParameters(direct: Mat3): LatticeParameters {
  const [a, b, c] = direct
  const angle = (u: Vec3, v: Vec3) =>
    Math.acos(Math.max(-1, Math.min(1, dot(u, v) / (norm(u) * norm(v))))) * DEG
  return {
    lengths: [norm(a), norm(b), norm(c)],
    angles: [angle(b, c), angle(c, a), angle(a, b)],
  }
}

/** The rotation nearest `m` (the polar decomposition, by Higham's iteration). */
function nearestRotation(m: Mat3): Mat3 {
  let q = m
  for (let i = 0; i < 20; i++) {
    const inv = transpose(inverse(q))
    q = mapRows(q, (row, r) => scale(add(row, inv[r] ?? row), 0.5))
  }
  return q
}

export interface Rotation {
  /** U, with measured lab vectors v = U · v_ref. */
  matrix: Mat3
  /** 0 to 180. */
  angleDeg: number
  /** The unit axis in beamline coordinates; null at a zero angle. */
  axis: Vec3 | null
}

/**
 * The rotation that turns the reference cell into the measured one. With row bases, R = R_ref · Uᵀ,
 * so U = (R_ref⁻¹ · R)ᵀ, made a rotation (the nearest one) when the measured cell is strained.
 */
export function rotationFromReference(
  reference: Mat3,
  reciprocal: Mat3
): Rotation {
  const u = nearestRotation(transpose(multiply(inverse(reference), reciprocal)))
  const trace = u[0][0] + u[1][1] + u[2][2]
  const angle = Math.acos(Math.max(-1, Math.min(1, (trace - 1) / 2)))
  return { matrix: u, angleDeg: angle * DEG, axis: rotationAxis(u, angle) }
}

/**
 * The unit axis of a rotation by `angle` (radians): from its antisymmetric part, or near 180°, where
 * that part vanishes, from its symmetric part (U + I = 2 n nᵀ), the sign of the largest component +.
 */
function rotationAxis(u: Mat3, angle: number): Vec3 | null {
  const antisymmetric: Vec3 = [
    u[2][1] - u[1][2],
    u[0][2] - u[2][0],
    u[1][0] - u[0][1],
  ]
  if (angle < 1e-9) return null
  if (Math.PI - angle > 1e-3) return unit(antisymmetric)
  // the column of U + I with the largest diagonal entry is 2 n_i n
  const diagonal = [u[0][0], u[1][1], u[2][2]]
  const i = diagonal.indexOf(Math.max(...diagonal))
  const column: Vec3 = [u[0][i] ?? 0, u[1][i] ?? 0, u[2][i] ?? 0]
  const n = unit([
    column[0] + (i === 0 ? 1 : 0),
    column[1] + (i === 1 ? 1 : 0),
    column[2] + (i === 2 ? 1 : 0),
  ])
  // keep the sense the antisymmetric part still gives, where it gives one
  return dot(n, antisymmetric) < 0 ? scale(n, -1) : n
}

/**
 * The equivalent setting of `reciprocal` nearest the reference: of P·R for each of the crystal's
 * `settings` (integer matrices that relabel a, b, c without changing the lattice), the one with the
 * smallest rotation from the reference. Without settings, `reciprocal` as it is.
 */
export function nearestSetting(
  reference: Mat3,
  reciprocal: Mat3,
  settings: readonly Mat3[]
): Mat3 {
  let best = reciprocal
  let bestAngle = rotationFromReference(reference, reciprocal).angleDeg
  for (const setting of settings) {
    const candidate = multiply(setting, reciprocal)
    const angle = rotationFromReference(reference, candidate).angleDeg
    if (angle < bestAngle - 1e-9) {
      bestAngle = angle
      best = candidate
    }
  }
  return best
}

/**
 * The crystal direction [u v w] along a lab vector, its largest component ±1: with v = Σ x_i a_i,
 * x_i = v · b*_i / 2π.
 */
export function crystalDirection(reciprocal: Mat3, lab: Vec3): Vec3 {
  const x: Vec3 = [
    dot(reciprocal[0], lab),
    dot(reciprocal[1], lab),
    dot(reciprocal[2], lab),
  ]
  const largest = Math.max(...x.map(Math.abs)) || 1
  return scale(x, 1 / largest)
}

// ---------------------------------------------------------------------------
// The camera: perspective, orbiting the cell's centre

export interface Camera {
  /** Degrees about the lab's Y. */
  yaw: number
  /** Degrees above the horizontal, -89 to 89. */
  pitch: number
}

export const DEFAULT_CAMERA: Camera = { yaw: 35, pitch: 22 }

export interface CameraBasis {
  right: Vec3
  up: Vec3
  /** Toward the viewer. */
  toward: Vec3
}

/** At yaw 0 and pitch 0 the beam (Z) points right, Y up and X away from the viewer. */
export function cameraBasis({ yaw, pitch }: Camera): CameraBasis {
  const t = yaw / DEG
  const p = pitch / DEG
  const toward: Vec3 = [
    -Math.cos(p) * Math.cos(t),
    Math.sin(p),
    Math.cos(p) * Math.sin(t),
  ]
  const right = unit(cross([0, 1, 0], toward))
  return { right, up: cross(toward, right), toward }
}

/** A drag of (dx, dy) CSS px: 0.6° a pixel, the pitch kept within ±89°. */
export function dragged(camera: Camera, dx: number, dy: number): Camera {
  return {
    yaw: camera.yaw + dx * 0.6,
    pitch: Math.max(-89, Math.min(89, camera.pitch + dy * 0.6)),
  }
}

/** Screen x, y (up positive) and depth (toward the viewer positive), in scene units. */
export function project(basis: CameraBasis, v: Vec3): Vec3 {
  return [dot(v, basis.right), dot(v, basis.up), dot(v, basis.toward)]
}

// ---------------------------------------------------------------------------
// The cell's faces and edges, centred on the origin

/** `direct` scaled so that its longest edge is 1. */
export function normalized(direct: Mat3): Mat3 {
  const longest =
    Math.max(norm(direct[0]), norm(direct[1]), norm(direct[2])) || 1
  return mapRows(direct, (row) => scale(row, 1 / longest))
}

/** The corner where a, b and c start. */
export function originCorner(direct: Mat3): Vec3 {
  return scale(add(add(direct[0], direct[1]), direct[2]), -0.5)
}

/** The largest distance of a corner from the centre: half the longest body diagonal. */
export function cellRadius(direct: Mat3): number {
  const [a, b, c] = direct
  let radius = 0
  for (const sb of [1, -1])
    for (const sc of [1, -1])
      radius = Math.max(radius, norm(add(add(a, scale(b, sb)), scale(c, sc))))
  return radius / 2
}

export type Axis = 0 | 1 | 2

export interface Face {
  corners: readonly [Vec3, Vec3, Vec3, Vec3]
  /** Outward. */
  normal: Vec3
  /** The basis vector it is not parallel to: 0 for the two faces spanned by b and c. */
  axis: Axis
  /** It faces the eye. */
  front: boolean
}

export interface Edge {
  from: Vec3
  to: Vec3
  /** The basis vector it is parallel to. */
  axis: Axis
  /** One of its two faces faces the eye: drawn solid, else dashed. */
  front: boolean
  /** It starts at the origin corner (where the a, b, c arrows are drawn). */
  fromOrigin: boolean
}

const AXES: readonly Axis[] = [0, 1, 2]
const next = (axis: Axis, by: 1 | 2): Axis => ((axis + by) % 3) as Axis

/** The six faces, in the order axis 0 near side, axis 0 far side, axis 1 ..., for an eye at `eye`. */
export function cellFaces(direct: Mat3, eye: Vec3): Face[] {
  const o = originCorner(direct)
  const faces: Face[] = []
  for (const axis of AXES) {
    const u = direct[next(axis, 1)]
    const v = direct[next(axis, 2)]
    const w = direct[axis]
    for (const side of [0, 1]) {
      const base = add(o, scale(w, side))
      const corners = [
        base,
        add(base, u),
        add(add(base, u), v),
        add(base, v),
      ] as const
      const centre = add(base, scale(add(u, v), 0.5))
      let normal = unit(cross(u, v))
      if (dot(normal, w) > 0 !== (side === 1)) normal = scale(normal, -1)
      faces.push({
        corners,
        normal,
        axis,
        front: dot(normal, sub(eye, centre)) > 0,
      })
    }
  }
  return faces
}

export function cellEdges(direct: Mat3, eye: Vec3): Edge[] {
  const o = originCorner(direct)
  const faces = cellFaces(direct, eye)
  const facing = (axis: Axis, side: number) =>
    faces[axis * 2 + side]?.front ?? false
  const edges: Edge[] = []
  for (const axis of AXES) {
    const j = next(axis, 1)
    const k = next(axis, 2)
    for (const sj of [0, 1])
      for (const sk of [0, 1]) {
        const from = add(add(o, scale(direct[j], sj)), scale(direct[k], sk))
        edges.push({
          from,
          to: add(from, direct[axis]),
          axis,
          front: facing(j, sj) || facing(k, sk),
          fromOrigin: sj === 0 && sk === 0,
        })
      }
  }
  return edges
}
