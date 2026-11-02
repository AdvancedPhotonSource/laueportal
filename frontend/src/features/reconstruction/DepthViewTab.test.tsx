import { RouterProvider, createMemoryHistory } from "@tanstack/react-router"
import { act } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { Health, ReconstructionPoint, ReconstructionTrace } from "@/api"
import { Providers } from "@/app/Providers"
import { createQueryClient } from "@/app/query-client"
import { createAppRouter } from "@/app/router"
import { checkRow, rowCheckbox, stubGridLayout } from "@/test/grid"
import {
  answerReconstruction,
  makePoint,
  makePoints,
  makeReconstructionRunDetail,
  resetWireViewerStore,
} from "@/test/reconstruction"
import {
  byRole,
  cleanup,
  click,
  render,
  SLOW_TEST,
  stubMatchMedia,
} from "@/test/render"

// The Depth View in the app's router: R1's points (wire_1 and wire_3 complete, 64 × 48 frames of
// 101 depths from -50 to 50 µm), each point's trace (`BRIGHTEST`) and its frames, whose pixels are
// depth × 1000 + y × 48 + x, so the hover readout tells which depth a pixel shows.

const HEALTH: Health = {
  db: "ok",
  worker: "running",
  version: "2.0.0.dev0",
}
const HEIGHT = 64
const WIDTH = 48
const DEPTHS = Array.from({ length: 101 }, (_, i) => i - 50)
const BRIGHTEST: Record<string, number> = { wire_1: 37, wire_3: 12 }

function trace(pointId: string): ReconstructionTrace {
  return {
    reconstruction_id: 1,
    point_id: pointId,
    depth_um: DEPTHS,
    values: DEPTHS.map(() => 1),
    brightest_index: BRIGHTEST[pointId] ?? 0,
  }
}

function frame(depth: number): Response {
  const pixels = Uint16Array.from(
    { length: HEIGHT * WIDTH },
    (_, i) => depth * 1000 + i
  )
  return new Response(pixels.buffer, {
    headers: {
      "content-type": "application/octet-stream",
      "x-frame-shape": `${String(HEIGHT)},${String(WIDTH)}`,
      "x-frame-dtype": "uint16",
      "x-depth-um": String(DEPTHS[depth]),
      etag: `"f${String(depth)}"`,
    },
  })
}

type Answer = (pointId: string) => Response | undefined

/** A capped route's busy answer (a 503 with `Retry-After`, 1 s). */
function busyAnswer(what: string): Response {
  return Response.json(
    {
      title: `Too many ${what} waiting`,
      status: 503,
      detail: `4 ${what} are already waiting.`,
    },
    {
      status: 503,
      headers: {
        "content-type": "application/problem+json",
        "retry-after": "1",
      },
    }
  )
}

/** A check of a file system that did not answer: a 503 of the not-responding type. */
const NOT_RESPONDING_DETAIL =
  "/net/data/rec_1 did not answer within 5 s: its file system may be hung."
function notRespondingAnswer(): Response {
  return Response.json(
    {
      type: "urn:laueportal:problem:not-responding",
      title: "Not responding",
      status: 503,
      detail: NOT_RESPONDING_DETAIL,
    },
    {
      status: 503,
      headers: {
        "content-type": "application/problem+json",
        "retry-after": "30",
      },
    }
  )
}

async function openDepthView(
  url: string,
  {
    points = makePoints,
    traceAnswer = () => undefined,
    frameAnswer = () => undefined,
  }: {
    points?: () => ReconstructionPoint[]
    traceAnswer?: Answer
    frameAnswer?: Answer
  } = {}
) {
  stubMatchMedia()
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined)
  const fetch = vi.fn((input: RequestInfo | URL) => {
    const where = new URL(
      input instanceof Request ? input.url : input.toString(),
      "http://localhost"
    )
    if (where.pathname === "/api/v1/health") {
      return Promise.resolve(Response.json(HEALTH))
    }
    const depth =
      /^\/api\/v1\/reconstruction-runs\/1\/points\/([^/]+)\/(trace|frames\/(\d+))$/.exec(
        where.pathname
      )
    if (depth) {
      const pointId = decodeURIComponent(depth[1] ?? "")
      if (depth[2] === "trace") {
        return Promise.resolve(
          traceAnswer(pointId) ?? Response.json(trace(pointId))
        )
      }
      return Promise.resolve(frameAnswer(pointId) ?? frame(Number(depth[3])))
    }
    const body = answerReconstruction(where, {
      points,
      runs: (id) => (id === 1 ? makeReconstructionRunDetail() : undefined),
    })
    return Promise.resolve(
      body instanceof Response ? body : Response.json(body ?? {})
    )
  })
  vi.stubGlobal("fetch", fetch)
  const queryClient = createQueryClient()
  const history = createMemoryHistory({ initialEntries: [url] })
  const router = createAppRouter(queryClient, history)
  const root = await render(
    <Providers queryClient={queryClient}>
      <RouterProvider router={router} />
    </Providers>
  )
  await act(() => router.load())
  /** The paths the page asked for under R1's points (as sent: encoded). */
  const asked = () =>
    fetch.mock.calls
      .map(([input]) =>
        new URL(
          input instanceof Request ? input.url : input.toString(),
          "http://localhost"
        ).pathname.replace("/api/v1/reconstruction-runs/1/points/", "")
      )
      .filter((path) => /\/(trace|frames\/\d+)$/.test(path))
  return { router, history, root, asked }
}

