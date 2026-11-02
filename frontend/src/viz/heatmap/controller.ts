// The heatmap's canvas (Canvas2D, no WebGL). The frame is coloured once into an offscreen canvas at
// its own resolution through a 65,536-entry table (lut.ts); pan and zoom only draw that canvas again
// (drawImage, without smoothing), and a range, scale or colormap change rebuilds the table and
// recolours, nothing more. Work is coalesced per animation frame, so a colorbar drag recolours at
// most once a frame. Overlays and axes are vectors on top. The pixel readout is written to its
// element directly.
import { drawAxes, type PlotBox, type Theme } from "../axes"
import { deferredClick } from "../deferred-click"
import {
  colorizeFloat32,
  colorizeUint16,
  float32Lut,
  LUT_SIZE,
  uint16Lut,
  type Quantizer,
} from "../color/lut"
import type { ColorScale } from "../color/scale"
import {
  fitView,
  resizeView,
  type Size,
  type View,
  type ZoomLimits,
} from "../scatter/view"
import type { Frame } from "./frame"
import {
  frameBounds,
  imageBox,
  imageToScreen,
  imageZoomLimits,
  panImage,
  pixelAt,
  screenToImage,
  visibleRect,
  zoomImage,
} from "./image-view"
import { OVERLAY_STYLES, type OverlayBox, type OverlayLayer } from "./overlays"

export interface HeatmapOptions {
  frame: Frame | null
  /** The frame's size when there is no frame (the overlays' width and height). */
  extent: { width: number; height: number } | null
  /** The colormap's packed pixels (colormapPixels). */
  colors: Uint32Array
  range: readonly [number, number] | null
  scale: ColorScale
  opacity: number
  showImage: boolean
  overlays: readonly OverlayLayer[]
  /** Outlined pixel boxes (ROIs), drawn over the marks; a new list redraws, never recolours. */
  boxes: readonly OverlayBox[]
  markerSize: number
  labelSize: number
  axes: { x: string; y: string } | null
  onPixelClick: ((x: number, y: number) => void) | undefined
}

const AXES_MARGIN = { left: 56, top: 8, right: 12, bottom: 40 }
const PLAIN_MARGIN = { left: 0, top: 0, right: 0, bottom: 0 }
/** Heavy layers: the canvas stays at most 1.5 device pixels per CSS pixel. */
const MAX_DPR = 1.5
const CLICK_SLOP_PX = 4
const LABEL_HALO = "rgba(255, 255, 255, 0.85)"
const BOX_RIM = "rgba(0, 0, 0, 0.55)"

interface Drag {
  x0: number
  y0: number
  view: View
  moved: boolean
}

export class HeatmapController {
  private readonly container: HTMLElement
  private readonly canvas: HTMLCanvasElement
  private readonly ctx: CanvasRenderingContext2D | null
  private readonly readout: HTMLElement | null
  private readonly image: HTMLCanvasElement
  private readonly imageCtx: CanvasRenderingContext2D | null
  private pixels: ImageData | null = null
  private buf = new Uint32Array(0)
  private readonly lut = new Uint32Array(LUT_SIZE)
  private quantizer: Quantizer = { a: 0, k: 0, log: false }
  private options: HeatmapOptions | null = null
  private box: PlotBox = { left: 0, top: 0, width: 0, height: 0 }
  private dpr = 1
  private view: View | null = null
  private fitted = true
  private limits: ZoomLimits | undefined = undefined
  private drag: Drag | null = null
  private dragged = false
  // A pixel click waits out the double-click interval: a double-click only resets the view.
  private readonly pixelClick = deferredClick()
  private needsColor = true
  private frame = 0
  private theme: Theme & { family: string } = {
    foreground: "#000",
    font: "11px sans-serif",
    family: "sans-serif",
  }
  private readonly cleanup: (() => void)[] = []

