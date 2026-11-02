// At most one run per animation frame, with the latest argument: pointer events can come faster
// than frames, and the 3D view picks once per frame.

export interface FrameThrottle<T> {
  /** Run on the next animation frame; a later call before then only replaces the argument. */
  schedule(arg: T): void
  /** Drop the pending run, if any. */
  cancel(): void
}

export function frameThrottle<T>(
  run: (arg: T) => void,
  request: (callback: () => void) => number = (callback) =>
    requestAnimationFrame(callback),
  cancelRequest: (id: number) => void = (id) => {
    cancelAnimationFrame(id)
  }
): FrameThrottle<T> {
  let pending: { id: number; arg: T } | null = null
  return {
    schedule(arg) {
      if (pending) {
        pending.arg = arg
        return
      }
      const next = { id: 0, arg }
      pending = next
      next.id = request(() => {
        pending = null
        run(next.arg)
      })
    },
    cancel() {
      if (pending) cancelRequest(pending.id)
      pending = null
    },
  }
}
