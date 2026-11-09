// The unit-cell view's Canvas2D drawing: the cell in perspective (front edges solid and stronger
// the nearer they are, hidden ones dashed, the faces toward the viewer faintly tinted), its a, b, c
// arrows, the beam's direction from their origin, and the X, Y, Z triad.

import {
  add,
  cameraBasis,
  cellEdges,
  cellFaces,
  cellRadius,
  originCorner,
  project,
  scale,
  type Camera,
  type CameraBasis,
  type Mat3,
  type Vec3,
} from "./geometry"

/** The colours the canvas reads from the theme's tokens. */
export interface CellTheme {
  foreground: string
  muted: string
  /** The labels' halo: the surface behind the canvas. */
  halo: string
  axes: readonly [string, string, string]
  beam: string
  font: string
}

export interface CellDrawing {
  width: number
  height: number
  camera: Camera
  /** Rows a, b, c, the longest 1 (`normalized`). */
  direct: Mat3
  showBeam: boolean
  theme: CellTheme
}

/** The camera's distance from the centre, in cell radii: enough perspective to tell the near side. */
export const PERSPECTIVE = 3.2

const AXIS_NAMES = ["a", "b", "c"] as const

interface View {
  ctx: CanvasRenderingContext2D
  basis: CameraBasis
  eye: number
  cx: number
  cy: number
  /** Pixels a scene unit. */
  s: number
  width: number
  height: number
  theme: CellTheme
}

/** Screen x and y (CSS px) and depth of a scene point. */
export function toScreen(
  view: Pick<View, "basis" | "eye" | "cx" | "cy" | "s">,
  p: Vec3
): Vec3 {
  const [x, y, z] = project(view.basis, p)
  const f = view.eye / Math.max(1e-3, view.eye - z)
  return [view.cx + x * view.s * f, view.cy - y * view.s * f, z]
}

export function drawUnitCell(ctx: CanvasRenderingContext2D, d: CellDrawing) {
  ctx.clearRect(0, 0, d.width, d.height)
  const radius = cellRadius(d.direct) || 1
  const view: View = {
    ctx,
    basis: cameraBasis(d.camera),
    eye: radius * PERSPECTIVE,
    cx: d.width / 2,
    cy: d.height / 2 - 4,
    s: (Math.min(d.width, d.height) * 0.33) / radius,
    width: d.width,
    height: d.height,
    theme: d.theme,
  }
  const eye = scale(view.basis.toward, view.eye)

  for (const face of cellFaces(d.direct, eye)) {
    if (!face.front) continue
    ctx.save()
    ctx.globalAlpha = 0.07
    ctx.fillStyle = d.theme.axes[face.axis]
    ctx.beginPath()
    face.corners.forEach((p, i) => {
      const [x, y] = toScreen(view, p)
      if (i === 0) ctx.moveTo(x, y)
      else ctx.lineTo(x, y)
    })
    ctx.closePath()
    ctx.fill()
    ctx.restore()
  }

  const edges = cellEdges(d.direct, eye)
  for (const edge of [...edges].sort(
    (p, q) => Number(p.front) - Number(q.front)
  )) {
    if (edge.fromOrigin) continue // the arrows
    // nearer front edges are thicker and stronger
    const depth = project(view.basis, scale(add(edge.from, edge.to), 0.5))[2]
    const near = Math.max(0, Math.min(1, 0.5 + (0.5 * depth) / radius))
    line(view, edge.from, edge.to, d.theme.muted, {
      width: edge.front ? 0.9 + 1.1 * near : 1,
      alpha: edge.front ? 0.55 + 0.45 * near : 0.45,
      dash: edge.front ? [] : [3, 3],
    })
  }

  const o = originCorner(d.direct)
  d.direct.forEach((vector, i) => {
    const tip = add(o, vector)
    const color = d.theme.axes[i] ?? d.theme.foreground
    // an arrow on a hidden edge is dashed and faint, as the edge would be
    const hidden = !edges.some(
      (edge) => edge.fromOrigin && edge.axis === i && edge.front
    )
    arrow(view, o, tip, color, {
      width: hidden ? 1.2 : 2,
      alpha: hidden ? 0.5 : 1,
      dash: hidden ? [3, 3] : [],
    })
    tipLabel(view, o, tip, AXIS_NAMES[i] ?? "", color, 12)
  })

  if (d.showBeam) beam(view, o, radius)
  triad(view, 30, d.height - 28)
}

/** The beam's direction (+Z) from the a, b, c origin: dotted, an arrow downstream. */
function beam(view: View, origin: Vec3, radius: number) {
  const r = radius * 1.45
  const color = view.theme.beam
  const along = (distance: number) => add(origin, [0, 0, distance])
  const tip = along(r)
  line(view, origin, along(r * 0.86), color, { width: 1.5, dash: [2, 4] })
  arrow(view, along(r * 0.8), tip, color, { width: 1.5 })
  tipLabel(view, origin, tip, "beam", color, 10)
}

interface Stroke {
  width: number
  alpha?: number
  dash?: number[]
}

