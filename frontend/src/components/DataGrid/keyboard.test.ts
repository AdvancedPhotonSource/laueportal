// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest"

import { followCellLink, tabThroughLinks } from "./keyboard"

// The helpers on plain markup shaped like AG Grid's cells (DataGrid.keyboard.test.tsx runs them in
// the grid).

afterEach(() => {
  document.body.replaceChildren()
})

function row() {
  document.body.innerHTML = `
    <div class="ag-row">
      <div class="ag-cell" tabindex="-1" id="source">
        <span><a href="/scans/5">SN5</a> <a href="/reconstructions/3">R3</a></span>
      </div>
      <div class="ag-cell" tabindex="-1" id="job"><a href="/jobs/41">41</a></div>
      <div class="ag-cell" tabindex="-1" id="author">ann</div>
    </div>`
  const get = (selector: string) => {
    const element = document.querySelector<HTMLElement>(selector)
    if (!element) throw new Error(selector)
    return element
  }
  return {
    source: get("#source"),
    sn: get('#source a[href="/scans/5"]'),
    r: get('#source a[href="/reconstructions/3"]'),
    job: get("#job"),
    jobLink: get("#job a"),
    author: get("#author"),
  }
}

function keydown(
  target: HTMLElement,
  key: string,
  init: KeyboardEventInit = {}
) {
  target.focus()
  const event = new KeyboardEvent("keydown", {
    key,
    bubbles: true,
    cancelable: true,
    ...init,
  })
  Object.defineProperty(event, "target", { value: target })
  return event
}

describe("tabThroughLinks", () => {
  it("tabs from the cell to each of its links, then leaves to the next cell", () => {
    const { source, sn, r } = row()
    const first = keydown(source, "Tab")
    expect(tabThroughLinks(first)).toBe(true)
    expect(first.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(sn)

    expect(tabThroughLinks(keydown(sn, "Tab"))).toBe(true)
    expect(document.activeElement).toBe(r)

    // Past the last link AG Grid takes the key (to the next cell).
    const last = keydown(r, "Tab")
    expect(tabThroughLinks(last)).toBe(false)
    expect(last.defaultPrevented).toBe(false)
  })

  it("shift-tabs from the cell to its links in reverse, then leaves to the previous cell", () => {
    const { source, sn, r } = row()
    expect(tabThroughLinks(keydown(source, "Tab", { shiftKey: true }))).toBe(
      true
    )
    expect(document.activeElement).toBe(r)
    expect(tabThroughLinks(keydown(r, "Tab", { shiftKey: true }))).toBe(true)
    expect(document.activeElement).toBe(sn)
    expect(tabThroughLinks(keydown(sn, "Tab", { shiftKey: true }))).toBe(false)
  })

  it("leaves other keys, modified Tabs and cells without links to AG Grid", () => {
    const { source, author } = row()
    expect(tabThroughLinks(keydown(source, "Enter"))).toBe(false)
    expect(tabThroughLinks(keydown(source, "Tab", { ctrlKey: true }))).toBe(
      false
    )
    expect(tabThroughLinks(keydown(author, "Tab"))).toBe(false)
    expect(document.activeElement).toBe(author)
  })
})

describe("followCellLink", () => {
  it("follows a focused cell's first link on Enter", () => {
    const { source, job } = row()
    const followed: (string | null)[] = []
    const clicked = (event: Event) => {
      event.preventDefault() // jsdom would otherwise try to navigate
      followed.push((event.target as Element).getAttribute("href"))
    }
    document.addEventListener("click", clicked)
    expect(followCellLink(keydown(job, "Enter"))).toBe(true)
    expect(followCellLink(keydown(source, "Enter"))).toBe(true)
    expect(followed).toEqual(["/jobs/41", "/scans/5"])
    document.removeEventListener("click", clicked)
  })

  it("leaves Enter on a link to the link, and other keys and cells alone", () => {
    const { jobLink, job, author } = row()
    const clicked = vi.fn()
    document.addEventListener("click", clicked)
    expect(followCellLink(keydown(jobLink, "Enter"))).toBe(false)
    expect(followCellLink(keydown(job, " "))).toBe(false)
    expect(followCellLink(keydown(job, "Enter", { metaKey: true }))).toBe(false)
    expect(followCellLink(keydown(author, "Enter"))).toBe(false)
    expect(clicked).not.toHaveBeenCalled()
    document.removeEventListener("click", clicked)
  })
})
