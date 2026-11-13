// The indexing viewer's binary products: the colour map and the pole figure, and the detector
// frame. The first two are one Arrow IPC stream (one record batch) whose schema metadata `product`
// is a JSON object; the frame is raw pixels with `X-Frame-Shape` and `X-Frame-Dtype`. The hooks
// fetch them with the scope the caller passes (`useScopeParams()`; the frame takes none), decode
// them once into typed arrays, and revalidate them by their ETag: a 304 keeps the decoded product
// (the same object, so the views neither re-pack nor re-stamp nor recolour it).
import {
  useQuery,
  useQueryClient,
  type Query,
  type QueryCache,
  type QueryKey,
  type UseQueryResult,
} from "@tanstack/react-query"
import { tableFromIPC, type Table, type Vector } from "apache-arrow"

import { decodeFrame, type Frame } from "@/viz/heatmap/frame"

import { apiRevalidate, type ErrorType } from "./fetcher"
import {
  getGetCalibrationFrameQueryKey,
  getGetCalibrationFrameUrl,
  getGetIndexingDetectorImageQueryKey,
  getGetIndexingDetectorImageUrl,
  getGetIndexingMapQueryKey,
  getGetIndexingMapUrl,
  getGetIndexingPoleFigureQueryKey,
  getGetIndexingPoleFigureUrl,
  getGetReconstructionFrameQueryKey,
  getGetReconstructionFrameUrl,
  getGetReconstructionReferenceQueryKey,
  getGetReconstructionReferenceUrl,
} from "./generated/endpoints"
import type {
  GetIndexingMapParams,
  GetIndexingPoleFigureParams,
  MapColor,
  OrientationSymmetry,
  Palette,
  ReconstructionReferenceKind,
} from "./generated/model"
import type { RetryRule } from "./roi-traces"

const ARROW_STREAM = "application/vnd.apache.arrow.stream"
const OCTET_STREAM = "application/octet-stream"
const METADATA_KEY = "product"

/** The map's metadata (`product`): what the colours are and how to label the plot. */
export interface MapProductMeta {
  color: MapColor
  color_kind: "rgb" | "scalar"
  color_label: string
  /** lauelab's axis names, as requested. */
  axes: string[]
  axis_labels: string[]
  /** The axes are lengths in one unit: the plot may lock its aspect. */
  spatial_axes: boolean
  /** The symmetry `rodrigues` and `misorientation` applied, else null. */
  symmetry: OrientationSymmetry | null
  /** Scalar modes only (echoed: the browser maps the values). */
  palette?: Palette
  reverse?: boolean
  range?: [number, number] | null
  /** The data's finite range (null without values). */
  auto_range?: [number, number] | null
}

/** The pole figure's metadata. */
export interface PoleFigureProductMeta {
  hkl: [number, number, number]
  scheme: "hsv" | "ipf" | "uniform"
  color_kind: "rgb" | "uniform"
  /** The HSV scheme's colour radius, in stereographic units. */
  color_radius: number | null
  center: [number, number]
}

/** The columns every product has: one entry a record. */
interface Records {
  length: number
  x: Float64Array
  y: Float64Array
  /** The frame's identity (an integer, or text for results that name frames by text). */
  frameId: readonly (number | string)[]
  /** The pattern's rank in its frame; -1 for a frame-only record. */
  patternIndex: Int32Array
  step: Int32Array
}

export type ProductColors =
  | { kind: "rgb"; r: Uint8Array; g: Uint8Array; b: Uint8Array }
  /** A scalar mode's values (NaN without one); the page maps them through its colorbar. */
  | { kind: "scalar"; values: Float64Array }

export interface MapProduct extends Records {
  meta: MapProductMeta
  /** With three axes. */
  z: Float64Array | null
  colors: ProductColors
  /** 1 for a pattern with a finite orientation, else 0 (the not-indexed style). */
  indexed: Uint8Array
}

export interface PoleFigureProduct extends Records {
  meta: PoleFigureProductMeta
  r: Uint8Array
  g: Uint8Array
  b: Uint8Array
}

function column(table: Table, name: string): Vector {
  const vector = table.getChild(name)
  if (!vector) throw new Error(`The product has no column "${name}"`)
  // The API writes no nulls (NaN marks a missing value): a null would read as 0, a point drawn at
  // the origin, so a product with one fails (and the tab keeps its last good map) instead.
  if (vector.nullCount > 0) {
    throw new Error(`The product's column "${name}" has null values`)
  }
  return vector
}

