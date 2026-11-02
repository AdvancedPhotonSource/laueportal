// "Copy link to this view" for every viewer: the view is packed ahead of the click (the page's
// ViewLinkButton copies it); opening a page with a packed `link` applies it (the URL's values,
// then the store's settings) and removes it with replace. A link of another schema version, or
// one that cannot be read, is ignored with a toast. Each viewer describes its link once
// (`ViewerLinks`, a module constant) and wraps these hooks.
import { useCallback, useEffect, useRef } from "react"
import { toast } from "sonner"

import { ERROR_TOAST_MS } from "@/lib/toast"
import { viewLinkUrl, type OpenedLink } from "@/lib/view-link"
import { compactSearch } from "@/lib/view-search"

/** A viewer's link: its schema version, how it packs and opens a view, and its store's settings. */
export interface ViewerLinks<S extends { link?: string }, T> {
  version: number
  pack: (search: S, settings: T) => Promise<string>
  open: (packed: string) => Promise<OpenedLink<S, T>>
  /** The store's settings now (read in the click). */
  getSettings: () => T
  /** Replace the store's settings with a link's. */
  applySettings: (settings: T) => void
}

/** Apply the search's packed link, if it has one; `replace` writes a search with replace. */
export function useOpenViewLink<S extends { link?: string }, T>(
  viewer: ViewerLinks<S, T>,
  search: S,
  replace: (next: S) => void
) {
  const packed = search.link
  const latest = useRef({ search, replace })
  useEffect(() => {
    latest.current = { search, replace }
  })
  useEffect(() => {
    if (packed === undefined) return
    let cancelled = false
    void viewer.open(packed).then((opened) => {
      if (cancelled) return
      if (opened.kind === "apply") {
        latest.current.replace(compactSearch(opened.search))
        viewer.applySettings(opened.settings)
        return
      }
      latest.current.replace(
        compactSearch({ ...latest.current.search, link: undefined })
      )
      if (opened.kind === "mismatch") {
        toast.error("This link is from another version of the viewer", {
          description: `It holds a view of version ${String(opened.version)}; this viewer reads version ${String(viewer.version)}. The page opens with your own settings.`,
          duration: ERROR_TOAST_MS,
        })
      } else {
        toast.error("This link's view cannot be read", {
          description: "The page opens with your own settings.",
          duration: ERROR_TOAST_MS,
        })
      }
    })
    return () => {
      cancelled = true
    }
  }, [packed, viewer])
}

/**
 * The link to the view (`search` as the page shows it, and the store's `settings`), packed ahead of
 * the click: packing is asynchronous (CompressionStream), and a copy after an await may lose the
 * click's user activation. The returned function gives the URL at once when the packed view is
 * current, else a promise of it.
 */
export function usePackedViewLink<S extends { link?: string }, T>(
  viewer: ViewerLinks<S, T>,
  search: S,
  settings: T
): () => string | Promise<string> {
  const latest = useRef(search)
  const packed = useRef<{ key: string; value: string } | null>(null)
  useEffect(() => {
    latest.current = search
  })
  const key = viewKey(search, settings)
  useEffect(() => {
    let cancelled = false
    const view = JSON.parse(key) as [S, T]
    const timer = setTimeout(() => {
      void viewer.pack(...view).then((value) => {
        if (!cancelled) packed.current = { key, value }
      })
    }, PACK_DELAY_MS)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [key, viewer])
  return useCallback(() => {
    const search = latest.current
    const settings = viewer.getSettings()
    const ready = packed.current
    if (ready?.key === viewKey(search, settings)) {
      return viewLinkUrl(window.location.href, ready.value)
    }
    return viewer
      .pack(search, settings)
      .then((value) => viewLinkUrl(window.location.href, value))
  }, [viewer])
}

// Settings change in bursts (a drag on the colorbar): pack once they settle.
const PACK_DELAY_MS = 150

function viewKey(search: unknown, settings: unknown): string {
  return JSON.stringify([search, settings])
}
