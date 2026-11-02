import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { DefinitionList } from "@/components/DefinitionList"
import { Button } from "@/components/ui/button"
import { DEPTH_DEBOUNCE_MS, depthReadout } from "./depth-view"

/**
 * The depth index (0 to count − 1): a slider between Previous and Next, the index and the readout
 * "depth µm (first … last)". The slider shows its index and depth at once and asks for the depth
 * once it rests for `DEPTH_DEBOUNCE_MS` or is let go; the buttons ask at once. Give it a new `key`
 * for another point (a pending move belongs to the point left).
 */
export function DepthControl({
  value,
  count,
  depthAt,
  range,
  onChange,
}: {
  /** The depth shown (its frame asked for), or undefined while the point's default is not known. */
  value: number | undefined
  count: number
  /** The depth (µm) of an index, as far as it is known. */
  depthAt: (index: number) => number | null
  range: readonly [number, number] | null
  onChange: (index: number) => void
}) {
  const [draft, setDraft] = useState<number | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const onChangeRef = useRef(onChange)
  useEffect(() => {
    onChangeRef.current = onChange
  })
  useEffect(
    () => () => {
      clearTimeout(timer.current)
    },
    []
  )

  const index = draft ?? value
  const last = count - 1
  const commit = (next: number) => {
    clearTimeout(timer.current)
    timer.current = undefined
    setDraft(null)
    if (next !== value) onChangeRef.current(next)
  }
  const move = (next: number) => {
    setDraft(next)
    clearTimeout(timer.current)
    timer.current = setTimeout(() => {
      commit(next)
    }, DEPTH_DEBOUNCE_MS)
  }
  const read = (target: EventTarget) =>
    Number((target as HTMLInputElement).value)
  const disabled = index === undefined || count < 1

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-1">
        <Button
          size="icon-sm"
          variant="outline"
          aria-label="Previous depth"
          disabled={disabled || index <= 0}
          onClick={() => {
            if (index !== undefined) commit(index - 1)
          }}
        >
          <ChevronLeftIcon />
        </Button>
        <input
          type="range"
          aria-label="Depth index"
          title="Depth index (arrow keys step)"
          min={0}
          max={Math.max(last, 0)}
          step={1}
          value={index ?? 0}
          disabled={disabled}
          className="h-4 min-w-0 flex-1 accent-primary"
          onChange={(event) => {
            move(read(event.target))
          }}
          onPointerUp={(event) => {
            commit(read(event.target))
          }}
        />
        <Button
          size="icon-sm"
          variant="outline"
          aria-label="Next depth"
          disabled={disabled || index >= last}
          onClick={() => {
            if (index !== undefined) commit(index + 1)
          }}
        >
          <ChevronRightIcon />
        </Button>
      </div>
      <DefinitionList
        aria-label="Depth"
        rows={[
          {
            label: "Index",
            value:
              index === undefined
                ? "—"
                : `${String(index)} (0 … ${String(Math.max(last, 0))})`,
          },
          {
            label: "Depth",
            value: depthReadout(
              index === undefined ? null : depthAt(index),
              range
            ),
          },
        ]}
      />
    </div>
  )
}
