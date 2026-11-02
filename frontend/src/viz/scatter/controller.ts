// The scatter's interaction: the live view, wheel zoom at the cursor, drag to pan, box and lasso
// selection, hover picking and clicks. Frames are coalesced per animation frame; while a pan or
// zoom runs, frames only move the cached bitmap, and the points are stamped again once it ends.
// The coordinate readout and the tooltip's position are written to their elements directly: a
// pointer move renders nothing in React, and the component hears only when the hovered point
// changes.
import { selectionPairs } from "../linked-selection"
import type { PlotBox, Theme } from "../axes"
import { ScatterRenderer, type Decorations, type PointsLayer } from "./renderer"
import { boxSelect, lassoSelect } from "./select"
import { nearestPoint, type GridIndex } from "./spatial-index"
import type { MarkerShape } from "./stamp"
import type { ScatterSelection, ScatterTool } from "./types"
import {
  fitView,
  panView,
  resizeView,
  toData,
  zoomLimits,
  zoomView,
  type Size,
  type View,
  type ZoomLimits,
} from "./view"

export interface ControllerOptions {
  x: ArrayLike<number>
  y: ArrayLike<number>
  /** Packed colours; 0 hides a point (not drawn, picked or selected). */
  colors: Uint32Array
  index: GridIndex
  /** What a fit shows, padded by `padding` (a fraction of the span). */
  bounds: View | null
  padding: number
  selected: Uint8Array | null
  step: ArrayLike<number> | null
  patternIndex: ArrayLike<number> | null
  markerSize: number
  markerShape: MarkerShape
  equalAspect: boolean
  axes: Decorations["axes"]
  pole: Decorations["pole"]
  tool: ScatterTool
  /** The hovered point changed (null: none). */
  onHoverChange: (index: number | null) => void
  onPointClick?: ((index: number) => void) | undefined
  /** A click with the "center" tool: the point under the cursor (null: none, within the pick radius) and the data coordinates. */
  onCenterClick?:
    ((index: number | null, point: [number, number]) => void) | undefined
  onSelect?: ((selection: ScatterSelection) => void) | undefined
}

/** The elements the controller writes on every pointer move. */
export interface HoverElements {
  readout: HTMLElement | null
  tooltip: HTMLElement | null
}

interface Drag {
  tool: ScatterTool
  x0: number
  y0: number
  moved: boolean
  view: View
  /** The box's corners or the lasso's vertices, in plot pixels. */
  points: number[]
}

const AXES_MARGIN = { left: 64, top: 8, right: 12, bottom: 40 }
const PLAIN_MARGIN = { left: 4, top: 4, right: 4, bottom: 4 }
const MAX_DPR = 2
const CLICK_SLOP_PX = 4
const PICK_PX = 6
const WHEEL_IDLE_MS = 150
const TOOLTIP_GAP_PX = 12

export class ScatterController {
  private readonly container: HTMLElement
  private readonly canvas: HTMLCanvasElement
  private readonly elements: HoverElements
  private readonly renderer: ScatterRenderer
  private options: ControllerOptions | null = null
  private box: PlotBox = { left: 0, top: 0, width: 0, height: 0 }
  private size: Size = { width: 0, height: 0 }
  private view: View | null = null
  /** The view is the automatic fit: no pan or zoom since the last fit or reset. */
  private fitted = true
  private limits: ZoomLimits | undefined = undefined
  private hover = -1
  private drag: Drag | null = null
  /** The last press was a drag, so its click event is not a click. */
  private dragged = false
  private needsRender = true
  private frame = 0
  private wheelTimer: ReturnType<typeof setTimeout> | null = null
  private theme: Theme = { foreground: "#000", font: "11px sans-serif" }
  private readonly cleanup: (() => void)[] = []

