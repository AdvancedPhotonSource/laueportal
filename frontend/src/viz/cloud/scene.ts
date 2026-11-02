// The 3D view's VTK.js scene. Only PointCloud3DView loads this module, with import(), so VTK.js
// lands in its own chunk, fetched on the first switch to 3D.
//
// Points are square point sprites sized in screen pixels. Under a selection the other points are
// dimmed, and the selected ones are drawn again over the cloud with rings (renderer layers that
// share the camera). Hover and click pick on the CPU (./pick.ts): measured, VTK.js's hardware
// selector took far over a frame per pick, the CPU path a millisecond per camera change.
import "@kitware/vtk.js/Rendering/OpenGL/Actor"
import "@kitware/vtk.js/Rendering/OpenGL/Camera"
import "@kitware/vtk.js/Rendering/OpenGL/CubeAxesActor"
import "@kitware/vtk.js/Rendering/OpenGL/PolyDataMapper"
import "@kitware/vtk.js/Rendering/OpenGL/Renderer"
import "@kitware/vtk.js/Rendering/OpenGL/Texture"
import vtkDataArray from "@kitware/vtk.js/Common/Core/DataArray"
import vtkPolyData from "@kitware/vtk.js/Common/DataModel/PolyData"
import vtkInteractorStyleTrackballCamera from "@kitware/vtk.js/Interaction/Style/InteractorStyleTrackballCamera"
import vtkActor from "@kitware/vtk.js/Rendering/Core/Actor"
import vtkCubeAxesActor from "@kitware/vtk.js/Rendering/Core/CubeAxesActor"
import vtkMapper from "@kitware/vtk.js/Rendering/Core/Mapper"
import vtkRenderer from "@kitware/vtk.js/Rendering/Core/Renderer"
import vtkRenderWindow from "@kitware/vtk.js/Rendering/Core/RenderWindow"
import vtkRenderWindowInteractor from "@kitware/vtk.js/Rendering/Core/RenderWindowInteractor"
import vtkOpenGLRenderWindow from "@kitware/vtk.js/Rendering/OpenGL/RenderWindow"

import { frameThrottle } from "../frame-throttle"
import { placeTooltip } from "../scatter/controller"
import { resolveColor } from "../scatter/renderer"
import { pickPoint, projectPoints, type ScreenPoints } from "./pick"
import {
  boundsOf,
  drawnPoints,
  type Bounds,
  interleave,
  pointRgb,
  ringedPoints,
  vertexCells,
} from "./points"
import type { PointCloudAxes } from "./types"

export interface SceneOptions {
  x: ArrayLike<number>
  y: ArrayLike<number>
  z: ArrayLike<number>
  /** Packed colours (0xAABBGGRR; alpha 0 hides a point), as the scatter packs them. */
  colors: Uint32Array
  /** 1 per selected point; null without a selection. */
  selected: Uint8Array | null
  /** Marker size in CSS pixels. */
  markerSize: number
  axes: PointCloudAxes | null
  onHoverChange: (index: number | null) => void
  onPointClick?: ((index: number) => void) | undefined
}

/** Elements the scene positions directly (no React render per pointer move). */
export interface SceneElements {
  tooltip: HTMLElement | null
  /** The hover ring, a circle around the hovered point (as the 2D views draw it). */
  marker: HTMLElement | null
}

type Rgb = readonly [number, number, number]

/** A press that moves less than this (CSS px) is a click, not a rotation (as in the 2D views). */
const CLICK_SLOP_PX = 4

/** One layer of the selection's overlay: its renderer (sharing the camera), mapper and actor. */
function overlay() {
  return {
    renderer: vtkRenderer.newInstance(),
    mapper: vtkMapper.newInstance(),
    actor: vtkActor.newInstance(),
  }
}

