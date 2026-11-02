import { XIcon } from "lucide-react"
import { useMemo, useState } from "react"

import {
  ApiError,
  useReconstructionReference,
  useReconstructionRoiTraces,
  type ReconstructionPoint,
  type ReconstructionReferenceKind,
} from "@/api"
import { EmptyState } from "@/components/EmptyState"
import { ErrorState } from "@/components/ErrorState"
import { LoadingState } from "@/components/LoadingState"
import { Button } from "@/components/ui/button"
import { Card, CardAction, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Choice, Control, Section } from "@/features/indexing/controls"
import { NumberField } from "@/features/indexing/NumberField"
import { surface } from "@/lib/styles"
import { formatSignificant } from "@/lib/format"
import { cn } from "@/lib/utils"
import {
  ColorBar,
  COLORMAP_LABELS,
  FRAME_AUTO_RANGE,
  FRAME_COLORMAPS,
  HeatmapView,
  resolveRange,
  useDistribution,
  type Frame,
  type OverlayBox,
} from "@/viz"
import { errorStatus } from "./artifact-bar"
import {
  BUSY_RETRY,
  busyStatus,
  isUnavailable,
  type PlotStatus,
} from "@/lib/busy"
import { PlotStatusLine } from "./PlotStatusLine"
import { AXIS_LABELS, traceChart } from "./roi-chart"
import { RoiTracesChart } from "./RoiTracesChart"
import {
  addRoi,
  MAX_ROIS,
  REFUSAL_TEXT,
  roiBounds,
  roiCenter,
  usePointRois,
  type Roi,
} from "./rois"
import {
  isRoiSize,
  REFERENCE_KINDS,
  TRACE_AXES,
  TRACE_SCALES,
  TRACE_VALUES,
  useWireViewerStore,
} from "./viewer-store"

/** lauelab's `REFERENCE_OPTIONS` (the sum of the reconstructed frames, the first raw frame, the sum of the raw frames), short. */
const REFERENCE_LABELS: Record<ReconstructionReferenceKind, string> = {
  sum_reconstructed: "Reconstructed sum",
  first_raw: "First raw frame",
  sum_raw: "Raw sum",
}
const SCALE_LABELS = { linear: "Linear", log: "Log" } as const
/** lauelab's `INTENSITY_OPTIONS`: each ROI's sums, or those divided by its maximum. */
const VALUE_LABELS = { sum: "Sum", normalized: "Normalized" } as const

/** A 4xx the API answers about the point or the request (not a failure to retry). */
const isRefusal = (error: unknown) =>
  error instanceof ApiError && error.status >= 400 && error.status < 500

/** The status of a capped route's query: busy, a refusal (its title, the detail as the tooltip). */
function queryStatus(query: {
  isFetching: boolean
  failureReason: unknown
  error: unknown
}): PlotStatus | null {
  const busy = busyStatus(query)
  if (busy) return busy
  if (isRefusal(query.error)) {
    const { text, hint } = errorStatus(query.error)
    return { text, hint }
  }
  return null
}

/** A failure that is neither busy, nor not responding, nor a refusal: an ErrorState with Retry. */
const isFailure = (error: unknown) =>
  error !== null && !isRefusal(error) && !isUnavailable(error)

/**
 * The ROI Inspector tab: the selected point's reference image, where a click adds a square ROI; the
 * ROI list; the ROIs' traces against depth. The ROIs are kept per run and point in the browser.
 */
export function RoiInspectorTab({
  reconstructionId,
  loading,
  unavailable,
  point,
}: {
  reconstructionId: number
  loading: boolean
  /** Why the run has no reconstruction to show (`noReconstruction`); undefined when it has one. */
  unavailable: string | undefined
  point: ReconstructionPoint | undefined
}) {
  if (!point) {
    return loading ? (
      <LoadingState label="Loading the points" />
    ) : (
      <EmptyState title={unavailable ?? "No point selected"} />
    )
  }
  return (
    <RoiPoint
      key={point.point_id}
      reconstructionId={reconstructionId}
      pointId={point.point_id}
    />
  )
}