function typed<T extends ArrayBufferView>(
  table: Table,
  name: string,
  type: new (length: number) => T
): T {
  const values: unknown = column(table, name).toArray()
  if (!(values instanceof type)) {
    throw new Error(`The product's column "${name}" has an unexpected type`)
  }
  return values
}

function frameIds(table: Table): (number | string)[] {
  const vector = column(table, "frame_id")
  const out = new Array<number | string>(vector.length)
  for (let i = 0; i < vector.length; i++) {
    const value: unknown = vector.get(i)
    out[i] =
      typeof value === "bigint"
        ? Number(value)
        : typeof value === "number"
          ? value
          : String(value)
  }
  return out
}

function flags(table: Table, name: string): Uint8Array {
  const vector = column(table, name)
  const out = new Uint8Array(vector.length)
  for (let i = 0; i < vector.length; i++) out[i] = vector.get(i) ? 1 : 0
  return out
}

function metadata(table: Table): unknown {
  const text = table.schema.metadata.get(METADATA_KEY)
  if (text === undefined) throw new Error("The product has no metadata")
  return JSON.parse(text)
}

function records(table: Table): Records {
  return {
    length: table.numRows,
    x: typed(table, "x", Float64Array),
    y: typed(table, "y", Float64Array),
    frameId: frameIds(table),
    patternIndex: typed(table, "pattern_index", Int32Array),
    step: typed(table, "step", Int32Array),
  }
}

/** The colour map's Arrow stream as typed arrays and its metadata. */
export function decodeMapProduct(bytes: Uint8Array): MapProduct {
  const table = tableFromIPC(bytes)
  const meta = metadata(table) as MapProductMeta
  return {
    ...records(table),
    meta,
    z: table.getChild("z") ? typed(table, "z", Float64Array) : null,
    colors:
      meta.color_kind === "scalar"
        ? { kind: "scalar", values: typed(table, "value", Float64Array) }
        : {
            kind: "rgb",
            r: typed(table, "r", Uint8Array),
            g: typed(table, "g", Uint8Array),
            b: typed(table, "b", Uint8Array),
          },
    indexed: flags(table, "indexed"),
  }
}

/** The pole figure's Arrow stream as typed arrays and its metadata. */
export function decodePoleFigureProduct(bytes: Uint8Array): PoleFigureProduct {
  const table = tableFromIPC(bytes)
  return {
    ...records(table),
    meta: metadata(table) as PoleFigureProductMeta,
    r: typed(table, "r", Uint8Array),
    g: typed(table, "g", Uint8Array),
    b: typed(table, "b", Uint8Array),
  }
}

/** A decoded product with the ETag it was sent with. */
export interface Tagged<T> {
  etag: string | null
  product: T
}

/** Decodes a product's body, read with its response's headers. */
type Decode<T> = (bytes: Uint8Array, headers: Headers) => T

/**
 * Fetch a product, revalidating the copy held (`previous`): a 304 returns that copy itself; a 200 is
 * decoded. Exported for tests; the hooks below use it.
 */
export async function fetchProduct<T>(
  url: string,
  decode: Decode<T>,
  previous: Tagged<T> | undefined,
  signal?: AbortSignal,
  accept: string = ARROW_STREAM
): Promise<Tagged<T>> {
  const response = await apiRevalidate(url, previous?.etag ?? null, {
    signal,
    headers: { Accept: accept },
  })
  if (response.status === 304 && previous) return previous
  const bytes = new Uint8Array(await response.arrayBuffer())
  return {
    etag: response.headers.get("etag"),
    product: decode(bytes, response.headers),
  }
}

/** A detector frame's body read with its `X-Frame-Shape` and `X-Frame-Dtype` (uint16 or float32). */
export function decodeDetectorFrame(
  bytes: Uint8Array,
  headers: Headers
): Frame {
  const { buffer, byteOffset, byteLength } = bytes
  // The fetched body is the whole buffer: no copy of its megabytes.
  const body =
    buffer instanceof ArrayBuffer &&
    byteOffset === 0 &&
    byteLength === buffer.byteLength
      ? buffer
      : bytes.slice().buffer
  return decodeFrame(
    body,
    headers.get("x-frame-shape"),
    headers.get("x-frame-dtype")
  )
}

