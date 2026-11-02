import { InfoIcon, TriangleAlertIcon } from "lucide-react"
import { useMemo, useState, type ReactNode } from "react"

import {
  useDetectorFrame,
  useGetIndexingDetectorOverlays,
  type DetectorOverlays,
  type DetectorPattern,
  type IndexingResultsSummary,
} from "@/api"
import { EmptyState } from "@/components/EmptyState"
import { ErrorState } from "@/components/ErrorState"
import { LoadingState } from "@/components/LoadingState"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { BUSY_RETRY, busyStatus } from "@/lib/busy"
import { formatSignificant } from "@/lib/format"
import { surface } from "@/lib/styles"
import { cn } from "@/lib/utils"
import {
  ColorBar,
  FRAME_AUTO_RANGE,
  HeatmapView,
  overlayLayers,
  resolveRange,
  useDistribution,
  type Frame,
} from "@/viz"
import { DetectorControls } from "./DetectorControls"
import {
  checkedPatterns,
  detectorStep,
  isImageNotFound,
  overlayRequests,
} from "./detector"
import { useLastGood } from "./use-last-good"
import type { IndexingView } from "./viewer-search"
import { useIndexingViewerStore, useScopeParams } from "./viewer-store"

const NONE: ReadonlySet<number> = new Set()
/** The simulation's parameters while it is off (its query is disabled, so they are never sent). */
const SIMULATION_OFF = { simulate: true }

/** A step's frame and overlays, shown together once both have settled (loaded or failed). */
interface StepView {
  step: number
  frame: Frame | null
  overlays: DetectorOverlays | null
}

/**
 * The Detector View tab: a step's frame (`HeatmapView`) with the detected peaks, the indexed peaks
 * with their hkl, and on request the simulated reflections a pattern did not index; the colorbar;
 * the step's pattern checklist and summary card. The step is the URL's `step` (pushed to history),
 * limited to the scope's steps; its default is the summary's, kept while it is there when the
 * results are read again (a live run's, `detectorStep`).
 */
export function DetectorTab({
  indexingId,
  summary,
  view,
  onViewChange,
}: {
  indexingId: number
  summary: IndexingResultsSummary
  view: IndexingView
  onViewChange: (change: Partial<IndexingView>) => void
}) {
  // The default step shown, for this scope: kept while it is there (`detectorStep`).
  const scope = JSON.stringify(useScopeParams())
  const [kept, setKept] = useState<{ scope: string; step: number }>()
  const keptStep = kept?.scope === scope ? kept.step : undefined
  const step = detectorStep(view.step, summary, keptStep)
  const showsDefault =
    view.step === undefined || !summary.steps.includes(view.step)
  if (showsDefault && step !== null && step !== keptStep) {
    setKept({ scope, step })
  }
  if (step === null) {
    return <EmptyState title="No steps match the scope" />
  }
  return (
    <DetectorStep
      indexingId={indexingId}
      steps={summary.steps}
      step={step}
      onStepChange={(next) => {
        // The summary's default is not written to the URL: picked while another step is kept, it is kept.
        if (next === summary.default_step && keptStep !== undefined) {
          setKept({ scope, step: next })
        }
        onViewChange({ step: next })
      }}
    />
  )
}

