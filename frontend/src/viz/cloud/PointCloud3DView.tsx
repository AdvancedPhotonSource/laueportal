// The map's 3D view. VTK.js is loaded with import() when the view first mounts (its own chunk);
// without WebGL the view shows a message instead and the 2D views keep working. The colours and
// the selection mask are memoized on the arrays (and the lookup), as the scatter does.
import { Maximize2Icon } from "lucide-react"
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

import { selectionMask, type LinkedSelection } from "../linked-selection"
import {
  colorParts,
  colorsFromParts,
  packColors,
  type NonIndexedColor,
} from "../scatter/stamp"
import type { CloudScene } from "./scene"
import type { PointCloudAxes, PointCloudData } from "./types"
import { webGLAvailable } from "./webgl"

export interface PointCloud3DViewProps {
  data: PointCloudData
  /** The axis titles (the selected axes' names); null hides the axes. */
  axes?: PointCloudAxes | null
  /** Marker size in screen (CSS) pixels. */
  markerSize?: number
  /** The colour of the points whose `indexed` is 0. */
  nonIndexedColor?: NonIndexedColor
  /** The linked selection to highlight (needs `data.step` and `data.patternIndex`). */
  selection?: LinkedSelection | null
  /** The hovered point changed (null: none). */
  onHover?: (index: number | null) => void
  /** A click (no drag) on a point. */
  onPointClick?: (index: number) => void
  /** The hovered point's details; the view adds its x, y, z below them. */
  renderTooltip?: (index: number) => ReactNode
  /** The view's accessible name. */
  label?: string
  className?: string
}

export function PointCloud3DView(props: PointCloud3DViewProps) {
  const [webgl] = useState(webGLAvailable)
  if (!webgl)
    return (
      <div
        role="status"
        className={cn(
          "flex h-full min-h-60 w-full items-center justify-center p-4",
          props.className
        )}
      >
        <p className="text-center text-sm text-muted-foreground">
          3D needs WebGL, which this browser or machine doesn&apos;t provide.
        </p>
      </div>
    )
  return <Cloud {...props} />
}

type Status = "loading" | "ready" | "error"

function Cloud({
  data,
  axes = null,
  markerSize = 8,
  nonIndexedColor = "gray",
  selection = null,
  onHover,
  onPointClick,
  renderTooltip,
  label,
  className,
}: PointCloud3DViewProps) {
  const viewRef = useRef<HTMLDivElement>(null)
  const tooltipRef = useRef<HTMLDivElement>(null)
  const markerRef = useRef<HTMLDivElement>(null)
  const sceneRef = useRef<CloudScene | null>(null)
  const [status, setStatus] = useState<Status>("loading")
  const [attempt, setAttempt] = useState(0)
  const [hovered, setHovered] = useState<number | null>(null)

  const { x, y, z, indexed = null, step = null, patternIndex = null } = data
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
  const selected = useMemo(
    () =>
      step && patternIndex
        ? selectionMask(selection, step, patternIndex)
        : null,
    [selection, step, patternIndex]
  )

  // Load VTK.js (once per page load; later mounts reuse the chunk) and build the scene. Unmounting
  // destroys it and releases its WebGL context.
  useEffect(() => {
    const view = viewRef.current
    if (!view) return
    let cancelled = false
    let scene: CloudScene | null = null
    import("./scene").then(
      ({ CloudScene }) => {
        if (cancelled) return
        try {
          scene = new CloudScene(view, {
            tooltip: tooltipRef.current,
            marker: markerRef.current,
          })
        } catch {
          // No WebGL context after all (e.g. the browser's limit); the scene deleted what it built.
          setStatus("error")
          return
        }
        sceneRef.current = scene
        setStatus("ready")
      },
      () => {
        if (!cancelled) setStatus("error")
      }
    )
    return () => {
      cancelled = true
      scene?.destroy()
      sceneRef.current = null
    }
  }, [attempt])

  // The scene updates only what changed.
  useEffect(() => {
    sceneRef.current?.update({
      x,
      y,
      z,
      colors: packed,
      selected,
      markerSize,
      axes,
      onHoverChange: (i) => {
        setHovered(i)
        onHover?.(i)
      },
      onPointClick,
    })
  }, [
    status,
    x,
    y,
    z,
    packed,
    selected,
    markerSize,
    axes,
    onHover,
    onPointClick,
  ])

  const tooltip =
    hovered !== null ? (
      <>
        {renderTooltip?.(hovered)}
        <dl className="mt-0.5 grid grid-cols-[auto_auto] gap-x-2 font-mono tabular-nums">
          {(
            [
              [axes?.x ?? "x", x],
              [axes?.y ?? "y", y],
              [axes?.z ?? "z", z],
            ] as const
          ).map(([name, values], k) => (
            <div key={k} className="contents">
              <dt className="text-muted-foreground">{name}</dt>
              <dd>{coordinate(values[hovered] as number)}</dd>
            </div>
          ))}
        </dl>
      </>
    ) : null
  return (
    <div
      className={cn(
        "relative h-full min-h-60 w-full overflow-hidden bg-background select-none",
        className
      )}
    >
      {/* VTK.js's canvas goes in here (React renders nothing else into it). */}
      <div
        ref={viewRef}
        role="img"
        aria-label={label}
        className="absolute inset-0"
      />
      {status !== "ready" && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-sm text-muted-foreground">
          {status === "loading" ? (
            <p>Loading the 3D view…</p>
          ) : (
            <>
              <p>The 3D view could not be loaded.</p>
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  setStatus("loading")
                  setAttempt((a) => a + 1)
                }}
              >
                Retry
              </Button>
            </>
          )}
        </div>
      )}
      {status === "ready" && count === 0 && (
        <p className="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-muted-foreground">
          No points to show
        </p>
      )}
      <div className="absolute top-1 right-1 flex items-center gap-1 rounded-md bg-background/80 p-0.5">
        <Button
          size="icon-xs"
          variant="ghost"
          aria-label="Reset view"
          title="Reset view"
          disabled={status !== "ready"}
          onClick={() => {
            sceneRef.current?.resetCamera()
          }}
        >
          <Maximize2Icon />
        </Button>
      </div>
      {/* The hover ring and the tooltip, positioned by the scene; React renders only the content. */}
      <div
        ref={markerRef}
        className="pointer-events-none absolute rounded-full border-2 border-foreground"
        style={{ display: "none" }}
      />
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

function coordinate(value: number): string {
  return String(Number(value.toPrecision(6)))
}
