// @vitest-environment jsdom
import { describe, expect, it } from "vitest"

import { makeReconstructionRunDetail } from "@/test/reconstruction"
import { parameterGroups } from "./parameters"

describe("the wire viewer's parameters", () => {
  it("groups the stored values as the wire form, paths with their root-relative form", () => {
    const groups = parameterGroups(makeReconstructionRunDetail())
    expect(groups.map((group) => group.title)).toEqual([
      "Identity",
      "File Paths",
      "Wire Reconstruction Parameters",
      "Notes",
      "Other recorded values",
    ])
    const paths = groups[1]?.rows ?? []
    expect(paths).toEqual([
      {
        label: "Folder Path",
        value: "/data/demo/synthetic-reconstruction",
        mono: true,
        relative: "data/demo/synthetic-reconstruction",
      },
      { label: "Filename", value: "wire_%d.h5", mono: true },
      { label: "Scan Indices", value: "1" },
      {
        label: "Output Path",
        value: "/data/runs/rec_1",
        mono: true,
        relative: "data/runs/rec_1",
      },
      // Outside every file root: no relative form.
      { label: "Geometry File", value: "/data/demo/geometry.xml", mono: true },
    ])
    expect(groups[2]?.rows.slice(-2)).toEqual([
      { label: "Frame Skip [slices]", value: "default" },
      { label: "Wire Skip [entries]", value: "default" },
    ])
  })

  it("shows only the run's own values without stored parameters", () => {
    const groups = parameterGroups(
      makeReconstructionRunDetail({ parameters: null, notes: "n" })
    )
    expect(groups.map((group) => group.title)).toEqual([
      "Identity",
      "File Paths",
      "Notes",
      "Other recorded values",
    ])
    expect(groups[3]?.rows).toEqual([{ label: "Method", value: "wire" }])
  })
})
