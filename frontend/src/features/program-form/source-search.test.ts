import { describe, expect, it } from "vitest"
import { sourceQuery } from "./source-search"

describe("sourceQuery", () => {
  it("finds an ID by number (with its prefix or not), else a sample (a run's scan's), latest first", () => {
    expect(sourceQuery("scan", "")).toEqual({ sort: "-time", limit: 20 })
    expect(JSON.parse(sourceQuery("scan", "SN276990").filter ?? "")).toEqual({
      scan_number: { filterType: "number", type: "equals", filter: 276990 },
    })
    expect(
      JSON.parse(sourceQuery("reconstruction", "r3").filter ?? "")
    ).toEqual({
      id: { filterType: "number", type: "equals", filter: 3 },
    })
    expect(sourceQuery("indexing", "12").sort).toBe("-created_at")
    expect(JSON.parse(sourceQuery("scan", "Si wafer").filter ?? "")).toEqual({
      sample_name: { filterType: "text", type: "contains", filter: "Si wafer" },
    })
    expect(JSON.parse(sourceQuery("indexing", "Ni").filter ?? "")).toEqual({
      sample_name: { filterType: "text", type: "contains", filter: "Ni" },
    })
  })

  it("keeps a form's clone list to its own program's runs, with or without a search", () => {
    const method = {
      method: { filterType: "text", type: "equals", filter: "laue_matching" },
    }
    expect(
      JSON.parse(sourceQuery("indexing", "", "laue_matching").filter ?? "")
    ).toEqual(method)
    expect(
      JSON.parse(sourceQuery("indexing", "12", "laue_matching").filter ?? "")
    ).toEqual({
      ...method,
      id: { filterType: "number", type: "equals", filter: 12 },
    })
  })
})
