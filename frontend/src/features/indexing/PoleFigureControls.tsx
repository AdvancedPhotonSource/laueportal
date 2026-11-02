import { useState, type ReactNode } from "react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Choice, Control, Section } from "./controls"
import { MatrixField, NumberField } from "./NumberField"
import {
  centerText,
  hklText,
  hklUpdate,
  patternText,
  type HklText,
} from "./pole-figure"
import type { Hkl } from "./viewer-search"
import {
  isMarkerSize,
  MARKER_SIZE_RANGE,
  POLE_SCHEMES,
  SURFACE_LABELS,
  SURFACES,
  type PoleReference,
  type PoleScheme,
  type SurfaceChoice,
} from "./viewer-slices"
import { useIndexingViewerStore } from "./viewer-store"

const SCHEME_LABELS: Record<PoleScheme, string> = {
  hsv: "Position HSV",
  ipf: "Cubic IPF",
  uniform: "Uniform",
}

/** The bounds of the HSV colour radius. */
const RADIUS_RANGE = [0.1, 90] as const
const isRadius = (value: number) =>
  value >= RADIUS_RANGE[0] && value <= RADIUS_RANGE[1]

const XYZ = ["x", "y", "z"] as const

/**
 * The Pole Figure tab's controls: the HKL (applied by Update, enabled only for a valid HKL other
 * than the applied one), the surface, the colour scheme and HSV radius, the colour centre and the
 * misorientation reference, "Reset color center" while they are set, the colour key, and the marker
 * size. The HKL is the URL's; the rest is the store's pole slice. Numbers apply on Enter or on
 * leaving the field.
 */
export function PoleFigureControls({
  hkl,
  onHklChange,
  reference,
  onResetCenter,
  colorKey,
}: {
  /** The applied HKL (the URL's). */
  hkl: Hkl
  onHklChange: (hkl: Hkl) => void
  /** This run's colour centre and reference, if set. */
  reference: PoleReference | null
  /** Set while this run's colour centre is set. */
  onResetCenter: (() => void) | null
  colorKey: ReactNode
}) {
  const pole = useIndexingViewerStore((state) => state.pole)
  const setPole = useIndexingViewerStore((state) => state.setPole)

  return (
    <div className="flex flex-col gap-4">
      <Section title="Pole">
        <HklFields hkl={hkl} onApply={onHklChange} />
        <Choice<SurfaceChoice>
          label="Surface"
          value={pole.surface}
          options={SURFACES.map((value) => ({
            value,
            label: SURFACE_LABELS[value],
          }))}
          onChange={(surface) => {
            setPole({ surface })
          }}
        />
        {pole.surface === "custom" && (
          <MatrixField
            label="Custom frame"
            rows={["tilt", "roll", "normal"]}
            columns={XYZ}
            values={pole.surfaceMatrix}
            onChange={(values) => {
              if (values.every((v) => v !== null)) {
                setPole({ surfaceMatrix: values })
              }
            }}
          />
        )}
      </Section>

      <Section title="Color">
        <Choice<PoleScheme>
          label="Scheme"
          value={pole.scheme}
          options={POLE_SCHEMES.map((value) => ({
            value,
            label: SCHEME_LABELS[value],
          }))}
          onChange={(scheme) => {
            setPole({ scheme })
          }}
        />
        <Control label="HSV radius">
          <div className="flex items-center gap-1">
            <NumberField
              className="min-w-0 flex-1"
              aria-label="HSV radius"
              value={pole.radius}
              isValid={isRadius}
              title={`${String(RADIUS_RANGE[0])}° to ${String(RADIUS_RANGE[1])}°`}
              onCommit={(radius) => {
                setPole({ radius })
              }}
            />
            <span className="text-muted-foreground">°</span>
          </div>
        </Control>
        <Control label="Center">
          <span
            className="tabular-nums"
            title="Set with the plot's Set color center tool, or by clicking a pole"
          >
            {centerText(reference?.center ?? null)}
          </span>
        </Control>
        <Control label="Reference">
          <span
            className="truncate tabular-nums"
            title="The misorientation reference: the clicked pole's step / pattern"
          >
            {patternText(reference?.pattern ?? null)}
          </span>
        </Control>
        {onResetCenter && (
          <Control label="">
            <Button
              variant="secondary"
              size="sm"
              onClick={onResetCenter}
              className="justify-self-start"
            >
              Reset color center
            </Button>
          </Control>
        )}
        {colorKey}
      </Section>

      <Section title="Display">
        <Control label="Marker">
          <NumberField
            aria-label="Marker size"
            inputMode="numeric"
            value={pole.markerSize}
            isValid={isMarkerSize}
            title={`${String(MARKER_SIZE_RANGE[0])} to ${String(MARKER_SIZE_RANGE[1])} px`}
            onCommit={(markerSize) => {
              setPole({ markerSize })
            }}
          />
        </Control>
      </Section>
    </div>
  )
}

/** h, k and l, applied by Update (or Enter) when they name a valid HKL other than the applied one. */
function HklFields({
  hkl,
  onApply,
}: {
  hkl: Hkl
  onApply: (hkl: Hkl) => void
}) {
  const [text, setText] = useState<HklText>(() => hklText(hkl))
  // Another HKL arrived (Update, back or forward, a link): the fields show it. Compared by value: the
  // view parses the URL's HKL into a new array each render.
  const applied = hkl.join(",")
  const [shown, setShown] = useState(applied)
  if (shown !== applied) {
    setShown(applied)
    setText(hklText(hkl))
  }
  const next = hklUpdate(text, hkl)

  return (
    <form
      className="grid grid-cols-[5.5rem_1fr] items-center gap-2 text-sm"
      onSubmit={(event) => {
        event.preventDefault()
        if (next) onApply(next)
      }}
    >
      <span className="text-muted-foreground">HKL</span>
      <div className="flex gap-1">
        {(["h", "k", "l"] as const).map((name, i) => (
          <Input
            key={name}
            aria-label={name}
            type="text"
            inputMode="numeric"
            value={text[i]}
            className="h-7 min-w-0 flex-1 px-1 text-center font-mono text-xs tabular-nums"
            onChange={(event) => {
              const changed = [...text] as [string, string, string]
              changed[i] = event.target.value
              setText(changed)
            }}
          />
        ))}
      </div>
      <Button
        type="submit"
        size="sm"
        variant="outline"
        disabled={!next}
        className="col-start-2 justify-self-start"
      >
        Update
      </Button>
    </form>
  )
}
