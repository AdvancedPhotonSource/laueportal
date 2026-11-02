// A click that a double-click may still cancel. A view whose double-click resets it (the heatmap)
// reports a click only once the double-click interval has passed, so one gesture does one thing:
// a double-click resets the view without reporting a click (the ROI Inspector adds an ROI on each
// click).

/** About a user's double-click interval: longer waits feel slow, shorter ones split double-clicks. */
export const DOUBLE_CLICK_MS = 300

export interface DeferredClick {
  /** Report after `delay` ms unless a newer click or `cancel` comes first. */
  click(report: () => void): void
  /** A double-click (or teardown): the pending click is not reported. */
  cancel(): void
}

export function deferredClick(delay = DOUBLE_CLICK_MS): DeferredClick {
  let timer: ReturnType<typeof setTimeout> | undefined
  const cancel = () => {
    clearTimeout(timer)
    timer = undefined
  }
  return {
    click(report) {
      cancel()
      timer = setTimeout(() => {
        timer = undefined
        report()
      }, delay)
    },
    cancel,
  }
}
