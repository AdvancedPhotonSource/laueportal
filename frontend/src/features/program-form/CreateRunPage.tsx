// A create page: the program's form from the kit, a sidebar of anchors to its sections, and one source:
// the "Source" switch with one picker (Scan, Reconstruction on the LaueGo form, Clone) and the IDs the
// source sets, read-only. The URL's `scan`, `reconstruction`, `indexing` are taken once and removed and
// open the matching kind. A scan or a reconstruction fills its data fields, a clone the form (the user's
// edits kept, the kit's rule); another kind, a cleared picker or a refused source clears the source, its
// IDs and the data fields, so the form never mixes two sources.
import { useNavigate } from "@tanstack/react-router"
import { useCallback, useMemo, useState } from "react"

import { type ProgramKind, useListPrograms } from "@/api"
import { LoadingState } from "@/components/LoadingState"
import { type CreatePrefill, singleId, useConsumedPrefill } from "@/lib/prefill"
import { surface } from "@/lib/styles"
import { cn } from "@/lib/utils"
import type { RequestValues } from "./form-values"
import { PrefillAlert } from "./PrefillAlert"
import { SectionIcon } from "./SectionIcon"
import { type PrefillFills, ProgramForm } from "./ProgramForm"
import {
  isProgram,
  programSpec,
  sectionAnchor,
  type SectionSpec,
} from "./program-spec"
import { SourceBlock } from "./SourceBlock"
import {
  CLEARED_FIELDS,
  fillFields,
  modePrefill,
  type SourceMode,
  sourceModeOf,
} from "./source-mode"
import { CLONE_SOURCE } from "./source-search"
import { useProgramPrefill } from "./use-program-prefill"

/**
 * The prefill the form was last given: the source context it came from, the kind its values belong to, and
 * whether it is that context's own (`loaded`) or a clear standing for it (another kind, a refused source).
 */
interface Applied {
  source: CreatePrefill
  values?: RequestValues
  fills: PrefillFills
  mode: SourceMode
  loaded: boolean
}

/** No source of `mode` for the context `source`: its IDs and data fields go back to the defaults. */
function cleared(source: CreatePrefill, mode: SourceMode): Applied {
  return { source, values: {}, fills: CLEARED_FIELDS, mode, loaded: false }
}

const hasSource = (source: CreatePrefill) => Object.keys(source).length > 0

/**
 * The create page of `kind`. `search` is the route's prefill (`validateCreatePrefill`); the page removes it
 * from the URL itself, with replace (a pushed entry would bring Back to the consumed URL again).
 */
export function CreateRunPage({
  kind,
  title,
  search,
}: {
  kind: ProgramKind
  title: string
  /** The route's prefill parameters. */
  search: CreatePrefill
}) {
  const navigate = useNavigate()
  const clearSearch = useCallback(() => {
    void navigate({ to: ".", search: {}, replace: true })
  }, [navigate])
  const [source, choose] = useConsumedPrefill(search, clearSearch)
  const { prefill, current, loading, error, retry, retrying } =
    useProgramPrefill(kind, source)
  const programs = useListPrograms()
  const spec = useMemo(() => {
    const program = programs.data?.programs.find(isProgram(kind))
    return program ? programSpec(program) : undefined
  }, [programs.data, kind])

  // The kind follows each new source context (the URL's, a picker's); the switch sets it otherwise. A
  // kind set from outside clears like the switch, before anything is applied.
  const [mode, setMode] = useState<SourceMode>(() => sourceModeOf(kind, source))
  const [modeFor, setModeFor] = useState(source)
  const [applied, setApplied] = useState<Applied>(() => ({
    source: {},
    fills: undefined,
    mode,
    loaded: false,
  }))
  let shown = mode
  let next = applied
  if (modeFor !== source) {
    setModeFor(source)
    if (hasSource(source)) {
      shown = sourceModeOf(kind, source)
      if (shown !== mode) {
        setMode(shown)
        next = cleared(source, shown)
      }
    }
  }
  // Each source context's prefill is given to the form once it loads: a clone's fills the form, a scan's
  // or a reconstruction's its data fields and the source IDs (the cleared fields too over another kind's
  // values). A refused or failed prefill clears the previous source.
  if (current !== undefined && !(next.source === source && next.loaded)) {
    next = {
      source,
      values: current.values,
      fills: fillFields(shown, applied.mode),
      mode: shown,
      loaded: true,
    }
  } else if (error && hasSource(source) && next.source !== source) {
    next = cleared(source, shown)
  }
  if (next !== applied) setApplied(next)
  /** No source (another kind, a cleared picker): its IDs and data fields go back to the defaults. */
  const clearSource = (to: SourceMode) => {
    const none: CreatePrefill = {}
    choose(none)
    setMode(to)
    setApplied(cleared(none, to))
  }
  const onPick = (picked: SourceMode, id: number | null) => {
    if (id === null) clearSource(picked)
    else choose(modePrefill(kind, picked, id))
  }
  // The run cloned; none for a refused source (an unknown run).
  const clone =
    mode === "clone" && !error ? singleId(source[CLONE_SOURCE[kind]]) : null

  // Once shown, the form stays while a prefill loads (another source keeps the user's edits, the kit's
  // rule); the skeleton stands only for the URL's first source.
  const [formShown, setFormShown] = useState(false)
  const showForm = formShown || !(loading && prefill === undefined)
  if (showForm && !formShown) setFormShown(true)

  return (
    <section className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold">{title}</h1>
      <div className="grid items-start gap-4 md:grid-cols-[13rem_minmax(0,1fr)]">
        <SectionNav sections={spec?.sections ?? []} />
        <div className="flex min-w-0 flex-col gap-4">
          {error && (
            <PrefillAlert error={error} onRetry={retry} retrying={retrying} />
          )}
          {showForm ? (
            <ProgramForm
              kind={kind}
              values={applied.values}
              fills={applied.fills}
              source={
                spec &&
                ((slot) => (
                  <SourceBlock
                    program={kind}
                    spec={spec}
                    slot={slot}
                    mode={mode}
                    onModeChange={clearSource}
                    clone={clone}
                    onPick={onPick}
                  />
                ))
              }
            />
          ) : (
            <LoadingState variant="cards" count={3} />
          )}
        </div>
      </div>
    </section>
  )
}

/** The sidebar: a link to each section of the form. */
function SectionNav({ sections }: { sections: readonly SectionSpec[] }) {
  if (sections.length === 0) return <div aria-hidden />
  return (
    <nav
      aria-label="Sections"
      className={cn(surface, "flex flex-col gap-0.5 p-2 md:sticky md:top-16")}
    >
      {sections.map((section) => {
        const anchor = sectionAnchor(section.group)
        return (
          <a
            key={section.group}
            href={`#${anchor}`}
            className="flex items-start gap-2 rounded-md px-2 py-1.5 text-sm text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
            onClick={(event) => {
              // Scroll without a history entry: the URL holds no anchor.
              // The section takes the keyboard focus, as an anchor's target would.
              event.preventDefault()
              const target = document.getElementById(anchor)
              target?.scrollIntoView({ behavior: "smooth", block: "start" })
              target?.focus({ preventScroll: true })
            }}
          >
            <SectionIcon title={section.group} className="mt-0.5" />
            {section.group}
          </a>
        )
      })}
    </nav>
  )
}
