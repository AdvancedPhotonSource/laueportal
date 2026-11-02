// "Copy link to this view" for the wire reconstruction viewer: the URL's values and the store's
// settings, packed with the viewer's schema version, on the shared hooks
// (src/lib/use-view-link.ts). A link of another version is ignored with a toast.
import { useShallow } from "zustand/shallow"

import {
  useOpenViewLink,
  usePackedViewLink,
  type ViewerLinks,
} from "@/lib/use-view-link"
import {
  openViewLink,
  packViewLink,
  searchStrings,
  type OpenedLink,
} from "@/lib/view-link"
import {
  validateReconstructionSearch,
  type ReconstructionSearch,
} from "./viewer-search"
import {
  readSettings,
  settingsOf,
  useWireViewerStore,
  WIRE_VIEWER_SCHEMA_VERSION,
  type WireViewerSettings,
} from "./viewer-store"

/** The `link` value of a link to this view. */
export function packReconstructionView(
  search: ReconstructionSearch,
  settings: WireViewerSettings
): Promise<string> {
  return packViewLink({
    v: WIRE_VIEWER_SCHEMA_VERSION,
    url: searchStrings(search),
    settings,
  })
}

/** What a `link` value asks for: its search and settings, checked as the URL and the store check theirs. */
export function openReconstructionView(
  packed: string
): Promise<OpenedLink<ReconstructionSearch, WireViewerSettings>> {
  return openViewLink(
    packed,
    WIRE_VIEWER_SCHEMA_VERSION,
    validateReconstructionSearch,
    readSettings
  )
}

const WIRE_LINKS: ViewerLinks<ReconstructionSearch, WireViewerSettings> = {
  version: WIRE_VIEWER_SCHEMA_VERSION,
  pack: packReconstructionView,
  open: openReconstructionView,
  getSettings: () => settingsOf(useWireViewerStore.getState()),
  applySettings: (settings) => {
    useWireViewerStore.getState().applySettings(settings)
  },
}

/** Apply the search's packed link, if it has one; `replace` writes a search with replace. */
export function useOpenReconstructionLink(
  search: ReconstructionSearch,
  replace: (next: ReconstructionSearch) => void
) {
  useOpenViewLink(WIRE_LINKS, search, replace)
}

/** The link to the view and the store's settings, packed ahead of the click. */
export function usePackedReconstructionLink(
  search: ReconstructionSearch
): () => string | Promise<string> {
  const settings = useWireViewerStore(useShallow(settingsOf))
  return usePackedViewLink(WIRE_LINKS, search, settings)
}