function RoiPoint({
  reconstructionId,
  pointId,
}: {
  reconstructionId: number
  pointId: string
}) {
  const settings = useWireViewerStore((state) => state.roi)
  const setRoiView = useWireViewerStore((state) => state.setRoiView)
  const [rois, setRois] = usePointRois(reconstructionId, pointId)
  // The ROIs unchecked in the list (this page only: all are shown when the point opens).
  const [hidden, setHidden] = useState<ReadonlySet<Roi>>(() => new Set())
  const [refusal, setRefusal] = useState<string | null>(null)

  const reference = useReconstructionReference(
    reconstructionId,
    pointId,
    settings.reference,
    { rule: BUSY_RETRY }
  )
  // The last image shown stays while another kind loads; a kind that fails shows none, never the
  // previous kind's under its name.
  const [last, setLast] = useState<Frame | null>(null)
  if (reference.data && reference.data !== last) setLast(reference.data)
  const shown = reference.data ?? (reference.isError ? null : last)

  const asked = useMemo(
    () => rois.map(({ x, y, size }) => ({ x, y, size })),
    [rois]
  )
  const traces = useReconstructionRoiTraces(
    reconstructionId,
    pointId,
    asked,
    settings.values === "normalized",
    BUSY_RETRY
  )

  const shape = shown ? ([shown.height, shown.width] as const) : null
  const boxes = useMemo<OverlayBox[]>(
    () =>
      shape
        ? rois.flatMap((roi, index) => {
            const bounds = hidden.has(roi) ? null : roiBounds(roi, shape)
            return bounds
              ? [{ bounds, color: roi.color, label: String(index + 1) }]
              : []
          })
        : [],
    // The shape's values, not the tuple's identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rois, hidden, shape?.[0], shape?.[1]]
  )

  const onPixelClick = (x: number, y: number) => {
    if (!shape) return
    const next = addRoi(rois, { x, y }, settings.size, shape)
    if ("refused" in next) {
      setRefusal(REFUSAL_TEXT[next.refused])
    } else {
      setRefusal(null)
      setRois(next.rois)
    }
  }
  const remove = (roi: Roi) => {
    setRefusal(null)
    setRois(rois.filter((other) => other !== roi))
  }

  const referenceStatus =
    queryStatus(reference) ??
    (reference.isFetching && !reference.data
      ? { text: "Loading the reference…" }
      : null)

  return (
    <div className="grid gap-4 md:grid-cols-[18rem_minmax(0,1fr)]">
      <aside
        aria-label="ROI inspector settings"
        className={cn(surface, "flex h-fit flex-col gap-5 p-3")}
      >
        <Section title="Reference">
          <Choice
            label="Image"
            value={settings.reference}
            options={REFERENCE_KINDS.map((kind) => ({
              value: kind,
              label: REFERENCE_LABELS[kind],
            }))}
            onChange={(reference) => {
              setRoiView({ reference })
            }}
          />
          <Choice
            label="Colormap"
            value={settings.colormap}
            options={FRAME_COLORMAPS.map((name) => ({
              value: name,
              label: COLORMAP_LABELS[name],
            }))}
            onChange={(colormap) => {
              setRoiView({ colormap })
            }}
          />
        </Section>
        <Section title="ROIs">
          <Control label="Size (px)">
            <NumberField
              aria-label="ROI size"
              title="Side of the next ROI, in pixels"
              className="h-7"
              value={settings.size}
              isValid={isRoiSize}
              onCommit={(size) => {
                setRoiView({ size })
              }}
            />
          </Control>
        </Section>
        <Section title="Traces">
          <Choice
            label="X axis"
            value={settings.axis}
            options={TRACE_AXES.map((axis) => ({
              value: axis,
              label: AXIS_LABELS[axis],
            }))}
            onChange={(axis) => {
              setRoiView({ axis })
            }}
          />
          <Choice
            label="Y axis"
            value={settings.scale}
            options={TRACE_SCALES.map((scale) => ({
              value: scale,
              label: SCALE_LABELS[scale],
            }))}
            onChange={(scale) => {
              setRoiView({ scale })
            }}
          />
          <Choice
            label="Values"
            value={settings.values}
            options={TRACE_VALUES.map((values) => ({
              value: values,
              label: VALUE_LABELS[values],
            }))}
            onChange={(values) => {
              setRoiView({ values })
            }}
          />
        </Section>
      </aside>
      <div className="grid min-w-0 content-start gap-4 xl:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="flex min-w-0 flex-col gap-3">
          {isFailure(reference.error) && (
            <ErrorState
              error={reference.error}
              onRetry={() => void reference.refetch()}
              retrying={reference.isFetching}
              stale={shown !== null}
            />
          )}
          <ReferenceCard
            frame={shown}
            boxes={boxes}
            status={referenceStatus}
            onRetry={() => void reference.refetch()}
            onPixelClick={onPixelClick}
            label={`${REFERENCE_LABELS[settings.reference]} of point ${pointId}`}
          />
        </div>
        <RoiList
          rois={rois}
          hidden={hidden}
          refusal={refusal}
          onShow={(roi, show) => {
            const next = new Set(hidden)
            if (show) next.delete(roi)
            else next.add(roi)
            setHidden(next)
          }}
          onDelete={remove}
        />
        <TracesCard
          rois={rois}
          hidden={hidden}
          traces={traces}
          axis={settings.axis}
          scale={settings.scale}
        />
      </div>
    </div>
  )
}

