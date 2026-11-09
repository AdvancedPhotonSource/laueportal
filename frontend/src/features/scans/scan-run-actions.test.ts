import { describe, expect, it } from "vitest"

import type { IndexingRunSummary, ReconstructionRunSummary } from "@/api"
import { validateCreatePrefill } from "@/lib/prefill"
import { indexingCardLinks, reconstructionCardLinks } from "./scan-run-actions"

function reconstruction(id: number, method = "wire"): ReconstructionRunSummary {
  return {
    id,
    method,
    scan_number: 42,
    sample_name: "Si",
    n_points: 21,
    percent_brightest: 5,
    depth_start: -50,
    depth_end: 50,
    author: "adoe",
    notes: null,
    created_at: "2026-01-05T08:00:00Z",
    start_time: null,
    finish_time: null,
    status: "finished",
    n_inputs: 21,
    n_succeeded: 21,
    n_failed: 0,
    n_not_run: 0,
    n_processed: 21,
  }
}

function indexing(
  id: number,
  reconstructionId: number | null
): IndexingRunSummary {
  return {
    id,
    method: "lauego",
    method_label: "LaueGo",
    scan_number: 42,
    sample_name: "Si",
    reconstruction_id: reconstructionId,
    n_points: 21,
    n_depths: null,
    box_size: 5,
    crystal_file: "Ni.xml",
    author: "adoe",
    notes: null,
    created_at: "2026-01-05T08:00:00Z",
    status: "finished",
    n_inputs: 21,
    n_succeeded: 21,
    n_failed: 0,
    n_not_run: 0,
    n_processed: 21,
  }
}

describe("the Reconstructions card's links", () => {
  it("fall back to this scan with nothing selected", () => {
    expect(reconstructionCardLinks(42, undefined)).toEqual({
      newRecon: { scan: "42" },
      newIndex: { scan: "42" },
    })
  })

  it("clone the selected run and index it, with this scan", () => {
    expect(reconstructionCardLinks(42, reconstruction(7))).toEqual({
      newRecon: { scan: "42", reconstruction: "7" },
      newIndex: { scan: "42", reconstruction: "7" },
    })
  })

  it("give the wire form a wire run only", () => {
    expect(reconstructionCardLinks(42, reconstruction(7, "mask"))).toEqual({
      newRecon: { scan: "42" },
      newIndex: { scan: "42", reconstruction: "7" },
    })
  })
})

describe("the Peak Indexing card's links", () => {
  it("fall back to this scan with nothing selected", () => {
    expect(indexingCardLinks(42, undefined)).toEqual({
      newRecon: { scan: "42" },
      newIndex: { scan: "42" },
    })
  })

  it("reconstruct the run's reconstruction and clone the run", () => {
    expect(indexingCardLinks(42, indexing(9, 3))).toEqual({
      newRecon: { scan: "42", reconstruction: "3" },
      newIndex: { scan: "42", reconstruction: "3", indexing: "9" },
    })
    expect(indexingCardLinks(42, indexing(9, null))).toEqual({
      newRecon: { scan: "42" },
      newIndex: { scan: "42", indexing: "9" },
    })
  })

  it("are one source context the create forms accept as it is", () => {
    const links = [
      indexingCardLinks(42, indexing(9, 3)),
      reconstructionCardLinks(42, reconstruction(7)),
    ].flatMap(({ newRecon, newIndex }) => [newRecon, newIndex])
    for (const prefill of links) {
      const validated = validateCreatePrefill({ ...prefill })
      expect(
        Object.fromEntries(
          Object.entries(validated).filter(([, value]) => value !== undefined)
        )
      ).toEqual(prefill)
    }
  })
})