  constructor(
    container: HTMLElement,
    canvas: HTMLCanvasElement,
    elements: HoverElements
  ) {
    this.container = container
    this.canvas = canvas
    this.elements = elements
    this.renderer = new ScatterRenderer(canvas)
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
      this.endGesture()
    })
    this.listen(canvas, "pointerleave", () => {
      if (!this.drag) this.clearHover()
    })
    this.listen(canvas, "click", (e) => {
      this.onClick(e)
    })
    this.listen(canvas, "dblclick", (e) => {
      // Reset only from empty space: a double click on a point is a click (onClick drops the second).
      if (!this.dragged && this.pick(...this.plotPoint(e)) < 0) this.resetView()
    })
    this.listen(window, "keydown", (e) => {
      if (e.key === "Escape" && this.drag && this.drag.tool !== "pan") {
        this.drag = null
        this.schedule(false)
      }
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
    // A theme switch changes the tokens the canvas reads (the ring and axis colours).
    const themeObserver = new MutationObserver(() => {
      this.readTheme()
      this.schedule(true)
    })
    themeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class", "style"],
    })
    this.cleanup.push(() => {
      themeObserver.disconnect()
    })
  }

  update(next: ControllerOptions): void {
    const prev = this.options
    this.options = next
    if (!prev || (prev.axes === null) !== (next.axes === null)) this.layout()
    if (
      !prev ||
      prev.padding !== next.padding ||
      prev.equalAspect !== next.equalAspect ||
      (this.fitted &&
        (!sameView(prev.bounds, next.bounds) ||
          prev.markerSize !== next.markerSize))
    ) {
      // Another kind of view, or new bounds or marker size (its margin) under the automatic fit: fit
      // again. The user's view (after a pan or zoom) stays when only those change.
      this.view = null
    }
    if (prev && prev.tool !== next.tool) this.drag = null
    if (
      (prev && (prev.x !== next.x || prev.y !== next.y)) ||
      next.colors[this.hover] === 0
    ) {
      this.clearHover()
    }
    this.updateCursor()
    this.schedule(
      !prev ||
        this.view === null ||
        prev.x !== next.x ||
        prev.y !== next.y ||
        prev.colors !== next.colors ||
        prev.selected !== next.selected ||
        prev.markerSize !== next.markerSize ||
        prev.markerShape !== next.markerShape
    )
  }

  /** Fit the view to the bounds again (the Reset button, a double click in empty space). */
  resetView(): void {
    this.view = null
    this.schedule(true)
  }

  destroy(): void {
    cancelAnimationFrame(this.frame)
    if (this.wheelTimer !== null) clearTimeout(this.wheelTimer)
    for (const undo of this.cleanup) undo()
  }

  private listen<K extends keyof HTMLElementEventMap>(
    target: HTMLElement | Window,
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

  private currentView(): View {
    if (!this.view) {
      const o = this.options
      this.view = fitView(o?.bounds ?? null, this.plot, {
        equalAspect: o?.equalAspect ?? false,
        padding: o?.padding ?? 0,
        marginPx: (o?.markerSize ?? 0) / 2,
      })
      this.fitted = true
      this.limits = zoomLimits(this.view)
    }
    return this.view
  }

  private layout(): void {
    const rect = this.container.getBoundingClientRect()
    const width = Math.floor(rect.width)
    const height = Math.floor(rect.height)
    if (width <= 0 || height <= 0) return
    const m = this.options?.axes ? AXES_MARGIN : PLAIN_MARGIN
    const box = {
      left: m.left,
      top: m.top,
      width: Math.max(1, width - m.left - m.right),
      height: Math.max(1, height - m.top - m.bottom),
    }
    const old = this.plot
    this.box = box
    this.size = { width, height }
    this.renderer.resize(
      { width, height },
      box,
      Math.min(window.devicePixelRatio || 1, MAX_DPR)
    )
    this.view = resizeView(this.view, old, this.plot, {
      equalAspect: this.options?.equalAspect ?? false,
      fitted: this.fitted,
    })
    this.readTheme()
    this.schedule(true)
  }

  private readTheme(): void {
    const style = getComputedStyle(this.canvas)
    this.theme = {
      foreground: style.color || "#000",
      font: `11px ${style.fontFamily || "sans-serif"}`,
    }
  }

  private schedule(render: boolean): void {
    this.needsRender ||= render
    if (this.frame) return
    this.frame = requestAnimationFrame(() => {
      this.frame = 0
      this.draw()
    })
  }

  private layer(o: ControllerOptions): PointsLayer {
    return {
      x: o.x,
      y: o.y,
      colors: o.colors,
      size: o.markerSize,
      shape: o.markerShape,
      selected: o.selected,
    }
  }

  private draw(): void {
    const o = this.options
    if (!o || this.box.width <= 0) return
    const view = this.currentView()
    const layer = this.layer(o)
    const gesture = this.drag?.tool === "pan" || this.wheelTimer !== null
    if (this.needsRender && !gesture) {
      this.renderer.render(view, layer, this.theme)
      this.needsRender = false
    }
    const drag = this.drag?.moved ? this.drag : null
    const p = drag?.points ?? []
    this.renderer.present(
      view,
      layer,
      {
        axes: o.axes,
        pole: o.pole,
        hover: this.hover,
        lasso: drag?.tool === "lasso" ? p : null,
        box:
          drag?.tool === "box"
            ? [p[0] ?? 0, p[1] ?? 0, p[2] ?? 0, p[3] ?? 0]
            : null,
      },
      this.theme
    )
  }

  private plotPoint(e: MouseEvent): [number, number] {
    const rect = this.canvas.getBoundingClientRect()
    return [
      e.clientX - rect.left - this.box.left,
      e.clientY - rect.top - this.box.top,
    ]
  }

  private pick(px: number, py: number): number {
    const o = this.options
    if (!o) return -1
    const radius = Math.max(PICK_PX, o.markerSize / 2 + 2)
    return nearestPoint(
      o.index,
      o.x,
      o.y,
      this.currentView(),
      this.plot,
      px,
      py,
      radius,
      o.colors
    )
  }

  private onPointerDown(e: PointerEvent): void {
    const o = this.options
    if (!o || e.button !== 0) return
    this.canvas.setPointerCapture(e.pointerId)
    const [px, py] = this.plotPoint(e)
    this.dragged = false
    this.drag = {
      tool: o.tool,
      x0: px,
      y0: py,
      moved: false,
      view: this.currentView(),
      points: [px, py],
    }
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
    if (d.tool === "center") return
    if (d.tool === "pan") {
      this.view = panView(d.view, this.plot, px - d.x0, py - d.y0)
      this.fitted = false
      this.clearHover()
    } else if (d.tool === "box") {
      d.points = [d.x0, d.y0, px, py]
    } else {
      const lx = d.points[d.points.length - 2] ?? d.x0
      const ly = d.points[d.points.length - 1] ?? d.y0
      if (Math.hypot(px - lx, py - ly) >= 2) d.points.push(px, py)
    }
    this.schedule(false)
  }

  private onPointerUp(e: PointerEvent): void {
    const d = this.drag
    const o = this.options
    if (!d || !o) return
    if (this.canvas.hasPointerCapture(e.pointerId)) {
      this.canvas.releasePointerCapture(e.pointerId)
    }
    this.dragged = d.moved
    if (!d.moved) {
      // A click: the click event reports it.
      this.drag = null
      this.updateCursor()
      return
    }
    if (d.tool === "pan") {
      this.endGesture()
      return
    }
    this.drag = null
    if (d.tool === "center") {
      // A drag with the centre tool sets nothing.
      this.updateCursor()
      return
    }
    const view = this.currentView()
    let indices: Int32Array
    if (d.tool === "box") {
      const [x0, y0] = toData(view, this.plot, d.x0, d.y0)
      const [x1, y1] = toData(view, this.plot, ...this.plotPoint(e))
      indices = boxSelect(o.x, o.y, { x0, y0, x1, y1 }, o.colors)
    } else {
      const polygon: number[] = []
      for (let k = 0; k + 1 < d.points.length; k += 2) {
        polygon.push(
          ...toData(view, this.plot, d.points[k] ?? 0, d.points[k + 1] ?? 0)
        )
      }
      indices = lassoSelect(o.x, o.y, polygon, o.colors)
    }
    const pairs =
      o.step && o.patternIndex
        ? selectionPairs(indices, o.step, o.patternIndex)
        : []
    o.onSelect?.({ indices, pairs })
    this.schedule(false)
  }

  private onClick(e: MouseEvent): void {
    // Not after a drag, and once per double click (its second click has detail 2).
    if (this.dragged || e.detail > 1) return
    const [px, py] = this.plotPoint(e)
    const index = this.pick(px, py)
    const o = this.options
    if (o?.tool === "center") {
      o.onCenterClick?.(
        index >= 0 ? index : null,
        toData(this.currentView(), this.plot, px, py)
      )
      return
    }
    if (index >= 0) o?.onPointClick?.(index)
  }

  private endGesture(): void {
    this.drag = null
    this.updateCursor()
    this.schedule(true)
  }

  private updateCursor(): void {
    const tool = this.drag?.tool ?? this.options?.tool
    this.canvas.style.cursor =
      tool !== "pan" ? "crosshair" : this.drag ? "grabbing" : "grab"
  }

  private onWheel(e: WheelEvent): void {
    if (!this.options) return
    e.preventDefault()
    const [px, py] = this.plotPoint(e)
    const lines = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1
    const factor = Math.exp(-e.deltaY * lines * 0.002)
    const view = this.currentView()
    const next = zoomView(view, this.plot, px, py, factor, this.limits)
    if (next === view) return
    this.view = next
    this.fitted = false
    if (this.wheelTimer !== null) clearTimeout(this.wheelTimer)
    this.wheelTimer = setTimeout(() => {
      this.wheelTimer = null
      this.schedule(true)
    }, WHEEL_IDLE_MS)
    this.hoverAt(px, py)
    this.schedule(false)
  }

  private hoverAt(px: number, py: number): void {
    const o = this.options
    if (!o) return
    const { width, height } = this.box
    if (px < 0 || py < 0 || px > width || py > height) {
      this.clearHover()
      return
    }
    const [x, y] = toData(this.currentView(), this.plot, px, py)
    const { readout, tooltip } = this.elements
    if (readout) {
      readout.textContent = `${o.axes?.x ?? "x"} ${coordinate(x)}, ${o.axes?.y ?? "y"} ${coordinate(y)}`
    }
    if (tooltip)
      placeTooltip(tooltip, px + this.box.left, py + this.box.top, this.size)
    this.setHover(this.pick(px, py))
  }

  private clearHover(): void {
    if (this.elements.readout) this.elements.readout.textContent = ""
    this.setHover(-1)
  }

  private setHover(index: number): void {
    if (index === this.hover) return
    this.hover = index
    this.schedule(false)
    this.options?.onHoverChange(index >= 0 ? index : null)
  }
}

function sameView(a: View | null, b: View | null): boolean {
  if (a === null || b === null) return a === b
  return a.x0 === b.x0 && a.y0 === b.y0 && a.x1 === b.x1 && a.y1 === b.y1
}

function coordinate(value: number): string {
  return String(Number(value.toPrecision(4)))
}

/** Beside the cursor (container pixels), on the side with more room. */
export function placeTooltip(
  el: HTMLElement,
  left: number,
  top: number,
  size: Size
): void {
  const right = left >= size.width / 2
  const below = top < size.height / 2
  el.style.left = right ? "" : `${String(left + TOOLTIP_GAP_PX)}px`
  el.style.right = right
    ? `${String(size.width - left + TOOLTIP_GAP_PX)}px`
    : ""
  el.style.top = below ? `${String(top + TOOLTIP_GAP_PX)}px` : ""
  el.style.bottom = below
    ? ""
    : `${String(size.height - top + TOOLTIP_GAP_PX)}px`
}
