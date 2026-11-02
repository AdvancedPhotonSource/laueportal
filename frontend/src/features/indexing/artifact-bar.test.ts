// @vitest-environment jsdom
import { QueryClient } from "@tanstack/react-query"
import { describe, expect, it } from "vitest"

import { ApiError, ConversionStatus, getSelectionStatsQueryKey } from "@/api"
import { makeArtifact } from "@/test/indexing"
import {
  artifactRefetchInterval,
  conversionText,
  onResultsReload,
  reloadResults,
  resultsReplaced,
} from "./artifact-bar"

describe("reloadResults", () => {
  it("reads the run's selection statistics again, and no other run's", async () => {
    const queryClient = new QueryClient()
    const patterns = [
      [0, 0],
      [1, 0],
    ] as const
    const own = getSelectionStatsQueryKey(1, patterns)
    const other = getSelectionStatsQueryKey(2, patterns)
    queryClient.setQueryData(own, { mean: 1 })
    queryClient.setQueryData(other, { mean: 2 })
    await reloadResults(queryClient, 1)
    expect(queryClient.getQueryState(own)?.isInvalidated).toBe(true)
    expect(queryClient.getQueryState(other)?.isInvalidated).toBe(false)
  })

  it("tells the grids, which fetch outside React Query, which run to load again", async () => {
    const heard: number[] = []
    const stop = onResultsReload((id) => heard.push(id))
    await reloadResults(new QueryClient(), 1)
    stop()
    await reloadResults(new QueryClient(), 2)
    expect(heard).toEqual([1])
  })
})

describe("conversionText", () => {
  it("says a queued conversion an open live session holds", () => {
    const queued = {
      id: "convert_results_7",
      kind: "convert_results" as const,
      indexing_id: 7,
      status: ConversionStatus.queued,
      message: null,
      enqueued_at: null,
      started_at: null,
      ended_at: null,
    }
    expect(conversionText({ ...queued, held: false })).toBe(
      "Conversion queued: waiting for a worker…"
    )
    expect(conversionText({ ...queued, held: true })).toBe(
      "Conversion queued: held by the live session…"
    )
  })
})

describe("artifactRefetchInterval", () => {
  it("polls a running conversion every 2 s, backs off after failed reads, and stops on a 404", () => {
    const converting = makeArtifact({
      state: "xml_only",
      conversion: {
        id: "convert_results_1",
        kind: "convert_results",
        indexing_id: 1,
        status: "running",
        message: null,
        enqueued_at: "2026-10-21T10:00:00Z",
        started_at: "2026-10-21T10:00:01Z",
        ended_at: null,
        held: false,
      },
    })
    const failed = new ApiError(503, "Service Unavailable")
    expect(artifactRefetchInterval(converting, null, 0)).toBe(2000)
    expect(artifactRefetchInterval(converting, failed, 2)).toBe(8000)
    expect(
      artifactRefetchInterval(converting, new ApiError(404, "Not Found"), 1)
    ).toBe(false)
    expect(artifactRefetchInterval(makeArtifact(), null, 0)).toBe(false)
  })

  it("polls every 5 s while the run writes its results or its run directory does not respond", () => {
    for (const state of ["writing", "not_responding"] as const) {
      expect(artifactRefetchInterval(makeArtifact({ state }), null, 0)).toBe(
        5000
      )
    }
    for (const state of ["empty", "not_on_host"] as const) {
      expect(artifactRefetchInterval(makeArtifact({ state }), null, 0)).toBe(
        false
      )
    }
  })

  it("polls an open live run every 5 s, and a ready batch run not at all", () => {
    expect(
      artifactRefetchInterval(makeArtifact({ live_open: true }), null, 0)
    ).toBe(5000)
    expect(
      artifactRefetchInterval(makeArtifact({ live_open: true }), null, 1)
    ).toBe(10_000)
    expect(artifactRefetchInterval(makeArtifact(), null, 0)).toBe(false)
  })
})

describe("resultsReplaced", () => {
  it("is another results file of a ready artifact, not the first answer or another state", () => {
    const ready = makeArtifact({ results_version: "a" })
    expect(resultsReplaced(ready, makeArtifact({ results_version: "b" }))).toBe(
      true
    )
    expect(resultsReplaced(ready, makeArtifact({ results_version: "a" }))).toBe(
      false
    )
    expect(resultsReplaced(undefined, ready)).toBe(false)
    const writing = makeArtifact({ state: "writing", results_version: null })
    expect(resultsReplaced(writing, ready)).toBe(false)
  })
})
