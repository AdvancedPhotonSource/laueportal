import { afterEach, describe, expect, it } from "vitest"

import { cleanup, render, renderWithRouter } from "@/test/render"
import { RunProgressBar } from "./RunProgressBar"
import {
  IndexingRunLink,
  JobLink,
  ReconstructionRunLink,
  RunLink,
  ScanLink,
  SourceLinks,
} from "./RunLinks"
import { RunStatusBadge } from "./RunStatusBadge"

// The shared status, progress and link components (grids and cards).

afterEach(cleanup)

function links(container: HTMLElement): [string, string | null][] {
  return Array.from(container.querySelectorAll("a")).map((a) => [
    a.textContent,
    a.getAttribute("href"),
  ])
}

describe("RunStatusBadge", () => {
  it.each([
    ["queued", "Queued"],
    ["running", "Running"],
    ["finished", "Finished"],
    ["failed", "Failed"],
    ["cancelled", "Cancelled"],
  ] as const)("shows %s as %s", async (status, label) => {
    const container = await render(<RunStatusBadge status={status} />)
    const badge = container.querySelector("[data-status]")
    expect(badge?.getAttribute("data-status")).toBe(status)
    expect(badge?.textContent).toBe(label)
  })

  it("shows processed/total while running", async () => {
    const container = await render(
      <>
        <RunStatusBadge status="running" processed={3} total={10} />
        <RunStatusBadge status="finished" processed={10} total={10} />
      </>
    )
    const badges = Array.from(container.querySelectorAll("[data-status]"))
    expect(badges.map((badge) => badge.textContent)).toEqual([
      "Running 3/10",
      "Finished",
    ])
  })

  it("names a status it does not know", async () => {
    const container = await render(<RunStatusBadge status="paused" />)
    expect(container.textContent).toBe("Unknown (paused)")
  })
})

describe("RunProgressBar", () => {
  it("shows its text over a bar of succeeded, failed, not-run and pending inputs", async () => {
    const container = await render(
      <RunProgressBar
        counts={{ n_inputs: 10, n_succeeded: 5, n_failed: 1, n_not_run: 2 }}
        status="running"
      />
    )
    expect(container.textContent).toBe("6/10 processed, 1 failed, 2 not run")
    const bar = container.querySelector('[role="progressbar"]')
    expect(bar?.getAttribute("aria-valuenow")).toBe("6")
    expect(bar?.getAttribute("aria-valuemax")).toBe("10")
    expect(bar?.getAttribute("aria-valuetext")).toBe(
      "6/10 processed, 1 failed, 2 not run"
    )
    const segments = Array.from(
      container.querySelectorAll<HTMLElement>("[data-segment]")
    ).map((segment) => [
      segment.dataset.segment,
      segment.style.width,
      segment.title,
      segment.classList.contains("progress-stripes"),
    ])
    expect(segments).toEqual([
      ["succeeded", "50%", "5 succeeded", false],
      ["failed", "10%", "1 failed", false],
      ["notRun", "20%", "2 not run", false],
      ["pending", "20%", "2 pending", true],
    ])
  })

  it("stops the stripes once the run ended", async () => {
    const container = await render(
      <RunProgressBar
        counts={{ n_inputs: 4, n_succeeded: 2, n_failed: 0, n_not_run: 0 }}
        status="cancelled"
      />
    )
    const pending = container.querySelector<HTMLElement>(
      '[data-segment="pending"]'
    )
    expect(pending?.title).toBe("2 unaccounted")
    expect(pending?.classList.contains("progress-stripes")).toBe(false)
  })

  it('says "No inputs" without inputs', async () => {
    const container = await render(
      <RunProgressBar
        counts={{ n_inputs: 0, n_succeeded: 0, n_failed: 0, n_not_run: 0 }}
      />
    )
    expect(container.textContent).toBe("No inputs")
    expect(container.querySelector('[role="progressbar"]')).toBeNull()
  })
})

describe("links", () => {
  it("links a scan by its number to its page", async () => {
    const container = await renderWithRouter(<ScanLink scan={276} />)
    expect(links(container)).toEqual([["276", "/scans/276"]])
  })

  it("links runs and jobs by their labels", async () => {
    const container = await renderWithRouter(
      <>
        <IndexingRunLink id={12} />
        <ReconstructionRunLink id={3} />
        <JobLink id={41} />
        <RunLink run={{ kind: "indexing", id: 70, label: "I70" }} />
        <RunLink run={{ kind: "reconstruction", id: 5, label: "R5" }} />
      </>
    )
    expect(links(container)).toEqual([
      ["I12", "/indexing/12"],
      ["R3", "/reconstructions/3"],
      ["41", "/jobs/41"],
      ["I70", "/indexing/70"],
      ["R5", "/reconstructions/5"],
    ])
  })

  it('says "Direct" for an indexation without a reconstruction, and nothing for a job without a run', async () => {
    const container = await renderWithRouter(
      <>
        <ReconstructionRunLink id={null} />
        <RunLink run={null} />
      </>
    )
    expect(links(container)).toEqual([])
    expect(container.textContent).toBe("Direct")
  })

  it('links a run\'s sources as SN and R, or says "Unlinked"', async () => {
    const both = await renderWithRouter(
      <SourceLinks scan={276} reconstruction={3} />
    )
    expect(links(both)).toEqual([
      ["SN276", "/scans/276"],
      ["R3", "/reconstructions/3"],
    ])
    cleanup()
    const scanOnly = await renderWithRouter(<SourceLinks scan={276} />)
    expect(links(scanOnly)).toEqual([["SN276", "/scans/276"]])
    cleanup()
    const none = await renderWithRouter(
      <SourceLinks scan={null} reconstruction={null} />
    )
    expect(links(none)).toEqual([])
    expect(none.textContent).toBe("Unlinked")
  })
})
