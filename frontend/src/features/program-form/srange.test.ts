import { describe, expect, it } from "vitest"
import { srangeCount } from "./srange"

describe("srangeCount", () => {
  it.each([
    ["", 0],
    ["5", 1],
    ["1-10", 10],
    ["3,5,9-20", 14],
    ["1-10:3", 4],
    ["20-30,1-4", 15],
    ["1-5,4-8", null],
    ["10-1", null],
    ["1-x", null],
  ])("counts %j as %j", (text, count) => {
    expect(srangeCount(text)).toBe(count)
  })
})