function DetectorStep({
  indexingId,
  steps,
  step,
  onStepChange,
}: {
  indexingId: number
  steps: readonly number[]
  step: number
  onStepChange: (step: number) => void
}) {
  const settings = useIndexingViewerStore((state) => state.detector)
  const setDetector = useIndexingViewerStore((state) => state.setDetector)
  const scope = useScopeParams()
  const requests = useMemo(
    () => overlayRequests(scope, settings.simulate),
    [scope, settings.simulate]
  )

  const plain = useGetIndexingDetectorOverlays(
    indexingId,
    step,
    requests.plain,
    { query: { retry: false } }
  )
  // The simulation takes about 4 s a pattern; the fast layers show meanwhile (`plain`).
  const simulated = useGetIndexingDetectorOverlays(
    indexingId,
    step,
    requests.simulated ?? SIMULATION_OFF,
    { query: { enabled: requests.simulated !== null, retry: false } }
  )
  const frameQuery = useDetectorFrame(indexingId, step, {
    enabled: settings.showImage,
    rule: BUSY_RETRY,
  })
  // A busy image (a 503 while many wait for a turn) is asked again after its Retry-After; once it
  // gives up, its notice offers Retry.
  const frameBusy = settings.showImage ? busyStatus(frameQuery) : null
  const frameWait = frameBusy?.retry === true ? null : (frameBusy?.text ?? null)

  // The step's frame and overlays appear together: the last pair stays while the next loads. A busy
  // image does not hold its step back: the overlays show, with the wait over the plot.
  const frameSettled =
    !settings.showImage || !frameQuery.isPending || frameWait !== null
  const current = useMemo<StepView | undefined>(
    () =>
      frameSettled && !plain.isPending
        ? {
            step,
            frame: frameQuery.data ?? null,
            overlays: plain.data ?? null,
          }
        : undefined,
    [step, frameSettled, frameQuery.data, plain.isPending, plain.data]
  )
  const shown = useLastGood(current)
  const loadingStep = shown && shown.step !== step ? step : null

  // Every pattern of the shown step is checked until unchecked: whenever another step is shown
  // (a move, back or forward, a step seen before), all its patterns are checked again.
  const [checklist, setChecklist] = useState<{
    step: number
    unchecked: ReadonlySet<number>
  }>({ step, unchecked: NONE })
  if (shown && checklist.step !== shown.step) {
    setChecklist({ step: shown.step, unchecked: NONE })
  }
  const unchecked =
    shown && checklist.step === shown.step ? checklist.unchecked : NONE

  const simulatedData =
    settings.simulate && shown?.step === step ? simulated.data : undefined
  const overlays = simulatedData ?? shown?.overlays ?? null
  const simulating =
    settings.simulate && simulated.isFetching && !simulated.data

  const [draftOpacity, setDraftOpacity] = useState<number | null>(null)
  const opacity = draftOpacity ?? settings.opacity

  const notices: ReactNode[] = []
  if (settings.showImage && frameQuery.error && !loadingStep) {
    notices.push(
      isImageNotFound(frameQuery.error) ? (
        <Notice key="image" title="The detector image is not on this host">
          {frameQuery.error.problem?.detail ?? "The image file was not found."}
        </Notice>
      ) : (
        <ErrorState
          key="image"
          error={frameQuery.error}
          onRetry={() => void frameQuery.refetch()}
          retrying={frameQuery.isFetching}
        />
      )
    )
  }
  if (plain.error && !loadingStep) {
    notices.push(
      <ErrorState
        key="overlays"
        error={plain.error}
        onRetry={() => void plain.refetch()}
        retrying={plain.isFetching}
      />
    )
  }
  if (settings.simulate && simulated.error) {
    notices.push(
      <ErrorState
        key="simulation"
        error={simulated.error}
        onRetry={() => void simulated.refetch()}
        retrying={simulated.isFetching}
      />
    )
  }
  if (simulatedData?.simulation_error) {
    notices.push(
      <Notice key="simulation-error" tone="warning">
        Simulated missing reflections unavailable:{" "}
        {simulatedData.simulation_error}
      </Notice>
    )
  }

  return (
    <div className="grid gap-4 md:grid-cols-[16rem_minmax(0,1fr)]">
      <aside
        aria-label="Detector view settings"
        className={cn(surface, "h-fit p-3")}
      >
        <DetectorControls
          steps={steps}
          step={step}
          onStepChange={onStepChange}
          patterns={shown?.overlays?.patterns ?? null}
          unchecked={unchecked}
          onUncheckedChange={(next) => {
            if (shown) setChecklist({ step: shown.step, unchecked: next })
          }}
          opacity={opacity}
          onOpacityChange={(value, done) => {
            if (done) {
              setDraftOpacity(null)
              if (value !== settings.opacity) setDetector({ opacity: value })
            } else {
              setDraftOpacity(value)
            }
          }}
        />
      </aside>
      <div className="flex min-w-0 flex-col gap-3">
        {notices}
        {shown ? (
          <DetectorPlot
            frame={shown.frame}
            overlays={overlays}
            unchecked={unchecked}
            opacity={opacity}
            status={
              loadingStep !== null
                ? `Loading step ${String(loadingStep)}…`
                : (frameWait ??
                  (simulating
                    ? simulationStatus(overlays?.patterns.length ?? 0)
                    : null))
            }
          />
        ) : (
          <LoadingState label="Loading the detector view" />
        )}
        {overlays && <SummaryCard overlays={overlays} unchecked={unchecked} />}
      </div>
    </div>
  )
}

function simulationStatus(patterns: number): string {
  const count = patterns === 1 ? "1 pattern" : `${String(patterns)} patterns`
  return `Simulating the missing reflections of ${count} (about 4 s a pattern)…`
}

function Notice({
  title,
  tone = "info",
  children,
}: {
  title?: string
  tone?: "info" | "warning"
  children: ReactNode
}) {
  const Icon = tone === "warning" ? TriangleAlertIcon : InfoIcon
  return (
    <Alert>
      <Icon aria-hidden />
      {title && <AlertTitle>{title}</AlertTitle>}
      <AlertDescription className="[overflow-wrap:anywhere]">
        {children}
      </AlertDescription>
    </Alert>
  )
}

