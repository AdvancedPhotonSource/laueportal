// The Calibration page: LaueGo's detector calibration (lauelab.calibration). A frame of the calibration
// crystal is searched for peaks (more added or removed by clicking), indexed with the starting
// geometry, given some measured energies, and the detector's rotation and translation are fitted
// (with the sample rotation for detector 0). Save writes the starting geometry file with the refined
// detector into the shared geometry folder. The server keeps nothing; the draft is in the browser.
import { useMemo, useState } from "react"

import {
  fitCalibrationPeak,
  useCalibrationFrame,
  useCalibrationIndexing,
  useCalibrationInputs,
  useCalibrationPeaks,
  useGetCalibrationSetup,
  useRefineCalibration,
  type CalibrationIndexRequest,
  type CalibrationRefineRequest,
} from "@/api"
import { ErrorState } from "@/components/ErrorState"
import { LoadingState } from "@/components/LoadingState"
import { BUSY_RETRY } from "@/lib/busy"
import { surface as surfaceClass } from "@/lib/styles"
import { toastError } from "@/lib/toast"
import { cn } from "@/lib/utils"
import {
  ColorBar,
  colorbarSettings,
  FRAME_AUTO_RANGE,
  HeatmapView,
  resolveRange,
  useDistribution,
  type ColorbarSettings,
  type Frame,
  type OverlayLayer,
} from "@/viz"

import { calibrationLayers } from "./calibration"
import {
  IndexCard,
  InputsCard,
  PeaksCard,
  RefineCard,
  SaveCard,
  type PeakTool,
} from "./CalibrationCards"
import {
  EMPTY_DRAFT,
  listedPeaks,
  measurementsFor,
  nearestPeak,
  peakKey,
  useCalibrationDraftStore,
} from "./draft"
import { ReflectionsTable } from "./ReflectionsTable"

/** A click within this many pixels of a peak removes it (Remove peak). */
const REMOVE_RADIUS_PX = 12

export function CalibrationPage() {
  const setup = useGetCalibrationSetup({ query: { ...BUSY_RETRY } })
  const files = useCalibrationDraftStore((state) => state.files)
  const frames = useCalibrationDraftStore((state) => state.frames)
  const detector0 = useCalibrationDraftStore((state) => state.detector0)
  const updateFrame = useCalibrationDraftStore((state) => state.updateFrame)
  const [tool, setTool] = useState<PeakTool>(null)
  const labels = useCalibrationDraftStore((state) => state.showLabels)
  const labelSize = useCalibrationDraftStore((state) => state.labelSize)

  const defaults = setup.data
  const frame = files.frame.trim()
  const geometry = files.geometry.trim() || defaults?.default_geometry || ""
  const crystal = files.crystal.trim()
  const draft = frames[frame] ?? EMPTY_DRAFT

  const inputs = useCalibrationInputs(
    frame && geometry && crystal ? { frame, geometry, crystal } : null,
    BUSY_RETRY
  )
  const peakSettings = draft.peakSettings ?? defaults?.peak_settings ?? null
  const indexSettings = draft.indexSettings ?? defaults?.index_settings ?? null
  const peaks = useCalibrationPeaks(
    frame && peakSettings && inputs.data
      ? { frame, settings: peakSettings }
      : null,
    BUSY_RETRY
  )
  const listed = useMemo(
    () => listedPeaks(peaks.data?.peaks ?? [], draft),
    [peaks.data, draft]
  )

  const indexRequest = useMemo<CalibrationIndexRequest | null>(
    () =>
      inputs.data && peaks.data && indexSettings
        ? {
            frame,
            geometry,
            crystal,
            peaks: listed.map(({ x, y }) => ({ x, y })),
            settings: indexSettings,
            start: draft.start,
          }
        : null,
    [
      inputs.data,
      peaks.data,
      indexSettings,
      frame,
      geometry,
      crystal,
      listed,
      draft.start,
    ]
  )
  const indexing = useCalibrationIndexing(indexRequest, BUSY_RETRY)

  const fitSample = inputs.data?.fit_sample_rotation ?? true
  const sampleReciprocal =
    !fitSample && detector0 && detector0.frame !== frame
      ? detector0.reciprocal
      : null
  const refineRequest = useMemo<CalibrationRefineRequest | null>(
    () =>
      indexRequest
        ? {
            ...indexRequest,
            measurements: measurementsFor(listed, draft),
            sample_reciprocal: sampleReciprocal,
            azimuth_deg: defaults?.azimuth_deg ?? 90,
          }
        : null,
    [indexRequest, listed, draft, sampleReciprocal, defaults?.azimuth_deg]
  )
  const refine = useRefineCalibration()
  // A fit belongs to the request it answered: once the inputs change, it is shown no longer.
  const refinedRequest = refine.variables?.data
  const fresh =
    refine.data !== undefined &&
    refineRequest !== null &&
    JSON.stringify(refinedRequest) === JSON.stringify(refineRequest)
  const result = fresh ? refine.data : undefined

  const image = useCalibrationFrame(frame, {
    enabled: inputs.data !== undefined,
    rule: BUSY_RETRY,
  })

  const layers = useMemo(
    () => calibrationLayers(listed, indexing.data, result, { labels }),
    [listed, indexing.data, result, labels]
  )

  const onPixelClick = (x: number, y: number) => {
    if (!frame || tool === null) return
    if (tool === "remove") {
      const peak = nearestPeak(listed, x, y, REMOVE_RADIUS_PX)
      if (!peak) return
      updateFrame(frame, (current) => {
        const measurements = { ...current.measurements }
        // eslint-disable-next-line @typescript-eslint/no-dynamic-delete -- a record keyed by peak
        delete measurements[peak.key]
        return peak.origin === "found"
          ? {
              ...current,
              removed: [...current.removed, peak.key],
              measurements,
            }
          : {
              ...current,
              added: current.added.filter((p) => peakKey(p) !== peak.key),
              measurements,
            }
      })
      return
    }
    fitCalibrationPeak({ frame, x, y })
      .then((answer) => {
        if (!answer.peak) {
          toastError(new Error("No peak near this pixel"), "Add peak")
          return
        }
        const { x: px, y: py } = answer.peak
        updateFrame(frame, (current) => ({
          ...current,
          added: [...current.added, { x: px, y: py }],
          removed: current.removed.filter(
            (key) => key !== peakKey(answer.peak ?? { x: px, y: py })
          ),
        }))
      })
      .catch((error: unknown) => toastError(error, "Add peak"))
  }

  if (setup.isPending) return <LoadingState label="Loading calibration" />
  if (setup.isError || !defaults) {
    return (
      <ErrorState
        error={setup.error}
        onRetry={() => void setup.refetch()}
        retrying={setup.isFetching}
      />
    )
  }

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-2xl font-semibold tracking-tight">Calibration</h1>
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_26rem]">
        <div className="flex min-w-0 flex-col gap-4">
          <FramePlot
            labelSize={labelSize}
            frame={image.data ?? null}
            layers={layers}
            status={plotStatus(
              frame,
              image,
              peaks.isFetching,
              indexing.isFetching
            )}
            onPixelClick={tool === null ? undefined : onPixelClick}
            error={image.error}
            onRetry={() => void image.refetch()}
          />
          <ReflectionsTable
            frame={frame}
            peaks={listed}
            indexing={indexing.data}
            result={result}
            reindexing={indexing.isPlaceholderData}
          />
        </div>
        <div className="flex flex-col gap-4">
          <InputsCard
            files={{ frame: files.frame, geometry, crystal: files.crystal }}
            folder={defaults.geometry_folder}
            info={inputs.data}
            error={inputs.error}
            loading={inputs.isFetching}
            sampleFromDetector0={sampleReciprocal !== null}
          />
          <PeaksCard
            frame={frame}
            peaks={peaks}
            listed={listed}
            draft={draft}
            settings={peakSettings}
            tool={tool}
            onToolChange={setTool}
            labels={labels}
            labelSize={labelSize}
          />
          <IndexCard
            frame={frame}
            indexing={indexing}
            draft={draft}
            settings={indexSettings}
            warningDeg={defaults.surface_warning_deg}
          />
          <RefineCard
            frame={frame}
            request={refineRequest}
            indexing={indexing.data}
            refine={refine}
            result={result}
            stale={refine.data !== undefined && !fresh}
            warningDeg={defaults.surface_warning_deg}
          />
          <SaveCard
            folder={defaults.geometry_folder}
            request={refineRequest}
            result={result}
          />
        </div>
      </div>
    </div>
  )
}