const readout = () =>
  Array.from(
    document.querySelectorAll('[aria-label="Depth"] dd'),
    (dd) => dd.textContent
  )
const plotStatus = () =>
  document.querySelector('[aria-label="Depth frame"] [role="status"]')
const canvas = () => document.querySelector("canvas[role='img']")
const hoverText = () =>
  document.querySelector('[aria-label="Depth frame"] .font-mono')
    ?.textContent ?? ""

/** The pixel the hover readout names at a point of the plot, and its value. */
async function hover(x: number, y: number) {
  await act(async () => {
    canvas()?.dispatchEvent(
      new MouseEvent("pointermove", { clientX: x, clientY: y, bubbles: true })
    )
    await Promise.resolve()
  })
  const [, px, py, value] =
    /^x (\d+) {2}y (\d+) {2}(\S+)$/.exec(hoverText()) ?? []
  return { pixel: `${px ?? "?"},${py ?? "?"}`, value: Number(value) }
}

/** Waits until the plot shows a depth's frame (no status over it). */
async function shows(depth: number, timeout = 1000) {
  await vi.waitFor(
    () => {
      expect(readout()[0]).toBe(`${String(depth)} (0 … 100)`)
      expect(plotStatus()).toBeNull()
      expect(canvas()).not.toBeNull()
    },
    { timeout }
  )
}

beforeEach(() => {
  stubGridLayout()
  // A plot of 800 × 600 CSS pixels; jsdom draws nothing.
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(
    DOMRect.fromRect({ x: 0, y: 0, width: 800, height: 600 })
  )
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null)
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  resetWireViewerStore()
})

