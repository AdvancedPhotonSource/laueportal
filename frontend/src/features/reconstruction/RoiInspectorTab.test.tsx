import { RouterProvider, createMemoryHistory } from "@tanstack/react-router"
import { act } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { Health, ReconstructionRoiTracesRequest } from "@/api"
import { Providers } from "@/app/Providers"
import { createQueryClient } from "@/app/query-client"
import { createAppRouter } from "@/app/router"
import { stubGridLayout } from "@/test/grid"
import {
  answerReconstruction,
  makePoint,
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
import { MAX_BUSY_RETRIES } from "@/lib/busy"
import { useWireViewerStore } from "./viewer-store"

// The ROI Inspector in the app's router: R1's one point, whose ID needs encoding in a path, with a
// 64 × 48 reference image (float32, as a sum) and its ROI traces over 3 depths.

const HEALTH: Health = {
  db: "ok",
  worker: "running",
  version: "2.0.0.dev0",
}
const POINT = "wire 1&x"
const HEIGHT = 64
const WIDTH = 48

function reference(): Response {
  const pixels = Float32Array.from({ length: HEIGHT * WIDTH }, (_, i) => i)
  return new Response(pixels.buffer, {
    headers: {
      "content-type": "application/octet-stream",
      "x-frame-shape": `${String(HEIGHT)},${String(WIDTH)}`,
      "x-frame-dtype": "float32",
      etag: '"ref"',
    },
  })
}

function busy(): Response {
  return Response.json(
    {
      title: "Too many point computations waiting",
      status: 503,
      detail: "4 point computations are already waiting.",
    },
    {
      status: 503,
      headers: {
        "content-type": "application/problem+json",
        "retry-after": "0",
      },
    }
  )
}

async function openRoiInspector({
  referenceAnswer = reference,
  tracesHeld,
}: {
  /** The answer for a reference kind. */
  referenceAnswer?: (kind: string | null) => Response
  /** While set, the traces' answers wait for it. */
  tracesHeld?: () => Promise<void> | undefined
} = {}) {
  stubMatchMedia()
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined)
  const asked: { path: string; body?: ReconstructionRoiTracesRequest }[] = []
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : null
    const url =
      input instanceof Request
        ? input.url
        : input instanceof URL
          ? input.href
          : input
    const where = new URL(url, "http://localhost")
    if (where.pathname === "/api/v1/health") return Response.json(HEALTH)
    const point =
      /^\/api\/v1\/reconstruction-runs\/1\/points\/([^/]+)\/(reference|roi-traces)$/.exec(
        where.pathname
      )
    if (point) {
      const path = `${point[1] ?? ""}/${point[2] ?? ""}${where.search}`
      if (point[2] === "reference") {
        asked.push({ path })
        return referenceAnswer(where.searchParams.get("kind"))
      }
      const text = request
        ? await request.text()
        : typeof init?.body === "string"
          ? init.body
          : ""
      const body = JSON.parse(text) as ReconstructionRoiTracesRequest
      asked.push({ path, body })
      await tracesHeld?.()
      return Response.json({
        reconstruction_id: 1,
        point_id: POINT,
        normalize: body.normalize ?? false,
        depth_um: [-1, 0, 1],
        depth_index: [0, 1, 2],
        traces: body.rois.map(() => ({
          bounds: [0, 9, 0, 9],
          values: [1, 3, 2],
          reason: null,
        })),
      })
    }
    const answer = answerReconstruction(where, {
      points: () => [makePoint({ point_id: POINT })],
      runs: (id) => (id === 1 ? makeReconstructionRunDetail() : undefined),
    })
    return answer instanceof Response ? answer : Response.json(answer ?? {})
  })
  vi.stubGlobal("fetch", fetch)
  const queryClient = createQueryClient()
  const history = createMemoryHistory({
    initialEntries: ["/reconstructions/1?tab=roi"],
  })
  const router = createAppRouter(queryClient, history)
  await render(
    <Providers queryClient={queryClient}>
      <RouterProvider router={router} />
    </Providers>
  )
  await act(() => router.load())
  return { asked }
}

const card = (name: string) =>
  document.querySelector(`[role="region"][aria-label="${name}"]`)
const canvas = () => card("Reference image")?.querySelector("canvas")
const referenceStatus = () =>
  card("Reference image")?.querySelector('[role="status"]') ?? null
const listRows = () =>
  Array.from(card("ROI list")?.querySelectorAll("tbody tr") ?? [], (row) =>
    Array.from(row.querySelectorAll("td"), (cell) => cell.textContent).slice(
      1,
      5
    )
  )

/** The pixel under a point of the plot (the hover readout's). */
async function pixelAt(x: number, y: number): Promise<[number, number]> {
  await act(async () => {
    canvas()?.dispatchEvent(
      new MouseEvent("pointermove", { clientX: x, clientY: y, bubbles: true })
    )
    await Promise.resolve()
  })
  const text =
    card("Reference image")?.querySelector(".font-mono")?.textContent ?? ""
  const [, px, py] = /^x (\d+) {2}y (\d+)/.exec(text) ?? []
  return [Number(px), Number(py)]
}

async function clickAt(x: number, y: number) {
  await act(async () => {
    canvas()?.dispatchEvent(
      new MouseEvent("click", {
        clientX: x,
        clientY: y,
        detail: 1,
        bubbles: true,
      })
    )
    await Promise.resolve()
  })
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
  resetWireViewerStore()
})

