import { describe, expect, it } from "vitest"
import { PART_TITLES } from "@/features/live/live-form"
import { PROGRAMS, specOf } from "@/test/programs"
import { SECTION_IDENTITIES, sectionIdentity } from "./section-identity"

describe("section identity", () => {
  it("styles every section the programs draw (batch and live) and every Live card", () => {
    const programs = PROGRAMS.programs.map((program) => program.kind)
    expect(programs).toEqual(
      expect.arrayContaining(["lauego", "wire", "lauego-live", "wire-live"])
    )
    const titles = new Set([
      ...programs.flatMap((kind) =>
        specOf(kind).sections.map((section) => section.group)
      ),
      ...Object.values(PART_TITLES),
    ])
    const missing = [...titles].filter(
      (title) => !SECTION_IDENTITIES.has(title)
    )
    expect(missing).toEqual([])
  })

  it("gives an unknown title the neutral accent and a generic icon", () => {
    const unknown = sectionIdentity("Something New")
    expect(unknown.stripe).toBe(sectionIdentity("Identity").stripe)
    expect(unknown.accent).toBe(sectionIdentity("Notes").accent)
    const icons = new Set(
      [...SECTION_IDENTITIES.values()].map((identity) => identity.icon)
    )
    expect(icons.has(unknown.icon)).toBe(false)
  })
})
