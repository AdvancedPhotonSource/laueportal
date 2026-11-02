// The wire reconstruction viewer's URL: `tab`, `point` and `depth`, each only when it is not the
// default (the Parameters tab; the first complete point; the point's brightest depth). Values arrive
// as strings (src/lib/search.ts); an invalid one falls back to the default silently. `link` carries
// "Copy link to this view"'s packed state: applied once on open, then removed with replace.
import { notFound } from "@tanstack/react-router"
import type { QueryClient } from "@tanstack/react-query"

import { ApiError, getGetReconstructionRunQueryOptions } from "@/api"

export const RECONSTRUCTION_TABS = ["params", "points", "depth", "roi"] as const
export type ReconstructionTab = (typeof RECONSTRUCTION_TABS)[number]

export const TAB_LABELS: Record<ReconstructionTab, string> = {
  params: "Parameters",
  points: "Points",
  depth: "Depth View",
  roi: "ROI Inspector",
}

export function isReconstructionTab(
  value: unknown
): value is ReconstructionTab {
  return RECONSTRUCTION_TABS.some((tab) => tab === value)
}

export const DEFAULT_TAB = "params" satisfies ReconstructionTab

/** The longest point ID the URL takes (lauelab's IDs are file stems). */
export const MAX_POINT_ID_LENGTH = 255

/** The search as the URL holds it: only values other than the defaults. */
export interface ReconstructionSearch {
  tab?: Exclude<ReconstructionTab, "params">
  /** The selected point's ID; the first complete point (the default) is not written. */
  point?: string
  /** The Depth View's depth index; the point's brightest depth (the default) is not written. */
  depth?: number
  /** A packed "Copy link to this view" state, applied on open and then removed. */
  link?: string
}

/** Back and forward change only these (they add history entries; the others, the depth, replace). */
export const HISTORY_KEYS = ["tab", "point"] as const

/** A point ID the URL can name: a non-empty string of at most `MAX_POINT_ID_LENGTH` characters. */
export function isPointId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value !== "" &&
    value.length <= MAX_POINT_ID_LENGTH
  )
}

/** `validateSearch` of /reconstructions/$reconstructionId: each value checked, defaults dropped. */
export function validateReconstructionSearch(
  search: Record<string, unknown>
): ReconstructionSearch {
  const { tab, point, depth, link } = search
  return {
    tab: isReconstructionTab(tab) && tab !== DEFAULT_TAB ? tab : undefined,
    point: isPointId(point) ? point : undefined,
    depth: depthIndex(depth),
    link: typeof link === "string" && link !== "" ? link : undefined,
  }
}

/** A depth index: a non-negative integer (a number, or its digits as the URL holds them). */
function depthIndex(value: unknown): number | undefined {
  const number =
    typeof value === "string" && /^\d+$/.test(value) ? Number(value) : value
  return typeof number === "number" &&
    Number.isSafeInteger(number) &&
    number >= 0
    ? number
    : undefined
}

/**
 * The view a search shows, every value resolved (`point` and `depth` stay undefined for their
 * defaults).
 */
export interface ReconstructionView {
  tab: ReconstructionTab
  point: string | undefined
  depth: number | undefined
}

export function viewOf(search: ReconstructionSearch): ReconstructionView {
  return {
    tab: search.tab ?? DEFAULT_TAB,
    point: search.point,
    depth: search.depth,
  }
}

/**
 * The search change for a change of the view (defaults leave the URL). `defaultPoint` is the first
 * complete point, `defaultDepth` the point's brightest depth: neither is written. Another point
 * opens at its own brightest depth, so a point's change drops the depth.
 */
export function searchPatch(
  change: Partial<ReconstructionView>,
  defaultPoint?: string,
  defaultDepth?: number
): Partial<ReconstructionSearch> {
  const patch: Partial<ReconstructionSearch> = {}
  if ("tab" in change) {
    patch.tab = change.tab === DEFAULT_TAB ? undefined : change.tab
  }
  if ("point" in change) {
    patch.point = change.point === defaultPoint ? undefined : change.point
    patch.depth = undefined
  }
  if ("depth" in change) {
    patch.depth = change.depth === defaultDepth ? undefined : change.depth
  }
  return patch
}

/** The route's reconstruction ID, or undefined for a path that is not one. */
export function parseReconstructionId(text: string): number | undefined {
  if (!/^\d+$/.test(text)) return undefined
  const id = Number(text)
  return Number.isSafeInteger(id) ? id : undefined
}

/**
 * The route's loader: the run (GET /reconstruction-runs/{id}: its job and stored parameters) into the
 * query cache, or notFound() for an ID that is not one or a run the API does not know (404). Other
 * errors go to the route's error boundary.
 */
export async function loadReconstructionRun(
  queryClient: QueryClient,
  reconstructionId: string
): Promise<void> {
  const id = parseReconstructionId(reconstructionId)
  if (id === undefined) throw notFound()
  const { queryKey, queryFn } = getGetReconstructionRunQueryOptions(id)
  try {
    await queryClient.query({ queryKey, queryFn, staleTime: "static" })
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) throw notFound()
    throw error
  }
}