function plotStatus(
  frame: string,
  image: { isPending: boolean; isFetching: boolean; data?: Frame },
  peaksLoading: boolean,
  indexing: boolean
): string | null {
  if (!frame) return null
  if (image.isFetching && !image.data) return "Loading frame"
  if (peaksLoading) return "Finding peaks"
  if (indexing) return "Indexing"
  return null
}

/** The frame with its marks, the status over it, and the colorbar under it (one card). */
function FramePlot({
  labelSize,
  frame,
  layers,
  status,
  onPixelClick,
  error,
  onRetry,
}: {
  /** The hkl labels' size (CSS px). */
  labelSize: number
  frame: Frame | null
  layers: readonly OverlayLayer[]
  status: string | null
  onPixelClick: ((x: number, y: number) => void) | undefined
  error: unknown
  onRetry: () => void
}) {
  const [settings, setSettings] = useState<ColorbarSettings>(() =>
    colorbarSettings(FRAME_AUTO_RANGE)
  )
  const {
    distribution,
    error: distributionError,
    retry,
  } = useDistribution(frame?.data ?? null)
  const resolved = distribution ? resolveRange(settings, distribution) : null
  const lo = resolved?.[0]
  const hi = resolved?.[1]
  const range = useMemo(
    () => (lo !== undefined && hi !== undefined ? ([lo, hi] as const) : null),
    [lo, hi]
  )
  const { scale: scaleKind, gamma } = settings
  const scale = useMemo(() => ({ kind: scaleKind, gamma }), [scaleKind, gamma])
  return (
    <section
      aria-label="Detector image"
      className={cn(surfaceClass, "flex flex-col gap-2 p-3")}
    >
      <div
        className="relative h-[clamp(20rem,62vh,44rem)] overflow-hidden"
        aria-busy={status !== null || undefined}
      >
        {error ? (
          <ErrorState error={error} onRetry={onRetry} className="m-4" />
        ) : (
          <HeatmapView
            frame={frame}
            range={range}
            scale={scale}
            colormap="gray"
            overlays={layers}
            labelSize={labelSize}
            onPixelClick={onPixelClick}
            label="Calibration frame"
            className={onPixelClick ? "cursor-crosshair" : undefined}
          />
        )}
        {status && (
          <p
            role="status"
            className="absolute top-1 left-1/2 w-max max-w-[80%] -translate-x-1/2 rounded bg-background/80 px-2 py-0.5 text-xs text-muted-foreground"
          >
            {status}
          </p>
        )}
      </div>
      {frame && (
        <ColorBar
          distribution={distribution}
          error={distributionError}
          onRetry={retry}
          colormap="gray"
          value={settings}
          onChange={setSettings}
          defaultAuto={FRAME_AUTO_RANGE}
          title="Intensity"
        />
      )}
    </section>
  )
}
