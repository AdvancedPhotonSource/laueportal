// Where the keyboard goes after a page's Retry has loaded: its ErrorState (and the Retry button that
// had the focus) goes with the error, which would leave the focus on the page's body. The page's
// heading takes it instead (give it tabIndex={-1}), unless the user has put it elsewhere meanwhile.
import { useCallback, useEffect, useRef, type RefObject } from "react"

/**
 * Returns the mark to call from Retry. `failed`: the page shows its error; `fetching`: a fetch runs
 * (Retry's, or a poll's). A Retry that fails again leaves the focus on Retry (it stays enabled).
 */
export function useRetryFocus(
  failed: boolean,
  fetching: boolean,
  target: RefObject<HTMLElement | null>
): () => void {
  const pending = useRef(false)
  useEffect(() => {
    if (!pending.current) return
    if (failed) {
      if (!fetching) pending.current = false
      return
    }
    pending.current = false
    const active = document.activeElement
    if (active === null || active === document.body) target.current?.focus()
  }, [failed, fetching, target])
  return useCallback(() => {
    pending.current = true
  }, [])
}
