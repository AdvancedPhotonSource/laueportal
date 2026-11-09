import { describe, expect, it } from "vitest"

import type { IndexingRunSummary } from "@/api"
import { stringifySearch } from "@/lib/search"
import { indexingActions } from "./indexing-actions"

function run(
  id: number,
  scan_number: number | null,
  reconstruction_id: number | null
): IndexingRunSummary {
  return {
    id,
    method: "lauego",
    method_label: "LaueGo",
    scan_number,
    sample_name: null,
    reconstruction_id,
    n_points: 21,
    n_depths: null,
    author: "jdoe",
    notes: null,
    box_size: 5,
    crystal_file: "Si.xtal",
    created_at: "2026-10-19T12:00:00Z",
    status: "finished",
    n_inputs: 21,
    n_succeeded: 21,
    n_failed: 0,
    n_not_run: 0,
    n_processed: 21,
  }
}

describe("indexingActions", () => {
  it("clones a LaueMatching run on the LaueMatching form", () => {
    const actions = indexingActions([
      { ...run(70, 990001, 2), method: "laue_matching" },
    ])
    expect(actions.newIndex).toEqual({
      indexing: "70",
      program: "laue_matching",
    })
  })

  it("disables both buttons without a selection", () => {
    expect(indexingActions([])).toEqual({ count: 0 })
  })

  it("reconstructs one run's sources and clones it", () => {
    const actions = indexingActions([run(70, 990001, 2)])
    expect(actions).toEqual({
      count: 1,
      newRecon: { scan: "990001", reconstruction: "2" },
      newIndex: { indexing: "70" },
    })
    expect(stringifySearch({ ...actions.newRecon })).toBe(
      "?scan=990001&reconstruction=2"
    )
    expect(stringifySearch({ ...actions.newIndex })).toBe("?indexing=70")
  })

  it("leaves out the source a run does not have", () => {
    expect(indexingActions([run(71, 5, null)]).newRecon).toEqual({ scan: "5" })
    expect(indexingActions([run(74, null, 2)]).newRecon).toEqual({
      reconstruction: "2",
    })
  })

  it("disables New Recon for an unlinked run, not New Index", () => {
    expect(indexingActions([run(75, null, null)])).toEqual({
      count: 1,
      newIndex: { indexing: "75" },
    })
  })

  it("disables both buttons for more than one run (one source)", () => {
    expect(indexingActions([run(71, 5, null), run(69, 6, 2)])).toEqual({
      count: 2,
    })
  })
})
