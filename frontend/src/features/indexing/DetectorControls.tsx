import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react"
import { useState, type ReactNode } from "react"

import type { DetectorPattern } from "@/api"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { formatCount } from "@/lib/format"
import { COLORMAP_LABELS, FRAME_COLORMAPS } from "@/viz"
import { NumberField } from "./NumberField"
import { Choice, Control, Note, Section } from "./controls"
import { adjacentStep } from "./detector"
import {
  DETECTOR_LABEL_RANGE,
  DETECTOR_MARKER_RANGE,
  isDetectorLabelSize,
  isDetectorMarkerSize,
} from "./viewer-slices"
import { useIndexingViewerStore } from "./viewer-store"

const range = ([lo, hi]: readonly [number, number]) =>
  `${String(lo)} to ${String(hi)} px`

/**
 * The Detector View's sidebar: the step (limited to the scope's), the overlays and their sizes,
 * the image, and the step's pattern checklist.
 */
export function DetectorControls({
  steps,
  step,
  onStepChange,
  patterns,
  unchecked,
  onUncheckedChange,
  opacity,
  onOpacityChange,
}: {
  /** The steps in scope (the summary's), in order. */
  steps: readonly number[]
  step: number
  onStepChange: (step: number) => void
  /** The shown step's patterns (every one), or null while none are loaded. */
  patterns: readonly DetectorPattern[] | null
  unchecked: ReadonlySet<number>
  onUncheckedChange: (unchecked: ReadonlySet<number>) => void
  /** The opacity shown: the stored one, or the slider's while it is dragged. */
  opacity: number
  /** A move of the slider (`done` once the drag or key press ends: then it is stored). */
  onOpacityChange: (opacity: number, done: boolean) => void
}) {
  const settings = useIndexingViewerStore((state) => state.detector)
  const setDetector = useIndexingViewerStore((state) => state.setDetector)
  const previous = adjacentStep(steps, step, -1)
  const next = adjacentStep(steps, step, 1)
  const first = steps[0]
  const last = steps[steps.length - 1]

  return (
    <div className="flex flex-col gap-5">
      <Section title="Step">
        <Control label="Step #">
          <div className="flex items-center gap-1">
            <Button
              size="icon-sm"
              variant="outline"
              aria-label="Previous step"
              disabled={previous === null}
              onClick={() => {
                if (previous !== null) onStepChange(previous)
              }}
            >
              <ChevronLeftIcon />
            </Button>
            <NumberField
              aria-label="Step"
              inputMode="numeric"
              value={step}
              isValid={(value) => steps.includes(value)}
              title="A step in the scope"
              onCommit={onStepChange}
            />
            <Button
              size="icon-sm"
              variant="outline"
              aria-label="Next step"
              disabled={next === null}
              onClick={() => {
                if (next !== null) onStepChange(next)
              }}
            >
              <ChevronRightIcon />
            </Button>
          </div>
        </Control>
        <Note>
          {steps.length === 1
            ? "1 step in scope"
            : `${formatCount(steps.length)} steps in scope (${String(first)} to ${String(last)})`}
        </Note>
      </Section>

      <Section title="Overlays">
        <Toggle
          checked={settings.showDetected}
          onChange={(showDetected) => {
            setDetector({ showDetected })
          }}
        >
          Detected peaks
        </Toggle>
        <Toggle
          checked={settings.showIndexed}
          onChange={(showIndexed) => {
            setDetector({ showIndexed })
          }}
        >
          Indexed peaks
        </Toggle>
        <Toggle
          checked={settings.simulate}
          onChange={(simulate) => {
            setDetector({ simulate })
          }}
        >
          Simulated missing peaks
        </Toggle>
        <Toggle
          checked={settings.showLabels}
          onChange={(showLabels) => {
            setDetector({ showLabels })
          }}
        >
          hkl labels
        </Toggle>
        <Control label="Marker">
          <NumberField
            aria-label="Marker size"
            inputMode="numeric"
            value={settings.markerSize}
            isValid={isDetectorMarkerSize}
            title={range(DETECTOR_MARKER_RANGE)}
            onCommit={(markerSize) => {
              setDetector({ markerSize })
            }}
          />
        </Control>
        <Control label="Label size">
          <NumberField
            aria-label="Label size"
            inputMode="numeric"
            value={settings.labelSize}
            isValid={isDetectorLabelSize}
            title={range(DETECTOR_LABEL_RANGE)}
            onCommit={(labelSize) => {
              setDetector({ labelSize })
            }}
          />
        </Control>
      </Section>

      <Section title="Image">
        <Toggle
          checked={settings.showImage}
          onChange={(showImage) => {
            setDetector({ showImage })
          }}
        >
          Show detector image
        </Toggle>
        <Choice
          label="Colormap"
          value={settings.colormap}
          options={FRAME_COLORMAPS.map((value) => ({
            value,
            label: COLORMAP_LABELS[value],
          }))}
          onChange={(colormap) => {
            setDetector({ colormap })
          }}
        />
        <Control label="Opacity">
          <OpacitySlider value={opacity} onChange={onOpacityChange} />
        </Control>
      </Section>

      <Section title="Patterns">
        {patterns === null ? (
          <Note>Loading the step's patterns…</Note>
        ) : patterns.length === 0 ? (
          <Note>No pattern at this step.</Note>
        ) : (
          <div
            role="group"
            aria-label="Patterns"
            className="flex flex-col gap-1.5"
          >
            {patterns.map((pattern) => (
              <Toggle
                key={pattern.pattern_index}
                checked={!unchecked.has(pattern.pattern_index)}
                onChange={(checked) => {
                  const nextUnchecked = new Set(unchecked)
                  if (checked) nextUnchecked.delete(pattern.pattern_index)
                  else nextUnchecked.add(pattern.pattern_index)
                  onUncheckedChange(nextUnchecked)
                }}
              >
                Pattern {pattern.pattern_index} ({pattern.n_indexed} indexed)
                {!pattern.in_scope && (
                  <span className="text-muted-foreground">
                    {" "}
                    · outside the scope
                  </span>
                )}
              </Toggle>
            ))}
          </div>
        )}
      </Section>
    </div>
  )
}

function Toggle({
  checked,
  onChange,
  children,
}: {
  checked: boolean
  onChange: (checked: boolean) => void
  children: ReactNode
}) {
  return (
    <label className="flex items-center gap-2 text-sm">
      <Checkbox checked={checked} onCheckedChange={onChange} />
      <span>{children}</span>
    </label>
  )
}

/**
 * The image's opacity, 0 to 1 in steps of 0.05. It redraws while dragged and is stored when the drag
 * ends; a key press is stored at once.
 */
function OpacitySlider({
  value,
  onChange,
}: {
  value: number
  onChange: (value: number, done: boolean) => void
}) {
  const [dragging, setDragging] = useState(false)
  const read = (target: EventTarget) =>
    Number((target as HTMLInputElement).value)
  return (
    <div className="flex items-center gap-2">
      <input
        type="range"
        aria-label="Opacity"
        min={0}
        max={1}
        step={0.05}
        value={value}
        className="h-4 min-w-0 flex-1 accent-primary"
        onPointerDown={() => {
          setDragging(true)
        }}
        onChange={(event) => {
          onChange(read(event.target), !dragging)
        }}
        onPointerUp={(event) => {
          setDragging(false)
          onChange(read(event.target), true)
        }}
        onBlur={(event) => {
          if (!dragging) return
          setDragging(false)
          onChange(read(event.target), true)
        }}
      />
      <span className="w-8 text-right font-mono text-xs tabular-nums">
        {value.toFixed(2)}
      </span>
    </div>
  )
}