function useProduct<T>(
  queryKey: QueryKey,
  url: string,
  decode: Decode<T>,
  options: {
    enabled: boolean
    accept?: string
    /** A frame (8-16 MiB): dropped a minute after no view shows it, and bounded in number (`trimFrameCache`). */
    frame?: boolean
    staleTime?: number
    /** Retries (a capped route's busy answers); none by default. */
    rule?: RetryRule
  }
): UseQueryResult<T, ErrorType<unknown>> {
  const queryClient = useQueryClient()
  if (options.frame === true) watchFrameUse(queryClient.getQueryCache())
  return useQuery({
    queryKey,
    queryFn: async ({ signal }) => {
      const tagged = await fetchProduct(
        url,
        decode,
        queryClient.getQueryData<Tagged<T>>(queryKey),
        signal,
        options.accept
      )
      if (options.frame === true) trimFrameCache(queryClient.getQueryCache())
      return tagged
    },
    select: (tagged: Tagged<T>) => tagged.product,
    enabled: options.enabled,
    // A failed product is shown as stale at once: no silent retries.
    retry: false,
    ...options.rule,
    ...(options.frame === true ? { gcTime: FRAME_GC_TIME_MS } : {}),
    ...(options.staleTime === undefined
      ? {}
      : { staleTime: options.staleTime }),
  })
}

/** A point's depth frame (the wire viewer's Depth View) with its depth, `X-Depth-Um` (null without one). */
export interface DepthFrame {
  index: number
  depthUm: number | null
  frame: Frame
}

function depthOf(value: string | null): number | null {
  if (value === null || value.trim() === "") return null
  const depth = Number(value)
  return Number.isFinite(depth) ? depth : null
}

/**
 * How long a detector frame no view shows stays cached: a frame is 8 MiB (2048² uint16; 16 MiB in
 * float32), so stepping through a run must not keep every one for React Query's default 5 minutes.
 * A step left within a minute shows again at once; an older one is fetched again (a 200).
 */
const FRAME_GC_TIME_MS = 60_000

/**
 * How many frames no view shows stay cached at most (detector frames, depth frames and reference
 * images alike): stepping through a point's depths within the minute would otherwise keep every one
 * (101 depths × 8 MiB on R2). Eight, the ones used last, keep the depths just left for stepping back
 * (64-128 MiB at most).
 */
export const FRAME_CACHE_LIMIT = 8

/** When each cached query was last shown: stamped as its last view leaves it (`watchFrameUse`). */
const lastShown = new WeakMap<object, number>()
const watchedCaches = new WeakSet<QueryCache>()

/** Stamps a query when a view stops showing it, so `trimFrameCache` keeps the frames used last. */
export function watchFrameUse(cache: QueryCache): void {
  if (watchedCaches.has(cache)) return
  watchedCaches.add(cache)
  cache.subscribe((event) => {
    if (event.type === "observerRemoved") lastShown.set(event.query, Date.now())
  })
}

/**
 * Drops the cached frames no view shows beyond the `limit` used last (shown, or else fetched, most
 * recently; each new frame calls it), so stepping back over the depths just left stays instant; a
 * frame a view shows is never dropped.
 */
export function trimFrameCache(
  cache: QueryCache,
  limit: number = FRAME_CACHE_LIMIT
): void {
  watchFrameUse(cache)
  const used = (query: Query) =>
    Math.max(lastShown.get(query) ?? 0, query.state.dataUpdatedAt)
  const idle = cache
    .findAll({
      predicate: (query) =>
        query.queryKey.at(-1) === "frame" &&
        query.getObserversCount() === 0 &&
        query.state.data !== undefined,
    })
    .sort((a, b) => used(b) - used(a))
  for (const query of idle.slice(limit)) cache.remove(query)
}

/**
 * The colour map (`GET …/map`), decoded. `params` carries the scope (`useScopeParams()`) and the
 * colour settings; its query key is the generated one plus "arrow" (the cache holds decoded data).
 */
export function useMapProduct(
  indexingId: number,
  params: GetIndexingMapParams,
  options: { enabled?: boolean } = {}
): UseQueryResult<MapProduct, ErrorType<unknown>> {
  return useProduct(
    [...getGetIndexingMapQueryKey(indexingId, params), "arrow"],
    getGetIndexingMapUrl(indexingId, params),
    decodeMapProduct,
    { enabled: options.enabled ?? true }
  )
}

