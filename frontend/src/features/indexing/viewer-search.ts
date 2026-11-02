// The indexing viewer's URL: `tab`, `step`, `color`, `view` (`3d`; 2d is the default), `hkl` and
// `point`, each only when it is not the default. Values arrive as strings (src/lib/search.ts); an
// invalid one falls back to the default silently. `link` carries "Copy link to this view"'s packed
// state: applied once on open, then removed with replace.
import { notFound } from "@tanstack/react-router"
import type { QueryClient } from "@tanstack/react-query"

import { ApiError, getGetIndexingRunQueryOptions, MapColor } from "@/api"

export const INDEXING_TABS = [
  "params",
  "map",
  "pole",
  "detector",
  "peaks",
  "patterns",
] as const
export type IndexingTab = (typeof INDEXING_TABS)[number]

export const TAB_LABELS: Record<IndexingTab, string> = {
  params: "Parameters",
  map: "Color Map",
  pole: "Pole Figure",
  detector: "Detector View",
  peaks: "Peaks",
  patterns: "Patterns",
}

export function isIndexingTab(value: unknown): value is IndexingTab {
  return INDEXING_TABS.some((tab) => tab === value)
}

export type ViewMode = "2d" | "3d"
export type Hkl = readonly [number, number, number]

export const DEFAULT_TAB = "params" satisfies IndexingTab
export const DEFAULT_COLOR = MapColor.cubic_ipf
export const DEFAULT_VIEW_MODE: ViewMode = "2d"
export const DEFAULT_HKL: Hkl = [1, 0, 0]

/** The search as the URL holds it: only values other than the defaults. */
export interface IndexingSearch {
  tab?: Exclude<IndexingTab, "params">
  /** The detector's step; its default (the summary's first step with a pattern) is not written. */
  step?: number
  color?: Exclude<MapColor, "cubic_ipf">
  /** The map's 3D view (2D is the default). */
  view?: "3d"
  /** `h,k,l`, or three single digits (`110`) when every index has one. */
  hkl?: string
  /** The selected step (the map's click details). */
  point?: number
  /** A packed "Copy link to this view" state, applied on open and then removed. */
  link?: string
}

/** Back and forward change only these (they add history entries; the others replace). */
export const HISTORY_KEYS = ["tab", "step"] as const

function index(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) {
    return value
  }
  if (typeof value !== "string" || !/^\d+$/.test(value)) return undefined
  const number = Number(value)
  return Number.isSafeInteger(number) ? number : undefined
}

/** The HKL a value names (`1,1,0`, `110`, `1,-1,0`, `1-10`), or undefined; not 0,0,0. */
export function parseHkl(value: unknown): Hkl | undefined {
  if (typeof value !== "string") return undefined
  const match =
    /^(-?\d+),(-?\d+),(-?\d+)$/.exec(value) ??
    /^(-?\d)(-?\d)(-?\d)$/.exec(value)
  if (!match) return undefined
  const hkl = [Number(match[1]), Number(match[2]), Number(match[3])] as const
  if (hkl.every((n) => n === 0) || hkl.some((n) => !Number.isSafeInteger(n))) {
    return undefined
  }
  return hkl
}

/** An HKL as the URL writes it: compact while every index is one digit, else comma-separated. */
export function formatHkl(hkl: Hkl): string {
  return hkl.every((n) => n >= -9 && n <= 9) ? hkl.join("") : hkl.join(",")
}

function sameHkl(a: Hkl, b: Hkl): boolean {
  return a[0] === b[0] && a[1] === b[1] && a[2] === b[2]
}

/** `validateSearch` of /indexing/$indexingId: each value converted and checked, defaults dropped. */
export function validateIndexingSearch(
  search: Record<string, unknown>
): IndexingSearch {
  const { tab, step, color, view, hkl, point, link } = search
  const parsedHkl = parseHkl(hkl)
  return {
    tab: isIndexingTab(tab) && tab !== DEFAULT_TAB ? tab : undefined,
    step: index(step),
    color:
      Object.values(MapColor).some((mode) => mode === color) &&
      color !== DEFAULT_COLOR
        ? (color as Exclude<MapColor, "cubic_ipf">)
        : undefined,
    view: view === "3d" ? view : undefined,
    hkl:
      parsedHkl && !sameHkl(parsedHkl, DEFAULT_HKL)
        ? formatHkl(parsedHkl)
        : undefined,
    point: index(point),
    link: typeof link === "string" && link !== "" ? link : undefined,
  }
}

/** The view a search shows, every value resolved (`step` stays undefined for the summary's default). */
export interface IndexingView {
  tab: IndexingTab
  step: number | undefined
  color: MapColor
  mode: ViewMode
  hkl: Hkl
  point: number | undefined
}

export function viewOf(search: IndexingSearch): IndexingView {
  return {
    tab: search.tab ?? DEFAULT_TAB,
    step: search.step,
    color: search.color ?? DEFAULT_COLOR,
    mode: search.view ?? DEFAULT_VIEW_MODE,
    hkl: parseHkl(search.hkl) ?? DEFAULT_HKL,
    point: search.point,
  }
}

/**
 * The search change for a change of the view (defaults leave the URL). `defaultStep` is the summary's
 * default step: that step is not written.
 */
export function searchPatch(
  change: Partial<IndexingView>,
  defaultStep?: number | null
): Partial<IndexingSearch> {
  const patch: Partial<IndexingSearch> = {}
  if ("tab" in change) {
    patch.tab = change.tab === DEFAULT_TAB ? undefined : change.tab
  }
  if ("step" in change) {
    patch.step = change.step === defaultStep ? undefined : change.step
  }
  if ("color" in change) {
    patch.color = change.color === DEFAULT_COLOR ? undefined : change.color
  }
  if ("mode" in change) patch.view = change.mode === "3d" ? "3d" : undefined
  if ("hkl" in change) {
    patch.hkl =
      change.hkl === undefined || sameHkl(change.hkl, DEFAULT_HKL)
        ? undefined
        : formatHkl(change.hkl)
  }
  if ("point" in change) patch.point = change.point
  return patch
}

/** The route's indexing ID, or undefined for a path that is not one. */
export function parseIndexingId(text: string): number | undefined {
  return index(text)
}

/**
 * The route's loader: the run (GET /indexing-runs/{id}: its job and stored parameters) into the query
 * cache, or notFound() for an ID that is not one or a run the API does not know (404). Other errors
 * go to the route's error boundary.
 */
export async function loadIndexingRun(
  queryClient: QueryClient,
  indexingId: string
): Promise<void> {
  const id = parseIndexingId(indexingId)
  if (id === undefined) throw notFound()
  const { queryKey, queryFn } = getGetIndexingRunQueryOptions(id)
  try {
    await queryClient.query({ queryKey, queryFn, staleTime: "static" })
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) throw notFound()
    throw error
  }
}
