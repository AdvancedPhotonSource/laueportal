import { createFileRoute } from "@tanstack/react-router"

import { CreateRunPage } from "@/features/program-form"
import { validateCreatePrefill } from "@/lib/prefill"
import { titleHead } from "@/lib/title"

// The wire form. Its prefill (`scan`, `reconstruction`: a clone; from the lists' buttons) is taken
// once and removed from the URL.
export const Route = createFileRoute("/reconstructions/new")({
  validateSearch: validateCreatePrefill,
  head: titleHead("New Wire Reconstruction"),
  component: NewWireReconstruction,
})

function NewWireReconstruction() {
  return (
    <CreateRunPage
      kind="wire"
      title="New Wire Reconstruction"
      search={Route.useSearch()}
    />
  )
}
