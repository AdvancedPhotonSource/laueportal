import type { ReactNode } from "react"

import {
  MapColor,
  OrientationSymmetry,
  type AxisChoice,
  type ColorModeChoice,
} from "@/api"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  COLORMAP_LABELS,
  MAP_PALETTES,
  NON_INDEXED_COLORS,
  type NonIndexedColor,
} from "@/viz"
import { formatHkl, type IndexingView, type ViewMode } from "./viewer-search"
import { MatrixField, NumberField } from "./NumberField"
import { Choice, Control, Section, type Option } from "./controls"
import { centerText, patternText } from "./pole-figure"
import { poleReferenceOf, rodriguesStepOf } from "./color-map"
import {
  isMarkerSize,
  isScalarMode,
  isStep,
  MARKER_SIZE_RANGE,
  SURFACE_LABELS,
  SURFACES,
  type RodriguesReference,
  type SurfaceChoice,
} from "./viewer-slices"
import { useGoodnessLabel } from "./goodness-label"
import { useIndexingViewerStore } from "./viewer-store"

/** The colour modes' labels, until the summary names them. */
const COLOR_LABELS: Record<MapColor, string> = {
  cubic_ipf: "Cubic IPF",
  rodrigues: "Rodrigues RGB",
  misorientation: "Misorientation",
  pole_hsv: "Pole Figure HSV",
  n_indexed: "N Indexed",
  goodness: "Goodness",
  rms_error: "RMS Error",
  n_patterns: "N Patterns",
}

const SYMMETRY_LABELS: Record<OrientationSymmetry, string> = {
  auto: "Auto (crystal)",
  cubic: "Cubic",
  hexagonal: "Hexagonal",
  none: "None",
}

const REFERENCE_LABELS: Record<RodriguesReference, string> = {
  lab: "Lab system",
  step: "Step #",
  custom: "Custom G_ref",
}

const NON_INDEXED_LABELS: Record<NonIndexedColor, string> = {
  gray: "Gray",
  red: "Red",
  blue: "Blue",
  green: "Green",
  transparent: "Transparent",
}

const XYZ = ["x", "y", "z"] as const

/**
 * The Color Map tab's controls: the colour mode and its settings, the legend slot (`legend`), the
 * surface, the 2D/3D switch and the axes, the marker size and the colour of non-indexed points. The
 * colour mode and 2D/3D are the URL's (`view`, `onViewChange`); the rest is the store's map slice.
 * Numbers apply on Enter or on leaving the field.
 */
