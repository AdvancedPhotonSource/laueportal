// The heatmap: a detector or depth frame through a colormap, with the overlay layer. Canvas2D
// only, so it works without WebGL. The range and scale come from the interactive colorbar
// (`ColorBar`, `resolveRange`); changing them recolours the frame and nothing else.
import { Maximize2Icon } from "lucide-react"
import { useEffect, useMemo, useRef } from "react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

import { colormapPixels, type ColormapName } from "../color/colormaps"
import type { ColorScale } from "../color/scale"
import { HeatmapController } from "./controller"
import type { Frame } from "./frame"
import type { OverlayBox, OverlayLayer } from "./overlays"

const LINEAR: ColorScale = { kind: "linear", gamma: 2 }
const NO_OVERLAYS: readonly OverlayLayer[] = []
const NO_BOXES: readonly OverlayBox[] = []
const PIXEL_AXES = { x: "X pixel", y: "Y pixel" }

export interface HeatmapViewProps {
  /** The frame (decodeFrame), or null while it loads or when it is not available. */
  frame: Frame | null
  /** The frame's size without a frame, so the overlays still show (the overlays' width, height). */
  extent?: { width: number; height: number } | null
  colormap?: ColormapName
  reverse?: boolean
  /** The colorbar's range (resolveRange); the image is drawn once it is known. */
  range: readonly [number, number] | null
  scale?: ColorScale
  /** The image's opacity, 0–1 (the detector view's default is 0.8). */
  opacity?: number
  showImage?: boolean
  overlays?: readonly OverlayLayer[]
  /** Outlined pixel boxes (the wire viewer's ROIs); a new list redraws them, not the image's colours. */
  boxes?: readonly OverlayBox[]
  /** CSS pixels. */
  markerSize?: number
  labelSize?: number
  /** Axis titles; null hides the axes. */
  axes?: { x: string; y: string } | null
  /**
   * A click (not a drag) on a pixel of the frame, reported once the double-click interval has
   * passed: a double-click resets the view and reports no click.
   */
  onPixelClick?: (x: number, y: number) => void
  /** The canvas's accessible name. */
  label?: string
  className?: string
}

export function HeatmapView({
  frame,
  extent = null,
  colormap = "gray",
  reverse = false,
  range,
  scale = LINEAR,
  opacity = 1,
  showImage = true,
  overlays = NO_OVERLAYS,
  boxes = NO_BOXES,
  markerSize = 10,
  labelSize = 10,
  axes = PIXEL_AXES,
  onPixelClick,
  label = "Detector image",
  className,
}: HeatmapViewProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const readoutRef = useRef<HTMLSpanElement>(null)
  const controllerRef = useRef<HeatmapController | null>(null)
  const colors = useMemo(
    () => colormapPixels(colormap, reverse),
    [colormap, reverse]
  )

  useEffect(() => {
    const container = containerRef.current
    const canvas = canvasRef.current
    if (!container || !canvas) return
    const controller = new HeatmapController(
      container,
      canvas,
      readoutRef.current
    )
    controllerRef.current = controller
    return () => {
      controller.destroy()
      controllerRef.current = null
    }
  }, [])

  // The controller redraws only what changed (a new range recolours; a new overlay redraws).
  useEffect(() => {
    controllerRef.current?.update({
      frame,
      extent,
      colors,
      range,
      scale,
      opacity,
      showImage,
      overlays,
      boxes,
      markerSize,
      labelSize,
      axes,
      onPixelClick,
    })
  }, [
    frame,
    extent,
    colors,
    range,
    scale,
    opacity,
    showImage,
    overlays,
    boxes,
    markerSize,
    labelSize,
    axes,
    onPixelClick,
  ])

  const empty = !frame && !extent
  return (
    <div
      ref={containerRef}
      className={cn(
        "relative h-full min-h-60 w-full overflow-hidden select-none",
        className
      )}
    >
      <canvas
        ref={canvasRef}
        role="img"
        aria-label={label}
        className="absolute inset-0 size-full touch-none text-foreground"
      />
      {empty && (
        <p className="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-muted-foreground">
          No image to show
        </p>
      )}
      <div className="absolute top-1 right-1 flex items-center gap-1 rounded-md bg-background/80 p-0.5">
        {/* The pixel under the cursor and its value, written by the controller on each move. */}
        <span
          ref={readoutRef}
          className="px-1 font-mono text-xs whitespace-pre text-muted-foreground tabular-nums empty:hidden"
        />
        <Button
          size="icon-xs"
          variant="ghost"
          aria-label="Reset view"
          title="Reset view (double-click)"
          onClick={() => {
            controllerRef.current?.resetView()
          }}
        >
          <Maximize2Icon />
        </Button>
      </div>
    </div>
  )
}
