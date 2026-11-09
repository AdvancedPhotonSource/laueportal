import { createFileRoute } from "@tanstack/react-router"
import { useState } from "react"

import type { ProgramKind } from "@/api"
import { CreateRunPage, type ProgramCarry } from "@/features/program-form"
import {
  type CreatePrefill,
  isIndexingProgram,
  validateCreatePrefill,
} from "@/lib/prefill"
import { titleHead } from "@/lib/title"

const TITLE = "New Indexation"
const PROGRAMS = [
  { value: "lauego", label: "LaueGo" },
  { value: "laue_matching", label: "LaueMatching" },
] as const satisfies readonly { value: ProgramKind; label: string }[]

interface Shown {
  kind: ProgramKind
  /** The page is drawn afresh for each program, with what the switch kept. */
  carry?: ProgramCarry
}

// The indexing form, LaueGo's or LaueMatching's by the switch beside its title. Its prefill (`scan`,
// `reconstruction`: its scan.h5, `indexing`: a clone; from the lists' buttons) is taken once and removed from
// the URL; `program` opens the form on a program.
export const Route = createFileRoute("/indexing/new")({
  validateSearch: (search): CreatePrefill & { program?: ProgramKind } => {
    const program = search.program
    return {
      ...validateCreatePrefill(search),
      program: isIndexingProgram(program) ? program : undefined,
    }
  },
  head: titleHead(TITLE),
  component: NewIndexation,
})

function NewIndexation() {
  const { program, ...search } = Route.useSearch()
  const [shown, setShown] = useState<Shown>({ kind: program ?? "lauego" })
  return (
    <CreateRunPage
      key={shown.kind}
      kind={shown.kind}
      title={TITLE}
      search={shown.carry?.source ?? search}
      carried={shown.carry?.values}
      programSwitch={{
        options: PROGRAMS,
        onChange: (kind, carry) => {
          setShown({ kind, carry })
        },
      }}
    />
  )
}
