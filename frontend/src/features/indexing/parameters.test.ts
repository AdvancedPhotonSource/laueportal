// @vitest-environment jsdom
import { describe, expect, it } from "vitest"

import { makeIndexingRunDetail, makeLaueGoParameters } from "@/test/indexing"
import { parameterGroups } from "./parameters"

describe("the Parameters tab's groups", () => {
  it("groups the run's stored parameters as the create form, then the other stored values", () => {
    const run = makeIndexingRunDetail({
      reconstruction_id: 3,
      notes: "first pass",
      parameters: makeLaueGoParameters({
        filename_prefixes: ["Ni_%d.h5", "Ni_b_%d.h5"],
        threshold: null,
        max_number: null,
        smooth: true,
        index_h: 1,
        index_k: -1,
        index_l: 0,
        depth_range: "-5 5",
        depth_range_len: 11,
      }),
    })
    const groups = parameterGroups(run)
    expect(groups.map((group) => group.title)).toEqual([
      "Identity",
      "File Paths",
      "Peak Search Parameters",
      "Indexing Parameters",
      "Notes",
      "Other recorded values",
    ])
    const rows = Object.fromEntries(
      groups.map((group) => [
        group.title,
        Object.fromEntries(group.rows.map((row) => [row.label, row.value])),
      ])
    )
    expect(rows.Identity).toEqual({
      Scan: "990001",
      Reconstruction: "3",
      Author: "demo",
    })
    expect(rows["File Paths"]).toEqual({
      "Folder Path": "/data/scan_990001",
      Filename: "Ni_%d.h5, Ni_b_%d.h5",
      "Scan Indices": "1-4",
      "Depth Indices": "-5 5",
      "Geometry File": "/data/geoN.xml",
      "Crystal Structure File": "/data/Ni.xml",
      "Output Path": "/runs/index_1",
      "Output XML": "output.xml",
    })
    // The placeholders for an empty value: threshold auto, all spots
    expect(rows["Peak Search Parameters"]).toMatchObject({
      Threshold: "auto",
      "Max detected spots": "all",
      "Mask File": "none",
      "Smooth peak before fitting": "yes",
    })
    expect(rows["Indexing Parameters"]).toMatchObject({
      "Central HKL": "1 -1 0",
      "Depth [um]": "auto",
    })
    expect(groups.find((group) => group.title === "Notes")).toEqual({
      title: "Notes",
      rows: [{ label: "Notes", value: "first pass" }],
    })
    // the derived counts (scan_points_len, depth_range_len) are not shown
    expect(rows["Other recorded values"]).toEqual({
      "Detector Crop [px]": "x 0–2047, y 0–2047",
      "Energy Unit": "keV",
      "Exposure Unit": "sec",
      "Reciprocal Lattice Unit": "1/nm",
      "Lattice Parameters Unit": "nm",
      "Cosmic Filter": "no",
      Beamline: "34ID-E",
    })
  })

  it("sets only the paths and file names in a monospace font, not a placeholder", () => {
    const groups = parameterGroups(
      makeIndexingRunDetail({
        parameters: makeLaueGoParameters({ mask_file: null }),
      })
    )
    expect(
      groups.flatMap((group) =>
        group.rows.filter((row) => row.mono).map((row) => row.label)
      )
    ).toEqual([
      "Folder Path",
      "Filename",
      "Geometry File",
      "Crystal Structure File",
      "Output Path",
      "Output XML",
    ])
  })

  it("shows only the run's own values for a run without stored parameters", () => {
    const groups = parameterGroups(
      makeIndexingRunDetail({ method: "laue_matching", parameters: null })
    )
    expect(
      groups.map((group) => [group.title, group.rows.map((row) => row.label)])
    ).toEqual([
      ["Identity", ["Scan", "Reconstruction", "Author"]],
      ["File Paths", ["Folder Path", "Output Path"]],
      ["Notes", ["Notes"]],
    ])
  })
})
