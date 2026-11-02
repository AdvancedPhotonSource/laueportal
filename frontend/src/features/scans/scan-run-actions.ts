// The scan detail's run cards: what their New Recon and New Index buttons give the create forms as
// their prefill (src/lib/prefill.ts). A card's grid allows one checked row; every run listed is one
// of this scan's, so the source context is this scan with the selected run, or this scan alone when
// nothing is selected.
import type { IndexingRunSummary, ReconstructionRunSummary } from "@/api"
import { sourcePrefill, type CreatePrefill } from "@/lib/prefill"

/** What the Reconstructions card keeps of a selected run. */
export type SelectedReconstruction = Pick<
  ReconstructionRunSummary,
  "id" | "method"
>

/** What the Peak Indexing card keeps of a selected run. */
export type SelectedIndexing = Pick<
  IndexingRunSummary,
  "id" | "reconstruction_id"
>

export interface RunCardLinks {
  /** `/reconstructions/new?…` */
  newRecon: CreatePrefill
  /** `/indexing/new?…` */
  newIndex: CreatePrefill
}

/**
 * The Reconstructions card: New Recon clones the selected run (the wire form takes a wire run only,
 * so another method gives this scan alone), New Index indexes it.
 */
export function reconstructionCardLinks(
  scan: number,
  selected: SelectedReconstruction | undefined
): RunCardLinks {
  return {
    newRecon: sourcePrefill({
      scan,
      reconstruction: selected?.method === "wire" ? selected.id : null,
    }),
    newIndex: sourcePrefill({ scan, reconstruction: selected?.id }),
  }
}

/**
 * The Peak Indexing card: New Recon reconstructs the selected run's reconstruction (every
 * reconstruction is a wire one), New Index clones the selected run, with its reconstruction.
 */
export function indexingCardLinks(
  scan: number,
  selected: SelectedIndexing | undefined
): RunCardLinks {
  const reconstruction = selected?.reconstruction_id
  return {
    newRecon: sourcePrefill({ scan, reconstruction }),
    newIndex: sourcePrefill({ scan, reconstruction, indexing: selected?.id }),
  }
}