describe("the Depth View", () => {
  it(
    "opens a point at its brightest depth, and a newly selected point at its own",
    SLOW_TEST,
    async () => {
      const { router, history, root, asked } = await openDepthView(
        "/reconstructions/1?tab=depth&depth=40"
      )
      // The URL's depth first.
      await shows(40)
      expect(asked()).toEqual(["wire_1/trace", "wire_1/frames/40"])

      await click(byRole("tab", "Points")[0])
      // The grid starts slowly in jsdom when the suite runs in parallel.
      await vi.waitFor(
        () => {
          expect(rowCheckbox(root, "wire_1").checked).toBe(true)
        },
        { timeout: 5000 }
      )
      await checkRow(root, "wire_3", () =>
        history.location.search.includes("point=wire_3")
      )
      await vi.waitFor(() => {
        expect(router.state.location.search).toEqual({
          tab: "points",
          point: "wire_3",
        })
      })
      await click(byRole("tab", "Depth View")[0])
      await shows(BRIGHTEST.wire_3 ?? -1)
      expect(readout()[1]).toBe("-38 µm (-50 … 50)")
      expect(asked().slice(2)).toEqual(["wire_3/trace", "wire_3/frames/12"])
      await vi.waitFor(() => {
        expect(router.state.location.href).toBe(
          "/reconstructions/1?tab=depth&point=wire_3"
        )
      })
    }
  )

  it("writes a depth change to the URL replacing the history entry, the brightest depth not at all", async () => {
    const { router, history } = await openDepthView(
      "/reconstructions/1?tab=depth"
    )
    await shows(37)
    const entries = history.length

    await click(byRole("button", "Next depth")[0])
    await shows(38)
    await vi.waitFor(() => {
      expect(router.state.location.href).toBe(
        "/reconstructions/1?tab=depth&depth=38"
      )
    })
    expect(history.length).toBe(entries)

    await click(byRole("button", "Previous depth")[0])
    await shows(37)
    await vi.waitFor(() => {
      expect(router.state.location.href).toBe("/reconstructions/1?tab=depth")
    })
    expect(history.length).toBe(entries)
  })

  it("keeps the zoom across depths", async () => {
    await openDepthView("/reconstructions/1?tab=depth")
    await shows(37)
    const fitted = await hover(500, 200)

    await act(async () => {
      canvas()?.dispatchEvent(
        new WheelEvent("wheel", {
          clientX: 400,
          clientY: 300,
          deltaY: -400,
          bubbles: true,
          cancelable: true,
        })
      )
      await Promise.resolve()
    })
    const zoomed = await hover(500, 200)
    expect(zoomed.pixel).not.toBe(fitted.pixel)

    await click(byRole("button", "Next depth")[0])
    await shows(38)
    const next = await hover(500, 200)
    // The same pixel under the cursor, now depth 38's value.
    expect(next.pixel).toBe(zoomed.pixel)
    expect(next.value).toBe(zoomed.value + 1000)
  })

  it("shows no frame for a newly selected point that fails, not the last point's", async () => {
    const { history } = await openDepthView("/reconstructions/1?tab=depth", {
      traceAnswer: (pointId) =>
        pointId === "wire_3"
          ? Response.json(
              {
                title: "Point not reconstructed",
                status: 409,
                detail: "Point wire_3 has no images.",
              },
              {
                status: 409,
                headers: { "content-type": "application/problem+json" },
              }
            )
          : undefined,
    })
    const colourScale = () =>
      document.querySelector('[role="region"][aria-label="Colour scale"]')
    await shows(BRIGHTEST.wire_1 ?? -1)
    expect(colourScale()).not.toBeNull()

    act(() => {
      history.push("/reconstructions/1?tab=depth&point=wire_3")
    })
    await vi.waitFor(() => {
      expect(plotStatus()?.textContent).toBe("Point not reconstructed")
    })
    expect(colourScale()).toBeNull()
    expect(byRole("alert")).toEqual([])
  })

  it("waits out a busy trace, showing it as a status, and shows a point the API refuses as one", async () => {
    // An ID the path must encode.
    const points = () => [makePoint({ point_id: "wire 1&x" })]
    let busy = true
    const { asked } = await openDepthView("/reconstructions/1?tab=depth", {
      points,
      traceAnswer: () => {
        if (busy) {
          busy = false
          return busyAnswer("point computations")
        }
        return Response.json(
          {
            title: "Point not reconstructed",
            status: 409,
            detail: "Point wire 1&x has no images.",
          },
          {
            status: 409,
            headers: { "content-type": "application/problem+json" },
          }
        )
      },
    })
    await vi.waitFor(() => {
      expect(plotStatus()?.textContent).toBe("Busy: retrying in 1 s")
    })
    await vi.waitFor(
      () => {
        expect(plotStatus()?.textContent).toBe("Point not reconstructed")
      },
      { timeout: 3000 }
    )
    expect(plotStatus()?.getAttribute("title")).toBe(
      "Point wire 1&x has no images."
    )
    expect(byRole("alert")).toEqual([])
    expect(asked()).toEqual(["wire%201%26x/trace", "wire%201%26x/trace"])
  })

  it("shows a frame whose file system does not answer as not responding, with Retry, not as busy", async () => {
    let hung = true
    const { asked } = await openDepthView("/reconstructions/1?tab=depth", {
      frameAnswer: () => (hung ? notRespondingAnswer() : undefined),
    })
    await vi.waitFor(() => {
      expect(plotStatus()?.textContent).toBe("Not responding")
    })
    expect(plotStatus()?.getAttribute("title")).toBe(NOT_RESPONDING_DETAIL)
    expect(byRole("alert")).toEqual([])
    expect(asked()).toEqual(["wire_1/trace", "wire_1/frames/37"]) // not asked again by itself

    hung = false
    const frameRegion = document.querySelector('[aria-label="Depth frame"]')
    await click(byRole("button", "Retry", frameRegion ?? document)[0])
    await shows(BRIGHTEST.wire_1 ?? -1)
    expect(asked()).toEqual([
      "wire_1/trace",
      "wire_1/frames/37",
      "wire_1/frames/37",
    ])
  })

  it("waits out a busy frame, showing it as a status", async () => {
    let busy = true
    const { asked } = await openDepthView("/reconstructions/1?tab=depth", {
      frameAnswer: () => {
        if (!busy) return undefined
        busy = false
        return busyAnswer("frame reads")
      },
    })
    await vi.waitFor(() => {
      expect(plotStatus()?.textContent).toBe("Busy: retrying in 1 s")
    })
    await shows(BRIGHTEST.wire_1 ?? -1, 3000)
    expect(byRole("alert")).toEqual([])
    expect(asked()).toEqual([
      "wire_1/trace",
      "wire_1/frames/37",
      "wire_1/frames/37",
    ])
  })
})
