import { InfoIcon } from "lucide-react"
import {
  useCallback,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react"

import {
  useMapProduct,
  type IndexingResultsSummary,
  type MapProduct,
} from "@/api"
import { ErrorState } from "@/components/ErrorState"
import { LoadingState } from "@/components/LoadingState"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { formatSignificant } from "@/lib/format"
import { surface } from "@/lib/styles"
import { cn } from "@/lib/utils"
import {
  ColorBar,
  colormapRgb,
  MAP_AUTO_RANGE,
  PointCloud3DView,
  resolveRange,
  scalarLookup,
  ScatterView,
  useDistribution,
  useLinkedSelection,
  type ScatterColors,
  type ScatterSelection,
  type SelectionStore,
} from "@/viz"
import { ColorMapControls, ScalarPaletteControls } from "./ColorMapControls"
import { MapLegend } from "./MapLegend"
import { PointDetailsCard } from "./PointDetailsCard"
import { legendCaption, legendOf, mapRequest, resolveAxes } from "./color-map"
import type { IndexingView } from "./viewer-search"
import { isScalarMode, type ScalarMode } from "./viewer-slices"
import { useLastGood } from "./use-last-good"
import { useIndexingViewerStore, useScopeParams } from "./viewer-store"

/**
 * The Color Map tab: the controls, the map in 2D (ScatterView) or 3D (PointCloud3DView), the legend
 * or a scalar mode's colorbar, the hover readout and the clicked point's card. The map's request
 * carries the scope (useScopeParams) and only what the colour mode reads; a scalar mode's palette,
 * reverse and range recolour in the browser and never refetch it.
 */
export function ColorMapTab({
  indexingId,
  summary,
  view,
  onViewChange,
  selectionStore,
}: {
  indexingId: number
  summary: IndexingResultsSummary
  view: IndexingView
  onViewChange: (change: Partial<IndexingView>) => void
  /** The page's linked selection (shared with the pole figure). */
  selectionStore: SelectionStore
}) {
  const map = useIndexingViewerStore((state) => state.map)
  const pole = useIndexingViewerStore((state) => state.pole)
  const scope = useScopeParams()

  const storedAxes = view.mode === "3d" ? map.axes3d : map.axes2d
  const axes = useMemo(
    () => resolveAxes(storedAxes, summary.axes, view.mode),
    [summary, storedAxes, view.mode]
  )
  const modeChoice = summary.color_modes.find(
    (mode) => mode.value === view.color
  )
  const unavailable =
    modeChoice && !modeChoice.available
      ? (modeChoice.reason ??
        "This colour mode is not available for these results.")
      : null
  const request = useMemo(
    () =>
      axes
        ? mapRequest({
            indexingId,
            color: view.color,
            axes,
            map,
            pole,
            hkl: view.hkl,
            scope,
          })
        : null,
    [indexingId, view.color, view.hkl, axes, map, pole, scope]
  )
  const query = useMapProduct(indexingId, request?.params ?? {}, {
    enabled: Boolean(request?.params) && !unavailable,
  })
  const product = useLastGood(query.data)
  // The 3D view needs a product with z; while it loads, the 2D one is not shown in its place.
  const shown =
    product && (product.z !== null) === (view.mode === "3d")
      ? product
      : undefined

  // A product is stale when the one asked for failed, cannot be asked for, or is still coming.
  // A mode that cannot be drawn leaves the last map (another mode's) on screen: the hint says so.
  const previous = shown ? " Showing the previous map." : ""
  const notice: ReactNode = unavailable ? (
    <Notice>{unavailable + previous}</Notice>
  ) : request?.blocked ? (
    <Notice>{request.blocked + previous}</Notice>
  ) : query.error && shown ? (
    <ErrorState
      error={query.error}
      stale
      onRetry={() => void query.refetch()}
      retrying={query.isFetching}
    />
  ) : null

  const [clickedPattern, setClickedPattern] = useState<number | null>(null)
  // Stable while the product is (a 304 keeps it), so a revalidation redraws nothing; the page's
  // onViewChange is a new function each render, so the latest one is read through a ref.
  const viewChange = useRef(onViewChange)
  useLayoutEffect(() => {
    viewChange.current = onViewChange
  })
  const onPointClick = useCallback(
    (index: number) => {
      if (!shown) return
      setClickedPattern(shown.patternIndex[index] ?? null)
      viewChange.current({ point: shown.step[index] })
    },
    [shown]
  )

  const scalarColor =
    shown && isScalarMode(shown.meta.color) ? shown.meta.color : null
  const legendColor = shown ? legendOf(shown.meta.color) : legendOf(view.color)
  const legendSurface = legendColor === "pole_hsv" ? pole.surface : map.surface

  return (
    <div className="grid gap-4 md:grid-cols-[16rem_minmax(0,1fr)]">
      <aside
        aria-label="Color map settings"
        className={cn(surface, "h-fit p-3")}
      >
        <ColorMapControls
          indexingId={indexingId}
          view={view}
          onViewChange={onViewChange}
          colorModes={summary.color_modes}
          axisChoices={summary.axes}
          axes={axes}
          legend={
            isScalarMode(view.color) ? (
              <ScalarPaletteControls color={view.color} />
            ) : legendColor ? (
              <MapLegend
                indexingId={indexingId}
                legend={legendColor}
                caption={legendCaption(
                  shown?.meta.color ?? view.color,
                  legendSurface
                )}
              />
            ) : null
          }
        />
      </aside>
      <div className="flex min-w-0 flex-col gap-3">
        {notice}
        {shown ? (
          <MapPlot
            product={shown}
            scalarColor={scalarColor}
            mode={view.mode}
            selectionStore={selectionStore}
            updating={query.isFetching && query.data !== shown}
            onPointClick={onPointClick}
          />
        ) : query.error ? (
          <ErrorState
            error={query.error}
            onRetry={() => void query.refetch()}
            retrying={query.isFetching}
          />
        ) : notice ? null : !axes ? (
          <Notice>
            These results have too few axes for a {view.mode.toUpperCase()} map.
          </Notice>
        ) : (
          <LoadingState label="Loading the colour map" />
        )}
        {view.point !== undefined && axes && (
          <PointDetailsCard
            key={view.point}
            indexingId={indexingId}
            step={view.point}
            patternIndex={clickedPattern}
            axes={axes}
            scope={scope}
            onClose={() => {
              setClickedPattern(null)
              onViewChange({ point: undefined })
            }}
          />
        )}
      </div>
    </div>
  )
}

function Notice({ children }: { children: ReactNode }) {
  return (
    <Alert>
      <InfoIcon aria-hidden />
      <AlertDescription>{children}</AlertDescription>
    </Alert>
  )
}

/** The map (2D or 3D), its scalar colorbar and the hover readout. */
function MapPlot({
  product,
  scalarColor,
  mode,
  selectionStore,
  updating,
  onPointClick,
}: {
  product: MapProduct
  scalarColor: ScalarMode | null
  mode: IndexingView["mode"]
  selectionStore: SelectionStore
  updating: boolean
  onPointClick: (index: number) => void
}) {
  const markerSize = useIndexingViewerStore((state) => state.map.markerSize)
  const nonIndexed = useIndexingViewerStore((state) => state.map.nonIndexed)
  const scalar = useIndexingViewerStore((state) =>
    scalarColor ? state.map.scalar[scalarColor] : null
  )
  const setScalar = useIndexingViewerStore((state) => state.setScalar)
  const selection = useLinkedSelection(selectionStore)

  const values = product.colors.kind === "scalar" ? product.colors.values : null
  const { distribution, error, retry } = useDistribution(values)
  const range =
    (scalar && distribution
      ? resolveRange(scalar.colorbar, distribution)
      : null) ??
    product.meta.range ??
    null

  // The colour table changes only with these values: a drag recolours, nothing is fetched.
  const palette = scalar?.palette
  const reverse = scalar?.reverse ?? false
  const lo = range?.[0]
  const hi = range?.[1]
  const scaleKind = scalar?.colorbar.scale
  const gamma = scalar?.colorbar.gamma
  const lookup = useMemo(
    () =>
      palette && lo !== undefined && hi !== undefined && scaleKind && gamma
        ? scalarLookup(colormapRgb(palette, reverse), lo, hi, {
            kind: scaleKind,
            gamma,
          })
        : null,
    [palette, reverse, lo, hi, scaleKind, gamma]
  )

  const colors = useMemo<ScatterColors | null>(() => {
    if (product.colors.kind === "rgb") {
      const { r, g, b } = product.colors
      return { kind: "rgb", r, g, b }
    }
    return lookup
      ? { kind: "lookup", values: product.colors.values, lookup }
      : null
  }, [product, lookup])
  // The orientation modes draw the records without an orientation in the not-indexed style; the
  // scalar modes the records without a value (NaN).
  const indexed = useMemo(
    () =>
      product.colors.kind === "scalar"
        ? Uint8Array.from(product.colors.values, (v) =>
            Number.isFinite(v) ? 1 : 0
          )
        : product.indexed,
    [product]
  )

  // The 3D view adds the point's x, y and z itself.
  const renderTooltip = (index: number) => (
    <PointReadout product={product} index={index} coordinates={mode !== "3d"} />
  )
  const [xLabel = "", yLabel = "", zLabel = ""] = product.meta.axis_labels
  // Memoized with the callbacks, so a render that changes nothing (a 304) does not redraw the view.
  const axes2d = useMemo(() => ({ x: xLabel, y: yLabel }), [xLabel, yLabel])
  const axes3d = useMemo(
    () => ({ x: xLabel, y: yLabel, z: zLabel }),
    [xLabel, yLabel, zLabel]
  )
  const onSelect = useCallback(
    (selected: ScatterSelection) => {
      selectionStore.select(selected.pairs)
    },
    [selectionStore]
  )

  return (
    <div className="flex flex-col gap-2">
      <div
        className={cn(
          surface,
          "relative h-[clamp(20rem,62vh,44rem)] overflow-hidden"
        )}
        aria-busy={updating || undefined}
      >
        {colors &&
          (mode === "3d" && product.z ? (
            <PointCloud3DView
              data={{
                x: product.x,
                y: product.y,
                z: product.z,
                colors,
                indexed,
                step: product.step,
                patternIndex: product.patternIndex,
              }}
              axes={axes3d}
              markerSize={markerSize}
              nonIndexedColor={nonIndexed}
              selection={selection}
              onPointClick={onPointClick}
              renderTooltip={renderTooltip}
              label="Colour map (3D)"
            />
          ) : (
            <ScatterView
              key={product.meta.axes.join(",")}
              data={{
                x: product.x,
                y: product.y,
                colors,
                indexed,
                step: product.step,
                patternIndex: product.patternIndex,
              }}
              axes={axes2d}
              equalAspect={product.meta.spatial_axes}
              markerSize={markerSize}
              markerShape="square"
              nonIndexedColor={nonIndexed}
              selection={selection}
              onSelect={onSelect}
              onPointClick={onPointClick}
              renderTooltip={renderTooltip}
              label="Colour map"
            />
          ))}
        {updating && (
          <p
            role="status"
            className="absolute bottom-2 left-2 rounded bg-background/80 px-2 py-0.5 text-xs text-muted-foreground"
          >
            Updating…
          </p>
        )}
      </div>
      {scalar && scalarColor && (
        <section aria-label="Colour scale">
          <ColorBar
            distribution={distribution}
            error={error}
            onRetry={retry}
            colormap={scalar.palette}
            reverse={scalar.reverse}
            value={scalar.colorbar}
            onChange={(colorbar) => {
              setScalar(scalarColor, { colorbar })
            }}
            defaultAuto={MAP_AUTO_RANGE}
            title={product.meta.color_label}
          />
        </section>
      )}
    </div>
  )
}

/** The hover readout: the record's step, frame and pattern, and its colour value (from the product). */
function PointReadout({
  product,
  index,
  coordinates,
}: {
  product: MapProduct
  index: number
  /** Add the point's x and y. */
  coordinates: boolean
}) {
  const pattern = product.patternIndex[index] ?? -1
  const [xLabel = "x", yLabel = "y"] = product.meta.axis_labels
  const rows: [string, string][] = [
    ["Step", String(product.step[index])],
    ["Frame", String(product.frameId[index])],
    ["Pattern", pattern < 0 ? "none" : String(pattern)],
  ]
  if (product.colors.kind === "scalar") {
    rows.push([
      product.meta.color_label,
      formatSignificant(product.colors.values[index] ?? NaN),
    ])
  } else if (!product.indexed[index]) {
    rows.push(["", "not indexed"])
  }
  if (coordinates) {
    rows.push([xLabel, formatSignificant(product.x[index] ?? NaN)])
    rows.push([yLabel, formatSignificant(product.y[index] ?? NaN)])
  }
  return (
    <dl className="grid grid-cols-[auto_auto] gap-x-2 font-mono tabular-nums">
      {rows.map(([name, value]) => (
        <div key={name} className="contents">
          <dt className="text-muted-foreground">{name}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  )
}
