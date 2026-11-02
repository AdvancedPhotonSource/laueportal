// What the reconstructions page's buttons do with the selected run: the create forms get its one
// source context as their prefill (src/lib/prefill.ts).
import type { ReconstructionRunSummary } from "@/api"
import { sourcePrefill, type CreatePrefill } from "@/lib/prefill"

export interface ReconstructionActions {
  /** The runs selected. */
  count: number
  /** "New Wire Recon" clones the one selected wire run: `/reconstructions/new?reconstruction=R&scan=S`. */
  newWireRecon?: CreatePrefill
  /** "New Index" indexes the one selected run: `/indexing/new?reconstruction=R&scan=S`. */
  newIndex?: CreatePrefill
}

/** A run as a prefill: the run, and its scan when it has one (an unlinked run has none). */
function runPrefill(run: ReconstructionRunSummary): CreatePrefill {
  return sourcePrefill({ reconstruction: run.id, scan: run.scan_number })
}

/** Both buttons take exactly one run (one source); New Wire Recon only a wire one. */
export function reconstructionActions(
  selected: readonly ReconstructionRunSummary[]
): ReconstructionActions {
  const [run] = selected
  if (selected.length !== 1 || !run) return { count: selected.length }
  const prefill = runPrefill(run)
  const actions: ReconstructionActions = { count: 1, newIndex: prefill }
  if (run.method === "wire") actions.newWireRecon = prefill
  return actions
}