/** The reference image with the ROIs' outlines, the status over it, and its colorbar (one card). */
function ReferenceCard({
  frame,
  boxes,
  status,
  onRetry,
  onPixelClick,
  label,
}: {
  frame: Frame | null
  boxes: readonly OverlayBox[]
  status: PlotStatus | null
  onRetry: () => void
  onPixelClick: (x: number, y: number) => void
  label: string
}) {
  const settings = useWireViewerStore((state) => state.roi)
  const setRoiView = useWireViewerStore((state) => state.setRoiView)
  const { distribution, error, retry } = useDistribution(frame?.data ?? null)
  const resolved = distribution
    ? resolveRange(settings.colorbar, distribution)
    : null
  const lo = resolved?.[0]
  const hi = resolved?.[1]
  const range = useMemo(
    () => (lo !== undefined && hi !== undefined ? ([lo, hi] as const) : null),
    [lo, hi]
  )
  const { scale: scaleKind, gamma } = settings.colorbar
  const scale = useMemo(() => ({ kind: scaleKind, gamma }), [scaleKind, gamma])

  return (
    <Card aria-label="Reference image" role="region" className="gap-0 py-0">
      <CardHeader className="py-2.5">
        <CardTitle>Reference</CardTitle>
      </CardHeader>
      <div
        className="relative h-[clamp(18rem,52vh,36rem)] overflow-hidden"
        title="Click to add an ROI"
      >
        <HeatmapView
          frame={frame}
          colormap={settings.colormap}
          range={range}
          scale={scale}
          boxes={boxes}
          onPixelClick={onPixelClick}
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
              setRoiView({ colorbar })
            }}
            defaultAuto={FRAME_AUTO_RANGE}
            title="Intensity"
          />
        </div>
      )}
    </Card>
  )
}

const coordinate = (value: number) => formatSignificant(value)

