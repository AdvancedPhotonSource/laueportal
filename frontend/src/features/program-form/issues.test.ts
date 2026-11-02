import { describe, expect, it } from "vitest"
import { specOf } from "@/test/programs"
import { placeIssues, summaryText } from "./issues"

describe("placeIssues", () => {
  it("places each issue on the field its loc names, a template's by its position, the rest on the form", () => {
    const placed = placeIssues(specOf("lauego"), [
      {
        loc: ["box_size"],
        severity: "error",
        code: "greater_than",
        message: "Must be > 0",
      },
      {
        loc: ["filename_prefixes", 1],
        severity: "error",
        code: "placeholders",
        message: "Needs %d",
      },
      {
        loc: ["scan_number"],
        severity: "warning",
        code: "unlinked",
        message: "Unlinked",
      },
      { loc: [], severity: "error", code: "invalid", message: "Whole form" },
      {
        loc: ["computer_name"],
        code: "set_by_portal",
        message: "Set by the portal",
      },
    ])
    expect(placed.byField).toEqual({
      box_size: [
        {
          field: "box_size",
          severity: "error",
          code: "greater_than",
          message: "Must be > 0",
        },
      ],
      filename_prefixes: [
        {
          field: "filename_prefixes",
          severity: "error",
          code: "placeholders",
          message: "Template 2: Needs %d",
        },
      ],
      scan_number: [
        {
          field: "scan_number",
          severity: "warning",
          code: "unlinked",
          message: "Unlinked",
        },
      ],
    })
    expect(
      placed.all
        .filter((issue) => issue.field === null)
        .map((issue) => issue.message)
    ).toEqual(["Whole form", "Set by the portal"])
    expect([placed.errors, placed.warnings]).toEqual([4, 1])
  })

  it("names a field once, lists the errors first, and adds a template's position only when the message does not quote it", () => {
    const spec = specOf("lauego")
    const placed = placeIssues(spec, [
      {
        loc: ["scan_number"],
        severity: "warning",
        code: "unlinked",
        message: "Unlinked",
      },
      {
        loc: ["backend"],
        severity: "error",
        code: "backend_unavailable",
        message: "Run on: not offered",
      },
      { loc: ["box_size"], code: "missing", message: "Required" },
      {
        loc: ["filename_prefixes", 1],
        code: "placeholders",
        message: "Filename 'x_%d_%d_%d.h5' has more than two %d",
      },
      {
        loc: ["input_path"],
        code: "not_found",
        message: "Folder Path not found",
      },
    ])
    const lines = placed.all.map((issue) => [
      issue.message,
      summaryText(
        issue,
        issue.field === null ? undefined : spec.fields[issue.field]?.title
      ),
    ])
    expect(lines).toEqual([
      ["not offered", "Run on: not offered"],
      ["Required", "Box Size: Required"],
      [
        "Filename 'x_%d_%d_%d.h5' has more than two %d",
        "Filename 'x_%d_%d_%d.h5' has more than two %d",
      ],
      ["Folder Path not found", "Folder Path not found"],
      ["Unlinked", "Scan Number: Unlinked"],
    ])
  })
})
