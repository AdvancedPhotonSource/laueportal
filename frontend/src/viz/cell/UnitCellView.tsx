import { RotateCcwIcon } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { drawUnitCell, type CellTheme } from "./draw"
import {
  DEFAULT_CAMERA,
  dragged,
  normalized,
  type Camera,
  type Mat3,
} from "./geometry"

export interface UnitCellViewProps {
  /** The cell's rows a, b, c in beamline coordinates (any unit: the view scales it). */
  direct: Mat3
  /** Draw the beam's direction from the a, b, c origin. */
  showBeam: boolean
  /** The canvas's accessible name. */
  label: string
  className?: string
  /** A camera the caller keeps (with `onCameraChange`); else the view keeps its own. */
  camera?: Camera
  onCameraChange?: (camera: Camera) => void
}

/**
 * A unit cell in beamline coordinates (Canvas2D). Drag turns the camera about the lab's Y and
 * tilts it; a double click or the reset button restores it. The camera stays when `direct` changes,
 * so cells of other points compare in one view; a caller that also unmounts the view keeps it
 * (`camera`, `onCameraChange`).
 */
export function UnitCellView({
  direct,
  showBeam,
  label,
  className,
  camera: shownCamera,
  onCameraChange,
}: UnitCellViewProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [ownCamera, setOwnCamera] = useState<Camera>(DEFAULT_CAMERA)
  const camera = shownCamera ?? ownCamera
  const setCamera = (next: Camera) => {
    setOwnCamera(next)
    onCameraChange?.(next)
  }
  const [size, setSize] = useState<{ width: number; height: number } | null>(
    null
  )
  // A theme switch changes the tokens the canvas reads.
  const [themeVersion, setThemeVersion] = useState(0)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const measure = () => {
      const box = canvas.getBoundingClientRect()
      setSize({ width: Math.round(box.width), height: Math.round(box.height) })
    }
    measure()
    const resize =
      typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure)
    resize?.observe(canvas)
    const theme = new MutationObserver(() => {
      setThemeVersion((v) => v + 1)
    })
    theme.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class", "style"],
    })
    return () => {
      resize?.disconnect()
      theme.disconnect()
    }
  }, [])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || !size || size.width === 0 || size.height === 0) return
    const ratio = window.devicePixelRatio || 1
    canvas.width = Math.round(size.width * ratio)
    canvas.height = Math.round(size.height * ratio)
    const ctx = canvas.getContext("2d")
    if (!ctx) return
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0)
    drawUnitCell(ctx, {
      width: size.width,
      height: size.height,
      camera,
      direct: normalized(direct),
      showBeam,
      theme: readTheme(canvas),
    })
  }, [direct, showBeam, camera, size, themeVersion])

  const drag = useRef<{ x: number; y: number; camera: Camera } | null>(null)
  return (
    <div className={cn("relative aspect-square w-full", className)}>
      <canvas
        ref={canvasRef}
        role="img"
        aria-label={label}
        className="absolute inset-0 size-full cursor-grab touch-none rounded-md bg-muted/40 active:cursor-grabbing"
        onPointerDown={(event) => {
          event.currentTarget.setPointerCapture(event.pointerId)
          drag.current = { x: event.clientX, y: event.clientY, camera }
        }}
        onPointerMove={(event) => {
          const start = drag.current
          if (!start) return
          setCamera(
            dragged(
              start.camera,
              event.clientX - start.x,
              event.clientY - start.y
            )
          )
        }}
        onPointerUp={() => {
          drag.current = null
        }}
        onPointerCancel={() => {
          drag.current = null
        }}
        onDoubleClick={() => {
          setCamera(DEFAULT_CAMERA)
        }}
      />
      <Button
        variant="ghost"
        size="icon-xs"
        className="absolute top-1 right-1"
        aria-label="Reset the view"
        title="Reset the view"
        onClick={() => {
          setCamera(DEFAULT_CAMERA)
        }}
      >
        <RotateCcwIcon aria-hidden />
      </Button>
    </div>
  )
}

function readTheme(canvas: HTMLCanvasElement): CellTheme {
  const style = getComputedStyle(canvas)
  const token = (name: string, fallback: string) =>
    style.getPropertyValue(name).trim() || fallback
  return {
    foreground: token("--foreground", "#000"),
    muted: token("--muted-foreground", "#666"),
    halo: token("--card", "#fff"),
    axes: [
      token("--cell-a", "#d6323a"),
      token("--cell-b", "#1f8a4c"),
      token("--cell-c", "#2f5bd3"),
    ],
    beam: token("--cell-beam", "#c27c0e"),
    font: style.fontFamily || "sans-serif",
  }
}
