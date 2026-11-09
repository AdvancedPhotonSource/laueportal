import {
  expect,
  test,
  type Locator,
  type Page,
  type Route,
} from "@playwright/test"

import { internalError, type ProblemAnswer } from "./problems"

// The app shell with the health API mocked: the navbar's pages and Menu, the live Worker indicator
// and its tooltip, and the 404 page.
// e2e/stack.spec.ts checks the indicator against the real stack.

const HEALTH_OK = {
  db: "ok",
  worker: "running",
  version: "2.0.0.dev0",
}

/** An answer of GET /api/v1/health: a body, a problem, the API stopped, or none yet. */
type HealthAnswer = object | ProblemAnswer | "unreachable" | "pending"

/** Answer GET /api/v1/health with `answer()`, read on every request (so a test can change it). */
async function mockHealth(page: Page, answer: () => HealthAnswer) {
  await page.route("**/api/v1/health", (route: Route) => {
    const body = answer()
    if (body === "pending") return undefined // never answered: the first check stays pending
    if (body === "unreachable") return route.abort("connectionrefused")
    return "contentType" in body
      ? route.fulfill(body)
      : route.fulfill({ json: body })
  })
}

/** The open tooltip (Base UI's tooltips have no role; a closing one stays until it has faded). */
function openTooltip(page: Page) {
  return page.locator('[data-slot="tooltip-content"][data-open]')
}

function indicators(page: Page) {
  return page
    .getByRole("group", { name: "Live system status" })
    .getByRole("link")
}

/** The indicator's visible text: its content without the screen-reader-only name (a dot shows none). */
async function visibleText(page: Page): Promise<string> {
  return indicators(page).evaluate((link) => {
    const copy = link.cloneNode(true) as HTMLElement
    for (const hidden of copy.querySelectorAll(".sr-only")) hidden.remove()
    return copy.textContent.trim()
  })
}

/** A pill's state words ("not running"). */
function stateWords(page: Page) {
  return indicators(page).locator('[data-slot="status-words"]')
}

function dot(page: Page) {
  return indicators(page).locator('[data-slot="status-dot"]')
}

/** A CSS colour as the page computes it (in its theme's tokens), to compare with an element's. */
async function computedColour(page: Page, value: string): Promise<string> {
  return page.evaluate((value) => {
    const probe = document.createElement("span")
    probe.style.backgroundColor = value
    document.body.append(probe)
    const colour = getComputedStyle(probe).backgroundColor
    probe.remove()
    return colour
  }, value)
}

/** The dot's paint: its fill, and its outline's colour ("none" without one). */
async function dotPaint(page: Page) {
  return dot(page).evaluate((element) => {
    const style = getComputedStyle(element)
    return {
      fill: style.backgroundColor,
      outline: style.borderTopWidth === "0px" ? "none" : style.borderTopColor,
    }
  })
}

/** The colours of a pill's words: its subject's, then its state's. */
async function wordColours(page: Page): Promise<string[]> {
  return indicators(page).evaluate((link) =>
    [
      link.querySelector('[data-slot="status-subject"]'),
      link.querySelector('[data-slot="status-words"]'),
    ].map((part) => (part ? getComputedStyle(part).color : "none"))
  )
}

/**
 * A pill's words as drawn: the parts on the state words' one-line row, in reading order (a subject that does not
 * fit is on the row's second line, which the row hides), those of them cut short (to an ellipsis or a letter),
 * whether that row is one line high, and whether the state's words are whole and inside the pill's padding.
 */
async function pillLayout(page: Page) {
  return indicators(page).evaluate((link) => {
    const words = link.querySelector('[data-slot="status-words"]')
    const row = words?.parentElement
    if (!words || !row) throw new Error("no pill")
    const box = (element: Element) => element.getBoundingClientRect()
    const inRow = (part: Element) =>
      box(part).top >= box(row).top - 0.5 &&
      box(part).bottom <= box(row).bottom + 0.5
    const padding = parseFloat(getComputedStyle(link).paddingRight)
    const shown = [...row.children].filter(inRow)
    return {
      shown: shown.map((part) => part.textContent).join(" "),
      cut: shown
        .filter((part) => part.scrollWidth > part.clientWidth)
        .map((part) => part.textContent),
      oneLine:
        getComputedStyle(row).overflow === "hidden" &&
        box(row).height <= box(words).height + 0.5,
      wordsWhole:
        words.scrollWidth <= words.clientWidth &&
        box(words).right <= box(link).right - padding + 0.5,
    }
  })
}