/** The frame with its overlays, and the colorbar under it. */
function DetectorPlot({
  frame,
  overlays,
  unchecked,
  opacity,
  status,
}: {
  frame: Frame | null
  overlays: DetectorOverlays | null
  unchecked: ReadonlySet<number>
  opacity: number
  /** What is loading, over the plot. */
  status: string | null
}) {
  const settings = useIndexingViewerStore((state) => state.detector)
  const setDetector = useIndexingViewerStore((state) => state.setDetector)

  const { distribution, error, retry } = useDistribution(frame?.data ?? null)
  const resolved = distribution
    ? resolveRange(settings.colorbar, distribution)
    : null
  // Memoized on the values, so a render that changes nothing (a 304) redraws nothing.
  const lo = resolved?.[0]
  const hi = resolved?.[1]
  const range = useMemo(
    () => (lo !== undefined && hi !== undefined ? ([lo, hi] as const) : null),
    [lo, hi]
  )
  const { scale: scaleKind, gamma } = settings.colorbar
  const scale = useMemo(() => ({ kind: scaleKind, gamma }), [scaleKind, gamma])

  const width = overlays?.width
  const height = overlays?.height
  const extent = useMemo(
    () =>
      width !== undefined && height !== undefined ? { width, height } : null,
    [width, height]
  )
  const { showDetected, showIndexed, simulate, showLabels } = settings
  const layers = useMemo(
    () =>
      overlays
        ? overlayLayers(overlays, {
            detected: showDetected,
            indexed: showIndexed,
            simulated: simulate,
            labels: showLabels,
            patterns: checkedPatterns(overlays.patterns, unchecked),
          })
        : undefined,
    [overlays, showDetected, showIndexed, simulate, showLabels, unchecked]
  )

  return (
    <div className="flex flex-col gap-2">
      <div
        className={cn(
          surface,
          "relative h-[clamp(20rem,62vh,44rem)] overflow-hidden"
        )}
        aria-busy={status !== null || undefined}
      >
        <HeatmapView
          frame={frame}
          extent={extent}
          colormap={settings.colormap}
          range={range}
          scale={scale}
          opacity={opacity}
          showImage={settings.showImage}
          overlays={layers}
          markerSize={settings.markerSize}
          labelSize={settings.labelSize}
          label="Detector image"
        />
        {status && (
          <p
            role="status"
            className="absolute top-1 left-1/2 w-max max-w-[80%] -translate-x-1/2 rounded bg-background/80 px-2 py-0.5 text-xs text-muted-foreground"
          >
            {status}
          </p>
        )}
      </div>
      {frame && settings.showImage && (
        <section aria-label="Colour scale">
          <ColorBar
            distribution={distribution}
            error={error}
            onRetry={retry}
            colormap={settings.colormap}
            value={settings.colorbar}
            onChange={(colorbar) => {
              setDetector({ colorbar })
            }}
            defaultAuto={FRAME_AUTO_RANGE}
            title="Intensity"
          />
        </section>
      )}
    </div>
  )
}

const percent = (fraction: number) => `${String(Math.round(fraction * 100))}%`
const significant = (value: number | null) =>
  value === null ? "—" : formatSignificant(value)

/** The card under the detector view: the step, its frame and position, the counts, each pattern. */
function SummaryCard({
  overlays,
  unchecked,
}: {
  overlays: DetectorOverlays
  unchecked: ReadonlySet<number>
}) {
  const { summary } = overlays
  const [x = null, y = null, z = null] = summary.sample_position
  const patterns = overlays.patterns.filter(
    (pattern) => !unchecked.has(pattern.pattern_index)
  )
  return (
    <section aria-label="Step summary" className={cn(surface, "p-3 text-sm")}>
      <p>
        <strong>Step #{summary.step}</strong>{" "}
        <span className="text-muted-foreground">
          (frame {summary.frame_id})
        </span>{" "}
        · Motor position ({significant(x)}, {significant(y)}, {significant(z)})
        µm
      </p>
      <p>
        Detector {summary.detector_id ?? "?"} · Detected {summary.n_detected} ·
        Indexed peaks {summary.n_indexed} ({percent(summary.indexed_fraction)})
      </p>
      <p className="text-xs break-all text-muted-foreground">
        {summary.image.available
          ? `Image: ${summary.image.path ?? "?"}${summary.image.dataset ? ` [${summary.image.dataset}]` : ""}`
          : `Image: not on this host (${summary.input_image ?? "no inputImage recorded"})`}
      </p>
      {patterns.length > 0 && (
        <ul className="mt-1 list-disc pl-5 text-xs">
          {patterns.map((pattern) => (
            <PatternRow key={pattern.pattern_index} pattern={pattern} />
          ))}
        </ul>
      )}
    </section>
  )
}

function PatternRow({ pattern }: { pattern: DetectorPattern }) {
  const quality =
    pattern.rms_error_deg !== null
      ? `, RMS ${formatSignificant(pattern.rms_error_deg)}°, goodness ${significant(pattern.goodness)}`
      : ""
  const simulated = pattern.simulated
    ? `, ${String(pattern.simulated.length)} simulated missing`
    : ""
  return (
    <li>
      Pattern {pattern.pattern_index}: {pattern.n_indexed} indexed peaks,{" "}
      {pattern.peaks.length} predicted positions{quality}
      {simulated}
      {!pattern.in_scope && (
        <span className="text-muted-foreground"> (outside the scope)</span>
      )}
    </li>
  )
}
