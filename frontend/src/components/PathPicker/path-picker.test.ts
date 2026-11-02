import { describe, expect, it } from "vitest"

import {
  breadcrumbs,
  extensionFilter,
  folderOf,
  formatBytes,
  into,
  moveActive,
  nameOf,
  placeOf,
  startLocation,
  upFrom,
} from "./path-picker"

describe("folderOf and nameOf", () => {
  it.each([
    ["/data/2026/scan.xml", "/data/2026", "scan.xml"],
    ["/data/2026/", "/data", "2026"],
    ["/data", "/", "data"],
    ["/", "/", ""],
  ])("%s is %s in %s", (path, folder, name) => {
    expect(folderOf(path)).toBe(folder)
    expect(nameOf(path)).toBe(name)
  })
})

describe("startLocation", () => {
  it("opens a folder field at its folder, from the value", () => {
    expect(startLocation(" /data/scan_7/ ", "directory")).toEqual({
      kind: "path",
      path: "/data/scan_7",
      fromValue: true,
    })
  })

  it("opens a file field at the file's folder, the file highlighted", () => {
    expect(startLocation("/data/logs/scan.xml", "file")).toEqual({
      kind: "path",
      path: "/data/logs",
      highlight: "scan.xml",
      fromValue: true,
    })
  })

  it("falls back to the start path, then to the roots", () => {
    expect(startLocation("", "file", "/data/logs")).toEqual({
      kind: "path",
      path: "/data/logs",
      fromValue: true,
    })
    // Only absolute paths are tried: anything else is the roots.
    expect(startLocation("scan_7", "directory", "/data")).toEqual({
      kind: "path",
      path: "/data",
      fromValue: true,
    })
    expect(startLocation("~/data", "directory")).toEqual({ kind: "roots" })
    expect(startLocation("", "directory", "relative")).toEqual({
      kind: "roots",
    })
  })
})

describe("breadcrumbs", () => {
  it("names the root, then each folder below it", () => {
    expect(breadcrumbs("data", "/net/data", "/net/data/2026/scan_7")).toEqual([
      { label: "data", path: "/net/data" },
      { label: "2026", path: "/net/data/2026" },
      { label: "scan_7", path: "/net/data/2026/scan_7" },
    ])
  })

  it("is the root alone at the root's folder", () => {
    expect(breadcrumbs("data", "/net/data", "/net/data/")).toEqual([
      { label: "data", path: "/net/data" },
    ])
  })

  it("works for a root at the file system's top", () => {
    expect(breadcrumbs("all", "/", "/srv/data")).toEqual([
      { label: "all", path: "/" },
      { label: "srv", path: "/srv" },
      { label: "data", path: "/srv/data" },
    ])
  })

  it("does not mistake a sibling with the same prefix for a subfolder", () => {
    expect(breadcrumbs("data", "/net/data", "/net/database/x")).toEqual([
      { label: "data", path: "/net/data" },
    ])
  })
})

describe("upFrom", () => {
  const place = (path: string) => ({
    root: "data",
    rootPath: "/net/data",
    path,
  })

  it("goes to the folder above in the same root, highlighting the one left", () => {
    expect(upFrom(place("/net/data/2026/scan_7"))).toEqual({
      kind: "path",
      path: "/net/data/2026",
      root: "data",
      rootPath: "/net/data",
      highlight: "scan_7",
    })
  })

  it("goes to the roots from a root's folder", () => {
    expect(upFrom(place("/net/data"))).toEqual({ kind: "roots" })
    expect(upFrom(place("/net/database/x"))).toEqual({ kind: "roots" })
  })
})

describe("into and placeOf", () => {
  const data = { root: "data", rootPath: "/net/data", path: "/net/data/2026" }

  it("carries the root into a folder inside it", () => {
    const location = into(data, "/net/data/2026/scan_7")
    expect(location).toEqual({
      kind: "path",
      path: "/net/data/2026/scan_7",
      root: "data",
      rootPath: "/net/data",
    })
    expect(placeOf(location)).toEqual({
      root: "data",
      rootPath: "/net/data",
      path: "/net/data/2026/scan_7",
    })
  })

  it("does not for a folder elsewhere (a link into another root)", () => {
    const location = into(data, "/net/runs/r1")
    expect(location).toEqual({ kind: "path", path: "/net/runs/r1" })
    expect(placeOf(location)).toBeUndefined()
  })

  it("places a root opened from the roots at its folder", () => {
    expect(
      placeOf({ kind: "root", root: "data", rootPath: "/net/data" })
    ).toEqual({ root: "data", rootPath: "/net/data", path: "/net/data" })
    expect(placeOf({ kind: "root", root: "data" })).toBeUndefined()
    expect(placeOf({ kind: "roots" })).toBeUndefined()
  })
})

describe("moveActive", () => {
  it.each([
    ["ArrowDown", 0, 1],
    ["ArrowDown", 4, 4],
    ["ArrowDown", -1, 0],
    ["ArrowUp", 2, 1],
    ["ArrowUp", 0, 0],
    ["ArrowUp", -1, 0],
    ["Home", 3, 0],
    ["End", 0, 4],
    ["PageDown", 0, 4],
    ["PageUp", 4, 0],
  ])("%s from %i in 5 entries is %i", (key, from, to) => {
    expect(moveActive(key, from, 5)).toBe(to)
  })

  it("ignores other keys and empty lists", () => {
    expect(moveActive("Enter", 0, 5)).toBeUndefined()
    expect(moveActive("ArrowDown", -1, 0)).toBeUndefined()
  })
})

describe("formatBytes", () => {
  it.each([
    [0, "0 B"],
    [1023, "1023 B"],
    [1536, "1.5 KiB"],
    [1024 * 1024, "1 MiB"],
    [25 * 1024 * 1024 + 1, "25 MiB"],
    [3.25 * 1024 ** 3, "3.3 GiB"],
  ])("%i bytes is %s", (bytes, text) => {
    expect(formatBytes(bytes)).toBe(text)
  })
})

describe("extensionFilter", () => {
  it("joins the extensions as the API takes them", () => {
    expect(extensionFilter([".XML", " .txt "])).toBe(".xml,.txt")
    expect(extensionFilter([])).toBeUndefined()
    expect(extensionFilter(undefined)).toBeUndefined()
  })
})
