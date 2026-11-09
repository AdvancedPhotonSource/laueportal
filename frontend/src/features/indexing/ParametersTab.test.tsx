import { afterEach, describe, expect, it } from "vitest"

import { sectionIdentity } from "@/features/program-form"
import { makeIndexingRunDetail, makeLaueGoParameters } from "@/test/indexing"
import { specOf } from "@/test/programs"
import { byRole, cleanup, render } from "@/test/render"
import { ParametersTab } from "./ParametersTab"

afterEach(cleanup)

const cards = (root: ParentNode) =>
  Array.from(root.querySelectorAll<HTMLElement>('[data-slot="card"]'))
const titleOf = (card: HTMLElement) =>
  card.querySelector("h2")?.textContent ?? ""
/** The value of a row, by its label. */
const valueOf = (root: ParentNode, label: string) =>
  Array.from(root.querySelectorAll("dt")).find(
    (term) => term.textContent === label
  )?.nextElementSibling?.textContent

describe("the indexing viewer's Parameters tab", () => {
  it("shows the create form's section cards in its order, one column, then the other recorded values", async () => {
    const container = await render(
      <ParametersTab run={makeIndexingRunDetail()} />
    )
    const form = specOf("lauego").sections.map((section) => section.group)
    expect(cards(container).map(titleOf)).toEqual([
      ...form,
      "Other recorded values",
    ])
    expect(byRole("heading", "Peak Search Parameters")).toHaveLength(1)
    // One column: every card is an item of the tab's one flex column, none in a grid.
    const column = container.firstElementChild
    expect(column?.classList.contains("flex-col")).toBe(true)
    expect(
      cards(container).every((card) => card.parentElement === column)
    ).toBe(true)
    expect(container.querySelector('[class*="grid-cols-2"]')).toBeNull()
  })

  it("gives each card its section's stripe and icon, Other recorded values the neutral accent", async () => {
    const container = await render(
      <ParametersTab run={makeIndexingRunDetail()} />
    )
    for (const card of cards(container)) {
      const { stripe, accent } = sectionIdentity(titleOf(card))
      for (const name of stripe.split(" ")) {
        expect(card.classList.contains(name)).toBe(true)
      }
      const icon = card.querySelector('[data-slot="card-title"] svg')
      expect(icon?.classList.contains(accent)).toBe(true)
    }
    const other = cards(container).find(
      (card) => titleOf(card) === "Other recorded values"
    )
    expect(other?.classList.contains("before:bg-section-neutral")).toBe(true)
    expect(
      other
        ?.querySelector('[data-slot="card-title"] svg')
        ?.classList.contains("text-section-neutral")
    ).toBe(true)
    const peaks = cards(container).find(
      (card) => titleOf(card) === "Peak Search Parameters"
    )
    expect(peaks?.classList.contains("before:bg-section-violet")).toBe(true)
  })

  it("shows the stored values read-only", async () => {
    const container = await render(
      <ParametersTab
        run={makeIndexingRunDetail({
          notes: "first pass\nsecond line",
          parameters: makeLaueGoParameters({
            filename_prefixes: ["Ni_%d.h5", "Ni_b_%d.h5"],
          }),
        })}
      />
    )
    expect(
      container.querySelectorAll(
        "input, textarea, select, button, [contenteditable]"
      )
    ).toHaveLength(0)
    expect(valueOf(container, "Structures File")).toBe("/data/Ni.xml")
    expect(valueOf(container, "Filename")).toBe("Ni_%d.h5, Ni_b_%d.h5")
    expect(valueOf(container, "Notes")).toBe("first pass\nsecond line")
  })

  it("says that no parameters are stored, and shows the run's own values", async () => {
    const container = await render(
      <ParametersTab
        run={makeIndexingRunDetail({
          method: "laue_matching",
          parameters: null,
        })}
      />
    )
    expect(container.textContent).toContain(
      "No parameters stored (method laue_matching)."
    )
    expect(cards(container).map(titleOf)).toEqual([
      "Identity",
      "File Paths",
      "Notes",
    ])
  })
})
