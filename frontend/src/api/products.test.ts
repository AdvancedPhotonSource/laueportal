import {
  Schema,
  Table,
  tableToIPC,
  Utf8,
  Vector,
  vectorFromArray,
} from "apache-arrow"
import { QueryClient, QueryObserver } from "@tanstack/react-query"
import { afterEach, describe, expect, it, vi } from "vitest"

import { ApiError } from "./fetcher"
import {
  decodeDetectorFrame,
  decodeMapProduct,
  decodePoleFigureProduct,
  fetchProduct,
  FRAME_CACHE_LIMIT,
  type MapProduct,
  trimFrameCache,
} from "./products"

/** An Arrow IPC stream as the API writes it: the columns, and the metadata's `product` JSON. */
function arrowStream(
  columns: Record<
    string,
    | Vector
    | readonly unknown[]
    | Float64Array
    | Int32Array
    | Uint8Array
    | BigInt64Array
  >,
  product: object
): Uint8Array {
  const table = new Table(
    Object.fromEntries(
      Object.entries(columns).map(([name, values]) => [
        name,
        values instanceof Vector ? values : vectorFromArray(values),
      ])
    )
  )
  const schema = new Schema(
    table.schema.fields,
    new Map([["product", JSON.stringify(product)]])
  )
  return tableToIPC(new Table(schema, table.batches), "stream")
}

const RGB_META = {
  color: "cubic_ipf",
  color_kind: "rgb",
  color_label: "Cubic IPF",
  axes: ["X", "H"],
  axis_labels: ["X (µm)", "H (µm)"],
  spatial_axes: true,
  symmetry: null,
}

function rgbMap(): Uint8Array {
  return arrowStream(
    {
      x: Float64Array.from([1, 2, 3]),
      y: Float64Array.from([4, 5, 6]),
      r: Uint8Array.from([255, 0, 150]),
      g: Uint8Array.from([0, 255, 150]),
      b: Uint8Array.from([0, 0, 150]),
      frame_id: BigInt64Array.from([10n, 11n, 12n]),
      pattern_index: Int32Array.from([0, 0, -1]),
      step: Int32Array.from([0, 1, 2]),
      indexed: [true, true, false],
    },
    RGB_META
  )
}

describe("decodeMapProduct", () => {
  it("decodes an RGB map into typed arrays, with integer frame IDs and the metadata", () => {
    const map = decodeMapProduct(rgbMap())
    expect(map.length).toBe(3)
    expect(map.meta).toEqual(RGB_META)
    expect(map.x).toBeInstanceOf(Float64Array)
    expect([...map.y]).toEqual([4, 5, 6])
    expect(map.z).toBeNull()
    expect(map.colors.kind).toBe("rgb")
    if (map.colors.kind !== "rgb") return
    expect(map.colors.r).toBeInstanceOf(Uint8Array)
    expect([...map.colors.g]).toEqual([0, 255, 150])
    expect(map.frameId).toEqual([10, 11, 12])
    expect([...map.patternIndex]).toEqual([0, 0, -1])
    expect([...map.step]).toEqual([0, 1, 2])
    // The views compare with 0: booleans would never match.
    expect(map.indexed).toEqual(Uint8Array.from([1, 1, 0]))
  })

  it("refuses a column with null values instead of reading them as 0", () => {
    const stream = arrowStream(
      {
        x: [1, null, 3],
        y: Float64Array.from([4, 5, 6]),
        r: Uint8Array.from([255, 0, 150]),
        g: Uint8Array.from([0, 255, 150]),
        b: Uint8Array.from([0, 0, 150]),
        frame_id: BigInt64Array.from([10n, 11n, 12n]),
        pattern_index: Int32Array.from([0, 0, -1]),
        step: Int32Array.from([0, 1, 2]),
        indexed: [true, true, false],
      },
      RGB_META
    )
    expect(() => decodeMapProduct(stream)).toThrow(
      'The product\'s column "x" has null values'
    )
  })

  it("decodes a scalar map with three axes and text frame IDs", () => {
    const meta = {
      ...RGB_META,
      color: "goodness",
      color_kind: "scalar",
      axes: ["X", "Y", "Z"],
      palette: "viridis",
      reverse: false,
      range: [1, 9],
      auto_range: [1, 9],
    }
    const map = decodeMapProduct(
      arrowStream(
        {
          x: Float64Array.from([1, 2]),
          y: Float64Array.from([3, 4]),
          z: Float64Array.from([5, 6]),
          value: Float64Array.from([1, NaN]),
          frame_id: vectorFromArray(["a_1", "a_2"], new Utf8()),
          pattern_index: Int32Array.from([0, -1]),
          step: Int32Array.from([0, 1]),
          indexed: [true, false],
        },
        meta
      )
    )
    expect(map.meta.range).toEqual([1, 9])
    expect([...(map.z ?? [])]).toEqual([5, 6])
    expect(map.colors.kind).toBe("scalar")
    if (map.colors.kind !== "scalar") return
    expect(map.colors.values).toBeInstanceOf(Float64Array)
    expect(map.colors.values[1]).toBeNaN()
    expect(map.frameId).toEqual(["a_1", "a_2"])
  })
})

