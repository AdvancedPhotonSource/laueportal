import { afterEach, describe, expect, it } from "vitest"

import { cleanup, render } from "@/test/render"
import { DefinitionList } from "./DefinitionList"

afterEach(cleanup)

const pairs = (root: HTMLElement) =>
  Array.from(root.querySelectorAll("dt")).map((term) => [
    term.textContent,
    term.nextElementSibling?.textContent,
  ])

describe("DefinitionList", () => {
  it("pairs each label with its value, and reads a missing value as none", async () => {
    const root = await render(
      <DefinitionList
        rows={[
          { label: "Box Size [px]", value: "18" },
          { label: "Scan", value: "—" },
          { label: "Author", value: null },
          { label: "Status", value: <b>Finished</b> },
        ]}
      />
    )
    expect(pairs(root)).toEqual([
      ["Box Size [px]", "18"],
      ["Scan", "—none"],
      ["Author", "—none"],
      ["Status", "Finished"],
    ])
  })

  it("sets only paths, file names and code-like values in a monospace font", async () => {
    const root = await render(
      <DefinitionList
        rows={[
          { label: "Output Path", value: "/runs/index_1", mono: true },
          { label: "Box Size [px]", value: "18" },
        ]}
      />
    )
    const mono = Array.from(root.querySelectorAll("dd")).map((value) =>
      value.classList.contains("font-mono")
    )
    expect(mono).toEqual([true, false])
  })
})
