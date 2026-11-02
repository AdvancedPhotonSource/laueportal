import { QueryClient } from "@tanstack/react-query"
import { isNotFound } from "@tanstack/react-router"
import { afterEach, describe, expect, it, vi } from "vitest"

import { ApiError, getGetScanQueryKey } from "@/api"
import { makeCatalog, makeMetadata, makeScanDetail } from "@/test/scans"
import {
  apertureLabel,
  apertureValue,
  dimensionGroups,
  dimensionSections,
  isDepthScan,
  loadScan,
  metadataSections,
  parseScanId,
  scanInfoFields,
  searchTab,
  tabSearch,
  validateScanSearch,
  withUnit,
} from "./scan-detail"

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("the tab in the URL", () => {
  it.each([
    [{}, undefined, "info"],
    [{ tab: "metadata" }, "metadata", "metadata"],
    [{ tab: "catalog" }, "catalog", "catalog"],
    // The default tab is not written; anything else is dropped (no error page).
    [{ tab: "info" }, undefined, "info"],
    [{ tab: "Catalog" }, undefined, "info"],
    [{ tab: "plot" }, undefined, "info"],
    [{ tab: 2 }, undefined, "info"],
  ])("%j is tab %s, showing %s", (search, tab, shown) => {
    const validated = validateScanSearch(search)
    // Every key is in the result, so a dropped value overwrites the URL's.
    expect(validated).toEqual({ tab })
    expect(Object.keys(validated)).toEqual(["tab"])
    expect(searchTab(validated)).toBe(shown)
  })

  it("writes only a tab other than the default", () => {
    expect(tabSearch("info")).toEqual({ tab: undefined })
    expect(tabSearch("metadata")).toEqual({ tab: "metadata" })
    expect(tabSearch("catalog")).toEqual({ tab: "catalog" })
  })
})

describe("parseScanId", () => {
  it.each([
    ["990001", 990001],
    ["0", 0],
    ["abc", undefined],
    ["-1", undefined],
    ["1.5", undefined],
    ["", undefined],
    ["99999999999999999999", undefined],
  ])("%s is %s", (text, id) => {
    expect(parseScanId(text)).toBe(id)
  })
})