export class CloudScene {
  private readonly container: HTMLElement
  private readonly elements: SceneElements
  private readonly renderWindow = vtkRenderWindow.newInstance()
  private readonly renderer = vtkRenderer.newInstance()
  private readonly glWindow = vtkOpenGLRenderWindow.newInstance()
  private readonly interactor = vtkRenderWindowInteractor.newInstance()
  private readonly style = vtkInteractorStyleTrackballCamera.newInstance()
  private readonly points = vtkPolyData.newInstance()
  private readonly rings = vtkPolyData.newInstance()
  private readonly colorArray = vtkDataArray.newInstance({
    name: "rgb",
    numberOfComponents: 3,
    values: new Uint8Array(0),
  })
  private readonly pointMapper = vtkMapper.newInstance()
  private readonly pointActor = vtkActor.newInstance()
  /**
   * The selection, drawn over the cloud in three more layers, each over the last whatever the
   * depth (as the 2D views draw the selected points over the dimmed ones): the rings (foreground
   * squares), their gaps (background squares), then the selected points.
   */
  private readonly ring = overlay()
  private readonly gap = overlay()
  private readonly top = overlay()
  private readonly axesActor = vtkCubeAxesActor.newInstance()
  private readonly cleanup: (() => void)[] = []
  private readonly pickThrottle = frameThrottle<{ x: number; y: number }>(
    ({ x, y }) => {
      this.hoverAt(x, y)
    }
  )

  private options: SceneOptions | null = null
  private xyz: Float32Array = new Float32Array(0)
  private drawn: Int32Array = new Int32Array(0)
  private bounds: Bounds | null = null
  private background: Rgb = [255, 255, 255]
  private dpr = 1
  private size = { width: 0, height: 0 }
  /** The drawn points on screen, for picking; null once the camera or size changes. */
  private screen: ScreenPoints | null = null
  private pointer: { x: number; y: number } | null = null
  /** The button press under way; `moved` once it has gone past the click slop (a drag). */
  private press: { x: number; y: number; moved: boolean } | null = null
  private hover = -1
  /**
   * The camera is the automatic one (new bounds refit it); false once the user drags past the click
   * slop or zooms. A plain click on a point keeps it.
   */
  private fitted = true
  private renderRequest = 0

  constructor(container: HTMLElement, elements: SceneElements) {
    this.container = container
    this.elements = elements
    this.dpr = Math.min(window.devicePixelRatio || 1, 2)
    // VTK.js throws when it gets no WebGL context (e.g. after a GPU crash, though the probe passed):
    // delete what was built, or the interactor's document listener keeps it all alive per attempt.
    try {
      this.renderWindow.setNumberOfLayers(4)
      this.renderWindow.addRenderer(this.renderer)
      this.renderWindow.addView(this.glWindow)
      this.glWindow.setContainer(container)
      const canvas = this.glWindow.getCanvas()
      if (canvas) {
        canvas.style.position = "absolute"
        canvas.style.inset = "0"
        canvas.style.width = "100%"
        canvas.style.height = "100%"
        canvas.style.touchAction = "none"
      }
      this.interactor.setView(this.glWindow)
      this.interactor.initialize()
      this.interactor.setContainer(container)
      this.interactor.setInteractorStyle(this.style)

      // The selected points share the cloud's points and colours.
      this.points.getPointData().setScalars(this.colorArray)
      this.rings.setPoints(this.points.getPoints())
      this.rings.getPointData().setScalars(this.colorArray)
      this.pointMapper.setInputData(this.points)
      this.pointActor.setMapper(this.pointMapper)
      this.renderer.addActor(this.pointActor)
      const camera = this.renderer.getActiveCamera()
      for (const [k, layer] of [this.ring, this.gap, this.top].entries()) {
        layer.renderer.setLayer(k + 1)
        layer.renderer.setInteractive(false)
        layer.renderer.setActiveCamera(camera)
        layer.mapper.setInputData(this.rings)
        layer.actor.setMapper(layer.mapper)
        layer.renderer.addActor(layer.actor)
        this.renderWindow.addRenderer(layer.renderer)
      }
      this.ring.mapper.setScalarVisibility(false)
      this.gap.mapper.setScalarVisibility(false)
      for (const mapper of [this.pointMapper, this.top.mapper]) {
        mapper.setColorModeToDirectScalars()
        mapper.setScalarModeToUsePointData()
      }
      for (const actor of [
        this.pointActor,
        this.ring.actor,
        this.gap.actor,
        this.top.actor,
      ]) {
        actor.getProperty().setLighting(false)
        actor.getProperty().setAmbient(1)
        actor.getProperty().setDiffuse(0)
      }
      this.axesActor.setCamera(camera)

      this.listen(container, "pointermove", (e) => {
        this.onPointerMove(e)
      })
      this.listen(container, "pointerdown", (e) => {
        this.press = { ...this.local(e), moved: false }
        this.setHover(-1)
      })
      this.listen(container, "pointerup", (e) => {
        this.onPointerUp(e)
      })
      this.listen(container, "pointerleave", () => {
        this.pointer = null
        this.pickThrottle.cancel()
        this.setHover(-1)
      })
      // VTK.js starts an interaction on every press, so a drag (onPointerMove) or a zoom is what ends
      // the automatic view, not the interaction's start.
      this.listen(container, "wheel", () => {
        this.fitted = false
      })
      // Any camera change (rotation, zoom, reset) moves the points on screen.
      const moved = camera.onModified(() => {
        this.screen = null
        if (this.pointer && !this.press)
          this.pickThrottle.schedule(this.pointer)
      })
      this.cleanup.push(() => {
        moved.unsubscribe()
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
      // A theme switch changes the background, the rings and the axes.
      const themeObserver = new MutationObserver(() => {
        this.readTheme()
        this.applyColors()
        this.requestRender()
      })
      themeObserver.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ["class", "style"],
      })
      this.cleanup.push(() => {
        themeObserver.disconnect()
      })
      this.readTheme()
      this.layout()
    } catch (e) {
      this.destroy()
      throw e
    }
  }