export function ColorMapControls({
  indexingId,
  view,
  onViewChange,
  colorModes,
  axisChoices,
  axes,
  legend,
}: {
  indexingId: number
  view: IndexingView
  onViewChange: (change: Partial<IndexingView>) => void
  /** The summary's colour modes (each with whether it is available and why not). */
  colorModes: readonly ColorModeChoice[] | undefined
  axisChoices: readonly AxisChoice[]
  /** The axes plotted (resolved for the results). */
  axes: readonly string[] | null
  /** The legend of the plot shown (key image, or palette and reverse for a scalar mode). */
  legend: ReactNode
}) {
  const map = useIndexingViewerStore((state) => state.map)
  const poleSurface = useIndexingViewerStore((state) => state.pole.surface)
  const poleRadius = useIndexingViewerStore((state) => state.pole.radius)
  const reference = useIndexingViewerStore((state) =>
    poleReferenceOf(state.pole, indexingId)
  )
  const setMap = useIndexingViewerStore((state) => state.setMap)
  const color = view.color
  const goodnessLabel = useGoodnessLabel()

  const modeOptions: Option<MapColor>[] = Object.values(MapColor).map(
    (value) => {
      const choice = colorModes?.find((mode) => mode.value === value)
      return {
        value,
        label:
          value === MapColor.goodness
            ? goodnessLabel
            : (choice?.label ?? COLOR_LABELS[value]),
        reason:
          choice && !choice.available ? (choice.reason ?? "unavailable") : null,
      }
    }
  )
  const axisOptions = axisChoices.map((axis) => ({
    value: axis.value,
    label: axis.label,
  }))

  const setAxis = (index: number, value: string) => {
    if (!axes) return
    const next = [...axes]
    // Choosing an axis already plotted swaps the two.
    const other = next.indexOf(value)
    if (other >= 0) next[other] = next[index] as string
    next[index] = value
    if (view.mode === "3d") setMap({ axes3d: next as [string, string, string] })
    else setMap({ axes2d: next as [string, string] })
  }

  return (
    <div className="flex flex-col gap-4">
      <Section title="Color">
        <Choice
          label="Color by"
          value={color}
          options={modeOptions}
          onChange={(next) => {
            onViewChange({ color: next })
          }}
        />
        {(color === MapColor.rodrigues ||
          color === MapColor.misorientation) && (
          <Choice
            label="Symmetry"
            value={map.rodriguesSymmetry}
            options={Object.values(OrientationSymmetry).map((value) => ({
              value,
              label: SYMMETRY_LABELS[value],
            }))}
            onChange={(next) => {
              setMap({ rodriguesSymmetry: next })
            }}
          />
        )}
        {color === MapColor.rodrigues && (
          <>
            <Choice
              label="Reference"
              value={map.rodriguesReference}
              options={(["lab", "step", "custom"] as const).map((value) => ({
                value,
                label: REFERENCE_LABELS[value],
              }))}
              onChange={(next) => {
                setMap({ rodriguesReference: next })
              }}
            />
            {map.rodriguesReference === "step" && (
              <Control label="Ref step">
                <NumberField
                  aria-label="Reference step"
                  title="Its pattern 0 is the reference orientation"
                  inputMode="numeric"
                  value={rodriguesStepOf(map, indexingId)}
                  isValid={isStep}
                  onCommit={(value) => {
                    setMap({ rodriguesStep: { indexingId, step: value } })
                  }}
                />
              </Control>
            )}
            {map.rodriguesReference === "custom" && (
              <>
                <MatrixField
                  label="G_ref"
                  title="Rows a*, b*, c*, in 1/nm including 2π"
                  rows={["a*", "b*", "c*"]}
                  columns={XYZ}
                  values={map.rodriguesMatrix}
                  onChange={(values) => {
                    setMap({ rodriguesMatrix: values })
                  }}
                />
              </>
            )}
          </>
        )}
        {color === MapColor.misorientation && (
          <Control label="Reference">
            <PoleSetting>{patternText(reference?.pattern ?? null)}</PoleSetting>
          </Control>
        )}
        {color === MapColor.pole_hsv && (
          <>
            <Control label="HKL">
              <PoleSetting>{formatHkl(view.hkl)}</PoleSetting>
            </Control>
            <Control label="Surface">
              <PoleSetting>{SURFACE_LABELS[poleSurface]}</PoleSetting>
            </Control>
            <Control label="HSV radius">
              <PoleSetting>{poleRadius}°</PoleSetting>
            </Control>
            <Control label="Center">
              <PoleSetting>{centerText(reference?.center ?? null)}</PoleSetting>
            </Control>
          </>
        )}
        {legend}
      </Section>

      {color === MapColor.cubic_ipf && (
        <Section title="Projection">
          <Choice<SurfaceChoice>
            label="Surface"
            value={map.surface}
            options={SURFACES.map((value) => ({
              value,
              label: SURFACE_LABELS[value],
            }))}
            onChange={(next) => {
              setMap({ surface: next })
            }}
          />
          {map.surface === "custom" && (
            <MatrixField
              label="Custom frame"
              rows={["tilt", "roll", "normal"]}
              columns={XYZ}
              values={map.surfaceMatrix}
              onChange={(values) => {
                if (values.every((v) => v !== null)) {
                  setMap({ surfaceMatrix: values })
                }
              }}
            />
          )}
        </Section>
      )}

      <Section title="View">
        <Control label="View">
          <div role="group" aria-label="2D or 3D" className="flex gap-1">
            {(["2d", "3d"] as const satisfies readonly ViewMode[]).map(
              (mode) => (
                <Button
                  key={mode}
                  size="sm"
                  variant={view.mode === mode ? "default" : "outline"}
                  aria-pressed={view.mode === mode}
                  onClick={() => {
                    onViewChange({ mode })
                  }}
                >
                  {mode.toUpperCase()}
                </Button>
              )
            )}
          </div>
        </Control>
        {axes &&
          axes.map((axis, index) => (
            <Choice
              key={`${view.mode}-${String(index)}`}
              label={`${(XYZ[index] ?? "").toUpperCase()} axis`}
              value={axis}
              options={axisOptions}
              onChange={(next) => {
                setAxis(index, next)
              }}
            />
          ))}
      </Section>

      <Section title="Display">
        <Control label="Marker">
          <NumberField
            aria-label="Marker size"
            inputMode="numeric"
            value={map.markerSize}
            isValid={isMarkerSize}
            title={`${String(MARKER_SIZE_RANGE[0])} to ${String(MARKER_SIZE_RANGE[1])} px`}
            onCommit={(value) => {
              setMap({ markerSize: value })
            }}
          />
        </Control>
        <Choice
          label="Non-indexed"
          value={map.nonIndexed}
          options={NON_INDEXED_COLORS.map((value) => ({
            value,
            label: NON_INDEXED_LABELS[value],
          }))}
          onChange={(next) => {
            setMap({ nonIndexed: next })
          }}
        />
      </Section>
    </div>
  )
}

/** A scalar mode's palette and reverse (the range is the colorbar's, under the plot). */
export function ScalarPaletteControls({ color }: { color: MapColor }) {
  const settings = useIndexingViewerStore((state) =>
    isScalarMode(color) ? state.map.scalar[color] : null
  )
  const setScalar = useIndexingViewerStore((state) => state.setScalar)
  if (!settings || !isScalarMode(color)) return null
  return (
    <>
      <Choice
        label="Palette"
        value={settings.palette}
        options={MAP_PALETTES.map((value) => ({
          value,
          label: COLORMAP_LABELS[value],
        }))}
        onChange={(palette) => {
          setScalar(color, { palette })
        }}
      />
      <Control label="">
        <label className="flex items-center gap-2 text-sm">
          <Checkbox
            checked={settings.reverse}
            onCheckedChange={(reverse) => {
              setScalar(color, { reverse })
            }}
          />
          Reverse
        </label>
      </Control>
    </>
  )
}

/** A setting of the pole figure the map reads, shown read-only (it is set on the Pole Figure tab). */
function PoleSetting({ children }: { children: ReactNode }) {
  return (
    <span className="truncate tabular-nums" title="Set on the Pole Figure tab">
      {children}
    </span>
  )
}