/** The ROI list: shown (checkbox), number and colour, centre Y and X, size, delete. */
function RoiList({
  rois,
  hidden,
  refusal,
  onShow,
  onDelete,
}: {
  rois: readonly Roi[]
  hidden: ReadonlySet<Roi>
  refusal: string | null
  onShow: (roi: Roi, show: boolean) => void
  onDelete: (roi: Roi) => void
}) {
  return (
    <Card aria-label="ROI list" role="region" className="h-fit gap-0 py-0">
      <CardHeader className="py-2.5">
        <CardTitle>ROIs</CardTitle>
        <CardAction className="flex items-center gap-3 text-xs text-muted-foreground">
          {refusal && (
            <span role="status" className="text-destructive">
              {refusal}
            </span>
          )}
          <span title={`At most ${String(MAX_ROIS)}`}>
            {rois.length} / {MAX_ROIS}
          </span>
        </CardAction>
      </CardHeader>
      {rois.length === 0 ? (
        <p className="px-4 py-3 text-sm text-muted-foreground">No ROIs</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-8 pl-4">
                <span className="sr-only">Shown</span>
              </TableHead>
              <TableHead>ROI</TableHead>
              <TableHead className="text-right">Y</TableHead>
              <TableHead className="text-right">X</TableHead>
              <TableHead className="text-right">Size</TableHead>
              <TableHead className="w-8 pr-4">
                <span className="sr-only">Delete</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rois.map((roi, index) => {
              const center = roiCenter(roi)
              const name = `ROI ${String(index + 1)} at Y ${coordinate(center.y)}, X ${coordinate(center.x)}`
              return (
                <TableRow key={index} className="tabular-nums">
                  <TableCell className="pl-4">
                    <Checkbox
                      aria-label={`Show ${name}`}
                      checked={!hidden.has(roi)}
                      onCheckedChange={(checked) => {
                        onShow(roi, checked)
                      }}
                    />
                  </TableCell>
                  <TableCell>
                    <span className="inline-flex items-center gap-1.5">
                      <span
                        aria-label={roi.color}
                        role="img"
                        className="inline-block size-3.5 rounded-sm"
                        style={{ backgroundColor: roi.color }}
                      />
                      {index + 1}
                    </span>
                  </TableCell>
                  <TableCell className="text-right">
                    {coordinate(center.y)}
                  </TableCell>
                  <TableCell className="text-right">
                    {coordinate(center.x)}
                  </TableCell>
                  <TableCell className="text-right">{roi.size}</TableCell>
                  <TableCell className="pr-4">
                    <Button
                      variant="ghost"
                      size="icon-xs"
                      aria-label={`Delete ${name}`}
                      title="Delete"
                      onClick={() => {
                        onDelete(roi)
                      }}
                    >
                      <XIcon aria-hidden />
                    </Button>
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      )}
    </Card>
  )
}

/** The shown ROIs' traces, with the statuses over the chart. */
function TracesCard({
  rois,
  hidden,
  traces,
  axis,
  scale,
}: {
  rois: readonly Roi[]
  hidden: ReadonlySet<Roi>
  traces: ReturnType<typeof useReconstructionRoiTraces>
  axis: (typeof TRACE_AXES)[number]
  scale: (typeof TRACE_SCALES)[number]
}) {
  const chart = traceChart(traces.data, rois, { axis, scale, hidden })
  // Also while other traces load over the last answer (its placeholder): a new ROI has no line yet.
  const status =
    queryStatus(traces) ??
    (traces.isFetching && (!traces.data || traces.isPlaceholderData)
      ? { text: "Loading the traces…" }
      : null)
  return (
    <Card
      aria-label="ROI traces"
      role="region"
      className="gap-0 py-0 xl:col-span-2"
    >
      <CardHeader className="py-2.5">
        <CardTitle>Traces</CardTitle>
        {chart.notes.length > 0 && (
          <CardAction className="flex flex-col items-end text-xs text-muted-foreground">
            {chart.notes.map((note) => (
              <span key={note} role="status">
                {note}
              </span>
            ))}
          </CardAction>
        )}
      </CardHeader>
      {isFailure(traces.error) && (
        <ErrorState
          className="m-3 w-auto"
          error={traces.error}
          onRetry={() => void traces.refetch()}
          retrying={traces.isFetching}
          stale={traces.data !== undefined}
        />
      )}
      {rois.every((roi) => hidden.has(roi)) ? (
        <p className="px-4 py-3 text-sm text-muted-foreground">No ROIs shown</p>
      ) : (
        <div className="relative p-3">
          <RoiTracesChart chart={chart} />
          {status && (
            <PlotStatusLine
              status={status}
              onRetry={() => void traces.refetch()}
            />
          )}
        </div>
      )}
    </Card>
  )
}
