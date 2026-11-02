// The interactive colorbar: the data's histogram along the bar, minimum and maximum handles that
// recolour live while dragged (only the colour table changes, no request), the scale, exact values
// on double-click, and the automatic ranges. One component for the map's scalar modes, the detector
// view and the wire viewer's depth frames: the page keeps its `ColorbarSettings` and passes the
// resolved range (`resolveRange`) to the heatmap or to the scatter's lookup (`scalarLookup`).
import {
  FoldHorizontalIcon,
  RotateCcwIcon,
  UnfoldHorizontalIcon,
} from "lucide-react"
import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
} from "react"

import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { cn } from "@/lib/utils"

import { colormapPixels, type ColormapName } from "../color/colormaps"
import type { Distribution } from "../color/distribution"
import { colorIndex } from "../color/lut"
import {
  GAMMA_LIMITS,
  normalizer,
  SCALE_KINDS,
  SCALE_LABELS,
} from "../color/scale"
import {
  AUTO_RANGES,
  axisDomain,
  axisTicks,
  colorScaleOf,
  dataDomain,
  effectiveScale,
  formatValue,
  histogramBars,
  labelPositions,
  moveHandle,
  positionToValue,
  resolveRange,
  valueToPosition,
  type AutoRangeId,
  type AxisMode,
  type ColorbarSettings,
  type Range,
} from "./logic"

export interface ColorBarProps {
  /** The data's distribution (`useDistribution`), or null while it is computed. */
  distribution: Distribution | null
  /** Why the distribution could not be computed (`useDistribution`'s `error`), shown on the bar. */
  error?: string | null
  /** Computes it again (`useDistribution`'s `retry`), offered with the error. */
  onRetry?: () => void
  colormap: ColormapName
  reverse?: boolean
  value: ColorbarSettings
  /** Called on every move of a handle (live), and on each control's change. */
  onChange: (next: ColorbarSettings) => void
  /** The automatic range Reset returns to (FRAME_AUTO_RANGE or MAP_AUTO_RANGE). */
  defaultAuto: AutoRangeId
  /** A short title over the bar (e.g. "Intensity"). */
  title?: string
  className?: string
}

type Handle = "min" | "max"

const LABEL_TOP = 0
const BAR_TOP = 18
const HIST_HEIGHT = 30
const STRIP_HEIGHT = 10
const TICKS_TOP = BAR_TOP + HIST_HEIGHT + STRIP_HEIGHT + 3
/** The value labels' width: monospace 11 px digits plus padding. */
const labelWidth = (text: string) => text.length * 6.7 + 8

const AUTO_LABELS: Record<string, string> = {
  ...Object.fromEntries(AUTO_RANGES.map((r) => [r.id, r.label])),
  manual: "Manual",
}

