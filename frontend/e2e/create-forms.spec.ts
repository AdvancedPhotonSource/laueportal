import { expect, test, type Locator, type Page } from "@playwright/test"

// The create forms' header on the seeded stack: Run on, Validate and Submit beside the title, the validation
// summary under them, all in view while the form scrolls (1280 px wide); a sidebar link puts its card below
// the header; Enter in a field submits the form, and the first error's field scrolls to below the header; a
// refused submission's message is under the title's row; the Submitted panel is below the header, and its
// controls scroll to below it when they take the focus (the validate and create answers routed); a fix that
// shrinks the header leaves the page where the user scrolled it. The LaueGo form's structure file is labelled
// Structures File.

const FORMS = [
  ["/reconstructions/new", "New Wire Reconstruction"],
  ["/indexing/new", "New Indexation"],
] as const

async function box(locator: Locator) {
  const found = await locator.boundingBox()
  if (!found) throw new Error("not laid out")
  return found
}

function header(page: Page) {
  return {
    title: page.getByRole("heading", { level: 1 }),
    runOn: page.getByText("Run on", { exact: true }),
    validate: page.getByRole("button", { name: "Validate", exact: true }),
    submit: page.getByRole("button", { name: "Submit", exact: true }),
    validation: page.getByRole("region", { name: "Validation" }),
  }
}

for (const [url, title] of FORMS) {
  test(`${url}: Run on, Validate and Submit beside the title, the summary under them, in view at the last section`, async ({
    page,
  }) => {
    await page.goto(url)
    const parts = header(page)
    await expect(parts.title).toHaveText(title)
    await expect(parts.submit).toBeVisible()
    const heading = await box(parts.title)
    const middle = heading.y + heading.height / 2
    for (const control of [parts.runOn, parts.validate, parts.submit]) {
      const placed = await box(control)
      // One row: each control's middle within the title's height, right of the title.
      expect(Math.abs(placed.y + placed.height / 2 - middle)).toBeLessThan(
        heading.height
      )
      expect(placed.x).toBeGreaterThan(heading.x + heading.width)
    }
    const submit = await box(parts.submit)
    const summary = await box(parts.validation)
    expect(summary.y).toBeGreaterThanOrEqual(submit.y + submit.height)
    expect(summary.y - (submit.y + submit.height)).toBeLessThan(32)

    // The last section: the page scrolls, and the header stays where it was.
    await page.getByLabel("Notes", { exact: true }).scrollIntoViewIfNeeded()
    expect(await page.evaluate(() => window.scrollY)).toBeGreaterThan(0)
    for (const part of Object.values(parts)) await expect(part).toBeInViewport()
    // At most the navbar's 1 px border: the header sticks at 3rem, under it.
    expect(
      Math.abs((await box(parts.submit)).y - submit.y)
    ).toBeLessThanOrEqual(1)
  })
}

/** The header's bottom edge in the viewport. */
async function headerBottom(page: Page) {
  const placed = await box(page.locator('[data-slot="create-header"]'))
  return placed.y + placed.height
}

/** Until the page has stopped scrolling (a smooth scroll's end): two samples 200 ms apart agree. */
async function scrollSettled(page: Page) {
  let last = Number.NaN
  await expect
    .poll(
      async () => {
        const y = await page.evaluate(() => window.scrollY)
        const same = y === last
        last = y
        return same
      },
      { intervals: [200] }
    )
    .toBe(true)
}

/** The validate route answers no issues, so Submit goes on to the create (the POST of `path`). */
async function noIssues(page: Page, path: string) {
  await page.route(`**${path}/validate`, (route) =>
    route.fulfill({ json: { issues: [] } })
  )
}

test("a sidebar link puts its card just below the sticky header", async ({
  page,
}) => {
  await page.goto("/indexing/new")
  // A middle card: the page can scroll it to the top (the last card stops at the page's end).
  await page
    .getByRole("navigation", { name: "Sections" })
    .getByRole("link", { name: "Peak Search Parameters" })
    .click()
  const card = page.locator("#section-peak-search-parameters")
  await expect(card).toBeFocused()
  await scrollSettled(page)
  const gap = (await box(card)).y - (await headerBottom(page))
  expect(gap).toBeGreaterThanOrEqual(0)
  expect(gap).toBeLessThan(32)
})

test("Enter in a field submits: the first error's field, under the header before, takes the focus below it; nothing is sent", async ({
  page,
}) => {
  const sent: string[] = []
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      new URL(request.url()).pathname === "/api/v1/reconstruction-runs"
    )
      sent.push(request.url())
  })
  await page.goto("/reconstructions/new")
  // Author, the first field with an error, scrolled to 20 px above the header's bottom (under the header).
  const author = page.getByLabel("Author", { exact: true })
  await expect(author).toBeVisible()
  const under = (await box(author)).y - ((await headerBottom(page)) - 20)
  await page.evaluate((by) => {
    window.scrollBy(0, by)
  }, under)
  await scrollSettled(page)
  expect((await box(author)).y).toBeLessThan(await headerBottom(page))

  // Enter in Depth Start, focused without scrolling.
  await page.evaluate(() => {
    document
      .querySelector<HTMLInputElement>('[name="depth_start"]')
      ?.focus({ preventScroll: true })
  })
  await page.keyboard.press("Enter")
  // Only a submit attempt lists an untouched field's error in the summary.
  await expect(
    header(page).validation.locator('li[data-severity="error"]', {
      hasText: "Author: Required",
    })
  ).toBeVisible()
  await expect(author).toBeFocused()
  await scrollSettled(page)
  expect((await box(author)).y).toBeGreaterThanOrEqual(await headerBottom(page))
  expect(sent).toEqual([])
})

