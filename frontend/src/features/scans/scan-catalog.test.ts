import { describe, expect, it } from "vitest"

import { ApiError, UpdateScanCatalogBody } from "@/api"

import { makeCatalog } from "@/test/scans"
import {
  catalogFieldProblems,
  catalogFormSchema,
  catalogFormValues,
  catalogUpdate,
  type CatalogFormValues,
} from "./scan-catalog"

const VALUES: CatalogFormValues = {
  aperture: "wire",
  sample_name: "Ni film",
  filefolder: "/net/data/2026/scan_42",
  filename_prefix: [{ value: "Ni_%d.h5" }],
  notes: "first note",
}

function problems(values: CatalogFormValues) {
  const result = catalogFormSchema.safeParse(values)
  return result.success
    ? {}
    : Object.fromEntries(
        result.error.issues.map((issue) => [
          issue.path.join("."),
          issue.message,
        ])
      )
}

describe("the catalog form's values", () => {
  it("come from the stored catalog", () => {
    expect(catalogFormValues(makeCatalog())).toEqual({
      aperture: "wire",
      sample_name: "Ni film",
      filefolder: "/net/data/2026/scan_42",
      filename_prefix: [{ value: "Ni_%d.h5" }, { value: "Si_%d.h5" }],
      notes: "first note",
    })
  })

  it("read a migrated catalog's None as none, and leave an aperture the form does not offer unchosen", () => {
    expect(catalogFormValues(makeCatalog({ aperture: "None" })).aperture).toBe(
      "none"
    )
    expect(catalogFormValues(makeCatalog({ aperture: "mask" })).aperture).toBe(
      "mask"
    )
    expect(
      catalogFormValues(makeCatalog({ aperture: "synthetic" })).aperture
    ).toBeNull()
  })

  it("are empty for a scan without a catalog", () => {
    expect(catalogFormValues(null)).toEqual({
      aperture: null,
      sample_name: "",
      filefolder: "",
      filename_prefix: [],
      notes: "",
    })
    expect(
      catalogFormValues(makeCatalog({ sample_name: null, notes: null }))
    ).toMatchObject({ sample_name: "", notes: "" })
  })
})

describe("the catalog form's rules", () => {
  it("accept a complete form", () => {
    expect(problems(VALUES)).toEqual({})
  })

  it("need an aperture and an absolute data folder", () => {
    expect(problems({ ...VALUES, aperture: null })).toEqual({
      aperture: "Choose an aperture.",
    })
    expect(problems({ ...VALUES, filefolder: "  " })).toEqual({
      filefolder: "Enter the data folder.",
    })
    expect(problems({ ...VALUES, filefolder: "data/scan_42" })).toEqual({
      filefolder: "Enter an absolute path (it starts with /).",
    })
  })
})

describe("the catalog sent", () => {
  it("is the whole catalog, trimmed, without empty prefixes, blank text as null", () => {
    const update = catalogUpdate(
      {
        aperture: "none",
        sample_name: "  ",
        filefolder: " /net/data/scan_42 ",
        filename_prefix: [
          { value: " a_%d.h5 " },
          { value: "" },
          { value: " " },
        ],
        notes: "\n",
      },
      null
    )
    expect(update).toEqual({
      version: null,
      aperture: "none",
      sample_name: null,
      filefolder: "/net/data/scan_42",
      filename_prefix: ["a_%d.h5"],
      notes: null,
    })
    // What the API's own schema (Orval's Zod) accepts.
    expect(UpdateScanCatalogBody.safeParse(update).success).toBe(true)
  })

  it("keeps notes as written", () => {
    expect(catalogUpdate({ ...VALUES, aperture: "wire" }, 'W/"v1"')).toEqual({
      version: 'W/"v1"',
      aperture: "wire",
      sample_name: "Ni film",
      filefolder: "/net/data/2026/scan_42",
      filename_prefix: ["Ni_%d.h5"],
      notes: "first note",
    })
  })
})

describe("catalogFieldProblems", () => {
  it("places the 400 for a folder outside the roots at the Files Path", () => {
    const error = new ApiError(400, "Bad Request", {
      title: "Path not allowed",
      status: 400,
      detail: "/tmp/x is outside the allowed roots (data).",
      errors: [
        {
          loc: ["filefolder"],
          message: "/tmp/x is outside the allowed roots (data).",
          code: "path_not_allowed",
          severity: "error",
        },
      ],
    })
    expect(catalogFieldProblems(error)).toEqual([
      {
        field: "filefolder",
        message: "/tmp/x is outside the allowed roots (data).",
      },
    ])
  })

  it("ignores problems of no field of the form, and other errors", () => {
    const error = new ApiError(422, "Unprocessable", {
      title: "Validation failed",
      status: 422,
      errors: [
        {
          loc: ["filename_prefix", 0],
          message: "Input should be a valid string",
          code: "string_type",
          severity: "error",
        },
        {
          loc: ["aperture"],
          message: "Input should be 'none', 'wire' or 'mask'",
          code: "literal_error",
          severity: "error",
        },
      ],
    })
    expect(catalogFieldProblems(error)).toEqual([
      {
        field: "aperture",
        message: "Input should be 'none', 'wire' or 'mask'",
      },
    ])
    expect(catalogFieldProblems(new Error("boom"))).toEqual([])
    expect(catalogFieldProblems(new ApiError(500, "Server Error"))).toEqual([])
  })
})
