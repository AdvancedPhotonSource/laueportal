import { describe, expect, it } from "vitest"

import { pageTitle, titleHead, titleHeadFor } from "./title"

describe("pageTitle", () => {
  it("formats '<page> · 3DMN Portal'", () => {
    expect(pageTitle("Scans")).toBe("Scans · 3DMN Portal")
    expect(pageTitle()).toBe("3DMN Portal")
  })
})

describe("titleHead", () => {
  it("builds a route head with the page title", () => {
    expect(titleHead("Run Monitor")({ match: { status: "success" } })).toEqual({
      meta: [{ title: "Run Monitor · 3DMN Portal" }],
    })
    expect(titleHead()({ match: { status: "success" } })).toEqual({
      meta: [{ title: "3DMN Portal" }],
    })
  })

  it("titles the 404 and error boundaries the router shows instead", () => {
    expect(titleHead("Scan 7")({ match: { status: "notFound" } })).toEqual({
      meta: [{ title: "Page not found · 3DMN Portal" }],
    })
    expect(titleHead("Scan 7")({ match: { status: "error" } })).toEqual({
      meta: [{ title: "Error · 3DMN Portal" }],
    })
  })
})

describe("titleHeadFor", () => {
  it("titles a detail page with the name its head builds", () => {
    expect(titleHeadFor({ status: "success" }, "Indexing I70")).toEqual({
      meta: [{ title: "Indexing I70 · 3DMN Portal" }],
    })
    expect(titleHeadFor({ status: "pending" }, "Job 5")).toEqual({
      meta: [{ title: "Job 5 · 3DMN Portal" }],
    })
  })

  it("titles the 404 and error boundaries the router shows instead", () => {
    expect(titleHeadFor({ status: "notFound" }, "Indexing I70")).toEqual({
      meta: [{ title: "Page not found · 3DMN Portal" }],
    })
    expect(titleHeadFor({ status: "error" }, "Indexing I70")).toEqual({
      meta: [{ title: "Error · 3DMN Portal" }],
    })
  })
})
