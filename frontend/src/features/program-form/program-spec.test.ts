import { describe, expect, it } from "vitest"
import { IndexingRunDetailSchema } from "@/api"
import { fieldOf, specOf } from "@/test/programs"
import { blocksOf, fieldHint } from "./program-spec"

describe("programSpec", () => {
  it("gives the sections in order from x-layout, Run on apart, Restore Default on the parameter sections", () => {
    const lauego = specOf("lauego")
    expect(
      lauego.sections.map((section) => [
        section.group,
        section.restorable,
        section.fields.map((field) => field.name),
      ])
    ).toEqual([
      ["Identity", false, ["scan_number", "reconstruction_id", "author"]],
      [
        "File Paths",
        false,
        [
          "input_path",
          "filename_prefixes",
          "scan_points",
          "depth_range",
          "geometry_file",
          "crystal_file",
          "output_path_template",
          "output_xml",
        ],
      ],
      [
        "Peak Search Parameters",
        true,
        [
          "box_size",
          "max_rfactor",
          "threshold",
          "threshold_ratio",
          "min_size",
          "min_separation",
          "max_number",
          "peak_shape",
          "mask_file",
          "smooth",
        ],
      ],
      [
        "Indexing Parameters",
        true,
        [
          "index_kev_max_calc",
          "index_kev_max_test",
          "index_angle_tolerance",
          "index_h",
          "index_k",
          "index_l",
          "index_cone",
          "max_peaks",
          "depth",
        ],
      ],
      ["Notes", false, ["notes"]],
    ])
    expect(lauego.target?.options).toEqual([{ value: "local", label: "APS" }])
    expect(specOf("wire").sections.map((section) => section.group)).toEqual([
      "Identity",
      "File Paths",
      "Wire Reconstruction Parameters",
      "Notes",
    ])
  })

  it("reads each field's title, hint, unit, limits, default, choices, file filter and requirement", () => {
    const { fields } = specOf("lauego")
    expect(fields.box_size).toMatchObject({
      title: "Box Size",
      type: "integer",
      unit: "px",
      exclusiveMinimum: 0,
      default: 18,
      required: true,
      nullable: false,
    })
    expect(fields.threshold).toMatchObject({
      nullable: true,
      minimum: 0,
      default: null,
    })
    expect(fields.peak_shape?.options?.map((option) => option.value)).toEqual([
      "Lorentzian",
      "Gaussian",
    ])
    expect(fields.author?.required).toBe(false)
    expect(fields.geometry_file?.extensions).toEqual([".xml"])
    // The tooltip: the user's hint (x-hint, not the API's description) and the limits.
    expect(fieldHint(fieldOf("lauego", "index_cone"))).toBe(
      "The opening angle of the cone around the Central HKL in which reflections are matched.\nLimits: ≥ 0, ≤ 180"
    )
  })

  it("draws the HKL fields as one block and the index ranges as one", () => {
    const blocks = (group: string) =>
      blocksOf(
        specOf("lauego").sections.find((section) => section.group === group)
          ?.fields ?? []
      ).map((block) =>
        block.kind === "field"
          ? block.field.name
          : `${block.kind}:${block.fields.map((f) => f.name).join(",")}`
      )
    expect(blocks("Indexing Parameters")).toContain(
      "hkl:index_h,index_k,index_l"
    )
    expect(blocks("File Paths")).toContain("indices:scan_points,depth_range")
  })

  it("agrees with the API's Zod schema of the stored LaueGo parameters (type and null)", () => {
    // The fixture's fields against the contract (openapi.json, drift-checked against the backend): each
    // field the stored parameters hold has the same value type there, and the same nullability.
    interface Def {
      type: string
      format?: string
      options?: readonly { def: Def }[]
    }
    const stored = IndexingRunDetailSchema.shape.parameters.def.options[0]
    const shape = stored.shape as unknown as Record<
      string,
      { def: Def } | undefined
    >
    const typeOf = (def: Def): string => {
      if (def.type === "number") return def.format ? "integer" : "number"
      return def.type === "array" ? "list" : def.type
    }
    const { fields } = specOf("lauego")
    const compared = Object.values(fields).flatMap((field) => {
      const def = shape[field.name]?.def
      return def ? [{ field, def }] : []
    })
    expect(compared.length).toBeGreaterThan(20)
    for (const { field, def } of compared) {
      const types = def.options
        ? def.options.map((option) => typeOf(option.def))
        : [typeOf(def)]
      expect({ name: field.name, types }).toEqual({
        name: field.name,
        types: field.nullable ? [field.type, "null"] : [field.type],
      })
    }
  })
})
