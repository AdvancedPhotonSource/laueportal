// The 2D scatter: the colour map's 2D view and the pole figure. Canvas2D only, so it works
// without WebGL. The work is memoized on the arrays (and the lookup), not on the objects that
// hold them: new x or y arrays rebuild the hover index and the bounds, new colour arrays or a new
// lookup re-pack the colours, and both stamp the points again.
import {
  CrosshairIcon,
  LassoIcon,
  Maximize2Icon,
  MoveIcon,
  SquareDashedIcon,
  XIcon,
  type LucideIcon,
} from "lucide-react"
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

import { selectionMask, type LinkedSelection } from "../linked-selection"
import { ScatterController } from "./controller"
import { buildGridIndex } from "./spatial-index"
import {
  colorParts,
  colorsFromParts,
  packColors,
  type MarkerShape,
  type NonIndexedColor,
} from "./stamp"
import type { ScatterData, ScatterSelection, ScatterTool } from "./types"
import { dataBounds, type View } from "./view"

interface CommonProps {
  data: ScatterData
  /** Marker size in screen (CSS) pixels. */
  markerSize?: number
  markerShape?: MarkerShape
  /** The colour of the points whose `indexed` is 0. */
  nonIndexedColor?: NonIndexedColor
  /** The linked selection to highlight (needs `data.step` and `data.patternIndex`). */
  selection?: LinkedSelection | null
  /** A box or lasso selection ended (an empty one clears). */
  onSelect?: (selection: ScatterSelection) => void
  /**
   * The hovered point changed (null: none), not every pointer move; the cursor's coordinates are
   * shown in the view's own readout.
   */
  onHover?: (index: number | null) => void
  /** A click (no drag) on a point. */
  onPointClick?: (index: number) => void
  /** The hovered point's details, shown next to the cursor. */
  renderTooltip?: (index: number) => ReactNode
  defaultTool?: ScatterTool
  /** The canvas's accessible name. */
  label?: string
  className?: string
}

export interface ScatterViewProps extends CommonProps {
  /** Axis titles; null hides the axes. */
  axes?: { x: string; y: string } | null
  /** One data unit spans the same pixels on both axes (spatial axes). */
  equalAspect?: boolean
}

export interface PoleFigureViewProps extends CommonProps {
  /** The HSV colour centre (stereographic x, y), marked with a cross. */
  center?: readonly [number, number] | null
  /** The HSV colour radius around the centre (stereographic units), dotted. */
  colorRadius?: number | null
  /**
   * Adds the "Set color center" tool: a click with it reports the pole under the cursor (its index,
   * within the hover pick radius; null for none) and the point (stereographic x, y).
   */
  onCenterClick?: (index: number | null, point: [number, number]) => void
}

interface ScatterProps extends CommonProps {
  onCenterClick?: PoleFigureViewProps["onCenterClick"]
  axes: ScatterViewProps["axes"]
  equalAspect: boolean
  pole: {
    center: readonly [number, number] | null
    colorRadius: number | null
  } | null
  fixedBounds: View | null
}

const POLE_BOUNDS: View = { x0: -1.1, y0: -1.1, x1: 1.1, y1: 1.1 }

const TOOLS: readonly { tool: ScatterTool; label: string; Icon: LucideIcon }[] =
  [
    { tool: "pan", label: "Pan", Icon: MoveIcon },
    { tool: "box", label: "Box select", Icon: SquareDashedIcon },
    { tool: "lasso", label: "Lasso select", Icon: LassoIcon },
  ]

const CENTER_TOOL = {
  tool: "center",
  label: "Set color center",
  Icon: CrosshairIcon,
} as const

/** A scatter of x, y points: the colour map's 2D view. */
export function ScatterView({
  axes = null,
  equalAspect = false,
  markerShape = "square",
  ...rest
}: ScatterViewProps) {
  return (
    <Scatter
      {...rest}
      markerShape={markerShape}
      axes={axes}
      equalAspect={equalAspect}
      pole={null}
      fixedBounds={null}
    />
  )
}

/** The pole figure: the scatter in the unit circle, with the HSV centre marker. */
export function PoleFigureView({
  center = null,
  colorRadius = null,
  markerShape = "circle",
  defaultTool = "lasso",
  ...rest
}: PoleFigureViewProps) {
  return (
    <Scatter
      {...rest}
      markerShape={markerShape}
      defaultTool={defaultTool}
      axes={null}
      equalAspect
      pole={{ center, colorRadius }}
      fixedBounds={POLE_BOUNDS}
    />
  )
}