  update(next: SceneOptions): void {
    const prev = this.options
    this.options = next
    const moved =
      !prev || prev.x !== next.x || prev.y !== next.y || prev.z !== next.z
    const newAxes = !prev || !sameAxes(prev.axes, next.axes)
    // Other axes make another plot: back to the automatic view, whatever the user did to the camera.
    let refit = newAxes && prev !== null
    if (moved) {
      this.xyz = interleave(next.x, next.y, next.z)
      this.points.getPoints().setData(this.xyz, 3)
      this.points.getPoints().modified()
    }
    if (moved || prev.colors !== next.colors) {
      // The hovered point may be gone or hidden.
      this.setHover(-1)
      this.drawn = drawnPoints(next.colors, next.x, next.y, next.z)
      this.points.getVerts().setData(vertexCells(this.drawn))
      this.points.modified()
      this.screen = null
      const bounds = boundsOf(this.xyz, this.drawn)
      if (!sameBounds(bounds, this.bounds)) {
        this.bounds = bounds
        this.axesActor.setDataBounds(axesBounds(bounds))
        // New bounds refit the camera unless the user moved it (then their view stays).
        if (this.fitted) refit = true
      }
    }
    if (moved || prev.colors !== next.colors || prev.selected !== next.selected)
      this.applyColors()
    if (!prev || prev.markerSize !== next.markerSize) this.applySizes()
    if (newAxes) {
      if (next.axes) {
        this.axesActor.setAxisLabels([next.axes.x, next.axes.y, next.axes.z])
        this.renderer.addActor(this.axesActor)
      } else this.renderer.removeActor(this.axesActor)
    }
    if (refit) this.resetCamera()
    this.requestRender()
  }

  /** The automatic view: the whole cloud from the (1, 1, 1) direction, z up. */
  resetCamera(): void {
    const camera = this.renderer.getActiveCamera()
    camera.setFocalPoint(0, 0, 0)
    camera.setPosition(1, 1, 1)
    camera.setViewUp(0, 0, 1)
    if (this.bounds) this.renderer.resetCamera(this.bounds)
    else this.renderer.resetCamera()
    this.fitted = true
    this.requestRender()
  }