  constructor(
    container: HTMLElement,
    canvas: HTMLCanvasElement,
    readout: HTMLElement | null
  ) {
    this.container = container
    this.canvas = canvas
    this.readout = readout
    this.ctx = canvas.getContext("2d")
    this.image = document.createElement("canvas")
    this.imageCtx = this.ctx ? this.image.getContext("2d") : null
    this.listen(canvas, "pointerdown", (e) => {
      this.onPointerDown(e)
    })
    this.listen(canvas, "pointermove", (e) => {
      this.onPointerMove(e)
    })
    this.listen(canvas, "pointerup", (e) => {
      this.onPointerUp(e)
    })
    this.listen(canvas, "pointercancel", () => {
      this.drag = null
      this.updateCursor()
    })
    this.listen(canvas, "pointerleave", () => {
      if (!this.drag) this.setReadout("")
    })
    this.listen(canvas, "click", (e) => {
      this.onClick(e)
    })
    this.listen(canvas, "dblclick", () => {
      this.pixelClick.cancel()
      this.resetView()
    })
    const wheel = (e: WheelEvent) => {
      this.onWheel(e)
    }
    canvas.addEventListener("wheel", wheel, { passive: false })
    this.cleanup.push(() => {
      canvas.removeEventListener("wheel", wheel)
    })
    if (typeof ResizeObserver !== "undefined") {
      const resize = new ResizeObserver(() => {
        this.layout()
      })
      resize.observe(container)
      this.cleanup.push(() => {
        resize.disconnect()
      })
    }
    // A theme switch changes the axes' colour.
    const themeObserver = new MutationObserver(() => {
      this.readTheme()
      this.schedule()
    })
    themeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class", "style"],
    })
    this.cleanup.push(() => {
      themeObserver.disconnect()
    })
    this.updateCursor()
  }

  update(next: HeatmapOptions): void {
    const prev = this.options
    this.options = next
    const newBounds = !prev || !sameView(boundsOf(prev), boundsOf(next))
    if (!prev || (this.fitted && newBounds)) {
      // New bounds under the automatic fit: fit again. The user's view (after a pan or zoom)
      // stays, so a new frame of the same size keeps the zoom (the wire viewer's depths).
      this.view = null
    }
    // The plot box follows the image's aspect (`imageBox`) and the axes' margins.
    if (!prev || newBounds || (prev.axes === null) !== (next.axes === null)) {
      this.layout()
    }
    if (!prev || prev.frame !== next.frame) this.allocate(next.frame)
    if (
      !prev ||
      prev.frame !== next.frame ||
      prev.colors !== next.colors ||
      prev.range?.[0] !== next.range?.[0] ||
      prev.range?.[1] !== next.range?.[1] ||
      prev.scale.kind !== next.scale.kind ||
      prev.scale.gamma !== next.scale.gamma
    ) {
      this.needsColor = true
    }
    this.schedule()
  }

  /** Fit the frame again (the Reset button, a double click). */
  resetView(): void {
    this.view = null
    this.schedule()
  }

  destroy(): void {
    cancelAnimationFrame(this.frame)
    this.pixelClick.cancel()
    for (const undo of this.cleanup) undo()
  }

  private listen<K extends keyof HTMLElementEventMap>(
    target: HTMLElement,
    type: K,
    handler: (e: HTMLElementEventMap[K]) => void
  ): void {
    const listener = handler as EventListener
    target.addEventListener(type, listener)
    this.cleanup.push(() => {
      target.removeEventListener(type, listener)
    })
  }

  private get plot(): Size {
    return { width: this.box.width, height: this.box.height }
  }

  private currentView(): View | null {
    if (!this.view) {
      const bounds = this.options ? boundsOf(this.options) : null
      if (!bounds) return null
      this.view = fitView(bounds, this.plot, { equalAspect: true, padding: 0 })
      this.fitted = true
      this.limits = imageZoomLimits(this.view)
    }
    return this.view
  }

  private allocate(frame: Frame | null): void {
    if (!frame || !this.imageCtx) {
      this.pixels = null
      return
    }
    if (
      !this.pixels ||
      this.pixels.width !== frame.width ||
      this.pixels.height !== frame.height
    ) {
      this.image.width = frame.width
      this.image.height = frame.height
      this.pixels = this.imageCtx.createImageData(frame.width, frame.height)
      this.buf = new Uint32Array(this.pixels.data.buffer)
    }
  }

  private layout(): void {
    const rect = this.container.getBoundingClientRect()
    const width = Math.floor(rect.width)
    const height = Math.floor(rect.height)
    if (width <= 0 || height <= 0) return
    const m = this.options?.axes ? AXES_MARGIN : PLAIN_MARGIN
    const old = this.plot
    this.box = imageBox(
      width,
      height,
      m,
      this.options ? boundsOf(this.options) : null
    )
    this.dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR)
    this.canvas.width = Math.max(1, Math.round(width * this.dpr))
    this.canvas.height = Math.max(1, Math.round(height * this.dpr))
    this.view = resizeView(this.view, old, this.plot, {
      equalAspect: true,
      fitted: this.fitted,
    })
    this.readTheme()
    this.schedule()
  }

  private readTheme(): void {
    const style = getComputedStyle(this.canvas)
    const family = style.fontFamily || "sans-serif"
    this.theme = {
      foreground: style.color || "#000",
      font: `11px ${family}`,
      family,
    }
  }

  private schedule(): void {
    if (this.frame) return
    this.frame = requestAnimationFrame(() => {
      this.frame = 0
      this.draw()
    })
  }

  /** Recolour the frame into the offscreen canvas: rebuild the table, then one lookup a pixel. */
  private recolor(o: HeatmapOptions): void {
    const { frame, range } = o
    if (!frame || !range || !this.pixels || !this.imageCtx) return
    const t0 = performance.now()
    const [lo, hi] = range
    if (frame.data instanceof Uint16Array) {
      uint16Lut(o.colors, lo, hi, o.scale, this.lut)
      colorizeUint16(frame.data, this.lut, this.buf)
    } else {
      this.quantizer = float32Lut(o.colors, lo, hi, o.scale, this.lut).quantizer
      colorizeFloat32(frame.data, this.lut, this.quantizer, this.buf)
    }
    this.imageCtx.putImageData(this.pixels, 0, 0)
    // For the browser check: how long the last recolour took (ms).
    this.canvas.dataset.recolorMs = (performance.now() - t0).toFixed(1)
  }

  private draw(): void {
    const o = this.options
    const ctx = this.ctx
    if (!o || !ctx || this.box.width <= 0) return
    if (this.needsColor) {
      this.recolor(o)
      this.needsColor = false
    }
    const { box, dpr } = this
    ctx.setTransform(1, 0, 0, 1, 0, 0)
    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height)
    const view = this.currentView()
    if (!view) return
    const plot = this.plot
    ctx.save()
    ctx.beginPath()
    ctx.rect(box.left * dpr, box.top * dpr, box.width * dpr, box.height * dpr)
    ctx.clip()
    if (o.showImage && o.frame && o.range && this.pixels) {
      const r = visibleRect(view, plot, o.frame.width, o.frame.height)
      if (r) {
        ctx.imageSmoothingEnabled = false
        ctx.globalAlpha = Math.min(1, Math.max(0, o.opacity))
        ctx.drawImage(
          this.image,
          r.sx,
          r.sy,
          r.sw,
          r.sh,
          (box.left + r.dx) * dpr,
          (box.top + r.dy) * dpr,
          r.dw * dpr,
          r.dh * dpr
        )
        ctx.globalAlpha = 1
      }
    }
    ctx.setTransform(dpr, 0, 0, dpr, box.left * dpr, box.top * dpr)
    drawOverlays(ctx, view, plot, o, this.theme)
    drawBoxes(ctx, view, plot, o.boxes, this.theme)
    ctx.restore()
    if (o.axes) {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      drawAxes(ctx, view, box, o.axes, this.theme, { yDown: true })
    }
  }

  private plotPoint(e: MouseEvent): [number, number] {
    const rect = this.canvas.getBoundingClientRect()
    return [
      e.clientX - rect.left - this.box.left,
      e.clientY - rect.top - this.box.top,
    ]
  }

  private onPointerDown(e: PointerEvent): void {
    const view = this.currentView()
    if (e.button !== 0 || !view) return
    this.canvas.setPointerCapture(e.pointerId)
    const [x0, y0] = this.plotPoint(e)
    this.dragged = false
    this.drag = { x0, y0, view, moved: false }
    this.updateCursor()
  }

  private onPointerMove(e: PointerEvent): void {
    const [px, py] = this.plotPoint(e)
    const d = this.drag
    if (!d) {
      this.hoverAt(px, py)
      return
    }
    if (!d.moved && Math.hypot(px - d.x0, py - d.y0) < CLICK_SLOP_PX) return
    d.moved = true
    this.view = panImage(d.view, this.plot, px - d.x0, py - d.y0)
    this.fitted = false
    this.schedule()
  }

  private onPointerUp(e: PointerEvent): void {
    if (this.canvas.hasPointerCapture(e.pointerId)) {
      this.canvas.releasePointerCapture(e.pointerId)
    }
    this.dragged = this.drag?.moved ?? false
    this.drag = null
    this.updateCursor()
  }

  private onClick(e: MouseEvent): void {
    // Not after a drag, nor for a double-click: its first click is reported only once the
    // double-click interval has passed (the dblclick cancels it), its second (detail 2) never.
    const o = this.options
    const view = this.currentView()
    if (this.dragged || e.detail > 1 || !o?.onPixelClick || !view) return
    const [ix, iy] = screenToImage(view, this.plot, ...this.plotPoint(e))
    const bounds = boundsOf(o)
    if (!bounds) return
    const x = Math.round(ix)
    const y = Math.round(iy)
    if (x >= 0 && y >= 0 && x < bounds.x1 && y < bounds.y1) {
      this.pixelClick.click(() => {
        this.options?.onPixelClick?.(x, y)
      })
    }
  }

  private updateCursor(): void {
    this.canvas.style.cursor = this.drag ? "grabbing" : "crosshair"
  }

  private onWheel(e: WheelEvent): void {
    const view = this.currentView()
    if (!view) return
    e.preventDefault()
    const [px, py] = this.plotPoint(e)
    const lines = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1
    const factor = Math.exp(-e.deltaY * lines * 0.002)
    const next = zoomImage(view, this.plot, px, py, factor, this.limits)
    if (next === view) return
    this.view = next
    this.fitted = false
    this.hoverAt(px, py)
    this.schedule()
  }

  private hoverAt(px: number, py: number): void {
    const o = this.options
    const view = this.view
    if (!o || !view) return
    if (px < 0 || py < 0 || px > this.box.width || py > this.box.height) {
      this.setReadout("")
      return
    }
    const [ix, iy] = screenToImage(view, this.plot, px, py)
    const hit = o.frame ? pixelAt(view, this.plot, px, py, o.frame) : null
    this.setReadout(
      hit
        ? `x ${String(hit.x)}  y ${String(hit.y)}  ${formatValue(hit.value)}`
        : `x ${ix.toFixed(1)}  y ${iy.toFixed(1)}`
    )
  }

  private setReadout(text: string): void {
    if (this.readout && this.readout.textContent !== text) {
      this.readout.textContent = text
    }
  }
}