test("a refused submission's message is under the title's row, which keeps Run on, Validate and Submit", async ({
  page,
}) => {
  await noIssues(page, "/api/v1/reconstruction-runs")
  await page.route(
    (url) => url.pathname === "/api/v1/reconstruction-runs",
    (route) =>
      route.request().method() === "POST"
        ? route.fulfill({
            status: 500,
            contentType: "application/problem+json",
            json: {
              type: "about:blank",
              title: "Internal Server Error",
              status: 500,
              detail:
                "The run could not be created: the database is not responding (ref 0123456789abcdef)",
            },
          })
        : route.fallback()
  )
  await page.goto("/reconstructions/new")
  const parts = header(page)
  await parts.submit.click()
  const message = page.locator('[data-slot="create-header"]').getByRole("alert")
  await expect(message).toContainText("the database is not responding")
  const heading = await box(parts.title)
  const middle = heading.y + heading.height / 2
  for (const control of [parts.runOn, parts.validate, parts.submit]) {
    const placed = await box(control)
    expect(Math.abs(placed.y + placed.height / 2 - middle)).toBeLessThan(
      heading.height
    )
  }
  // The message: below the row, above the summary.
  const submit = await box(parts.submit)
  const line = await box(message)
  expect(line.y).toBeGreaterThanOrEqual(submit.y + submit.height)
  expect(line.y + line.height).toBeLessThanOrEqual(
    (await box(parts.validation)).y
  )
})

/** Submit creates R999, which could not be queued (the Submitted panel stays). */
async function createdNotQueued(page: Page) {
  await noIssues(page, "/api/v1/reconstruction-runs")
  await page.route(
    (url) => url.pathname === "/api/v1/reconstruction-runs",
    (route) =>
      route.request().method() === "POST"
        ? route.fulfill({
            status: 201,
            json: {
              run: { id: 999, kind: "reconstruction", label: "R999" },
              job_id: 999,
              status: "failed",
              target: "local",
              queue_id: null,
              failure: "The queue is unavailable",
            },
          })
        : route.fallback()
  )
}

test("a run created but not queued: the Submitted panel is below the sticky header, scrolled into view", async ({
  page,
}) => {
  await createdNotQueued(page)
  await page.goto("/reconstructions/new")
  await page.getByLabel("Notes", { exact: true }).scrollIntoViewIfNeeded()
  await header(page).submit.click()
  const panel = page.getByRole("region", { name: "Submitted" })
  await expect(panel).toContainText("The queue is unavailable")
  await expect(
    page.locator('[data-slot="create-header"]').getByRole("region", {
      name: "Submitted",
    })
  ).toHaveCount(0)
  await scrollSettled(page)
  await expect(panel).toBeInViewport()
  expect((await box(panel)).y).toBeGreaterThanOrEqual(await headerBottom(page))
})

test("Shift+Tab to the Submitted panel's New Submission, under the header, scrolls it to below the header", async ({
  page,
}) => {
  await createdNotQueued(page)
  await page.goto("/reconstructions/new")
  await header(page).submit.click()
  const again = page
    .getByRole("region", { name: "Submitted" })
    .getByRole("button", { name: "New Submission" })
  await expect(again).toBeVisible()
  await scrollSettled(page)
  // Scrolled until New Submission is under the header.
  const placed = await box(again)
  const by = placed.y + placed.height - ((await headerBottom(page)) - 4)
  await page.evaluate((distance) => {
    window.scrollBy(0, distance)
  }, by)
  await scrollSettled(page)
  const under = await box(again)
  expect(under.y + under.height).toBeLessThan(await headerBottom(page))

  // From the first sidebar link (focused without scrolling), Shift+Tab.
  await page.evaluate(() => {
    document
      .querySelector<HTMLElement>('nav[aria-label="Sections"] a')
      ?.focus({ preventScroll: true })
  })
  await page.keyboard.press("Shift+Tab")
  await expect(again).toBeFocused()
  await scrollSettled(page)
  expect((await box(again)).y).toBeGreaterThanOrEqual(await headerBottom(page))
})

test("after Submit with errors, a fix that shrinks the header leaves the page where the user scrolled it", async ({
  page,
}) => {
  await page.goto("/reconstructions/new")
  const author = page.getByLabel("Author", { exact: true })
  await expect(author).toBeVisible()
  await header(page).submit.click()
  const listed = header(page).validation.locator('li[data-severity="error"]', {
    hasText: "Author: Required",
  })
  await expect(listed).toBeVisible()
  await expect(author).toBeFocused()
  await scrollSettled(page)

  // The page's scroll position and the header's height, after scrolling down by `by`.
  const place = (by: number) =>
    page.evaluate((distance) => {
      window.scrollBy(0, distance)
      const top = document.querySelector('[data-slot="create-header"]')
      return {
        y: window.scrollY,
        height: top?.getBoundingClientRect().height ?? 0,
      }
    }, by)
  await page.keyboard.type("Alice")
  // Scrolled down at once (Author goes under the header), before the validation answers (500 ms after typing).
  const before = await place(300)
  await expect(listed).toHaveCount(0)
  await scrollSettled(page)
  const after = await place(0)
  expect(after.height).toBeLessThan(before.height)
  // At most the header's own shrinking moves the page (scroll anchoring); nothing scrolls Author back.
  expect(before.y - after.y).toBeLessThanOrEqual(
    before.height - after.height + 1
  )
})

test("the LaueGo form labels its structure file Structures File", async ({
  page,
}) => {
  await page.goto("/indexing/new")
  await expect(
    page.getByLabel("Structures File", { exact: true })
  ).toBeVisible()
})
