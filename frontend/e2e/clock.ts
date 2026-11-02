import { expect, type Page } from "@playwright/test"

// The mocked polling tests run on Playwright's fake clock: `page.clock.install()` before the page loads, then
// the clock jumps over the app's 2, 5 and 10 s timers instead of the test waiting for them. Time still flows
// between jumps, so nothing else on the page stalls.

/**
 * Moves the page's fake clock on by `step` ms at a time until `check` passes (each `check` should give up fast,
 * e.g. an assertion with `{ timeout: 250 }`). A poll is one jump, but a failed poll's retries (after 1 and 2 s)
 * are timers that start only once the answer before them has come, so one jump cannot pass them all.
 */
export async function advanceUntil(
  page: Page,
  step: number,
  check: () => unknown
) {
  await expect(async () => {
    await page.clock.fastForward(step)
    await check()
  }).toPass({ intervals: [50], timeout: 10_000 })
}

/** How long an assertion inside `advanceUntil` waits before the clock moves on again. */
export const SHORT = { timeout: 250 }
