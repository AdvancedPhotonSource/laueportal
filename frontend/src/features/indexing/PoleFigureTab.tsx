import { useCallback, useLayoutEffect, useMemo, useRef } from "react"

import {
  LegendColor,
  usePoleFigureProduct,
  type PoleFigureProduct,
} from "@/api"
import { ErrorState } from "@/components/ErrorState"
import { LoadingState } from "@/components/LoadingState"
import { surface } from "@/lib/styles"
import { cn } from "@/lib/utils"
import {
  PoleFigureView,
  useLinkedSelection,
  type ScatterColors,
  type ScatterSelection,
  type SelectionStore,
} from "@/viz"
import { MapLegend } from "./MapLegend"
import { PoleFigureControls } from "./PoleFigureControls"
import { SelectionStatsCard } from "./SelectionStatsCard"
import { poleReferenceOf } from "./color-map"
import {
  clickPole,
  poleFigureRequest,
  resetPole,
  setCenter,
  statsRequest,
  type ClickedPole,
  type PoleChange,
} from "./pole-figure"
import { useLastGood } from "./use-last-good"
import type { IndexingView } from "./viewer-search"
import { useIndexingViewerStore, useScopeParams } from "./viewer-store"

/**
 * The Pole Figure tab: the controls (with the colour centre and the reference), the pole figure
 * (PoleFigureView, lasso by default) and the selection's card. Clicking a pole makes it the HSV
 * centre and the misorientation reference and shows the map in Pole HSV; clicking that pattern again,
 * or "Reset color center", restores the map's mode. The "Set color center" tool snaps the centre to a
 * pole the same way, or places it at any point in the unit circle, keeping the reference. A box or
 * lasso writes the page's selection store, which the map shares.
 */
export function PoleFigureTab({
  indexingId,
  view,
  onViewChange,
  selectionStore,
}: {
  indexingId: number
  view: IndexingView
  onViewChange: (change: Partial<IndexingView>) => void
  /** The page's linked selection (shared with the map). */
  selectionStore: SelectionStore
}) {
  const pole = useIndexingViewerStore((state) => state.pole)
  const setPole = useIndexingViewerStore((state) => state.setPole)
  const reference = poleReferenceOf(pole, indexingId)
  const scope = useScopeParams()

  const [h, k, l] = view.hkl
  const params = useMemo(
    () => poleFigureRequest(pole, reference, [h, k, l], scope),
    [pole, reference, h, k, l, scope]
  )
  const query = usePoleFigureProduct(indexingId, params)
  const product = useLastGood(query.data)

  // The page's onViewChange and the colour mode are new each render: the callbacks read the latest
  // through a ref, so they stay stable while the product is (a 304 redraws nothing).
  const latest = useRef({ onViewChange, color: view.color, reference })
  useLayoutEffect(() => {
    latest.current = { onViewChange, color: view.color, reference }
  })
  const apply = useCallback(
    (change: PoleChange) => {
      setPole({ reference: change.reference })
      latest.current.onViewChange({ color: change.color })
    },
    [setPole]
  )
  const onPointClick = useCallback(
    (index: number) => {
      if (!product) return
      apply(
        clickPole(
          indexingId,
          latest.current.reference,
          poleAt(product, index),
          latest.current.color
        )
      )
    },
    [product, indexingId, apply]
  )
  const onCenterClick = useCallback(
    (index: number | null, point: [number, number]) => {
      if (!product) return
      apply(
        setCenter(
          indexingId,
          latest.current.reference,
          { pole: index === null ? null : poleAt(product, index), point },
          latest.current.color
        )
      )
    },
    [product, indexingId, apply]
  )

  const colorKey =
    pole.scheme === "uniform" ? null : (
      <MapLegend
        indexingId={indexingId}
        legend={
          pole.scheme === "ipf" ? LegendColor.cubic_ipf : LegendColor.pole_hsv
        }
        caption={
          pole.scheme === "ipf" ? "Cubic IPF (m–3m)" : "Pole position (HSV)"
        }
      />
    )

  return (
    <div className="grid gap-4 md:grid-cols-[16rem_minmax(0,1fr)]">
      <aside
        aria-label="Pole figure settings"
        className={cn(surface, "h-fit p-3")}
      >
        <PoleFigureControls
          hkl={view.hkl}
          onHklChange={(hkl) => {
            onViewChange({ hkl })
          }}
          reference={reference}
          onResetCenter={
            reference
              ? () => {
                  apply(resetPole(reference))
                }
              : null
          }
          colorKey={colorKey}
        />
      </aside>
      <div className="flex min-w-0 flex-col gap-3">
        {query.error && product && (
          <ErrorState
            error={query.error}
            stale
            onRetry={() => void query.refetch()}
            retrying={query.isFetching}
          />
        )}
        {product ? (
          <PolePlot
            product={product}
            selectionStore={selectionStore}
            updating={query.isFetching && query.data !== product}
            centerMark={reference?.center ?? null}
            onPointClick={onPointClick}
            onCenterClick={onCenterClick}
          />
        ) : query.error ? (
          <ErrorState
            error={query.error}
            onRetry={() => void query.refetch()}
            retrying={query.isFetching}
          />
        ) : (
          <LoadingState label="Loading the pole figure" />
        )}
        {product && (
          <Selection
            indexingId={indexingId}
            product={product}
            selectionStore={selectionStore}
          />
        )}
      </div>
    </div>
  )
}

