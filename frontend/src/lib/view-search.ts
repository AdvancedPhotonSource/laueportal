// A viewer's coarse URL state: the page shows a change at once, and the URL follows after a 300 ms
// pause, holding only non-default values (the route's search helpers leave defaults undefined). A
// change of a history key (the tab, the step) adds a history entry; any other change replaces the
// current one. Back and forward then change only the history keys: the other values stay as the page
// last had them (written back with replace).
import { useLocation, useRouter } from "@tanstack/react-router"
import { useCallback, useEffect, useRef, useState } from "react"

export const VIEW_SEARCH_DELAY_MS = 300

type Search = Record<string, unknown>

function values(search: object): Search {
  return search as Search
}

/** The search without its undefined values (the URL leaves them out). */
export function compactSearch<S extends object>(search: S): S {
  return Object.fromEntries(
    Object.entries(values(search)).filter(([, value]) => value !== undefined)
  ) as S
}

/** Whether two searches hold the same values. */
export function sameSearch(a: object, b: object): boolean {
  const left = values(compactSearch(a))
  const right = values(compactSearch(b))
  const keys = Object.keys(left)
  return (
    keys.length === Object.keys(right).length &&
    keys.every((key) => left[key] === right[key])
  )
}

/** Whether going from `current` to `next` changes a history key (and so adds a history entry). */
export function changesHistory(
  current: object,
  next: object,
  historyKeys: readonly string[]
): boolean {
  return historyKeys.some((key) => values(current)[key] !== values(next)[key])
}

/** After back or forward: the history keys of the entry reached, every other value as it was. */
export function keepSettled<S extends object>(
  reached: S,
  settled: S,
  historyKeys: readonly string[]
): S {
  const others = Object.fromEntries(
    Object.entries(values(settled)).filter(
      ([key]) => !historyKeys.includes(key)
    )
  )
  const history = Object.fromEntries(
    Object.entries(values(reached)).filter(([key]) => historyKeys.includes(key))
  )
  return compactSearch({ ...others, ...history }) as S
}

/**
 * The page's view of its search, and the function that changes it. `search` is the route's validated
 * search; `write` navigates (`replace` true for a replace). The returned search includes the changes
 * not yet written.
 */
export function useViewSearch<S extends object>({
  search,
  write,
  historyKeys,
  delayMs = VIEW_SEARCH_DELAY_MS,
}: {
  search: S
  write: (next: S, replace: boolean) => void
  historyKeys: readonly (keyof S & string)[]
  delayMs?: number
}): [S, (patch: Partial<S>) => void] {
  const router = useRouter()
  const [pending, setPending] = useState<Partial<S> | null>(null)
  const pendingRef = useRef<Partial<S> | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const searchRef = useRef(search)
  const settled = useRef<S>(search)
  const popped = useRef(false)
  const writeRef = useRef(write)
  const keysRef = useRef<readonly string[]>(historyKeys)
  useEffect(() => {
    writeRef.current = write
    keysRef.current = historyKeys
  })

  useEffect(
    () =>
      // Set by back/forward, cleared by any push or replace (a link's, this hook's), so a later
      // search change is never taken for a pop that changed nothing.
      router.history.subscribe(({ action }) => {
        popped.current = action.type !== "PUSH" && action.type !== "REPLACE"
      }),
    [router]
  )

  // The router's search changed: after back or forward on the same path, keep the values other than
  // the history keys; otherwise this is the page's state now (another path, e.g. another run on the
  // same route, starts from its own URL).
  const path = useLocation({ select: (location) => location.pathname })
  const settledPath = useRef(path)
  useEffect(() => {
    searchRef.current = search
    const samePath = settledPath.current === path
    settledPath.current = path
    const wasPopped = popped.current
    popped.current = false
    if (!samePath || wasPopped) {
      // A change not yet written belonged to the entry left.
      clearTimeout(timer.current)
      timer.current = undefined
      pendingRef.current = null
      setPending(null)
    }
    if (wasPopped && samePath) {
      const kept = keepSettled(search, settled.current, keysRef.current)
      if (!sameSearch(kept, search)) {
        writeRef.current(kept, true)
        return
      }
    }
    settled.current = search
    if (timer.current === undefined) setPending(null)
  }, [search, path])

  // Leaving the page drops a change not yet written (writing it would come back here).
  useEffect(
    () => () => {
      clearTimeout(timer.current)
    },
    []
  )

  const update = useCallback(
    (patch: Partial<S>) => {
      pendingRef.current = { ...pendingRef.current, ...patch }
      setPending(pendingRef.current)
      clearTimeout(timer.current)
      timer.current = setTimeout(() => {
        timer.current = undefined
        const current = searchRef.current
        const next = compactSearch({ ...current, ...pendingRef.current })
        pendingRef.current = null
        if (sameSearch(next, current)) {
          setPending(null)
          return
        }
        popped.current = false
        writeRef.current(next, !changesHistory(current, next, keysRef.current))
      }, delayMs)
    },
    [delayMs]
  )

  const shown = pending ? compactSearch({ ...search, ...pending }) : search
  return [shown, update]
}