function line(view: View, p: Vec3, q: Vec3, color: string, stroke: Stroke) {
  const [x0, y0] = toScreen(view, p)
  const [x1, y1] = toScreen(view, q)
  const { ctx } = view
  ctx.save()
  ctx.globalAlpha = stroke.alpha ?? 1
  ctx.strokeStyle = color
  ctx.lineWidth = stroke.width
  ctx.setLineDash(stroke.dash ?? [])
  ctx.beginPath()
  ctx.moveTo(x0, y0)
  ctx.lineTo(x1, y1)
  ctx.stroke()
  ctx.restore()
}

function arrow(view: View, p: Vec3, q: Vec3, color: string, stroke: Stroke) {
  const [x0, y0] = toScreen(view, p)
  const [x1, y1] = toScreen(view, q)
  arrow2d(view.ctx, x0, y0, x1, y1, color, stroke)
}

function arrow2d(
  ctx: CanvasRenderingContext2D,
  x0: number,
  y0: number,
  x1: number,
  y1: number,
  color: string,
  stroke: Stroke,
  head = 7
) {
  const length = Math.hypot(x1 - x0, y1 - y0)
  if (length < 1) return
  const ux = (x1 - x0) / length
  const uy = (y1 - y0) / length
  const h = Math.min(head, length * 0.5)
  ctx.save()
  ctx.globalAlpha = stroke.alpha ?? 1
  ctx.strokeStyle = color
  ctx.fillStyle = color
  ctx.lineWidth = stroke.width
  ctx.lineCap = "round"
  ctx.setLineDash(stroke.dash ?? [])
  ctx.beginPath()
  ctx.moveTo(x0, y0)
  ctx.lineTo(x1 - ux * h * 0.8, y1 - uy * h * 0.8)
  ctx.stroke()
  ctx.beginPath()
  ctx.moveTo(x1, y1)
  ctx.lineTo(x1 - ux * h - uy * h * 0.5, y1 - uy * h + ux * h * 0.5)
  ctx.lineTo(x1 - ux * h + uy * h * 0.5, y1 - uy * h - ux * h * 0.5)
  ctx.closePath()
  ctx.fill()
  ctx.restore()
}

/** A label with a halo, kept inside the canvas. */
function label(
  view: View,
  text: string,
  x: number,
  y: number,
  color: string,
  options: { size?: number; weight?: number; align?: CanvasTextAlign } = {}
) {
  const { ctx, theme } = view
  ctx.save()
  ctx.font = `${String(options.weight ?? 600)} ${String(options.size ?? 11)}px ${theme.font}`
  ctx.textAlign = options.align ?? "center"
  ctx.textBaseline = "middle"
  const width = ctx.measureText(text).width
  const left =
    ctx.textAlign === "left"
      ? x
      : ctx.textAlign === "right"
        ? x - width
        : x - width / 2
  const shiftedX =
    x + Math.max(0, 3 - left) - Math.max(0, left + width - (view.width - 3))
  const shiftedY = Math.max(8, Math.min(view.height - 8, y))
  ctx.lineWidth = 3
  ctx.strokeStyle = theme.halo
  ctx.strokeText(text, shiftedX, shiftedY)
  ctx.fillStyle = color
  ctx.fillText(text, shiftedX, shiftedY)
  ctx.restore()
}

/** The label just past the tip of `from → to`. */
function tipLabel(
  view: View,
  from: Vec3,
  to: Vec3,
  text: string,
  color: string,
  size: number
) {
  const [x0, y0] = toScreen(view, from)
  const [x1, y1] = toScreen(view, to)
  const length = Math.hypot(x1 - x0, y1 - y0) || 1
  label(
    view,
    text,
    x1 + ((x1 - x0) / length) * 9,
    y1 + ((y1 - y0) / length) * 9,
    color,
    { size }
  )
}

/** The lab's X, Y, Z (the beam) from the corner (ox, oy); an axis along the view is ⊙ or ⊗. */
function triad(view: View, ox: number, oy: number, length = 18) {
  const { ctx, theme, basis } = view
  const axes: [Vec3, string, string][] = [
    [[1, 0, 0], "X", theme.muted],
    [[0, 1, 0], "Y", theme.muted],
    [[0, 0, 1], "Z beam", theme.beam],
  ]
  for (const [vector, name, color] of axes) {
    const [x, y, z] = project(basis, vector)
    if (Math.hypot(x, y) < 0.12) {
      label(view, `${name} ${z > 0 ? "⊙" : "⊗"}`, ox + 4, oy, color, {
        size: 9,
        weight: 500,
        align: "left",
      })
      continue
    }
    arrow2d(
      ctx,
      ox,
      oy,
      ox + x * length,
      oy - y * length,
      color,
      { width: 1.3 },
      5
    )
    label(view, name, ox + x * (length + 8), oy - y * (length + 8), color, {
      size: 9,
      weight: 500,
      align: x < -0.3 ? "right" : x > 0.3 ? "left" : "center",
    })
  }
}