function boundsOf(o: HeatmapOptions): View | null {
  const size = o.frame ?? o.extent
  return size && size.width > 0 && size.height > 0
    ? frameBounds(size.width, size.height)
    : null
}

function sameView(a: View | null, b: View | null): boolean {
  if (!a || !b) return a === b
  return a.x0 === b.x0 && a.y0 === b.y0 && a.x1 === b.x1 && a.y1 === b.y1
}

function formatValue(value: number): string {
  return Number.isInteger(value)
    ? String(value)
    : String(Number(value.toPrecision(5)))
}

/** The overlay marks (plot CSS pixels; the transform is set), culled to the plot. */
function drawOverlays(
  ctx: CanvasRenderingContext2D,
  view: View,
  plot: Size,
  o: HeatmapOptions,
  theme: { family: string }
): void {
  ctx.lineWidth = 1.5
  ctx.lineJoin = "round"
  for (const layer of o.overlays) {
    const style = OVERLAY_STYLES[layer.kind]
    const s = Math.max(1, o.markerSize + style.grow)
    const h = s / 2
    const visible: number[] = []
    ctx.strokeStyle = style.color
    ctx.beginPath()
    for (let i = 0; i < layer.x.length; i++) {
      const [px, py] = imageToScreen(
        view,
        plot,
        layer.x[i] as number,
        layer.y[i] as number
      )
      if (px < -s || py < -s || px > plot.width + s || py > plot.height + s) {
        continue
      }
      visible.push(i, px, py)
      if (style.shape === "square") {
        ctx.rect(px - h, py - h, s, s)
      } else if (style.shape === "cross") {
        ctx.moveTo(px - h, py - h)
        ctx.lineTo(px + h, py + h)
        ctx.moveTo(px + h, py - h)
        ctx.lineTo(px - h, py + h)
      } else {
        ctx.moveTo(px, py - h)
        ctx.lineTo(px + h * 0.866, py + h / 2)
        ctx.lineTo(px - h * 0.866, py + h / 2)
        ctx.closePath()
      }
    }
    ctx.stroke()
    const labels = layer.labels
    if (!labels || visible.length === 0) continue
    // "Top right" of each mark. The label colours are dark (red, purple), so a light halo keeps
    // them legible on any image and in either theme.
    ctx.save()
    ctx.font = `${String(Math.max(6, o.labelSize))}px ${theme.family}`
    ctx.textAlign = "left"
    ctx.textBaseline = "bottom"
    ctx.lineWidth = 2.5
    ctx.strokeStyle = LABEL_HALO
    ctx.fillStyle = style.color
    for (let k = 0; k < visible.length; k += 3) {
      const label = labels[visible[k] as number] ?? ""
      const x = (visible[k + 1] as number) + h + 1
      const y = (visible[k + 2] as number) - h - 1
      ctx.strokeText(label, x, y)
      ctx.fillText(label, x, y)
    }
    ctx.restore()
  }
}

