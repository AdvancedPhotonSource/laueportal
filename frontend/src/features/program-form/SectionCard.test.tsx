import { afterEach, describe, expect, it } from "vitest"

import { cleanup, render } from "@/test/render"
import { SectionCard } from "./SectionCard"

afterEach(cleanup)

const part = (root: ParentNode, slot: string) =>
  root.querySelector<HTMLElement>(`[data-slot="${slot}"]`)

describe("the section card", () => {
  it("with content: a header over a divider, then the content", async () => {
    const container = await render(
      <SectionCard title="Notes">
        <p>body</p>
      </SectionCard>
    )
    const card = part(container, "card")
    const header = part(container, "card-header")
    expect(part(container, "card-content")?.textContent).toBe("body")
    expect(header?.classList.contains("border-b-0")).toBe(false)
    expect(card?.classList.contains("pb-0")).toBe(false)
    expect(header?.querySelector("h2")?.textContent).toBe("Notes")
  })

  it("without content (none, or false): its header alone, with no divider and no bottom padding", async () => {
    for (const content of [undefined, false]) {
      cleanup()
      const container = await render(
        <SectionCard title="Reconstruction">{content}</SectionCard>
      )
      expect(part(container, "card-content")).toBeNull()
      expect(
        part(container, "card-header")?.classList.contains("border-b-0")
      ).toBe(true)
      expect(part(container, "card")?.classList.contains("pb-0")).toBe(true)
    }
  })

  it("draws no action cell for an action that draws nothing (a `cond && <Button>` that is false)", async () => {
    for (const action of [false, null, undefined]) {
      cleanup()
      const container = await render(
        <SectionCard title="Identity" action={action}>
          <p>body</p>
        </SectionCard>
      )
      // An empty action cell would take the header's second column (`has-data-[slot=card-action]`).
      expect(part(container, "card-action")).toBeNull()
    }
    cleanup()
    const container = await render(
      <SectionCard
        title="Peak Search Parameters"
        action={<button>Restore</button>}
      >
        <p>body</p>
      </SectionCard>
    )
    const header = part(container, "card-header")
    const action = part(container, "card-action")
    expect(action?.textContent).toBe("Restore")
    expect(header?.lastElementChild).toBe(action)
  })

  it("puts its lead first in the header, before the icon and the title, in a column of its own", async () => {
    const container = await render(
      <SectionCard title="Indexing" lead={<button role="switch">on</button>}>
        <p>body</p>
      </SectionCard>
    )
    const header = part(container, "card-header")
    expect(header?.firstElementChild?.getAttribute("role")).toBe("switch")
    expect(header?.firstElementChild?.nextElementSibling).toBe(
      part(container, "card-title")
    )
    expect(header?.classList.contains("grid-cols-[auto_1fr]")).toBe(true)

    cleanup()
    const plain = await render(
      <SectionCard title="Indexing" lead={false}>
        <p>body</p>
      </SectionCard>
    )
    const plainHeader = part(plain, "card-header")
    expect(plainHeader?.firstElementChild).toBe(part(plain, "card-title"))
    expect(plainHeader?.classList.contains("grid-cols-[auto_1fr]")).toBe(false)
  })
})
