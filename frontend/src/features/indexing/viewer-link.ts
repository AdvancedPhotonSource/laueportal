// "Copy link to this view" for the indexing viewer: the URL's values and the store's settings, packed
// with the viewer's schema version. A link of another version is ignored (the page says so in a
// toast).
import {
  openViewLink,
  packViewLink,
  searchStrings,
  type OpenedLink as ViewerOpenedLink,
} from "@/lib/view-link"
import { validateIndexingSearch, type IndexingSearch } from "./viewer-search"
import {
  readSettings,
  VIEWER_SCHEMA_VERSION,
  type IndexingViewerSettings,
} from "./viewer-store"

export { viewLinkUrl } from "@/lib/view-link"

/** The `link` value of a link to this view. */
export function packIndexingView(
  search: IndexingSearch,
  settings: IndexingViewerSettings
): Promise<string> {
  return packViewLink({
    v: VIEWER_SCHEMA_VERSION,
    url: searchStrings(search),
    settings,
  })
}

export type OpenedLink = ViewerOpenedLink<
  IndexingSearch,
  IndexingViewerSettings
>

/**
 * What a `link` value asks for: its search and settings (checked as the URL and the store check
 * theirs). A link never carries another link, so applying one cannot loop.
 */
export function openIndexingView(packed: string): Promise<OpenedLink> {
  return openViewLink(
    packed,
    VIEWER_SCHEMA_VERSION,
    validateIndexingSearch,
    readSettings
  )
}
