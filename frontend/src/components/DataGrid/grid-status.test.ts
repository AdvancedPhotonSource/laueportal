import { describe, expect, it } from "vitest"

import { ApiError } from "@/api"
import type { GridQuery } from "./datasource"
import { BlockStatus, heldBlocks } from "./grid-status"

const block = (offset: number, filter?: string): GridQuery =>
  filter === undefined ? { offset, limit: 100 } : { offset, limit: 100, filter }

const page = (total: number) => ({
  rows: [],
  total,
  as_of: "2026-10-19T12:00:00Z",
})

const down = new ApiError(503, "Service Unavailable")

describe("BlockStatus", () => {
  it("loads after a sort or filter change, then shows the rows or says there are none", () => {
    const status = new BlockStatus()
    expect(status.request(true, block(0))).toEqual({ kind: "loading" })
    expect(status.page(page(250), block(0))).toEqual({
      kind: "ready",
      total: 250,
      filtered: false,
    })
    expect(status.request(true, block(0, "{}"))).toEqual({ kind: "loading" })
    expect(status.page(page(0), block(0, "{}"))).toEqual({
      kind: "ready",
      total: 0,
      filtered: true,
    })
  })

  it("leaves the status alone for more blocks and refreshes", () => {
    const status = new BlockStatus()
    status.request(true, block(0))
    status.page(page(250), block(0))
    expect(status.request(false, block(100))).toBeUndefined()
    expect(status.request(false, block(0))).toBeUndefined()
  })

  it("shows a failed first block in place of the rows, and its retry", () => {
    const status = new BlockStatus()
    status.request(true, block(0))
    expect(status.error(down, block(0))).toEqual({
      kind: "error",
      error: down,
      hasRows: false,
      retrying: false,
    })
    expect(status.request(false, block(0))).toEqual({
      kind: "error",
      error: down,
      hasRows: false,
      retrying: true,
    })
    expect(status.page(page(250), block(0))).toEqual({
      kind: "ready",
      total: 250,
      filtered: false,
    })
  })

  it("keeps a later block's failure while other blocks answer", () => {
    const status = new BlockStatus()
    status.request(true, block(0))
    status.page(page(250), block(0))
    status.request(false, block(100))
    // Rows show: the error is a banner over them.
    expect(status.error(down, block(100))).toMatchObject({
      kind: "error",
      hasRows: true,
    })
    // Block 0 refreshed fine; block 100 still failed.
    expect(status.page(page(250), block(0))).toMatchObject({
      kind: "error",
      error: down,
      hasRows: true,
      retrying: false,
    })
    expect(status.page(page(250), block(100))).toEqual({
      kind: "ready",
      total: 250,
      filtered: false,
    })
  })

  it("forgets the failures when the sort or filter changes", () => {
    const status = new BlockStatus()
    status.request(true, block(0))
    status.error(down, block(0))
    expect(status.request(true, block(0, "{}"))).toEqual({ kind: "loading" })
    expect(status.page(page(3), block(0, "{}"))).toEqual({
      kind: "ready",
      total: 3,
      filtered: true,
    })
  })

  it("forgets the failure of a block the grid has dropped, back to the rows' status", () => {
    const status = new BlockStatus()
    status.request(true, block(0))
    status.page(page(1500), block(0))
    status.request(false, block(300))
    expect(status.error(down, block(300))).toMatchObject({
      kind: "error",
      hasRows: true,
    })
    status.request(false, block(1400))
    // Block 1400 answered; block 300 is still failed, so the banner stays.
    expect(status.page(page(1500), block(1400))).toMatchObject({
      kind: "error",
      error: down,
    })
    // Every block the grid holds is fine: nothing to forget.
    expect(status.retain(new Set([300, 1400]))).toBeUndefined()
    // AG Grid dropped block 300 (the least recently used): its failure goes with it.
    expect(status.retain(new Set([500, 1400]))).toEqual({
      kind: "ready",
      total: 1500,
      filtered: false,
    })
    // Scrolled back to, block 300 is asked for afresh: a new failure shows again.
    expect(status.request(false, block(300))).toBeUndefined()
    expect(status.error(down, block(300))).toMatchObject({ kind: "error" })
  })

  it("keeps the failures of the blocks the grid still holds", () => {
    const status = new BlockStatus()
    status.request(true, block(0))
    status.page(page(1500), block(0))
    const other = new ApiError(500, "Internal Server Error")
    status.error(down, block(300))
    status.error(other, block(700))
    expect(status.retain(new Set([0, 700]))).toEqual({
      kind: "error",
      error: other,
      hasRows: true,
      retrying: false,
    })
  })
})

describe("heldBlocks", () => {
  it("reads the blocks' first rows from AG Grid's cache state", () => {
    expect(
      heldBlocks({
        "3": {
          blockNumber: 3,
          startRow: 300,
          endRow: 400,
          pageStatus: "failed",
        },
        "14": {
          blockNumber: 14,
          startRow: 1400,
          endRow: 1500,
          pageStatus: "loaded",
        },
      })
    ).toEqual(new Set([300, 1400]))
    expect(heldBlocks({})).toEqual(new Set())
    expect(heldBlocks(undefined)).toEqual(new Set())
    expect(heldBlocks({ "1": { startRow: "100" }, "2": null })).toEqual(
      new Set()
    )
  })
})
