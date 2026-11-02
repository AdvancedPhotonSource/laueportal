import type { Locator, Page } from "@playwright/test"

/**
 * The prefill a create form asked for when `open` led to it: the query of its GET
 * /programs/{kind}/prefill (the page takes the URL's prefill once and removes it from the URL).
 */
export async function prefillAsked(
  page: Page,
  kind: "wire" | "lauego",
  open: () => Promise<void>
): Promise<Record<string, string>> {
  const request = page.waitForRequest(
    (sent) =>
      new URL(sent.url()).pathname === `/api/v1/programs/${kind}/prefill`
  )
  await open()
  return Object.fromEntries(new URL((await request).url()).searchParams)
}

/** The create form's Source switch: the button of a kind ("Scan", "Reconstruction", "Clone"). */
export function sourceKind(page: Page, kind: string): Locator {
  return page.getByRole("button", { name: kind, exact: true })
}

/** The IDs the form's source sets, shown read-only beside its picker. */
export function sourceIds(page: Page): Locator {
  return page.locator('dl[aria-label="Source IDs"]')
}
