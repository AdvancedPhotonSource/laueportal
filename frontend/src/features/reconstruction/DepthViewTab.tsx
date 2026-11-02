import { useMemo, useState } from "react"

import {
  ApiError,
  useGetReconstructionTrace,
  useReconstructionFrame,
  type DepthFrame,
  type ReconstructionPoint,
} from "@/api"
import { EmptyState } from "@/components/EmptyState"
import { ErrorState } from "@/components/ErrorState"
import { LoadingState } from "@/components/LoadingState"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { subheading, surface } from "@/lib/styles"
import { cn } from "@/lib/utils"
import {
  ColorBar,
  COLORMAP_LABELS,
  FRAME_AUTO_RANGE,
  FRAME_COLORMAPS,
  HeatmapView,
  resolveRange,
  useDistribution,
  type FrameColormap,
} from "@/viz"
import { errorStatus } from "./artifact-bar"
import {
  BUSY_RETRY,
  busyStatus,
  isUnavailable,
  type PlotStatus,
} from "@/lib/busy"
import { DepthControl } from "./DepthControl"
import { depthCount, depthRange, shownDepth } from "./depth-view"
import { PlotStatusLine } from "./PlotStatusLine"
import { useWireViewerStore } from "./viewer-store"

/** A 409 (the point is not complete, or cannot be read) or a 404: the point's state, not a failure. */
const isPointState = (error: unknown) =>
  error instanceof ApiError && (error.status === 409 || error.status === 404)

/**
 * The Depth View tab: the selected point's depth frame through the heatmap (equal aspect, origin
 * at the top left) with the colorbar, and the depth index in the side panel. A point opens at its
 * brightest depth (its trace's) unless the URL names a depth; the zoom stays across depths and
 * points while the frame size does.
 */
export function DepthViewTab({
  reconstructionId,
  loading,
  unavailable,
  point,
  depth,
  onDepthChange,
}: {
  reconstructionId: number
  /** The points are loading (the bar says why there are none otherwise). */
  loading: boolean
  /** Why the run has no reconstruction to show (`noReconstruction`); undefined when it has one. */
  unavailable: string | undefined
  /** The selected point's row, or undefined when there is none. */
  point: ReconstructionPoint | undefined
  /** The URL's depth index (undefined: the point's brightest). */
  depth: number | undefined
  /** A new depth index; `brightest` is the point's default, which the URL leaves out. */
  onDepthChange: (depth: number, brightest: number | undefined) => void
}) {
  if (!point) {
    return loading ? (
      <LoadingState label="Loading the points" />
    ) : (
      <EmptyState title={unavailable ?? "No point selected"} />
    )
  }
  return (
    <DepthPoint
      reconstructionId={reconstructionId}
      point={point}
      depth={depth}
      onDepthChange={onDepthChange}
    />
  )
}

function DepthPoint({
  reconstructionId,
  point,
  depth,
  onDepthChange,
}: {
  reconstructionId: number
  point: ReconstructionPoint
  depth: number | undefined
  onDepthChange: (depth: number, brightest: number | undefined) => void
}) {
  const pointId = point.point_id
  // The generated client puts the point ID in the path as it is.
  const trace = useGetReconstructionTrace(
    reconstructionId,
    encodeURIComponent(pointId),
    {
      query: {
        ...BUSY_RETRY,
        staleTime: Infinity,
      },
    }
  )
  const count = depthCount(point, trace.data)
  const index = shownDepth(depth, count, trace.data)
  const frameQuery = useReconstructionFrame(
    reconstructionId,
    pointId,
    index ?? 0,
    { enabled: index !== undefined, rule: BUSY_RETRY }
  )

  // The last frame shown stays while the next loads, so the plot never blanks, and when another depth
  // of the same point fails (marked stale); a newly selected point that fails shows no frame, never the
  // previous point's under its name.
  const [shown, setShown] = useState<{
    pointId: string
    data: DepthFrame
  } | null>(null)
  if (frameQuery.data && frameQuery.data !== shown?.data) {
    setShown({ pointId, data: frameQuery.data })
  }
  const current =
    shown?.pointId === pointId && shown.data.index === index ? shown : null
  const failed = trace.isError || (index !== undefined && frameQuery.isError)
  const plotted =
    shown && (shown.pointId === pointId || !failed) ? shown.data : null

  const axis = trace.data?.depth_um
  const depthAt = (at: number) =>
    current?.data.index === at
      ? (current.data.depthUm ?? axis?.[at] ?? null)
      : (axis?.[at] ?? null)

  const status = plotStatus({
    traceBusy: busyStatus(trace),
    frameBusy: index === undefined ? null : busyStatus(frameQuery),
    traceError: trace.error,
    frameError: index === undefined ? null : frameQuery.error,
    loading: index !== undefined && !current && frameQuery.isFetching,
    index,
  })
  const failure: unknown = [trace.error, frameQuery.error].find(
    (error) => error && !isPointState(error) && !isUnavailable(error)
  )

  return (
    <div className="grid gap-4 md:grid-cols-[16rem_minmax(0,1fr)]">
      <aside
        aria-label="Depth view settings"
        className={cn(surface, "flex h-fit flex-col gap-5 p-3")}
      >
        <section aria-label="Depth" className="flex flex-col gap-2">
          <h3 className={subheading}>Depth</h3>
          <DepthControl
            key={pointId}
            value={index}
            count={count ?? 0}
            depthAt={depthAt}
            range={depthRange(point, axis)}
            onChange={(next) => {
              onDepthChange(next, trace.data?.brightest_index)
            }}
          />
        </section>
        <section aria-label="Image" className="flex flex-col gap-2">
          <h3 className={subheading}>Image</h3>
          <ColormapChoice />
        </section>
      </aside>
      <div className="flex min-w-0 flex-col gap-3">
        {failure !== undefined && (
          <ErrorState
            error={failure}
            onRetry={() => {
              if (trace.error) void trace.refetch()
              if (frameQuery.error) void frameQuery.refetch()
            }}
            retrying={trace.isFetching || frameQuery.isFetching}
            stale={plotted !== null}
          />
        )}
        <DepthPlot
          shown={plotted}
          status={status}
          onRetry={() => {
            if (trace.error) void trace.refetch()
            if (frameQuery.error) void frameQuery.refetch()
          }}
          label={`Depth frame of point ${pointId}`}
        />
      </div>
    </div>
  )
}