  destroy(): void {
    this.pickThrottle.cancel()
    cancelAnimationFrame(this.renderRequest)
    for (const undo of this.cleanup) undo()
    const canvas = this.glWindow.getCanvas()
    this.interactor.setContainer(null)
    this.interactor.delete()
    this.style.delete()
    this.axesActor.setCamera(null)
    for (const layer of [this.ring, this.gap, this.top]) {
      layer.actor.delete()
      layer.mapper.delete()
      layer.renderer.delete()
    }
    this.pointActor.delete()
    this.axesActor.delete()
    this.pointMapper.delete()
    this.points.delete()
    this.rings.delete()
    this.renderer.delete()
    this.glWindow.delete()
    this.renderWindow.delete()
    // VTK.js leaves the context to the garbage collector; browsers cap live contexts (about 16 in
    // Chrome), so switching 2D and 3D repeatedly would lose the oldest. Release it now.
    canvas
      ?.getContext("webgl2")
      ?.getExtension("WEBGL_lose_context")
      ?.loseContext()
    canvas?.remove()
  }

  private listen<K extends keyof HTMLElementEventMap>(
    target: HTMLElement,
    type: K,
    handler: (e: HTMLElementEventMap[K]) => void
  ): void {
    target.addEventListener(type, handler)
    this.cleanup.push(() => {
      target.removeEventListener(type, handler)
    })
  }

  private local(e: PointerEvent): { x: number; y: number } {
    const box = this.container.getBoundingClientRect()
    return { x: e.clientX - box.left, y: e.clientY - box.top }
  }

  private onPointerMove(e: PointerEvent): void {
    this.pointer = this.local(e)
    if (e.buttons !== 0) {
      // Rotating, panning or zooming: no tooltip until the drag ends. Past the click slop the user
      // has moved the camera, so new bounds no longer refit it.
      const press = this.press
      if (
        press &&
        !press.moved &&
        Math.hypot(this.pointer.x - press.x, this.pointer.y - press.y) >=
          CLICK_SLOP_PX
      ) {
        press.moved = true
        this.fitted = false
      }
      this.pickThrottle.cancel()
      this.setHover(-1)
      return
    }
    this.pickThrottle.schedule(this.pointer)
  }

  private onPointerUp(e: PointerEvent): void {
    const press = this.press
    this.press = null
    const at = this.local(e)
    this.pointer = at
    if (
      press &&
      !press.moved &&
      e.button === 0 &&
      Math.hypot(at.x - press.x, at.y - press.y) < CLICK_SLOP_PX
    ) {
      const index = this.pick(at.x, at.y)
      if (index >= 0) this.options?.onPointClick?.(index)
    }
    this.pickThrottle.schedule(at)
  }

  private pick(x: number, y: number): number {
    const o = this.options
    if (!o || this.drawn.length === 0) return -1
    const { width, height } = this.size
    if (width === 0 || height === 0) return -1
    this.screen ??= projectPoints(
      this.xyz,
      this.drawn,
      this.renderer
        .getActiveCamera()
        .getCompositeProjectionMatrix(width / height, -1, 1),
      width,
      height
    )
    const half = o.markerSize / 2
    // The selected points are drawn over the others, so they win under the cursor.
    return pickPoint(this.screen, x, y, half, Math.max(6, half + 2), o.selected)
  }

  private hoverAt(x: number, y: number): void {
    const index = this.pick(x, y)
    const { tooltip, marker } = this.elements
    if (tooltip) placeTooltip(tooltip, x, y, this.size)
    if (marker && index >= 0 && this.screen && this.options) {
      // The screen slot of the picked point (the projection keeps the drawn order).
      const slot = this.screen.ids.indexOf(index)
      const d = this.options.markerSize + 8
      marker.style.display = "block"
      marker.style.width = `${String(d)}px`
      marker.style.height = `${String(d)}px`
      marker.style.left = `${String((this.screen.sx[slot] ?? 0) - d / 2)}px`
      marker.style.top = `${String((this.screen.sy[slot] ?? 0) - d / 2)}px`
    }
    this.setHover(index)
  }

  private setHover(index: number): void {
    if (index < 0 && this.elements.marker)
      this.elements.marker.style.display = "none"
    if (index === this.hover) return
    this.hover = index
    this.options?.onHoverChange(index >= 0 ? index : null)
  }