/** The pole figure (`GET …/pole-figure`), decoded. */
export function usePoleFigureProduct(
  indexingId: number,
  params: GetIndexingPoleFigureParams,
  options: { enabled?: boolean } = {}
): UseQueryResult<PoleFigureProduct, ErrorType<unknown>> {
  return useProduct(
    [...getGetIndexingPoleFigureQueryKey(indexingId, params), "arrow"],
    getGetIndexingPoleFigureUrl(indexingId, params),
    decodePoleFigureProduct,
    { enabled: options.enabled ?? true }
  )
}

/**
 * A step's detector frame (`GET …/detector/{step}/image`), decoded (uint16 or float32). It takes no
 * scope. A frame that is not on this host is a 404 "Detector image not found" whose detail says
 * where it was looked for and why each path was not opened. The route is capped (a 503 while many
 * wait for a turn): the caller passes its retry rule for busy answers.
 */
export function useDetectorFrame(
  indexingId: number,
  step: number,
  options: { enabled?: boolean; rule?: RetryRule } = {}
): UseQueryResult<Frame, ErrorType<unknown>> {
  return useProduct(
    [...getGetIndexingDetectorImageQueryKey(indexingId, step), "frame"],
    getGetIndexingDetectorImageUrl(indexingId, step),
    decodeDetectorFrame,
    {
      enabled: options.enabled ?? true,
      accept: OCTET_STREAM,
      frame: true,
      rule: options.rule,
    }
  )
}

/**
 * A calibration frame (`GET /calibration/frame?path=`), decoded: the Calibration page's image. Revalidated
 * by its ETag (the file's identity) and dropped a minute after no view shows it, as the detector frames.
 */
export function useCalibrationFrame(
  path: string,
  options: { enabled?: boolean; rule?: RetryRule } = {}
): UseQueryResult<Frame, ErrorType<unknown>> {
  return useProduct(
    [...getGetCalibrationFrameQueryKey({ path }), "frame"],
    getGetCalibrationFrameUrl({ path }),
    decodeDetectorFrame,
    {
      enabled: (options.enabled ?? true) && path !== "",
      accept: OCTET_STREAM,
      frame: true,
      rule: options.rule,
    }
  )
}

/**
 * A complete point's depth frame (`GET …/points/{point}/frames/{depth_index}`), decoded, with its
 * depth. The generated URL takes the point ID as it is, so it is encoded here. A finished point's
 * frame never changes (its ETag is the point file's identity): one already fetched is not asked for
 * again while it is cached (a minute after no view shows it, at most `FRAME_CACHE_LIMIT` of them, those used last, as
 * the detector frames), and a request
 * no view waits for any more (a superseded depth) is aborted. The route is capped: the caller passes
 * its retry rule for busy answers.
 */
export function useReconstructionFrame(
  reconstructionId: number,
  pointId: string,
  depthIndex: number,
  options: { enabled?: boolean; rule?: RetryRule } = {}
): UseQueryResult<DepthFrame, ErrorType<unknown>> {
  const point = encodeURIComponent(pointId)
  return useProduct(
    [
      ...getGetReconstructionFrameQueryKey(reconstructionId, point, depthIndex),
      "frame",
    ],
    getGetReconstructionFrameUrl(reconstructionId, point, depthIndex),
    (bytes, headers) => ({
      index: depthIndex,
      depthUm: depthOf(headers.get("x-depth-um")),
      frame: decodeDetectorFrame(bytes, headers),
    }),
    {
      enabled: options.enabled ?? true,
      accept: OCTET_STREAM,
      frame: true,
      staleTime: Infinity,
      rule: options.rule,
    }
  )
}

/**
 * A complete point's reference image for the ROI inspector (`GET …/points/{point}/reference?kind=`),
 * decoded as a frame (float32 for a sum). The point ID is encoded here, as for its frames. A finished
 * point's reference never changes: one fetched is not asked for again while cached (a minute after no
 * view shows it). The route is capped: the caller passes its retry rule for busy answers.
 */
export function useReconstructionReference(
  reconstructionId: number,
  pointId: string,
  kind: ReconstructionReferenceKind,
  options: { enabled?: boolean; rule?: RetryRule } = {}
): UseQueryResult<Frame, ErrorType<unknown>> {
  const point = encodeURIComponent(pointId)
  return useProduct(
    [
      ...getGetReconstructionReferenceQueryKey(reconstructionId, point, {
        kind,
      }),
      "frame",
    ],
    getGetReconstructionReferenceUrl(reconstructionId, point, { kind }),
    decodeDetectorFrame,
    {
      enabled: options.enabled ?? true,
      accept: OCTET_STREAM,
      frame: true,
      staleTime: Infinity,
      rule: options.rule,
    }
  )
}