/** A pole of the figure: its pattern and position. */
function poleAt(product: PoleFigureProduct, index: number): ClickedPole {
  return {
    step: product.step[index] ?? 0,
    patternIndex: product.patternIndex[index] ?? 0,
    center: [product.x[index] ?? 0, product.y[index] ?? 0],
  }
}

/** The pole figure and its hover readout. */
function PolePlot({
  product,
  selectionStore,
  updating,
  centerMark,
  onPointClick,
  onCenterClick,
}: {
  product: PoleFigureProduct
  selectionStore: SelectionStore
  updating: boolean
  /** The set centre, marked in the schemes that do not colour by it. */
  centerMark: readonly [number, number] | null
  onPointClick: (index: number) => void
  onCenterClick: (index: number | null, point: [number, number]) => void
}) {
  const markerSize = useIndexingViewerStore((state) => state.pole.markerSize)
  const selection = useLinkedSelection(selectionStore)
  const colors = useMemo<ScatterColors>(
    () => ({ kind: "rgb", r: product.r, g: product.g, b: product.b }),
    [product]
  )
  const onSelect = useCallback(
    (selected: ScatterSelection) => {
      selectionStore.select(selected.pairs)
    },
    [selectionStore]
  )
  const renderTooltip = (index: number) => (
    <PoleReadout product={product} index={index} />
  )
  const hsv = product.meta.scheme === "hsv"

  // The plot area is grey: the poles at the HSV centre are white.
  return (
    <div
      className={cn(
        surface,
        "relative h-[clamp(20rem,62vh,44rem)] overflow-hidden bg-muted"
      )}
      aria-busy={updating || undefined}
    >
      <PoleFigureView
        data={{
          x: product.x,
          y: product.y,
          colors,
          step: product.step,
          patternIndex: product.patternIndex,
        }}
        center={hsv ? product.meta.center : centerMark}
        colorRadius={hsv ? product.meta.color_radius : null}
        markerSize={markerSize}
        selection={selection}
        onSelect={onSelect}
        onPointClick={onPointClick}
        onCenterClick={onCenterClick}
        renderTooltip={renderTooltip}
        label="Pole figure"
      />
      {updating && (
        <p
          role="status"
          className="absolute bottom-2 left-2 rounded bg-background/80 px-2 py-0.5 text-xs text-muted-foreground"
        >
          Updating…
        </p>
      )}
    </div>
  )
}

/** The hovered pole: its step, frame and pattern. */
function PoleReadout({
  product,
  index,
}: {
  product: PoleFigureProduct
  index: number
}) {
  const rows: [string, string][] = [
    ["Step", String(product.step[index])],
    ["Frame", String(product.frameId[index])],
    ["Pattern", String(product.patternIndex[index])],
  ]
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

/** The selection's card (from the pole figure or the map: the page's store), or "No selection". */
function Selection({
  indexingId,
  product,
  selectionStore,
}: {
  indexingId: number
  product: PoleFigureProduct
  selectionStore: SelectionStore
}) {
  const selection = useLinkedSelection(selectionStore)
  const stats = useMemo(
    () => statsRequest(selection, product),
    [selection, product]
  )
  if (stats.kind === "empty") {
    return <p className="text-sm text-muted-foreground">No selection</p>
  }
  return (
    <SelectionStatsCard
      indexingId={indexingId}
      poles={stats.poles}
      patterns={stats.patterns}
      request={stats.request}
    />
  )
}