/**
 * The boxes' outlines (plot CSS pixels; the transform is set): a dark rim under the colour, so they
 * read on any image; a box's label above its top-left corner (below it at the plot's top), in its
 * colour over the same rim.
 */
function drawBoxes(
  ctx: CanvasRenderingContext2D,
  view: View,
  plot: Size,
  boxes: readonly OverlayBox[],
  theme: { family: string }
): void {
  if (boxes.length === 0) return
  ctx.lineJoin = "miter"
  ctx.font = `600 11px ${theme.family}`
  ctx.textAlign = "left"
  for (const { bounds, color, label } of boxes) {
    const [y0, y1, x0, x1] = bounds
    // Pixel i covers image coordinates [i - 0.5, i + 0.5).
    const [left, top] = imageToScreen(view, plot, x0 - 0.5, y0 - 0.5)
    const [right, bottom] = imageToScreen(view, plot, x1 - 0.5, y1 - 0.5)
    if (right < 0 || bottom < 0 || left > plot.width || top > plot.height) {
      continue
    }
    ctx.lineWidth = 3.5
    ctx.strokeStyle = BOX_RIM
    ctx.strokeRect(left, top, right - left, bottom - top)
    ctx.lineWidth = 2
    ctx.strokeStyle = color
    ctx.strokeRect(left, top, right - left, bottom - top)
    if (label) {
      const above = top >= 14
      ctx.textBaseline = above ? "bottom" : "top"
      const y = above ? top - 2 : bottom + 2
      ctx.lineWidth = 3
      ctx.lineJoin = "round"
      ctx.strokeStyle = BOX_RIM
      ctx.strokeText(label, left, y)
      ctx.fillStyle = color
      ctx.fillText(label, left, y)
      ctx.lineJoin = "miter"
    }
  }
}
