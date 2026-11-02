// @vitest-environment jsdom
import { describe, expect, it } from "vitest"

import type { RoiTracesAnswer } from "@/api"
import { traceChart } from "./roi-chart"
import type { Roi } from "./rois"

const A: Roi = { x: 10, y: 20, size: 9, color: "#3987e5" }
const B: Roi = { x: 30, y: 40, size: 4, color: "#d95926" }

function answer(
  normalize: boolean,
  traces: RoiTracesAnswer["answer"]["traces"]
): RoiTracesAnswer {
  return {
    rois: [A, B].map(({ x, y, size }) => ({ x, y, size })),
    answer: {
      reconstruction_id: 1,
      point_id: "wire_1",
      normalize,
      depth_um: [-1, 0, 1],
      depth_index: [0, 1, 2],
      traces,
    },
  }
}

const trace = (
  values: (number | null)[] | null,
  reason: string | null = null
) => ({
  bounds: [0, 1, 0, 1],
  values,
  reason,
})

describe("the ROI traces chart", () => {
  it("maps the x axis, the y scale and the values to the lines and labels", () => {
    const sums = answer(false, [trace([0, 5, 2]), trace([3, -1, 4])])

    const linear = traceChart(sums, [A, B], { axis: "depth", scale: "linear" })
    expect(linear.x).toEqual({ key: "depth_um", label: "Depth (µm)" })
    expect(linear.y).toEqual({ label: "Stored intensity", scale: "linear" })
    expect(linear.lines).toEqual([
      { key: "roi0", label: "ROI 1 (Y 20, X 10)", color: A.color },
      { key: "roi1", label: "ROI 2 (Y 39.5, X 29.5)", color: B.color },
    ])
    expect(linear.rows).toEqual([
      { depth_um: -1, depth_index: 0, roi0: 0, roi1: 3 },
      { depth_um: 0, depth_index: 1, roi0: 5, roi1: -1 },
      { depth_um: 1, depth_index: 2, roi0: 2, roi1: 4 },
    ])
    expect(linear.notes).toEqual([])

    // A log axis leaves gaps where a sample is not positive, and says how many.
    const log = traceChart(sums, [A, B], { axis: "index", scale: "log" })
    expect(log.x).toEqual({ key: "depth_index", label: "Depth index" })
    expect(log.y.scale).toBe("log")
    expect(log.rows.map((row) => [row.roi0, row.roi1])).toEqual([
      [null, 3],
      [5, null],
      [2, 4],
    ])
    expect(log.notes).toEqual(["Log axis: 2 of 6 samples ≤ 0 not shown"])

    // Normalized: the answer's own values; a trace without any has its reason as a status, no line.
    const normalized = traceChart(
      answer(true, [
        trace([0, 1, 0.4]),
        trace(null, "the trace maximum is not positive"),
      ]),
      [A, B],
      { axis: "depth", scale: "linear" }
    )
    expect(normalized.y.label).toBe("Normalized intensity")
    expect(normalized.lines.map((line) => line.key)).toEqual(["roi0"])
    expect(normalized.notes).toEqual([
      "ROI 2 (Y 39.5, X 29.5): the trace maximum is not positive",
    ])

    // Only the shown ROIs, numbered by their place in the list; one the answer lacks (still loading)
    // has no line yet.
    const other: Roi = { x: 1, y: 1, size: 9, color: "#199e70" }
    expect(
      traceChart(sums, [A, B, other], {
        axis: "depth",
        scale: "linear",
        hidden: new Set([A]),
      }).lines
    ).toEqual([
      { key: "roi1", label: "ROI 2 (Y 39.5, X 29.5)", color: B.color },
    ])
  })
})