describe("decodePoleFigureProduct", () => {
  it("decodes the poles and their metadata", () => {
    const meta = {
      hkl: [1, 0, 0],
      scheme: "hsv",
      color_kind: "rgb",
      color_radius: 0.2,
      center: [0, 0],
    }
    const pole = decodePoleFigureProduct(
      arrowStream(
        {
          x: Float64Array.from([0.1, -0.2]),
          y: Float64Array.from([0.3, 0.4]),
          r: Uint8Array.from([1, 2]),
          g: Uint8Array.from([3, 4]),
          b: Uint8Array.from([5, 6]),
          frame_id: BigInt64Array.from([7n, 7n]),
          pattern_index: Int32Array.from([0, 0]),
          step: Int32Array.from([3, 3]),
        },
        meta
      )
    )
    expect(pole.meta).toEqual(meta)
    expect(pole.length).toBe(2)
    expect([...pole.b]).toEqual([5, 6])
    expect(pole.frameId).toEqual([7, 7])
  })
})

describe("fetchProduct", () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function stubResponses(...responses: Response[]) {
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve(responses.shift() ?? new Response(null, { status: 500 }))
    )
    vi.stubGlobal("fetch", fetchMock)
    return fetchMock
  }

  const arrow = (etag: string) =>
    new Response(rgbMap().slice().buffer, {
      status: 200,
      headers: {
        "content-type": "application/vnd.apache.arrow.stream",
        etag,
      },
    })

  it("revalidates by ETag: a 304 keeps the decoded product itself, a 200 decodes the new one", async () => {
    const fetchMock = stubResponses(
      arrow('W/"a"'),
      new Response(null, { status: 304, headers: { etag: 'W/"a"' } }),
      arrow('W/"b"')
    )
    const url = "/api/v1/indexing-runs/1/map?color=cubic_ipf"
    const first = await fetchProduct(url, decodeMapProduct, undefined)
    expect(first.etag).toBe('W/"a"')
    expect(first.product.length).toBe(3)
    const firstHeaders = new Headers(fetchMock.mock.calls[0]?.[1]?.headers)
    expect(firstHeaders.has("If-None-Match")).toBe(false)
    expect(firstHeaders.get("Accept")).toBe(
      "application/vnd.apache.arrow.stream"
    )

    const same = await fetchProduct(url, decodeMapProduct, first)
    expect(same).toBe(first)
    const second = new Headers(fetchMock.mock.calls[1]?.[1]?.headers)
    expect(second.get("If-None-Match")).toBe('W/"a"')

    const changed = await fetchProduct<MapProduct>(url, decodeMapProduct, first)
    expect(changed).not.toBe(first)
    expect(changed.etag).toBe('W/"b"')
  })

  it("throws the problem of a product that failed", async () => {
    stubResponses(
      Response.json(
        {
          type: "urn:laueportal:problem:product-failed",
          title: "Product failed",
          status: 422,
          detail: "Misorientation coloring needs a reference pattern",
        },
        {
          status: 422,
          headers: { "content-type": "application/problem+json" },
        }
      )
    )
    const error: unknown = await fetchProduct(
      "/api/v1/indexing-runs/1/map",
      decodeMapProduct,
      undefined
    ).catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).problem?.type).toBe(
      "urn:laueportal:problem:product-failed"
    )
    expect((error as ApiError).message).toMatch(/reference pattern/)
  })

  it("reads a detector frame with its shape and type headers", async () => {
    const pixels = Float32Array.from([1.5, -2, 3, 4, 5, 6])
    const fetchMock = stubResponses(
      new Response(pixels.slice().buffer, {
        status: 200,
        headers: {
          "content-type": "application/octet-stream",
          "x-frame-shape": "2,3",
          "x-frame-dtype": "float32",
        },
      })
    )
    const { product } = await fetchProduct(
      "/api/v1/indexing-runs/1/detector/3/image",
      decodeDetectorFrame,
      undefined,
      undefined,
      "application/octet-stream"
    )
    expect(product).toMatchObject({ width: 3, height: 2 })
    expect(product.data).toEqual(pixels)
    const headers = new Headers(fetchMock.mock.calls[0]?.[1]?.headers)
    expect(headers.get("Accept")).toBe("application/octet-stream")
  })
})

