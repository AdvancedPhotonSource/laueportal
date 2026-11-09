import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react"
import { useMemo, useState } from "react"

import { ApiError, useGetIndexingPoint, type PointDetails } from "@/api"
import { DefinitionList, type Definition } from "@/components/DefinitionList"
import { ErrorState } from "@/components/ErrorState"
import { LoadingState } from "@/components/LoadingState"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Toggle } from "@/components/ui/toggle"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { surface } from "@/lib/styles"
import { cn } from "@/lib/utils"
import { DEFAULT_CAMERA, UnitCellView, type Camera } from "@/viz"
import {
  direction,
  orientedPatterns,
  readUnitCell,
  shownPattern,
  triple,
  type UnitCellReading,
} from "./unit-cell"
import { useLastGood } from "./use-last-good"
import { useIndexingViewerStore, type ScopeParams } from "./viewer-store"

interface CellViewProps {
  showBeam: boolean
  camera: Camera
  onCameraChange: (camera: Camera) => void
}

const PRESSED =
  "aria-pressed:bg-primary aria-pressed:text-primary-foreground aria-pressed:hover:bg-primary aria-pressed:hover:text-primary-foreground"

/**
 * The Color Map tab's crystal-orientation card, beside the map: the clicked point's unit cell in
 * beamline coordinates (`UnitCellView`) and its lattice parameters, or one line before a click. It
 * reads the point's details (`GET …/points/{step}`, the request of the point's card, so one fetch
 * serves both) and keeps the previous cell while the next loads. Collapsed, it is a strip that
 * opens it again; the choice is stored (`map.cellCollapsed`).
 */
export function UnitCellCard({
  indexingId,
  step,
  patternIndex,
  axes,
  scope,
}: {
  indexingId: number
  /** The clicked step; undefined before a click. */
  step: number | undefined
  /** The clicked record's pattern, when known. */
  patternIndex: number | null
  axes: readonly string[]
  scope: ScopeParams
}) {
  const collapsed = useIndexingViewerStore((state) => state.map.cellCollapsed)
  const showBeam = useIndexingViewerStore((state) => state.map.cellBeam)
  const setMap = useIndexingViewerStore((state) => state.setMap)
  // Held here, so it stays across points, a step without a cell and a failed request.
  const [camera, setCamera] = useState<Camera>(DEFAULT_CAMERA)

  if (collapsed) {
    return (
      <button
        type="button"
        aria-label="Show the crystal orientation"
        title="Show the crystal orientation"
        className={cn(
          surface,
          "flex w-full items-center gap-2 px-3 py-2 text-sm font-medium hover:bg-muted xl:h-56 xl:w-10 xl:flex-col xl:px-0 xl:py-3"
        )}
        onClick={() => {
          setMap({ cellCollapsed: false })
        }}
      >
        <ChevronLeftIcon aria-hidden className="size-4 shrink-0" />
        <span className="xl:[writing-mode:vertical-rl]">
          Crystal orientation
        </span>
      </button>
    )
  }

  return (
    <Card size="sm" role="region" aria-label="Crystal orientation">
      <CardHeader className="flex items-center gap-1.5 py-1.5">
        <CardTitle>
          <h3>Crystal orientation</h3>
        </CardTitle>
        <Toggle
          variant="outline"
          size="sm"
          className={cn("ml-auto h-6 px-2 text-xs", PRESSED)}
          pressed={showBeam}
          onPressedChange={(pressed) => {
            setMap({ cellBeam: pressed })
          }}
          title="The beam's direction from the a, b, c origin"
        >
          Beam
        </Toggle>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Collapse the crystal orientation"
          title="Collapse"
          onClick={() => {
            setMap({ cellCollapsed: true })
          }}
        >
          <ChevronRightIcon aria-hidden />
        </Button>
      </CardHeader>
      <CardContent>
        {step === undefined ? (
          <p className="text-muted-foreground">
            Click a point to show its unit cell.
          </p>
        ) : (
          <PointCell
            indexingId={indexingId}
            step={step}
            patternIndex={patternIndex}
            axes={axes}
            scope={scope}
            view={{ showBeam, camera, onCameraChange: setCamera }}
          />
        )}
      </CardContent>
    </Card>
  )
}

