import { describe, expect, it } from "vitest"
import { fieldOf, specOf } from "@/test/programs"
import {
  applyPrefill,
  defaultsOf,
  formValuesOf,
  requestOf,
  requestValueOf,
} from "./form-values"
import { sourceIdsOf } from "./program-spec"
import { CLEARED_FIELDS, SOURCE_FIELDS } from "./source-mode"

const f = (name: string) => fieldOf("lauego", name)

describe("form values", () => {
  it("send a number as a number, blank as null when the field may be (automatic), else blank", () => {
    expect(requestValueOf(f("box_size"), " 18 ")).toBe(18)
    expect(requestValueOf(f("min_size"), "2.5")).toBe(2.5)
    expect(requestValueOf(f("threshold"), "")).toBeNull()
    expect(requestValueOf(f("box_size"), "")).toBe("")
    // What does not parse goes as typed: the validate endpoint reports it on its field.
    expect(requestValueOf(f("box_size"), "12x")).toBe("12x")
    // Not finite ("1e400" is Infinity, which JSON sends as null): as typed too, never a silent null.
    expect(requestValueOf(f("box_size"), "1e400")).toBe("1e400")
    expect(requestValueOf(f("threshold"), "-1e999")).toBe("-1e999")
  })

  it("send the comma-separated Filename as its templates, a blank nullable text as null, a toggle as a boolean", () => {
    expect(
      requestValueOf(f("filename_prefixes"), "Si_%d.h5, Ge_%d.h5,")
    ).toEqual(["Si_%d.h5", "Ge_%d.h5"])
    expect(requestValueOf(f("mask_file"), "  ")).toBeNull()
    expect(requestValueOf(f("depth"), "12.5")).toBe("12.5")
    expect(requestValueOf(f("smooth"), true)).toBe(true)
  })

  it("fill the form from the prefill's values over the configured defaults, and back unchanged", () => {
    const spec = specOf("lauego")
    const values = formValuesOf(spec, {
      scan_number: 990001,
      filename_prefixes: ["a_%d.h5", "b_%d.h5"],
      threshold: null,
    })
    expect(values).toMatchObject({
      scan_number: "990001",
      filename_prefixes: "a_%d.h5, b_%d.h5",
      threshold: "",
      box_size: "18",
      smooth: false,
      backend: "local",
    })
    expect(requestOf(spec, values)).toMatchObject({
      scan_number: 990001,
      filename_prefixes: ["a_%d.h5", "b_%d.h5"],
      threshold: null,
      box_size: 18,
      index_l: 1,
      output_xml: "output.xml",
    })
    expect(defaultsOf([f("max_rfactor"), f("peak_shape")])).toEqual({
      max_rfactor: "0.5",
      peak_shape: "Lorentzian",
    })
  })

  it("a reconstruction's prefill fills only its data fields and the source IDs, a kind change resets only the data fields; everything else on screen stays", () => {
    const spec = specOf("lauego")
    const defaults = formValuesOf(spec)
    const ids = sourceIdsOf(spec)
    // A clone's values on screen (I1: SN990001, its ranges, crystal and output name, threshold ratio 4),
    // then the user's own edits.
    const clone = formValuesOf(spec, {
      scan_number: 990001,
      input_path: "/data/frames",
      scan_points: "1-4",
      depth_range: "1-5",
      crystal_file: "/data/Si.xml",
      output_xml: "i1.xml",
      threshold_ratio: 4,
      author: "",
    })
    const current = {
      ...clone,
      box_size: "20",
      geometry_file: "/mine.xml",
      author: "jdoe",
    }
    // The switch to another kind: the IDs and the data fields go back to the defaults.
    const cleared = applyPrefill(defaults, clone, defaults, current, {
      ids,
      fields: new Set([...ids, ...CLEARED_FIELDS]),
    })
    const after = { ...cleared.values, ...cleared.kept }
    expect(after).toMatchObject({
      scan_number: "",
      input_path: defaults.input_path,
      scan_points: defaults.scan_points,
      depth_range: defaults.depth_range,
      // Not data fields: the clone's crystal and output name, and the user's geometry, stay.
      crystal_file: "/data/Si.xml",
      output_xml: "i1.xml",
      geometry_file: "/mine.xml",
      threshold_ratio: "4",
      box_size: "20",
      author: "jdoe",
    })
    // R1's prefill: its scan.h5, scan points and scan, and the configured parameters (which do not apply).
    const next = formValuesOf(spec, {
      scan_number: 990001,
      reconstruction_id: 1,
      input_path: "/runs/rec_1/reconstruction/scan.h5",
      scan_points: "1",
    })
    const { values, kept } = applyPrefill(
      defaults,
      cleared.values,
      next,
      after,
      { ids, fields: new Set([...ids, ...SOURCE_FIELDS.reconstruction]) }
    )
    expect({ ...values, ...kept }).toMatchObject({
      reconstruction_id: "1",
      input_path: "/runs/rec_1/reconstruction/scan.h5",
      scan_points: "1",
      // A data field the source does not derive keeps the user's edit (the kit's rule).
      geometry_file: "/mine.xml",
      crystal_file: "/data/Si.xml",
      output_xml: "i1.xml",
      threshold_ratio: "4",
      box_size: "20",
      author: "jdoe",
    })
  })
})