/** What loads, or the point's state, over the plot; a busy trace's or frame's (its Retry once it gave up). */
function plotStatus({
  traceBusy,
  frameBusy,
  traceError,
  frameError,
  loading,
  index,
}: {
  traceBusy: PlotStatus | null
  frameBusy: PlotStatus | null
  traceError: unknown
  frameError: unknown
  loading: boolean
  index: number | undefined
}): PlotStatus | null {
  if (traceBusy) return traceBusy
  if (frameBusy) return frameBusy
  for (const error of [frameError, traceError]) {
    if (isPointState(error) || isUnavailable(error)) {
      const { text, hint } = errorStatus(error)
      return { text, hint }
    }
  }
  if (index === undefined) {
    return traceError ? null : { text: "Loading the point…" }
  }
  return loading ? { text: `Loading depth ${String(index)}…` } : null
}

/** The nine frame colormaps (the Depth View's own setting). */
function ColormapChoice() {
  const colormap = useWireViewerStore((state) => state.depth.colormap)
  const setDepthView = useWireViewerStore((state) => state.setDepthView)
  return (
    <div className="grid grid-cols-[5.5rem_minmax(0,1fr)] items-center gap-2 text-sm">
      <span className="text-muted-foreground">Colormap</span>
      <Select
        items={Object.fromEntries(
          FRAME_COLORMAPS.map((name) => [name, COLORMAP_LABELS[name]])
        )}
        value={colormap}
        onValueChange={(next) => {
          const name = FRAME_COLORMAPS.find((value) => value === next)
          if (name) setDepthView({ colormap: name satisfies FrameColormap })
        }}
      >
        <SelectTrigger size="sm" aria-label="Colormap" className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent className="w-auto">
          {FRAME_COLORMAPS.map((name) => (
            <SelectItem key={name} value={name}>
              {COLORMAP_LABELS[name]}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  )
}

/** The frame, with the status over it, and the colorbar under it (one card). */
function DepthPlot({
  shown,
  status,
  onRetry,
  label,
}: {
  shown: DepthFrame | null
  status: PlotStatus | null
  /** The status's Retry (a busy trace or frame that gave up). */
  onRetry: () => void
  label: string
}) {
  const settings = useWireViewerStore((state) => state.depth)
  const setDepthView = useWireViewerStore((state) => state.setDepthView)
  const frame = shown?.frame ?? null

  const { distribution, error, retry } = useDistribution(frame?.data ?? null)
  const resolved = distribution
    ? resolveRange(settings.colorbar, distribution)
    : null
  // Memoized on the values, so a frame with the same range recolours nothing more.
  const lo = resolved?.[0]
  const hi = resolved?.[1]
  const range = useMemo(
    () => (lo !== undefined && hi !== undefined ? ([lo, hi] as const) : null),
    [lo, hi]
  )
  const { scale: scaleKind, gamma } = settings.colorbar
  const scale = useMemo(() => ({ kind: scaleKind, gamma }), [scaleKind, gamma])

  return (
    <section aria-label="Depth frame" className={cn(surface, "flex flex-col")}>
      <div
        className="relative h-[clamp(20rem,62vh,44rem)] overflow-hidden"
        aria-busy={status?.text.startsWith("Loading") || undefined}
      >
        <HeatmapView
          frame={frame}
          colormap={settings.colormap}
          range={range}
          scale={scale}
          label={label}
        />
        {status && <PlotStatusLine status={status} onRetry={onRetry} />}
      </div>
      {frame && (
        <div aria-label="Colour scale" role="region" className="border-t p-3">
          <ColorBar
            distribution={distribution}
            error={error}
            onRetry={retry}
            colormap={settings.colormap}
            value={settings.colorbar}
            onChange={(colorbar) => {
              setDepthView({ colorbar })
            }}
            defaultAuto={FRAME_AUTO_RANGE}
            title="Intensity"
          />
        </div>
      )}
    </section>
  )
}