function PointCell({
  indexingId,
  step,
  patternIndex,
  axes,
  scope,
  view,
}: {
  indexingId: number
  step: number
  patternIndex: number | null
  axes: readonly string[]
  scope: ScopeParams
  view: CellViewProps
}) {
  const query = useGetIndexingPoint(indexingId, step, {
    axes: axes.join(","),
    ...scope,
  })
  // The previous point stays while the next loads, so the cell does not blink.
  const point = useLastGood(query.data)
  // A pattern chosen in the card holds only for its step.
  const [chosen, setChosen] = useState<{
    step: number
    pattern: number
  } | null>(null)
  const wanted = chosen?.step === step ? chosen.pattern : patternIndex
  // While the next point loads, the previous one keeps the pattern it showed.
  const fresh = point?.step === step
  const freshShown = point && fresh ? shownPattern(point, wanted) : null
  const lastShown = useLastGood(
    fresh ? `${String(step)}:${String(freshShown)}` : undefined
  )

  if (query.error && !query.isFetching) {
    return query.error instanceof ApiError && query.error.status === 404 ? (
      <p className="text-muted-foreground">
        Step {step} is not in these results.
      </p>
    ) : (
      <ErrorState
        error={query.error}
        onRetry={() => void query.refetch()}
        refreshing={query.isFetching}
      />
    )
  }
  if (!point) return <LoadingState label="Loading the orientation" />
  const [lastStep, lastPattern] = (lastShown ?? "").split(":")
  const shown = fresh
    ? freshShown
    : lastStep === String(point.step) && lastPattern !== "null"
      ? Number(lastPattern)
      : shownPattern(point, null)
  if (shown === null) {
    return (
      <p className="text-muted-foreground">Step {point.step}: not indexed.</p>
    )
  }
  return (
    <CellBody
      point={point}
      patternIndex={shown}
      view={view}
      onPatternChange={(pattern) => {
        setChosen({ step: point.step, pattern })
      }}
    />
  )
}

function CellBody({
  point,
  patternIndex,
  view,
  onPatternChange,
}: {
  point: PointDetails
  patternIndex: number
  view: CellViewProps
  onPatternChange: (pattern: number) => void
}) {
  const reading = useMemo(
    () => readUnitCell(point, patternIndex),
    [point, patternIndex]
  )
  const oriented = orientedPatterns(point)
  if (!reading) return null
  return (
    <div className="flex flex-col gap-2">
      {oriented.length > 1 && (
        <div className="flex items-center gap-2 text-xs">
          <span className="text-muted-foreground">Pattern</span>
          <ToggleGroup
            aria-label="Pattern"
            variant="outline"
            size="sm"
            spacing={0}
            value={[String(patternIndex)]}
            onValueChange={(value) => {
              const next = value[0]
              if (next !== undefined) onPatternChange(Number(next))
            }}
          >
            {oriented.map((pattern) => (
              <ToggleGroupItem
                key={pattern}
                value={String(pattern)}
                className={cn("h-6 px-2 text-xs", PRESSED)}
              >
                {pattern}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </div>
      )}
      <UnitCellView
        direct={reading.direct}
        showBeam={view.showBeam}
        camera={view.camera}
        onCameraChange={view.onCameraChange}
        label={`The unit cell of step ${String(point.step)}, pattern ${String(patternIndex)}, in beamline coordinates`}
      />
      <DefinitionList
        aria-label="Lattice parameters"
        className="text-xs"
        rows={cellRows(point, reading)}
      />
    </div>
  )
}

function cellRows(point: PointDetails, reading: UnitCellReading): Definition[] {
  const crystal = point.crystal
  const rows: Definition[] = []
  if (crystal?.name) {
    rows.push({
      label: "Crystal",
      value:
        crystal.space_group === null
          ? crystal.name
          : `${crystal.name} · SG ${String(crystal.space_group)}`,
    })
  }
  rows.push(
    {
      label: "Step",
      value: `${String(point.step)} · pattern ${String(reading.patternIndex)}`,
    },
    { label: "a b c (nm)", value: triple(reading.measured.lengths, 4) },
    { label: "α β γ (°)", value: triple(reading.measured.angles, 3) }
  )
  // The pattern's cell is the nominal one unless the results refined it: then both.
  if (reading.refined && reading.nominal) {
    rows.push({
      label: "Nominal",
      value: (
        <span className="text-muted-foreground">
          {triple(reading.nominal.lengths, 4)} ·{" "}
          {triple(reading.nominal.angles, 3)}
        </span>
      ),
    })
  }
  if (reading.rotation) {
    rows.push({
      label: "Rotation",
      value: (
        <span
          title={
            reading.symmetry === "none"
              ? "From the reference cell"
              : `From the reference cell: the smallest of the crystal's ${reading.symmetry} equivalent rotations`
          }
        >
          {reading.rotation.angleDeg.toFixed(2)}°
          {reading.rotation.axis && (
            <span className="text-muted-foreground">
              {" "}
              about {direction(reading.rotation.axis)} (lab)
            </span>
          )}
        </span>
      ),
    })
  }
  rows.push(
    { label: "Beam ∥", value: direction(reading.beam) },
    { label: "Up ∥", value: direction(reading.up) }
  )
  return rows
}
