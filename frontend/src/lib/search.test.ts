import { describe, expect, it } from "vitest"

import { parseSearch, stringifySearch } from "./search"

describe("parseSearch", () => {
  it("keeps every value a string", () => {
    expect(parseSearch("?scan=5&reconstruction=,2&indexing=5,6")).toEqual({
      scan: "5",
      reconstruction: ",2",
      indexing: "5,6",
    })
    expect(parseSearch("tab=params&flag=true&nothing=null")).toEqual({
      tab: "params",
      flag: "true",
      nothing: "null",
    })
    expect(parseSearch("")).toEqual({})
  })

  it("decodes percent-encoding and takes the first of a repeated key", () => {
    expect(parseSearch("?scan=5%2C6&note=a%20b+c&scan=7")).toEqual({
      scan: "5,6",
      note: "a b c",
    })
  })
})

describe("stringifySearch", () => {
  it("writes strings as they are, keeping commas readable", () => {
    expect(stringifySearch({ scan: "5" })).toBe("?scan=5")
    expect(stringifySearch({ scan: "5,6", reconstruction: ",2" })).toBe(
      "?scan=5,6&reconstruction=,2"
    )
    expect(stringifySearch({ hkl: "100", step: 3, open: true })).toBe(
      "?hkl=100&step=3&open=true"
    )
  })

  it("encodes characters that would change the query, and leaves out empty values", () => {
    expect(stringifySearch({ scan: "5&indexing=9", note: "a b#c" })).toBe(
      "?scan=5%26indexing%3D9&note=a+b%23c"
    )
    expect(stringifySearch({ scan: undefined, tab: null })).toBe("")
  })

  it("round-trips through parseSearch", () => {
    const search = { scan: "5,6", reconstruction: ",2", note: "x&y=z, w" }
    expect(parseSearch(stringifySearch(search))).toEqual(search)
  })
})
