// The scatter's Canvas2D layer (no WebGL). A full render stamps the points into an ImageData
// buffer and puts it into an offscreen cache; every frame (hover, a gesture, the lasso) only
// draws that cached bitmap, moved to the live view, plus the vector decorations on top.
import { drawAxes, type PlotBox, type Theme } from "../axes"
import { drawPoints, type MarkerShape } from "./stamp"
import { gestureTransform, toScreen, type Size, type View } from "./view"

export interface PointsLayer {
  x: ArrayLike<number>
  y: ArrayLike<number>
  /** Packed colours (stamp.ts); 0 hides a point. */
  colors: Uint32Array
  /** Marker size in CSS pixels. */
  size: number
  shape: MarkerShape
  selected: Uint8Array | null
}

export interface Decorations {
  axes: { x: string; y: string } | null
  /** The pole figure's unit circle, HSV centre and colour radius (data units). */
  pole: {
    center: readonly [number, number] | null
    colorRadius: number | null
  } | null
  /** The hovered point's index, or -1. */
  hover: number
  /** The lasso being drawn (flat plot pixels), or the box's two corners. */
  lasso: readonly number[] | null
  box: readonly [number, number, number, number] | null
}

/** Colours and font from the canvas element's computed style (theme tokens). */
export class ScatterRenderer {
  private readonly canvas: HTMLCanvasElement
  private readonly ctx: CanvasRenderingContext2D | null
  private readonly cache: HTMLCanvasElement
  private readonly cacheCtx: CanvasRenderingContext2D | null
  private image: ImageData | null = null
  private buf = new Uint32Array(0)
  private device: Size = { width: 0, height: 0 }
  private box: PlotBox = { left: 0, top: 0, width: 0, height: 0 }
  private dpr = 1
  private rendered: View | null = null
  private ringCss = ""
  private ringColor = 0xff000000

  constructor(canvas: HTMLCanvasElement) {
    this.canvas = canvas
    this.ctx = canvas.getContext("2d")
    this.cache = document.createElement("canvas")
    this.cacheCtx = this.ctx ? this.cache.getContext("2d") : null
  }

  /** Size the canvas for `css` pixels at `dpr`; the next frame needs a full render. */
  resize(css: Size, box: PlotBox, dpr: number): void {
    this.canvas.width = Math.max(1, Math.round(css.width * dpr))
    this.canvas.height = Math.max(1, Math.round(css.height * dpr))
    this.box = box
    this.dpr = dpr
    this.device = {
      width: Math.max(1, Math.round(box.width * dpr)),
      height: Math.max(1, Math.round(box.height * dpr)),
    }
    this.cache.width = this.device.width
    this.cache.height = this.device.height
    this.image =
      this.cacheCtx?.createImageData(this.device.width, this.device.height) ??
      null
    this.buf = this.image
      ? new Uint32Array(this.image.data.buffer)
      : new Uint32Array(0)
    this.rendered = null
  }

  /** Stamp every point under `view` into the cached bitmap. */
  render(view: View, layer: PointsLayer, theme: Theme): void {
    const { cacheCtx, image, dpr } = this
    if (!cacheCtx || !image) return
    if (theme.foreground !== this.ringCss) {
      this.ringCss = theme.foreground
      this.ringColor = resolveColor(theme.foreground)
    }
    drawPoints(this.buf, this.device, view, layer.x, layer.y, layer.colors, {
      size: layer.size * dpr,
      shape: layer.shape,
      selected: layer.selected,
      ringSize: (layer.size + 8) * dpr,
      ringWidth: Math.max(1, Math.round(2 * dpr)),
      ringColor: this.ringColor,
    })
    cacheCtx.putImageData(image, 0, 0)
    this.rendered = view
  }