/** The box shadow that draws an element's focus ring (Tailwind's `ring-3`). */
async function shadowOf(locator: Locator): Promise<string> {
  return locator.evaluate((element) => getComputedStyle(element).boxShadow)
}

/** The worker running: the success colour desaturated toward the muted text (`bg-success-quiet`). */
const QUIET_SUCCESS =
  "color-mix(in oklab, var(--success) 60%, var(--muted-foreground))"

/** The dot is small, its link is not: at least as large as a link with a short label ("Worker"). */
async function expectLabelSizedTarget(page: Page, width: number) {
  const box = await indicators(page).boundingBox()
  expect(
    box?.width ?? 0,
    `link width at ${String(width)} px`
  ).toBeGreaterThanOrEqual(73.6)
  expect(
    box?.height ?? 0,
    `link height at ${String(width)} px`
  ).toBeGreaterThanOrEqual(25.1)
}

test("the navbar and its Menu reach every page", async ({ page }) => {
  await mockHealth(page, () => HEALTH_OK)
  await page.goto("/")
  const nav = page.getByRole("navigation", { name: "Main" })
  const heading = page.getByRole("heading", { level: 1 })

  for (const [label, path] of [
    ["Scans", "/scans"],
    ["Reconstructions", "/reconstructions"],
    ["Indexations", "/indexing"],
    ["Run Monitor", "/runs"],
    // The e2e stack has live processing on (deploy/supervisor/e2e.sh), so GET /programs lists the live programs.
    ["Live", "/live"],
  ] as const) {
    const link = nav.getByRole("link", { name: label, exact: true })
    await link.click()
    await expect(page).toHaveURL(new RegExp(`${path}$`))
    await expect(page).toHaveTitle(`${label} · 3DMN Portal`)
    await expect(heading).toHaveText(label)
    await expect(link).toHaveAttribute("aria-current", "page")
    await expect(nav.locator('[aria-current="page"]')).toHaveCount(1)
  }

  for (const [item, path, section] of [
    ["Import Scans", "/scans/import", "Scans"],
    ["New Wire Reconstruction", "/reconstructions/new", "Reconstructions"],
    ["New Indexation", "/indexing/new", "Indexations"],
  ] as const) {
    await nav.getByRole("button", { name: "Menu" }).click()
    await expect(page.getByRole("menuitem")).toHaveText([
      "Import Scans",
      "New Wire Reconstruction",
      "New Indexation",
    ])
    await page.getByRole("menuitem", { name: item }).click()
    await expect(page).toHaveURL(new RegExp(`${path}$`))
    await expect(page).toHaveTitle(`${item} · 3DMN Portal`)
    await expect(heading).toHaveText(item)
    // Below its section's page, the section's link stays highlighted.
    const sectionLink = nav.getByRole("link", { name: section, exact: true })
    await expect(sectionLink).toHaveAttribute("aria-current", "true")
    await expect(sectionLink).toHaveAttribute("data-section", "active")
    await expect(nav.locator('[aria-current="page"]')).toHaveCount(0)
  }

  await page.getByRole("link", { name: "3DMN Portal" }).click()
  await expect(page).toHaveURL(/\/$/)
  await expect(page).toHaveTitle("Status · 3DMN Portal")
  await expect(heading).toHaveText("Status")
})

test("below 1024 px the Live link is the Menu's first item", async ({
  page,
}) => {
  await mockHealth(page, () => HEALTH_OK)
  await page.setViewportSize({ width: 900, height: 600 })
  await page.goto("/runs")
  const nav = page.getByRole("navigation", { name: "Main" })
  await expect(
    nav.getByRole("link", { name: "Live", exact: true })
  ).toBeHidden()
  await nav.getByRole("button", { name: "Menu" }).click()
  await expect(page.getByRole("menuitem")).toHaveText([
    "Live",
    "Import Scans",
    "New Wire Reconstruction",
    "New Indexation",
  ])
  await page.getByRole("menuitem", { name: "Live" }).click()
  await expect(page).toHaveURL(/\/live$/)
  await expect(page).toHaveTitle("Live · 3DMN Portal")
})