function Scatter({
  data,
  markerSize = 8,
  markerShape = "square",
  nonIndexedColor = "gray",
  selection = null,
  onSelect,
  onHover,
  onPointClick,
  onCenterClick,
  renderTooltip,
  defaultTool = "pan",
  label,
  className,
  axes,
  equalAspect,
  pole,
  fixedBounds,
}: ScatterProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const readoutRef = useRef<HTMLSpanElement>(null)
  const tooltipRef = useRef<HTMLDivElement>(null)
  const controllerRef = useRef<ScatterController | null>(null)
  const [tool, setTool] = useState<ScatterTool>(defaultTool)
  const [hovered, setHovered] = useState<number | null>(null)

  const { x, y, indexed = null, step = null, patternIndex = null } = data
  const count = x.length
  const [c0, c1, c2] = colorParts(data.colors)
  const packed = useMemo(
    () =>
      packColors(
        colorsFromParts([c0, c1, c2]),
        count,
        indexed,
        nonIndexedColor
      ),
    [c0, c1, c2, count, indexed, nonIndexedColor]
  )
  // From the coordinates only: a recolour or hiding points keeps the index and the zoom.
  const index = useMemo(() => buildGridIndex(x, y), [x, y])
  const bounds = useMemo(
    () => fixedBounds ?? dataBounds(x, y),
    [fixedBounds, x, y]
  )
  const selected = useMemo(
    () =>
      step && patternIndex
        ? selectionMask(selection, step, patternIndex)
        : null,
    [selection, step, patternIndex]
  )

  useEffect(() => {
    const container = containerRef.current
    const canvas = canvasRef.current
    if (!container || !canvas) return
    const controller = new ScatterController(container, canvas, {
      readout: readoutRef.current,
      tooltip: tooltipRef.current,
    })
    controllerRef.current = controller
    return () => {
      controller.destroy()
      controllerRef.current = null
    }
  }, [])

  // The controller redraws only what changed.
  useEffect(() => {
    controllerRef.current?.update({
      x,
      y,
      colors: packed,
      index,
      bounds,
      padding: fixedBounds ? 0 : 0.05,
      selected,
      step,
      patternIndex,
      markerSize,
      markerShape,
      equalAspect,
      axes: axes ?? null,
      pole,
      tool,
      onHoverChange: (i) => {
        setHovered(i)
        onHover?.(i)
      },
      onPointClick,
      onCenterClick,
      onSelect,
    })
  }, [
    x,
    y,
    packed,
    index,
    bounds,
    fixedBounds,
    selected,
    step,
    patternIndex,
    markerSize,
    markerShape,
    equalAspect,
    axes,
    pole,
    tool,
    onHover,
    onPointClick,
    onCenterClick,
    onSelect,
  ])

  const hasSelection = (selection?.keys.size ?? 0) > 0
  const tooltip =
    hovered !== null && renderTooltip ? renderTooltip(hovered) : null
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
      {count === 0 && (
        <p className="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-muted-foreground">
          No points to show
        </p>
      )}
      {/* Only the buttons take the pointer: the readout widens the box over the plot as the cursor
          moves, and must not take the pointer from the canvas (the hover would flicker). */}
      <div className="pointer-events-none absolute top-1 right-1 flex items-center gap-1 rounded-md bg-background/80 p-0.5">
        {/* The cursor's coordinates, written by the controller on each move. */}
        <span
          ref={readoutRef}
          className="px-1 font-mono text-xs text-muted-foreground tabular-nums empty:hidden"
        />
        <div
          role="toolbar"
          aria-label="Plot tools"
          className="pointer-events-auto flex gap-0.5"
        >
          {TOOLS.map(({ tool: t, label: name, Icon }) => (
            <Button
              key={t}
              size="icon-xs"
              variant={tool === t ? "secondary" : "ghost"}
              aria-pressed={tool === t}
              aria-label={name}
              title={name}
              onClick={() => {
                setTool(t)
              }}
            >
              <Icon />
            </Button>
          ))}
          {onCenterClick && (
            <>
              {/* Set apart from the drag tools: a click with it sets something. */}
              <span aria-hidden className="mx-0.5 my-1 w-px bg-border" />
              <Button
                size="icon-xs"
                variant={tool === CENTER_TOOL.tool ? "secondary" : "ghost"}
                aria-pressed={tool === CENTER_TOOL.tool}
                aria-label={CENTER_TOOL.label}
                title={CENTER_TOOL.label}
                onClick={() => {
                  setTool(CENTER_TOOL.tool)
                }}
              >
                <CENTER_TOOL.Icon />
              </Button>
            </>
          )}
          <Button
            size="icon-xs"
            variant="ghost"
            aria-label="Reset view"
            title="Reset view (double-click in empty space)"
            onClick={() => {
              controllerRef.current?.resetView()
            }}
          >
            <Maximize2Icon />
          </Button>
          {hasSelection && onSelect && (
            <Button
              size="icon-xs"
              variant="ghost"
              aria-label="Clear selection"
              title="Clear selection"
              onClick={() => {
                onSelect({ indices: new Int32Array(0), pairs: [] })
              }}
            >
              <XIcon />
            </Button>
          )}
        </div>
      </div>
      {/* Positioned by the controller beside the cursor; React renders only its content. */}
      <div
        ref={tooltipRef}
        className={cn(
          "pointer-events-none absolute z-10 max-w-64 rounded-md border bg-popover px-2 py-1 text-xs text-popover-foreground shadow-md",
          tooltip === null && "hidden"
        )}
      >
        {tooltip}
      </div>
    </div>
  )
}
