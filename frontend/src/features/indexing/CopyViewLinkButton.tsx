import { ViewLinkButton } from "@/components/ViewLinkButton"
import { usePackedViewLink } from "./use-view-link"
import type { IndexingSearch } from "./viewer-search"

/** "Copy link to this view" on the indexing viewer: its view packed ahead of the click. */
export function CopyViewLinkButton({ search }: { search: IndexingSearch }) {
  return <ViewLinkButton link={usePackedViewLink(search)} />
}