// Each state of GET /api/v1/health and its indicator: a dot alone (no words, no icon) while the worker runs
// (filled) or the first answer is awaited (a ring), a tinted pill with the tone's dot and words in the foreground
// colour when something is wrong. Its name says the state; its tooltip has the details. The unit tests:
// src/app/health-indicators.test.ts and HealthIndicators.test.tsx.
const STATES: {
  state: string
  answer: HealthAnswer
  /** Whether the answer is a failed check, shown after its two retries (after 1 and 2 s). */
  retried: boolean
  name: string
  tone: string
  /** A pill's words in full, and its state's own; null for a dot. */
  label: string | null
  words: string | null
  /** The dot's fill and outline, as CSS (null for none). */
  fill: string
  outline: string | null
  tooltip: RegExp
}[] = [
  {
    state: "the worker running",
    answer: HEALTH_OK,
    retried: false,
    name: "Local Worker: running",
    tone: "ok",
    label: null,
    words: null,
    fill: QUIET_SUCCESS,
    outline: null,
    tooltip: /^Worker: runningChecked at \d/,
  },
  {
    state: "no answer yet",
    answer: "pending",
    retried: false,
    name: "Local Worker: checking",
    tone: "unknown",
    label: null,
    words: null,
    fill: "transparent",
    outline: "var(--muted-foreground)",
    tooltip: /^Checking the worker…$/,
  },
  {
    state: "the worker late",
    answer: { ...HEALTH_OK, worker: "late" },
    retried: false,
    name: "Worker late",
    tone: "warning",
    label: "Worker late",
    words: "late",
    fill: "var(--warning)",
    outline: null,
    tooltip: /^Worker: lateNo sign of life for over 45 sChecked at \d/,
  },
  {
    state: "the worker not running",
    answer: { ...HEALTH_OK, worker: "not_running" },
    retried: false,
    name: "Worker not running",
    tone: "error",
    label: "Worker not running",
    words: "not running",
    fill: "var(--destructive)",
    outline: null,
    tooltip:
      /^Worker: not runningQueued runs wait until it startsChecked at \d/,
  },
  {
    state: "the database not answering",
    answer: { ...HEALTH_OK, db: "error", worker: null },
    retried: false,
    name: "Worker unknown",
    tone: "error",
    label: "Worker unknown",
    words: "unknown",
    fill: "var(--destructive)",
    outline: null,
    tooltip: /^Worker: unknownThe database does not answerChecked at \d/,
  },
  {
    state: "the API stopped",
    answer: "unreachable",
    retried: true,
    name: "API unreachable",
    tone: "error",
    label: "API unreachable",
    words: "unreachable",
    fill: "var(--destructive)",
    outline: null,
    tooltip:
      /^The server cannot be reached.*Checking again every 10 sNo answer yet$/,
  },
  {
    state: "the health check failing",
    answer: internalError(),
    retried: true,
    name: "API error",
    tone: "error",
    label: "API error",
    words: "error",
    fill: "var(--destructive)",
    outline: null,
    tooltip:
      /^Internal Server ErrorUnexpected error\..*Checking again every 10 sNo answer yet$/,
  },
]

for (const expected of STATES) {
  test(`the Worker indicator for ${expected.state}: ${expected.label ?? "a dot"}`, async ({
    page,
  }) => {
    await page.clock.install()
    await mockHealth(page, () => expected.answer)
    await page.goto("/scans")
    if (expected.retried) await page.clock.fastForward(5_000)
    await expect(indicators(page)).toHaveAccessibleName(expected.name)
    await expect(indicators(page)).toHaveAttribute("data-tone", expected.tone)
    expect(await visibleText(page)).toBe(expected.label ?? "Local")
    await expect(indicators(page).locator("svg")).toHaveCount(0)
    expect(await dotPaint(page)).toEqual({
      fill: await computedColour(page, expected.fill),
      outline:
        expected.outline === null
          ? "none"
          : await computedColour(page, expected.outline),
    })
    if (expected.words !== null) {
      // The tint carries the tone; the words stay in the foreground colour.
      const foreground = await computedColour(page, "var(--foreground)")
      expect(await wordColours(page)).toEqual([foreground, foreground])
    }

    await indicators(page).hover()
    await expect(openTooltip(page)).toHaveText(expected.tooltip)
  })
}

