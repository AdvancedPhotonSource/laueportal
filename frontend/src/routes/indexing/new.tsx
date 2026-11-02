import { createFileRoute } from "@tanstack/react-router"

import { CreateRunPage } from "@/features/program-form"
import { validateCreatePrefill } from "@/lib/prefill"
import { titleHead } from "@/lib/title"

// The LaueGo form. Its prefill (`scan`, `reconstruction`: its scan.h5, `indexing`: a clone; from the
// lists' buttons) is taken once and removed from the URL.
export const Route = createFileRoute("/indexing/new")({
  validateSearch: validateCreatePrefill,
  head: titleHead("New LaueGo Indexation"),
  component: NewLaueGoIndexation,
})

function NewLaueGoIndexation() {
  return (
    <CreateRunPage
      kind="lauego"
      title="New LaueGo Indexation"
      search={Route.useSearch()}
    />
  )
}
