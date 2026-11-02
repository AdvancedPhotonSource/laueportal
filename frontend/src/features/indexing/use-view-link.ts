// "Copy link to this view" on the indexing viewer, on the shared hooks (src/lib/use-view-link.ts):
// the view is packed ahead of the click (CopyViewLinkButton copies it); opening a page with a
// packed `link` applies it (the URL's values, then the store's settings) and removes it with
// replace.
import { useShallow } from "zustand/shallow"

import {
  useOpenViewLink as useOpenLink,
  usePackedViewLink as usePackedLink,
  type ViewerLinks,
} from "@/lib/use-view-link"
import { openIndexingView, packIndexingView } from "./viewer-link"
import type { IndexingSearch } from "./viewer-search"
import {
  settingsOf,
  useIndexingViewerStore,
  VIEWER_SCHEMA_VERSION,
  type IndexingViewerSettings,
} from "./viewer-store"

const INDEXING_LINKS: ViewerLinks<IndexingSearch, IndexingViewerSettings> = {
  version: VIEWER_SCHEMA_VERSION,
  pack: packIndexingView,
  open: openIndexingView,
  getSettings: () => settingsOf(useIndexingViewerStore.getState()),
  applySettings: (settings) => {
    useIndexingViewerStore.getState().applySettings(settings)
  },
}

/** Apply the search's packed link, if it has one; `replace` writes a search with replace. */
export function useOpenViewLink(
  search: IndexingSearch,
  replace: (next: IndexingSearch) => void
) {
  useOpenLink(INDEXING_LINKS, search, replace)
}

/** The link to the view and the store's settings, packed ahead of the click (see the shared hook). */
export function usePackedViewLink(
  search: IndexingSearch
): () => string | Promise<string> {
  const settings = useIndexingViewerStore(useShallow(settingsOf))
  return usePackedLink(INDEXING_LINKS, search, settings)
}
