// @vitest-environment jsdom
import { describe, expect, it } from "vitest"

import { ApiError, type ReconstructionArtifactState } from "@/api"
import {
  makePoint,
  makePoints,
  makeReconstructionArtifact,
} from "@/test/reconstruction"
import {
  artifactRefetchInterval,
  artifactStatus,
  canFollow,
  errorStatus,
  newestComplete,
  noReconstruction,
  selectedPoint,
} from "./artifact-bar"

const gone = new ApiError(404, "Not Found", {
  title: "Reconstruction run not found",
  status: 404,
  detail: "No reconstruction run R1",
})

describe("the wire viewer's artifact bar", () => {
  it("polls every 5 s while the run is active, backs off after failures, and stops on a 404 or once the run ends", () => {
    const running = makeReconstructionArtifact({ run_status: "running" })
    const queued = makeReconstructionArtifact({
      state: "writing",
      run_status: "queued",
    })
    expect(artifactRefetchInterval(running, null, 0)).toBe(5000)
    expect(artifactRefetchInterval(queued, null, 0)).toBe(5000)
    expect(artifactRefetchInterval(running, new Error("down"), 2)).toBe(20_000)
    expect(artifactRefetchInterval(running, gone, 1)).toBe(false)
    expect(artifactRefetchInterval(makeReconstructionArtifact(), null, 0)).toBe(
      false
    )
    expect(artifactRefetchInterval(undefined, new Error("down"), 1)).toBe(false)
    // A run directory that does not respond may come back.
    const hung = makeReconstructionArtifact({ state: "not_responding" })
    expect(artifactRefetchInterval(hung, null, 0)).toBe(5000)
  })

  it("says how many points are complete, or the artifact's state, or the problem", () => {
    expect(artifactStatus(makeReconstructionArtifact()).text).toBe(
      "2 of 3 points complete"
    )
    expect(
      artifactStatus(
        makeReconstructionArtifact({ state: "writing", run_status: "running" })
      ).text
    ).toBe("Writing")
    const without = (state: ReconstructionArtifactState) => {
      const artifact = makeReconstructionArtifact({ state })
      return [artifactStatus(artifact).text, noReconstruction(artifact)]
    }
    expect(without("empty")).toEqual([
      "Empty",
      "No reconstruction in the run directory",
    ])
    expect(without("not_on_host")).toEqual([
      "Not on this host",
      "Run directory not on this host",
    ])
    expect(without("not_responding")).toEqual([
      "Not responding",
      "Run directory not responding",
    ])
    expect(noReconstruction(makeReconstructionArtifact())).toBeUndefined()
    const unreadable = new ApiError(409, "Conflict", {
      title: "Reconstruction unreadable",
      status: 409,
      detail: "scan.h5 cannot be read: 'OSError: truncated'",
    })
    expect(errorStatus(unreadable)).toEqual({
      text: "Reconstruction unreadable",
      hint: "scan.h5 cannot be read: 'OSError: truncated'",
      tone: "destructive",
    })
    expect(errorStatus(gone).text).toBe("Not found")
  })

  it("shows the URL's point when it is complete, else the default, and none when no points can load", () => {
    const points = makePoints()
    expect(selectedPoint("wire_3", points, "wire_1", true)).toBe("wire_3")
    // Pending, or unknown: the first complete point.
    expect(selectedPoint("wire_2", points, "wire_1", true)).toBe("wire_1")
    expect(selectedPoint("nope", points, "wire_1", true)).toBe("wire_1")
    expect(selectedPoint(undefined, points, "wire_1", true)).toBe("wire_1")
    // While the points load (the artifact is ready), the URL's.
    expect(selectedPoint("wire_2", undefined, undefined, true)).toBe("wire_2")
    // No points to load (empty, writing, not here, unreadable): none.
    expect(selectedPoint("foo", undefined, undefined, false)).toBeUndefined()
  })

  it("follows the newest point, the last complete one in arrival order, and only for an open live run", () => {
    const points = [
      makePoint({ point_id: "w1" }),
      makePoint({ point_id: "w3", index: 1 }),
      makePoint({ point_id: "w2", index: 2, status: "pending" }),
    ]
    expect(newestComplete(points)).toBe("w3")
    expect(newestComplete([points[2] ?? makePoint()])).toBeUndefined()
    expect(canFollow(makeReconstructionArtifact({ live_open: true }))).toBe(
      true
    )
    expect(canFollow(makeReconstructionArtifact())).toBe(false)
    expect(canFollow(undefined)).toBe(false)
  })
})