describe("the ROI Inspector", () => {
  it("adds an ROI where the image is clicked, refuses one past its edge, and asks only for the traces", async () => {
    // The traces' answers wait while `held` is set, until `open`.
    const gate = {
      held: undefined as Promise<void> | undefined,
      open: (): void => undefined,
    }
    const { asked } = await openRoiInspector({ tracesHeld: () => gate.held })
    await vi.waitFor(() => {
      expect(canvas()).not.toBeNull()
      expect(referenceStatus()).toBeNull()
      expect(asked.map((call) => call.path)).toEqual([
        "wire%201%26x/reference?kind=sum_reconstructed",
      ])
    })

    // The plot's centre: a 9-pixel square centred on the pixel clicked (the hover readout's).
    await clickAt(400, 300)
    await vi.waitFor(() => {
      expect(listRows()).toHaveLength(1)
    })
    const [x, y] = await pixelAt(400, 300)
    // Its number and colour, its centre (the clicked pixel: the size is odd) and its size.
    expect(listRows()).toEqual([["1", String(y), String(x), "9"]])
    await vi.waitFor(() => {
      expect(asked.slice(1)).toEqual([
        {
          path: "wire%201%26x/roi-traces",
          body: { rois: [{ x, y, size: 9 }], normalize: false },
        },
      ])
    })
    // The image is not asked for again: only the outlines and the traces change.
    expect(
      asked.filter((call) => call.path.includes("reference"))
    ).toHaveLength(1)

    // Near the image's left edge, a 9-pixel square does not fit.
    let edge = 0
    while (Number.isNaN((await pixelAt(edge, 300))[0])) edge += 2
    expect((await pixelAt(edge, 300))[0]).toBeLessThan(4)
    await clickAt(edge, 300)
    await vi.waitFor(() => {
      expect(byRole("status", "ROI outside the image")).toHaveLength(1)
    })
    expect(listRows()).toHaveLength(1)

    // A second ROI: one request for both.
    await clickAt(400, 150)
    const [x2, y2] = await pixelAt(400, 150)
    await vi.waitFor(() => {
      expect(asked.slice(2)).toEqual([
        {
          path: "wire%201%26x/roi-traces",
          body: {
            rois: [
              { x, y, size: 9 },
              { x: x2, y: y2, size: 9 },
            ],
            normalize: false,
          },
        },
      ])
    })
    expect(listRows()[1]?.[0]).toBe("2")

    // Unchecking the second hides it without a request.
    await click(byRole("checkbox", /^Show ROI 2 at/)[0])
    expect(
      byRole("checkbox", /^Show ROI 2 at/)[0]?.getAttribute("aria-checked")
    ).toBe("false")
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)))
    expect(asked).toHaveLength(3)

    // Deleting the first asks once for the one left (now ROI 1); shown again meanwhile, its traces
    // are said to load over the last answer.
    gate.held = new Promise<void>((resolve) => {
      gate.open = resolve
    })
    await click(byRole("button", /^Delete ROI 1 at/)[0])
    await vi.waitFor(() => {
      expect(asked.slice(3)).toEqual([
        {
          path: "wire%201%26x/roi-traces",
          body: { rois: [{ x: x2, y: y2, size: 9 }], normalize: false },
        },
      ])
    })
    await click(byRole("checkbox", /^Show ROI 1 at/)[0])
    await vi.waitFor(() => {
      expect(byRole("status", "Loading the traces…")).toHaveLength(1)
    })
    gate.open()
    await vi.waitFor(() => {
      expect(byRole("status", "Loading the traces…")).toHaveLength(0)
    })
    expect(listRows()).toHaveLength(1)
    expect(
      asked.filter((call) => call.path.includes("reference"))
    ).toHaveLength(1)
  })

  it("shows no image for a reference kind that fails, not the last kind's", async () => {
    await openRoiInspector({
      referenceAnswer: (kind) =>
        kind === "first_raw"
          ? Response.json(
              {
                title: "Product failed",
                status: 422,
                detail: "Per-depth files hold no raw images.",
              },
              {
                status: 422,
                headers: { "content-type": "application/problem+json" },
              }
            )
          : reference(),
    })
    await vi.waitFor(() => {
      expect(card("Colour scale")).not.toBeNull()
    })

    act(() => {
      useWireViewerStore.getState().setRoiView({ reference: "first_raw" })
    })
    await vi.waitFor(() => {
      expect(referenceStatus()?.textContent).toBe("Product failed")
    })
    expect(card("Colour scale")).toBeNull()
  })

  it(
    "waits out a busy reference, then offers Retry once it gives up",
    SLOW_TEST,
    async () => {
      let answer = busy
      const { asked } = await openRoiInspector({
        referenceAnswer: () => answer(),
      })
      await vi.waitFor(
        () => {
          expect(referenceStatus()?.textContent).toBe(
            "Too many point computations waiting"
          )
        },
        { timeout: 5000 }
      )
      expect(asked).toHaveLength(MAX_BUSY_RETRIES + 1)
      expect(referenceStatus()?.getAttribute("title")).toBe(
        "4 point computations are already waiting."
      )

      answer = reference
      await click(
        byRole("button", "Retry", card("Reference image") ?? document)[0]
      )
      await vi.waitFor(() => {
        expect(referenceStatus()).toBeNull()
      })
      expect(asked).toHaveLength(MAX_BUSY_RETRIES + 2)
    }
  )
})
