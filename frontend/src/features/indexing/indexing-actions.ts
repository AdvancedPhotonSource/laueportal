// What the indexations page's buttons do with the selected run: the create forms get its one source
// context as their prefill (src/lib/prefill.ts).
import type { IndexingRunSummary } from "@/api"
import { programSearch, sourcePrefill, type CreatePrefill } from "@/lib/prefill"

export interface IndexingActions {
  /** The runs selected. */
  count: number
  /**
   * "New Recon" reconstructs the selected run's source: `/reconstructions/new?scan=…&reconstruction=…`,
   * each that the run has. Every reconstruction is a wire one (revision 0004 removed coded aperture).
   * Unset while the run has neither.
   */
  newRecon?: CreatePrefill
  /** "New Index" clones the selected run: `/indexing/new?indexing=…`. */
  newIndex?: CreatePrefill
}

/** Both buttons take exactly one run (one source). */
export function indexingActions(
  selected: readonly IndexingRunSummary[]
): IndexingActions {
  const [run] = selected
  if (selected.length !== 1 || !run) return { count: selected.length }
  const actions: IndexingActions = {
    count: 1,
    newIndex: {
      ...sourcePrefill({ indexing: run.id }),
      ...programSearch(run.method),
    },
  }
  if (run.scan_number !== null || run.reconstruction_id !== null) {
    actions.newRecon = sourcePrefill({
      scan: run.scan_number,
      reconstruction: run.reconstruction_id,
    })
  }
  return actions
}