test("a health check without an answer within 10 s turns the ring into API unreachable", async ({
  page,
}) => {
  await page.clock.install()
  await mockHealth(page, () => "pending")
  await page.goto("/scans")
  await expect(indicators(page)).toHaveAccessibleName("Local Worker: checking")
  await page.clock.fastForward(9_000)
  await expect(indicators(page)).toHaveAccessibleName("Local Worker: checking")

  await page.clock.fastForward(1_000)
  await expect(indicators(page)).toHaveAccessibleName("API unreachable")
  await expect(stateWords(page)).toHaveText("unreachable")
  await indicators(page).hover()
  await expect(openTooltip(page)).toContainText("The server cannot be reached")
})

test("keyboard focus draws the 3 px ring in --ring, around the dot's chip or the pill, in each theme", async ({
  page,
}) => {
  let answer: HealthAnswer = HEALTH_OK
  await mockHealth(page, () => answer)
  for (const colorScheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme })
    for (const [health, tone] of [
      [HEALTH_OK, "ok"],
      [{ ...HEALTH_OK, worker: "not_running" }, "error"],
    ] as const) {
      answer = health
      await page.goto("/runs")
      await expect(indicators(page)).toHaveAttribute("data-tone", tone)
      // The dot's ring is on the chip around it; a pill's on the link.
      const ringed = tone === "ok" ? dot(page).locator("..") : indicators(page)
      const ring = `${await computedColour(page, "var(--ring)")} 0px 0px 0px 3px`
      expect(await shadowOf(ringed)).not.toContain(ring)

      await page.getByRole("link", { name: "3DMN Portal" }).focus()
      await page.keyboard.press("Tab")
      await expect(indicators(page)).toBeFocused()
      await expect
        .poll(() => shadowOf(ringed), `${colorScheme}, ${tone}`)
        .toContain(ring)
    }
  }
})

test("the worker running is a 7 px dot in the quiet colour of each theme, a link to the status page", async ({
  page,
}) => {
  await mockHealth(page, () => HEALTH_OK)
  await page.goto("/runs")
  await expect(indicators(page)).toHaveAccessibleName("Local Worker: running")
  await expect(page.getByRole("banner")).not.toContainText(/redis/i)
  const box = await dot(page).boundingBox()
  expect([box?.width, box?.height]).toEqual([7, 7])
  await expectLabelSizedTarget(page, 1280)

  // The token follows the theme: the mix of each theme's success and muted text, not the success colour.
  const colours: string[] = []
  for (const colorScheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme })
    if (colorScheme === "dark")
      await expect(page.locator("html")).toHaveClass(/\bdark\b/)
    else await expect(page.locator("html")).not.toHaveClass(/\bdark\b/)
    const { fill } = await dotPaint(page)
    expect(fill).toBe(await computedColour(page, QUIET_SUCCESS))
    expect(fill).not.toBe(await computedColour(page, "var(--success)"))
    colours.push(fill)
  }
  expect(colours[0]).not.toBe(colours[1])

  // The whole target leads to the status page, which has the full details: here its right end, away from the dot.
  const link = await indicators(page).boundingBox()
  await indicators(page).click({
    position: { x: (link?.width ?? 0) - 2, y: (link?.height ?? 0) / 2 },
  })
  await expect(page).toHaveURL(/\/$/)
  await expect(page).toHaveTitle("Status · 3DMN Portal")
})

/**
 * At `width`: no sideways scrolling; brand, indicator, links and theme toggle side by side, none
 * clipped.
 */
