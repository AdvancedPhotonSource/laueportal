import { afterEach, describe, expect, it } from "vitest"

import { sectionIdentity } from "@/features/program-form"
import { specOf } from "@/test/programs"
import { makeReconstructionRunDetail } from "@/test/reconstruction"
import { byRole, cleanup, render } from "@/test/render"
import { ParametersTab } from "./ParametersTab"

afterEach(cleanup)

const cards = (root: ParentNode) =>
  Array.from(root.querySelectorAll<HTMLElement>('[data-slot="card"]'))
const titleOf = (card: HTMLElement) =>
  card.querySelector("h2")?.textContent ?? ""
/** A row's value cell, by its label. */
const cellOf = (root: ParentNode, label: string) =>
  Array.from(root.querySelectorAll("dt")).find(
    (term) => term.textContent === label
  )?.nextElementSibling

describe("the wire viewer's Parameters tab", () => {
  it("shows the wire form's section cards in its order, one column, each with its stripe and icon", async () => {
    const container = await render(
      <ParametersTab run={makeReconstructionRunDetail()} />
    )
    const form = specOf("wire").sections.map((section) => section.group)
    expect(cards(container).map(titleOf)).toEqual([
      ...form,
      "Other recorded values",
    ])
    const column = container.firstElementChild
    expect(column?.classList.contains("flex-col")).toBe(true)
    expect(
      cards(container).every((card) => card.parentElement === column)
    ).toBe(true)
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
      container.querySelectorAll(
        "input, textarea, select, button, [contenteditable]"
      )
    ).toHaveLength(0)
  })

  it("shows a path inside a file root with its root-relative form, which wraps", async () => {
    const container = await render(
      <ParametersTab run={makeReconstructionRunDetail()} />
    )
    const folder = cellOf(container, "Folder Path")
    const relative = folder?.querySelector(
      '[title="Relative to its file root"]'
    )
    expect(folder?.textContent).toBe(
      "/data/demo/synthetic-reconstructiondata/demo/synthetic-reconstruction"
    )
    expect(relative?.textContent).toBe("data/demo/synthetic-reconstruction")
    // A long path breaks anywhere rather than overflow a narrow card.
    expect(relative?.classList.contains("wrap-anywhere")).toBe(true)
    // Outside every file root: the path alone.
    expect(
      cellOf(container, "Geometry File")?.querySelector("[title]")
    ).toBeNull()
  })

  it("says that no wire parameters are stored", async () => {
    await render(
      <ParametersTab
        run={makeReconstructionRunDetail({ parameters: null, method: "x" })}
      />
    )
    expect(byRole("status").map((line) => line.textContent)).toEqual([
      "No wire parameters stored (method x)",
    ])
  })
})
