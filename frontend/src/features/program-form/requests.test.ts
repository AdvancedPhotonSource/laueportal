// Each program's form validates and creates through its own endpoint.
import { beforeEach, describe, expect, it, vi } from "vitest"

import { createProgramRun, validateProgram } from "./requests"

const api = vi.hoisted(() => ({
  validateIndexingRun: vi.fn(),
  validateLaueMatchingRun: vi.fn(),
  validateReconstructionRun: vi.fn(),
  createIndexingRun: vi.fn(),
  createLaueMatchingRun: vi.fn(),
  createReconstructionRun: vi.fn(),
}))

vi.mock("@/api", async (original) => ({
  ...(await original<typeof import("@/api")>()),
  ...api,
}))

describe("the program's requests", () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it.each([
    ["lauego", api.validateIndexingRun, api.createIndexingRun],
    ["laue_matching", api.validateLaueMatchingRun, api.createLaueMatchingRun],
    ["wire", api.validateReconstructionRun, api.createReconstructionRun],
  ] as const)("%s uses its own endpoints", async (kind, validate, create) => {
    await validateProgram(kind, { a: 1 })
    await createProgramRun(kind, { a: 1 }, "key")
    expect(validate).toHaveBeenCalledTimes(1)
    expect(create).toHaveBeenCalledTimes(1)
    const others = Object.values(api).filter(
      (mock) => mock !== validate && mock !== create
    )
    for (const other of others) expect(other).not.toHaveBeenCalled()
  })
})
