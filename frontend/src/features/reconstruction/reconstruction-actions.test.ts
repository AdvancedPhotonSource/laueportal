import { describe, expect, it } from "vitest"

import type { ReconstructionRunSummary } from "@/api"
import { validateCreatePrefill } from "@/lib/prefill"
import { stringifySearch } from "@/lib/search"
import { reconstructionActions } from "./reconstruction-actions"

function run(
  id: number,
  overrides: Partial<ReconstructionRunSummary> = {}
): ReconstructionRunSummary {
  return {
    id,
    method: "wire",
    scan_number: 990001,
    sample_name: "Si",
    n_points: 21,
    author: "jdoe",
    notes: null,
    created_at: "2026-10-19T12:00:00Z",
    start_time: "2026-10-19T12:00:05Z",
    finish_time: null,
    status: "running",
    n_inputs: 21,
    n_succeeded: 3,
    n_failed: 0,
    n_not_run: 0,
    n_processed: 3,
    depth_start: -50,
    depth_end: 50,
    percent_brightest: 2,
    ...overrides,
  }
}

describe("reconstructionActions", () => {
  it("disables both buttons without a selection", () => {
    expect(reconstructionActions([])).toEqual({ count: 0 })
  })

  it("clones or indexes exactly one selected wire run, with its scan", () => {
    const actions = reconstructionActions([run(3)])
    expect(actions).toEqual({
      count: 1,
      newWireRecon: { reconstruction: "3", scan: "990001" },
      newIndex: { reconstruction: "3", scan: "990001" },
    })
    // The URLs' search, as the router writes it (src/lib/search.ts), and as the forms read it.
    expect(stringifySearch({ ...actions.newWireRecon })).toBe(
      "?scan=990001&reconstruction=3"
    )
    expect(validateCreatePrefill({ ...actions.newIndex })).toEqual({
      scan: "990001",
      reconstruction: "3",
      indexing: undefined,
    })
  })

  it("leaves the scan out for an unlinked run", () => {
    expect(reconstructionActions([run(4, { scan_number: null })])).toEqual({
      count: 1,
      newWireRecon: { reconstruction: "4" },
      newIndex: { reconstruction: "4" },
    })
  })

  it("does not clone a run of another method as a wire one, but indexes it", () => {
    expect(reconstructionActions([run(5, { method: "ca" })])).toEqual({
      count: 1,
      newIndex: { reconstruction: "5", scan: "990001" },
    })
  })

  it("disables both buttons for more than one run (one source)", () => {
    expect(reconstructionActions([run(3), run(4)])).toEqual({ count: 2 })
  })
})