async function navbarFits(page: Page, width: number) {
  const parts = [
    page.getByRole("link", { name: "3DMN Portal" }),
    page.getByRole("group", { name: "Live system status" }),
    page.getByRole("navigation", { name: "Main" }),
    page.getByRole("button", { name: "Theme" }),
  ]
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
    `page width at ${String(width)} px`
  ).toBeLessThanOrEqual(width)
  expect(
    await parts[0]?.evaluate((brand) => brand.scrollWidth - brand.clientWidth)
  ).toBe(0)
  let left = 0
  for (const part of parts) {
    const box = await part.boundingBox()
    expect(box, `${String(width)} px`).not.toBeNull()
    expect(box?.x ?? -1, `${String(width)} px`).toBeGreaterThanOrEqual(left)
    left = (box?.x ?? 0) + (box?.width ?? 0)
  }
  expect(left).toBeLessThanOrEqual(width)
}

test("the navbar fits narrow desktop widths, the dot and its target whole", async ({
  page,
}) => {
  await mockHealth(page, () => HEALTH_OK)
  for (const width of [1280, 900, 768]) {
    await page.setViewportSize({ width, height: 600 })
    await page.goto("/runs")
    await expect(indicators(page)).toHaveAccessibleName("Local Worker: running")
    await navbarFits(page, width)
    await expect(dot(page)).toBeVisible()
    const box = await dot(page).boundingBox()
    expect([box?.width, box?.height], `dot at ${String(width)} px`).toEqual([
      7, 7,
    ])
    await expectLabelSizedTarget(page, width)
  }

  // The tooltip keeps the details.
  await indicators(page).hover()
  await expect(openTooltip(page)).toContainText("Worker: running")
})

test("at 768 px each pill shows its state's words whole, its subject going first; the pills differ", async ({
  page,
}) => {
  test.slow() // five pages, two of them after a failed check's retries
  await page.clock.install()
  let answer: HealthAnswer = HEALTH_OK
  await mockHealth(page, () => answer)
  const narrow: string[] = []
  for (const expected of STATES) {
    if (expected.label === null || expected.words === null) continue
    answer = expected.answer
    await page.setViewportSize({ width: 1280, height: 600 })
    await page.goto("/runs")
    if (expected.retried) await page.clock.fastForward(5_000)
    for (const width of [1280, 768]) {
      await page.setViewportSize({ width, height: 600 })
      // The name and the tooltip keep the full text at every width.
      await expect(indicators(page)).toHaveAccessibleName(expected.name)
      await navbarFits(page, width)
      const layout = await pillLayout(page)
      const where = `${expected.label} at ${String(width)} px`
      expect(layout.oneLine, where).toBe(true)
      expect(layout.wordsWhole, where).toBe(true)
      expect(layout.cut, where).toEqual([])
      if (width === 1280) expect(layout.shown, where).toBe(expected.label)
      else {
        // Whole or gone: the subject is never cut to a letter.
        expect([expected.label, expected.words], where).toContain(layout.shown)
        narrow.push(layout.shown)
      }
    }
  }
  expect(narrow).toHaveLength(5)
  expect(new Set(narrow).size).toBe(narrow.length)
})

test("the indicator says when the API is unreachable, and recovers on the next poll", async ({
  page,
}) => {
  await page.clock.install()
  let health: HealthAnswer = "unreachable"
  await mockHealth(page, () => health)
  await page.goto("/scans")

  // The check is retried twice (after 1 and 2 s) before it counts as failed.
  await page.clock.fastForward(5_000)
  await expect(stateWords(page)).toHaveText("unreachable")
  await indicators(page).hover()
  await expect(openTooltip(page)).toContainText("The server cannot be reached")

  health = HEALTH_OK
  await page.clock.fastForward(10_000)
  await expect(indicators(page)).toHaveAccessibleName("Local Worker: running")
  expect(await visibleText(page)).toBe("Local")
})

test("unknown URLs show the 404 page", async ({ page }) => {
  await mockHealth(page, () => HEALTH_OK)
  for (const path of ["/scan", "/job?job_id=abc", "/create-recon"]) {
    await page.goto(path)
    await expect(
      page.getByRole("heading", { name: "Page not found" })
    ).toBeVisible()
    await expect(page).toHaveTitle("Page not found · 3DMN Portal")
  }
})