describe("trimFrameCache", () => {
  it("keeps the frames a view shows and the most recently fetched others, up to the limit", () => {
    const client = new QueryClient()
    const key = (depth: number) => ["frames", depth, "frame"]
    for (let depth = 0; depth < FRAME_CACHE_LIMIT + 4; depth++) {
      client.setQueryData(key(depth), depth, { updatedAt: 1000 + depth })
    }
    client.setQueryData(["map", "arrow"], "map", { updatedAt: 0 })
    // The oldest frame is on screen.
    const shown = new QueryObserver(client, {
      queryKey: key(0),
      enabled: false,
    })
    const unsubscribe = shown.subscribe(() => undefined)

    trimFrameCache(client.getQueryCache())

    const kept = client
      .getQueryCache()
      .findAll({ predicate: (query) => query.queryKey.at(-1) === "frame" })
      .map((query) => query.queryKey[1] as number)
      .sort((a, b) => a - b)
    expect(kept).toEqual([
      0,
      ...Array.from({ length: FRAME_CACHE_LIMIT }, (_, i) => i + 4),
    ])
    expect(client.getQueryData(["map", "arrow"])).toBe("map")
    unsubscribe()
    client.clear()
  })

  it("keeps the frames used last: stepping 0 → 20, back to 11 and forward to 12 asks for 12 once", async () => {
    let now = 1000
    vi.spyOn(Date, "now").mockImplementation(() => now)
    const client = new QueryClient()
    const fetched: number[] = []
    let leave: (() => void) | undefined
    // As the Depth View steps: the new depth's view subscribes, then the last one's leaves.
    const show = async (depth: number) => {
      now += 10
      const observer = new QueryObserver(client, {
        queryKey: ["frames", depth, "frame"],
        staleTime: Infinity,
        // As useProduct: the frame arrives (a tick later), then the cache is trimmed.
        queryFn: async () => {
          fetched.push(depth)
          await Promise.resolve()
          trimFrameCache(client.getQueryCache())
          return depth
        },
      })
      const unsubscribe = observer.subscribe(() => undefined)
      leave?.()
      leave = unsubscribe
      await vi.waitFor(
        () => {
          expect(observer.getCurrentResult().data).toBe(depth)
        },
        { interval: 1 }
      )
    }
    for (let depth = 0; depth <= 20; depth++) await show(depth)
    for (let depth = 19; depth >= 11; depth--) await show(depth)
    fetched.length = 0
    await show(12)
    expect(fetched).toEqual([])
    leave?.()
    client.clear()
    vi.restoreAllMocks()
  })
})
