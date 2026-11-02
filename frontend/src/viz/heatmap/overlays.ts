// The detector view's overlay layer: detected peaks, the indexed peaks at their predicted positions
// with hkl labels, and the simulated reflections a pattern did not index. Styles follow lauelab's
// plot_detector_view: cyan open squares, red crosses and purple open triangles, the last two 4 px
// larger than the marker size.
import type { DetectorOverlays } from "@/api"

export type OverlayKind = "detected" | "indexed" | "simulated"

export interface OverlayLayer {
  kind: OverlayKind
  /** Image pixels (centres at integers, y down). */
  x: Float64Array
  y: Float64Array
  /** One "(h k l)" per mark, or null without labels. */
  labels: readonly string[] | null
}

/**
 * A square or rectangle of image pixels outlined in its colour (the wire viewer's ROIs): half-open
 * `[y0, y1, x0, x1]`, rows y0 to y1 - 1 and columns x0 to x1 - 1, as the API's ROI bounds.
 */
export interface OverlayBox {
  bounds: readonly [number, number, number, number]
  color: string
  /** A short name drawn by the outline's top-left corner (the ROI's number). */
  label?: string
}

export const OVERLAY_STYLES: Record<
  OverlayKind,
  { color: string; shape: "square" | "cross" | "triangle"; grow: number }
> = {
  detected: { color: "rgb(0, 180, 210)", shape: "square", grow: 0 },
  indexed: { color: "rgb(220, 50, 47)", shape: "cross", grow: 4 },
  simulated: { color: "rgb(128, 0, 128)", shape: "triangle", grow: 4 },
}

export interface OverlayOptions {
  detected: boolean
  indexed: boolean
  simulated: boolean
  /** hkl labels on the indexed and simulated marks. */
  labels: boolean
  /** The patterns (`pattern_index`) whose indexed peaks and reflections show; null for all. */
  patterns?: ReadonlySet<number> | null
}

export function hklLabel(h: number, k: number, l: number): string {
  return `(${String(h)} ${String(k)} ${String(l)})`
}

/** The visible layers, drawn in this order (detected underneath). */
export function overlayLayers(
  overlays: DetectorOverlays,
  options: OverlayOptions
): OverlayLayer[] {
  const layers: OverlayLayer[] = []
  const { width, height } = overlays
  const shown = overlays.patterns.filter(
    (p) => !options.patterns || options.patterns.has(p.pattern_index)
  )
  if (options.detected) {
    layers.push(layer("detected", overlays.detected, null))
  }
  if (options.indexed) {
    // As lauelab: only the predictions that fall on the frame.
    const peaks = shown.flatMap((p) =>
      p.peaks.filter(
        (q) =>
          q.x !== null &&
          q.y !== null &&
          q.x >= 0 &&
          q.x <= width - 1 &&
          q.y >= 0 &&
          q.y <= height - 1
      )
    )
    layers.push(layer("indexed", peaks, options.labels))
  }
  if (options.simulated) {
    layers.push(
      layer(
        "simulated",
        shown.flatMap((p) => p.simulated ?? []),
        options.labels
      )
    )
  }
  return layers
}

function layer(
  kind: OverlayKind,
  marks: readonly {
    x: number | null
    y: number | null
    h?: number
    k?: number
    l?: number
  }[],
  labels: boolean | null
): OverlayLayer {
  const kept = marks.filter((m) => Number.isFinite(m.x) && Number.isFinite(m.y))
  return {
    kind,
    x: Float64Array.from(kept, (m) => m.x ?? NaN),
    y: Float64Array.from(kept, (m) => m.y ?? NaN),
    labels: labels
      ? kept.map((m) => hklLabel(m.h ?? 0, m.k ?? 0, m.l ?? 0))
      : null,
  }
}