export function ColorBar({
  distribution: dist,
  error = null,
  onRetry,
  colormap,
  reverse = false,
  value,
  onChange,
  defaultAuto,
  title,
  className,
}: ColorBarProps) {
  const id = useId()
  const barRef = useRef<HTMLDivElement>(null)
  const stripRef = useRef<HTMLCanvasElement>(null)
  const cancelEdit = useRef(false)
  const [width, setWidth] = useState(0)
  const [axisMode, setAxisMode] = useState<AxisMode>("fit")
  // While a handle is dragged the axis stays as it was; it re-fits when the drag ends.
  const [drag, setDrag] = useState<{ which: Handle; axis: Range } | null>(null)
  const [editing, setEditing] = useState<Handle | null>(null)
  // The gamma field's text while it is typed in (null: it shows the setting).
  const [gammaText, setGammaText] = useState<string | null>(null)

  useEffect(() => {
    const bar = barRef.current
    if (!bar || typeof ResizeObserver === "undefined") return
    const observer = new ResizeObserver(() => {
      setWidth(Math.floor(bar.getBoundingClientRect().width))
    })
    observer.observe(bar)
    return () => {
      observer.disconnect()
    }
  }, [])

  const range = useMemo(
    () => (dist ? resolveRange(value, dist) : null),
    [dist, value]
  )
  // Log needs a positive minimum; without one the bar is linear, as the colours are.
  const kind = effectiveScale(value.scale, range ? range[0] : NaN)
  const noPositive = dist !== null && dist.n > 0 && !(dist.minPositive > 0)
  const domain = dist ? dataDomain(dist, kind) : null
  const fitted = range ? axisDomain(range, domain, kind, axisMode) : null
  const axis = drag?.axis ?? fitted
  const [a0, a1] = axis ?? [NaN, NaN]
  const [lo, hi] = range ?? [NaN, NaN]
  const scale = colorScaleOf(value)
  const gamma = scale.gamma

  // The histogram, recomputed only when the axis or the data change (never during a drag).
  const histogram = useMemo(() => {
    if (!dist || !(a1 > a0) || width <= 0) return ""
    const ax: Range = [a0, a1]
    const { edges, heights } = histogramBars(
      dist,
      ax,
      kind,
      Math.min(256, width / 2)
    )
    const x = (v: number) =>
      Math.min(width, Math.max(0, valueToPosition(v, ax, kind, width)))
    let d = `M0 ${String(HIST_HEIGHT)}`
    heights.forEach((h, i) => {
      const y = (HIST_HEIGHT - h * (HIST_HEIGHT - 1)).toFixed(1)
      d += `L${x(edges[i] as number).toFixed(1)} ${y}L${x(edges[i + 1] as number).toFixed(1)} ${y}`
    })
    return `${d}L${String(width)} ${String(HIST_HEIGHT)}Z`
  }, [dist, a0, a1, kind, width])

  // The colour strip: each column's colour for its value (below min and above max saturate).
  useEffect(() => {
    const canvas = stripRef.current
    const ctx = canvas?.getContext("2d")
    if (!canvas || !ctx || !(a1 > a0) || !(hi > lo) || width <= 0) return
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    const w = Math.max(1, Math.round(width * dpr))
    canvas.width = w
    canvas.height = 1
    const image = ctx.createImageData(w, 1)
    const out = new Uint32Array(image.data.buffer)
    const colors = colormapPixels(colormap, reverse)
    const f = normalizer(lo, hi, { kind, gamma })
    for (let i = 0; i < w; i++) {
      const v = positionToValue((i + 0.5) / dpr, [a0, a1], kind, width)
      out[i] = colors[colorIndex(f(v))] as number
    }
    ctx.putImageData(image, 0, 0)
  }, [a0, a1, lo, hi, kind, gamma, colormap, reverse, width])

  const change = (patch: Partial<ColorbarSettings>) => {
    onChange({ ...value, ...patch })
  }
  const setRange = (next: Range) => {
    if (next[0] !== lo || next[1] !== hi || value.auto !== null) {
      change({ auto: null, min: next[0], max: next[1] })
    }
  }
  // A gesture stays within `bounds` (what the bar shows); a typed value (null) is taken as given.
  const moveTo = (which: Handle, v: number, bounds: Range | null) => {
    if (dist && range) setRange(moveHandle(which, v, range, dist, bounds))
  }
  const pointerX = (e: PointerEvent) =>
    e.clientX - (barRef.current?.getBoundingClientRect().left ?? 0)
  const at = (x: number, ax: Range) =>
    positionToValue(Math.min(width, Math.max(0, x)), ax, kind, width)

  const ready = dist !== null && range !== null && axis !== null && width > 0
  const xMin = ready ? valueToPosition(lo, axis, kind, width) : 0
  const xMax = ready ? valueToPosition(hi, axis, kind, width) : 0

  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    // The value labels only take a double-click (to type a value).
    const element = e.target as HTMLElement
    if (!ready || e.button !== 0 || editing || element.closest("[data-label]"))
      return
    const x = pointerX(e)
    const target = element.closest<HTMLElement>("[data-handle]")
    const which: Handle = target
      ? (target.dataset.handle as Handle)
      : Math.abs(x - xMin) <= Math.abs(x - xMax) && !(x > xMax)
        ? "min"
        : "max"
    // A pressed handle captures the pointer itself: click and dblclick go to the capturing
    // element, so a capturing bar would take the handle's double-click (the value editor).
    const capturing = target ?? e.currentTarget
    capturing.setPointerCapture(e.pointerId)
    setDrag({ which, axis })
    // A press on the track moves the nearer handle there at once.
    if (!target) moveTo(which, at(x, axis), axis)
  }
  const onPointerMove = (e: PointerEvent<HTMLDivElement>) => {
    if (drag) moveTo(drag.which, at(pointerX(e), drag.axis), drag.axis)
  }
  const endDrag = (e: PointerEvent<HTMLDivElement>) => {
    const element = e.target as Element
    if (element.hasPointerCapture(e.pointerId)) {
      element.releasePointerCapture(e.pointerId)
    }
    setDrag(null)
  }

  const onHandleKey = (which: Handle, e: KeyboardEvent) => {
    if (!ready) return
    const current = which === "min" ? lo : hi
    if (e.key === "Enter") {
      setEditing(which)
      return
    }
    // Home and End go to the data's ends; the keys stay within the bar and the data.
    const bounds: Range = domain
      ? [Math.min(axis[0], domain[0]), Math.max(axis[1], domain[1])]
      : axis
    let next: number
    if (e.key === "Home") next = domain?.[0] ?? axis[0]
    else if (e.key === "End") next = domain?.[1] ?? axis[1]
    else {
      const sign =
        e.key === "ArrowRight" || e.key === "ArrowUp"
          ? 1
          : e.key === "ArrowLeft" || e.key === "ArrowDown"
            ? -1
            : 0
      if (sign === 0) return
      const x = valueToPosition(current, axis, kind, width)
      next = positionToValue(
        x + sign * width * (e.shiftKey ? 0.1 : 0.01),
        axis,
        kind,
        width
      )
      // Integer data moves at least one step.
      if (dist.integer && Math.round(next) === current) next = current + sign
    }
    e.preventDefault()
    moveTo(which, next, bounds)
  }

  const commitEdit = (which: Handle, text: string) => {
    setEditing(null)
    if (cancelEdit.current) {
      cancelEdit.current = false
      return
    }
    const v = Number(text.trim())
    if (text.trim() !== "" && Number.isFinite(v)) moveTo(which, v, null)
  }

  // Typed text commits on Enter or blur (so "0.5" can pass through "0" and "0."), clamped to the
  // limits; text that is not a number reverts. The arrow keys step by 0.1 at once.
  const commitGamma = (text: string) => {
    setGammaText(null)
    const g = Number(text.trim())
    if (text.trim() !== "" && Number.isFinite(g)) {
      const next = Math.min(GAMMA_LIMITS[1], Math.max(GAMMA_LIMITS[0], g))
      if (next !== gamma) change({ gamma: next })
    }
  }
  const stepGamma = (sign: number) => {
    const g = Math.round((gamma + sign * 0.1) * 100) / 100
    setGammaText(null)
    change({ gamma: Math.min(GAMMA_LIMITS[1], Math.max(GAMMA_LIMITS[0], g)) })
  }

  const label = (v: number) =>
    formatValue(
      v,
      kind === "log" ? Math.abs(v) : hi - lo,
      dist?.integer ?? false
    )
  const minText = ready ? label(lo) : ""
  const maxText = ready ? label(hi) : ""
  const [lMin, lMax] = labelPositions(
    xMin,
    xMax,
    labelWidth(minText),
    labelWidth(maxText),
    width
  )
  const ticks = ready
    ? axisTicks(axis, kind, Math.max(2, Math.floor(width / 80)))
    : null
  const note =
    ready && value.scale === "log" && kind !== "log"
      ? noPositive
        ? "No positive values: Log is shown linear"
        : "The minimum is not positive: Log is shown linear"
      : null
  const message = error
    ? `The histogram could not be computed (${error})`
    : dist === null
      ? "Computing the histogram…"
      : dist.n === 0
        ? "No values"
        : null

  return (
    <div
      className={cn("flex w-full min-w-48 flex-col gap-1.5 text-xs", className)}
    >
      {title && <div className="text-muted-foreground">{title}</div>}
      <div
        ref={barRef}
        className="relative touch-none select-none"
        style={{ height: TICKS_TOP + 14 }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
      >
        {message ? (
          <div
            role={error ? "alert" : undefined}
            className={cn(
              "absolute inset-0 flex items-center justify-center gap-2 text-center",
              error ? "text-destructive" : "text-muted-foreground"
            )}
          >
            <p>{message}</p>
            {error && onRetry && (
              <Button size="xs" variant="outline" onClick={onRetry}>
                Retry
              </Button>
            )}
          </div>
        ) : (
          <>
            <svg
              className="absolute left-0 w-full text-muted-foreground"
              style={{ top: BAR_TOP, height: HIST_HEIGHT }}
              viewBox={`0 0 ${String(Math.max(1, width))} ${String(HIST_HEIGHT)}`}
              preserveAspectRatio="none"
              aria-hidden
            >
              <defs>
                <clipPath id={`${id}-range`}>
                  <rect
                    x={Math.min(xMin, xMax)}
                    y={0}
                    width={Math.abs(xMax - xMin)}
                    height={HIST_HEIGHT}
                  />
                </clipPath>
              </defs>
              <path d={histogram} fill="currentColor" opacity={0.3} />
              <path
                d={histogram}
                className="text-foreground"
                fill="currentColor"
                opacity={0.65}
                clipPath={`url(#${id}-range)`}
              />
            </svg>
            <canvas
              ref={stripRef}
              className="absolute left-0 w-full rounded-[2px]"
              style={{ top: BAR_TOP + HIST_HEIGHT, height: STRIP_HEIGHT }}
              aria-hidden
            />
            {(["min", "max"] as const).map((which) => {
              const x = which === "min" ? xMin : xMax
              const v = which === "min" ? lo : hi
              const name = which === "min" ? "Minimum" : "Maximum"
              return (
                <div
                  key={which}
                  data-handle={which}
                  role="slider"
                  tabIndex={0}
                  aria-label={name}
                  aria-valuemin={axis?.[0]}
                  aria-valuemax={axis?.[1]}
                  aria-valuenow={v}
                  aria-valuetext={which === "min" ? minText : maxText}
                  title={`${name}: drag, or double-click to type a value`}
                  className="group absolute -translate-x-1/2 cursor-ew-resize rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  style={{
                    left: Math.min(width, Math.max(0, x)),
                    top: BAR_TOP - 2,
                    width: 14,
                    height: HIST_HEIGHT + STRIP_HEIGHT + 6,
                  }}
                  onDoubleClick={() => {
                    setEditing(which)
                  }}
                  onKeyDown={(e) => {
                    onHandleKey(which, e)
                  }}
                >
                  <div className="absolute inset-y-0 left-1/2 w-0.5 -translate-x-1/2 bg-foreground" />
                  <div
                    className={cn(
                      "absolute left-1/2 size-2.5 -translate-x-1/2 rounded-full border-2 border-foreground bg-background shadow-sm transition-transform group-hover:scale-125",
                      drag?.which === which && "scale-125"
                    )}
                    style={{ top: HIST_HEIGHT + STRIP_HEIGHT / 2 - 3 }}
                  />
                </div>
              )
            })}
            {(["min", "max"] as const).map((which) =>
              editing === which ? (
                <input
                  key={which}
                  aria-label={
                    which === "min" ? "Minimum value" : "Maximum value"
                  }
                  // The exact value, to read or replace.
                  defaultValue={String(which === "min" ? lo : hi)}
                  autoFocus
                  inputMode="decimal"
                  className="absolute z-10 h-4.5 w-32 rounded-sm border border-input bg-background px-1 font-mono text-xs tabular-nums outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  style={{
                    top: LABEL_TOP - 1,
                    left: Math.min(
                      Math.max(0, width - 128),
                      which === "min" ? lMin : lMax
                    ),
                  }}
                  onPointerDown={(e) => {
                    e.stopPropagation()
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") e.currentTarget.blur()
                    if (e.key === "Escape") {
                      cancelEdit.current = true
                      e.currentTarget.blur()
                    }
                  }}
                  onBlur={(e) => {
                    commitEdit(which, e.currentTarget.value)
                  }}
                />
              ) : (
                <span
                  key={which}
                  data-label
                  className="absolute cursor-text rounded-sm bg-muted px-1 font-mono leading-4 text-foreground tabular-nums"
                  style={{
                    top: LABEL_TOP,
                    left: which === "min" ? lMin : lMax,
                  }}
                  title="Double-click to type a value"
                  onDoubleClick={() => {
                    setEditing(which)
                  }}
                >
                  {which === "min" ? minText : maxText}
                </span>
              )
            )}
            {ticks?.values.map((t, i) => {
              const x = valueToPosition(t, axis ?? [0, 1], kind, width)
              return (
                <span
                  key={t}
                  className="pointer-events-none absolute -translate-x-1/2 text-[10px] leading-3 text-muted-foreground tabular-nums"
                  style={{ top: TICKS_TOP, left: x }}
                >
                  {ticks.labels[i]}
                </span>
              )
            })}
          </>
        )}
      </div>
      {note && <p className="text-muted-foreground">{note}</p>}
      <div className="flex flex-wrap items-center gap-1.5">
        <Select
          items={SCALE_LABELS}
          value={value.scale}
          onValueChange={(next) => {
            if (next) change({ scale: next })
          }}
        >
          <SelectTrigger size="sm" aria-label="Scale" className="w-30">
            <SelectValue />
          </SelectTrigger>
          {/* As wide as its items: the disabled Log says why. */}
          <SelectContent className="w-auto">
            {SCALE_KINDS.map((k) =>
              k === "log" && noPositive ? (
                <SelectItem key={k} value={k} disabled>
                  {SCALE_LABELS[k]} (no positive values)
                </SelectItem>
              ) : (
                <SelectItem key={k} value={k}>
                  {SCALE_LABELS[k]}
                </SelectItem>
              )
            )}
          </SelectContent>
        </Select>
        {value.scale === "gamma" && (
          <input
            aria-label="Gamma"
            title={`Gamma (t = linear^γ), ${String(GAMMA_LIMITS[0])} to ${String(GAMMA_LIMITS[1])}: type a value and press Enter, or use the arrow keys`}
            inputMode="decimal"
            value={gammaText ?? String(gamma)}
            className="h-7 w-16 rounded-md border border-input bg-transparent px-1.5 text-xs tabular-nums outline-none focus-visible:ring-2 focus-visible:ring-ring dark:bg-input/30"
            onChange={(e) => {
              setGammaText(e.currentTarget.value)
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") commitGamma(e.currentTarget.value)
              else if (e.key === "Escape") setGammaText(null)
              else if (e.key === "ArrowUp" || e.key === "ArrowDown") {
                e.preventDefault()
                stepGamma(e.key === "ArrowUp" ? 1 : -1)
              }
            }}
            onBlur={(e) => {
              commitGamma(e.currentTarget.value)
            }}
          />
        )}
        <Select
          items={AUTO_LABELS}
          value={value.auto ?? "manual"}
          onValueChange={(next) => {
            if (next && next !== "manual") change({ auto: next as AutoRangeId })
          }}
        >
          <SelectTrigger
            size="sm"
            aria-label="Automatic range"
            className="w-32"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {AUTO_RANGES.map((r) => (
              <SelectItem key={r.id} value={r.id}>
                {r.label}
              </SelectItem>
            ))}
            <SelectItem value="manual" disabled>
              Manual
            </SelectItem>
          </SelectContent>
        </Select>
        <div className="ml-auto flex items-center gap-0.5">
          <Button
            size="icon-xs"
            variant={axisMode === "data" ? "secondary" : "ghost"}
            aria-pressed={axisMode === "data"}
            aria-label="Show the whole data range"
            title={
              axisMode === "data"
                ? "Showing the whole data range (click to zoom to the colour range)"
                : "Show the whole data range on the bar"
            }
            onClick={() => {
              setAxisMode(axisMode === "data" ? "fit" : "data")
            }}
          >
            {axisMode === "data" ? (
              <FoldHorizontalIcon />
            ) : (
              <UnfoldHorizontalIcon />
            )}
          </Button>
          <Button
            size="xs"
            variant="ghost"
            title={`Reset the range (${AUTO_LABELS[defaultAuto] ?? defaultAuto})`}
            disabled={value.auto === defaultAuto}
            onClick={() => {
              change({ auto: defaultAuto })
            }}
          >
            <RotateCcwIcon />
            Reset
          </Button>
        </div>
      </div>
    </div>
  )
}