  private layout(): void {
    const box = this.container.getBoundingClientRect()
    this.dpr = Math.min(window.devicePixelRatio || 1, 2)
    this.size = { width: box.width, height: box.height }
    if (box.width < 1 || box.height < 1) return
    this.glWindow.setSize(
      Math.round(box.width * this.dpr),
      Math.round(box.height * this.dpr)
    )
    this.screen = null
    this.applySizes()
    // Render now: a resize clears the canvas.
    cancelAnimationFrame(this.renderRequest)
    this.renderRequest = 0
    this.renderWindow.render()
  }

  private readTheme(): void {
    const style = getComputedStyle(this.container)
    const background = resolveColor(
      style.getPropertyValue("--background").trim() || "#fff"
    )
    const foreground = style.getPropertyValue("--foreground").trim() || "#000"
    const muted = style.getPropertyValue("--muted-foreground").trim() || "#888"
    const font = style.fontFamily || "sans-serif"
    this.background = rgbOf(background)
    const [br, bg, bb] = this.background
    this.renderer.setBackground(br / 255, bg / 255, bb / 255, 1)
    this.ring.actor
      .getProperty()
      .setColor(...unit(rgbOf(resolveColor(foreground))))
    this.gap.actor.getProperty().setColor(br / 255, bg / 255, bb / 255)
    this.axesActor.getProperty().setColor(...unit(rgbOf(resolveColor(muted))))
    this.axesActor.setAxisTextStyle({ fontColor: foreground, fontFamily: font })
    this.axesActor.setTickTextStyle({ fontColor: foreground, fontFamily: font })
  }

  /** Colours (dimmed under a selection) and the rings. */
  private applyColors(): void {
    const o = this.options
    if (!o) return
    this.colorArray.setData(pointRgb(o.colors, o.selected, this.background), 3)
    this.colorArray.modified()
    this.rings
      .getVerts()
      .setData(vertexCells(ringedPoints(this.drawn, o.selected)))
    this.rings.modified()
    this.points.modified()
  }

  /** Marker, ring and label sizes in device pixels. */
  private applySizes(): void {
    const size = this.options?.markerSize ?? 8
    const dpr = this.dpr
    this.pointActor.getProperty().setPointSize(size * dpr)
    this.ring.actor.getProperty().setPointSize((size + 8) * dpr)
    this.gap.actor.getProperty().setPointSize((size + 4) * dpr)
    this.top.actor.getProperty().setPointSize(size * dpr)
    this.axesActor.setAxisTextStyle({ fontSize: 13 * dpr })
    this.axesActor.setTickTextStyle({ fontSize: 11 * dpr })
    this.axesActor.setAxisTitlePixelOffset(30 * dpr)
    this.axesActor.setTickLabelPixelOffset(10 * dpr)
  }

  private requestRender(): void {
    if (this.renderRequest) return
    this.renderRequest = requestAnimationFrame(() => {
      this.renderRequest = 0
      this.renderWindow.render()
    })
  }
}

function rgbOf(packed: number): Rgb {
  return [packed & 0xff, (packed >>> 8) & 0xff, (packed >>> 16) & 0xff]
}

function unit([r, g, b]: Rgb): [number, number, number] {
  return [r / 255, g / 255, b / 255]
}

function sameAxes(a: SceneOptions["axes"], b: SceneOptions["axes"]): boolean {
  if (a === null || b === null) return a === b
  return a.x === b.x && a.y === b.y && a.z === b.z
}

function sameBounds(a: Bounds | null, b: Bounds | null): boolean {
  if (a === null || b === null) return a === b
  return a.every((v, k) => v === b[k])
}

/** The axes need a span on every axis: a flat cloud gets ±0.5 around its value. */
function axesBounds(bounds: Bounds | null): number[] {
  const b = bounds ?? [0, 1, 0, 1, 0, 1]
  return b.map((v, k) => {
    const lo = b[k - (k % 2)] as number
    const hi = b[k - (k % 2) + 1] as number
    if (hi > lo) return v
    return k % 2 ? v + 0.5 : v - 0.5
  })
}
