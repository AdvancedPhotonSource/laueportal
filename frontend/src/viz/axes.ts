// Plot axes shared by the 2D renderers: a frame around the plot area, nice ticks with labels, and the
// axis titles, in the theme's foreground. The scatter's y runs up; the heatmap's (image rows) down.
import { formatTick, niceTicks, type View } from "./scatter/view"

/** The plot area within the canvas, in CSS pixels. */
export interface PlotBox {
  left: number
  top: number
  width: number
  height: number
}

export interface Theme {
  foreground: string
  font: string
}

/** Draw the axes in CSS pixels (the caller's transform scales them to the device). */
export function drawAxes(
  ctx: CanvasRenderingContext2D,
  view: View,
  box: PlotBox,
  labels: { x: string; y: string },
  theme: Theme,
  { yDown = false }: { yDown?: boolean } = {}
): void {
  const { left, top, width, height } = box
  const xAt = (v: number) => ((v - view.x0) * width) / (view.x1 - view.x0)
  const yAt = (v: number) =>
    ((yDown ? v - view.y0 : view.y1 - v) * height) / (view.y1 - view.y0)
  ctx.strokeStyle = theme.foreground
  ctx.fillStyle = theme.foreground
  ctx.lineWidth = 1
  ctx.globalAlpha = 0.5
  ctx.strokeRect(left - 0.5, top - 0.5, width + 1, height + 1)
  ctx.globalAlpha = 0.8
  ctx.font = theme.font
  const x = niceTicks(view.x0, view.x1, Math.max(2, Math.floor(width / 90)))
  ctx.textAlign = "center"
  ctx.textBaseline = "top"
  ctx.beginPath()
  for (const value of x.values) {
    const px = left + xAt(value)
    ctx.moveTo(px, top + height)
    ctx.lineTo(px, top + height + 4)
    ctx.fillText(formatTick(value, x.step), px, top + height + 6)
  }
  const y = niceTicks(view.y0, view.y1, Math.max(2, Math.floor(height / 50)))
  ctx.textAlign = "right"
  ctx.textBaseline = "middle"
  for (const value of y.values) {
    const py = top + yAt(value)
    ctx.moveTo(left - 4, py)
    ctx.lineTo(left, py)
    ctx.fillText(formatTick(value, y.step), left - 6, py)
  }
  ctx.stroke()
  ctx.globalAlpha = 1
  ctx.textAlign = "center"
  ctx.textBaseline = "top"
  ctx.fillText(labels.x, left + width / 2, top + height + 22)
  ctx.save()
  ctx.translate(12, top + height / 2)
  ctx.rotate(-Math.PI / 2)
  ctx.textBaseline = "middle"
  ctx.fillText(labels.y, 0, 0)
  ctx.restore()
}