describe("loadScan", () => {
  function answering(response: () => Promise<Response>) {
    const fetchMock = vi.fn(response)
    vi.stubGlobal("fetch", fetchMock)
    return fetchMock
  }

  it("puts the scan in the query cache", async () => {
    const scan = makeScanDetail()
    const fetchMock = answering(() => Promise.resolve(Response.json(scan)))
    const client = new QueryClient()
    await loadScan(client, "42")
    expect(client.getQueryData(getGetScanQueryKey(42))).toEqual(scan)
    // A cached scan is not fetched again.
    await loadScan(client, "42")
    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it("is not found for an ID that is not one, or a scan the API does not know", async () => {
    const fetchMock = answering(() =>
      Promise.resolve(
        Response.json(
          { title: "Scan not found", status: 404, detail: "No scan 7" },
          {
            status: 404,
            headers: { "content-type": "application/problem+json" },
          }
        )
      )
    )
    const client = new QueryClient()
    await expect(loadScan(client, "abc")).rejects.toSatisfy(isNotFound)
    expect(fetchMock).not.toHaveBeenCalled()
    await expect(loadScan(client, "7")).rejects.toSatisfy(isNotFound)
  })

  it("passes other errors to the route's error boundary", async () => {
    answering(() =>
      Promise.resolve(
        Response.json(
          { title: "Internal Server Error", status: 500 },
          {
            status: 500,
            headers: { "content-type": "application/problem+json" },
          }
        )
      )
    )
    await expect(loadScan(new QueryClient(), "7")).rejects.toBeInstanceOf(
      ApiError
    )
  })
})

describe("the aperture", () => {
  it.each([
    ["none", "none", "None"],
    ["wire", "wire", "Wire"],
    ["mask", "mask", "Coded Aperture"],
    // A migrated catalog's "None": shown as None, saved as `none`.
    ["None", "none", "None"],
    [" WIRE ", "wire", "Wire"],
    ["synthetic", null, "synthetic"],
    [null, null, ""],
  ])("%s is %s, shown as %s", (stored, value, label) => {
    expect(apertureValue(stored)).toBe(value)
    expect(apertureLabel(stored)).toBe(label)
  })

  it("is shown only for depth scans", () => {
    expect(isDepthScan("depth + area")).toBe(true)
    expect(isDepthScan("area + Depth")).toBe(true)
    expect(isDepthScan("area")).toBe(false)
    const labels = (technique: string) =>
      scanInfoFields(makeScanDetail({ technique })).map((field) => field.label)
    expect(labels("depth + area")).toEqual([
      "User",
      "Date",
      "Dimensions",
      "Scan Type",
      "Aperture",
      "Sample",
      "Data Folder",
    ])
    expect(labels("area")).not.toContain("Aperture")
  })
})

describe("Scan Info", () => {
  it("shows the scan's fields from its log and its catalog", () => {
    const fields = scanInfoFields(
      makeScanDetail({ catalog: makeCatalog({ aperture: "mask" }) })
    )
    expect(
      Object.fromEntries(fields.map((field) => [field.label, field.value]))
    ).toMatchObject({
      User: "adoe",
      Dimensions: "2D",
      "Scan Type": "depth + area",
      Aperture: "Coded Aperture",
      Sample: "Ni film",
      "Data Folder": "/net/data/2026/scan_42",
    })
    expect(fields.find((field) => field.label === "Date")?.value).toMatch(
      /^2026-01-0[45] \d\d:00:00$/
    )
  })

  it("tabulates each dimension's positioners with its completed points", () => {
    expect(dimensionGroups(makeScanDetail().dimensions)).toEqual([
      {
        key: "11",
        label: "Dim 2",
        completed: "Completed 80/81",
        positioners: [
          {
            key: "11:1",
            group: "Depth",
            pv: "34ide:t80:c0:m1.VAL",
            start: "-50",
            end: "50",
            step: "1.25",
            text: null,
          },
        ],
      },
      {
        key: "12",
        label: "Dim 1",
        completed: "Completed 21/21",
        positioners: [
          {
            key: "12:1",
            group: "Sample",
            pv: "34ide:mxv:c0:m1.VAL",
            start: "-10",
            end: "10",
            step: "1",
            text: null,
          },
          // Not "start end step": the text as written.
          {
            key: "12:2",
            group: "Sample",
            pv: "34ide:mxv:c0:m3.VAL",
            start: null,
            end: null,
            step: null,
            text: "0,5",
          },
        ],
      },
    ])
  })
})

describe("Metadata", () => {
  const section = (title: string) => {
    const found = metadataSections(makeMetadata(42)).find(
      (candidate) => candidate.title === title
    )
    return Object.fromEntries(
      (found?.items ?? []).map((item) => [item.label, item.value])
    )
  }

  it("shows the ID taper's own values", () => {
    expect(section("Source")).toMatchObject({
      "ID Gap": "11.5 mm",
      "ID Taper": "0.25 mrad",
      Energy: "20 keV",
    })
  })

  it("groups the log's record in sections", () => {
    expect(metadataSections(makeMetadata(42)).map((s) => s.title)).toEqual([
      "Identifiers",
      "Source",
      "Sample Positioner",
      "Aperture Positioner",
      "Scan End",
      "Motor Group Totals",
    ])
    expect(section("Aperture Positioner")).toEqual({
      "Aperture XYZ": "4,5,6 micron",
      Description: "wire",
      "Knife Scan": "120 micron",
    })
    // Only the motor groups the log counted.
    expect(section("Motor Group Totals")).toEqual({
      "Sample Points": "441",
      "Sample Completed": "441",
      "Depth Points": "81",
      "Depth Completed": "80",
    })
  })

  it("lists each scan row's set slots", () => {
    const [outer, inner] = dimensionSections(makeScanDetail().dimensions)
    expect(outer?.title).toBe("Scan Dim 2")
    expect(inner?.items.map((item) => item.label)).toEqual([
      "Points",
      "Points Completed",
      "After",
      "Positioner 1 PV",
      "Positioner 1",
      "Positioner 1 Abs/Rel",
      "Positioner 1 Mode",
      "Positioner 2 PV",
      "Positioner 2",
      "Positioner 2 Abs/Rel",
      "Positioner 2 Mode",
      "Detector Trigger 1 PV",
      "Detector Trigger 1 Value",
    ])
    expect(inner?.items[3]?.value).toBe("34ide:mxv:c0:m1.VAL (Sample)")
    // The PVs are code-like: monospace.
    expect(
      inner?.items.filter((item) => item.mono).map((item) => item.label)
    ).toEqual(["Positioner 1 PV", "Positioner 2 PV", "Detector Trigger 1 PV"])
  })

  it("writes values with their units, and nothing for a missing one", () => {
    expect(withUnit(20, "keV")).toBe("20 keV")
    expect(withUnit(0, "s")).toBe("0 s")
    expect(withUnit("x", null)).toBe("x")
    expect(withUnit(null, "keV")).toBe("")
    expect(withUnit("", "keV")).toBe("")
  })
})