  /** Draw a frame: the cached bitmap moved to `live`, then the decorations and the axes. */
  present(
    live: View,
    layer: PointsLayer | null,
    deco: Decorations,
    theme: Theme
  ): void {
    const { ctx, box, dpr } = this
    if (!ctx) return
    ctx.setTransform(1, 0, 0, 1, 0, 0)
    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height)
    ctx.save()
    ctx.beginPath()
    ctx.rect(
      box.left * dpr,
      box.top * dpr,
      this.device.width,
      this.device.height
    )
    ctx.clip()
    if (this.rendered) {
      const t = gestureTransform(this.rendered, live, this.device)
      ctx.imageSmoothingEnabled = false
      ctx.setTransform(
        t.ax,
        0,
        0,
        t.ay,
        box.left * dpr + t.bx,
        box.top * dpr + t.by
      )
      ctx.drawImage(this.cache, 0, 0)
    }
    // From here on, plot CSS pixels.
    ctx.setTransform(dpr, 0, 0, dpr, box.left * dpr, box.top * dpr)
    ctx.strokeStyle = theme.foreground
    ctx.fillStyle = theme.foreground
    const plot = { width: box.width, height: box.height }
    if (deco.pole) drawPole(ctx, live, plot, deco.pole)
    if (layer && deco.hover >= 0) {
      const [hx, hy] = toScreen(
        live,
        plot,
        layer.x[deco.hover] as number,
        layer.y[deco.hover] as number
      )
      ctx.lineWidth = 2
      ctx.beginPath()
      ctx.arc(hx, hy, layer.size / 2 + 4, 0, 2 * Math.PI)
      ctx.stroke()
    }
    drawSelectionShape(ctx, deco)
    ctx.restore()
    if (deco.axes) {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      drawAxes(ctx, live, box, deco.axes, theme)
    }
  }
}

function drawPole(
  ctx: CanvasRenderingContext2D,
  view: View,
  plot: Size,
  pole: NonNullable<Decorations["pole"]>
): void {
  const ellipse = (cx: number, cy: number, r: number) => {
    const [px, py] = toScreen(view, plot, cx, cy)
    const rx = (r * plot.width) / (view.x1 - view.x0)
    const ry = (r * plot.height) / (view.y1 - view.y0)
    ctx.beginPath()
    ctx.ellipse(px, py, rx, ry, 0, 0, 2 * Math.PI)
    ctx.stroke()
  }
  ctx.lineWidth = 1
  ellipse(0, 0, 1)
  if (!pole.center) return
  const [cx, cy] = pole.center
  if (pole.colorRadius !== null && pole.colorRadius > 0) {
    ctx.setLineDash([2, 3])
    ellipse(cx, cy, pole.colorRadius)
    ctx.setLineDash([])
  }
  // The centre marker: a cross in the foreground colour, 10 px across.
  const [px, py] = toScreen(view, plot, cx, cy)
  ctx.lineWidth = 2
  ctx.beginPath()
  ctx.moveTo(px - 5, py)
  ctx.lineTo(px + 5, py)
  ctx.moveTo(px, py - 5)
  ctx.lineTo(px, py + 5)
  ctx.stroke()
}

function drawSelectionShape(
  ctx: CanvasRenderingContext2D,
  deco: Decorations
): void {
  const { lasso, box } = deco
  if (!lasso && !box) return
  ctx.beginPath()
  if (box) {
    ctx.rect(box[0], box[1], box[2] - box[0], box[3] - box[1])
  } else if (lasso && lasso.length >= 2) {
    ctx.moveTo(lasso[0] as number, lasso[1] as number)
    for (let k = 2; k + 1 < lasso.length; k += 2) {
      ctx.lineTo(lasso[k] as number, lasso[k + 1] as number)
    }
    ctx.closePath()
  }
  ctx.globalAlpha = 0.08
  ctx.fill("evenodd") // as the selection counts crossings (select.ts)
  ctx.globalAlpha = 1
  ctx.lineWidth = 1
  ctx.setLineDash([4, 3])
  ctx.stroke()
  ctx.setLineDash([])
}

let probe: CanvasRenderingContext2D | null = null

/** A CSS colour as the packed pixel the browser paints for it. */
export function resolveColor(css: string): number {
  probe ??= document
    .createElement("canvas")
    .getContext("2d", { willReadFrequently: true })
  const ctx = probe
  if (!ctx) return 0xff000000
  ctx.canvas.width = 1
  ctx.canvas.height = 1
  ctx.clearRect(0, 0, 1, 1)
  ctx.fillStyle = css
  ctx.fillRect(0, 0, 1, 1)
  const [r = 0, g = 0, b = 0, a = 255] = ctx.getImageData(0, 0, 1, 1).data
  return ((a << 24) | (b << 16) | (g << 8) | r) >>> 0
}
