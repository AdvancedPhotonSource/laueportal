// A create page: a header with the title, Run on, Validate and Submit, and the validation summary under
// them, sticky below the navbar from `md` up; the program's form from the kit, a sidebar of anchors to its
// sections, and one source: the "Source" switch with one picker (Scan, Reconstruction on the LaueGo form,
// Clone) and the IDs the source sets, read-only. The URL's `scan`, `reconstruction`, `indexing` are taken
// once and removed and open the matching kind. A scan or a reconstruction fills its data fields, a clone the
// form (the user's edits kept, the kit's rule); another kind, a cleared picker or a refused source clears the
// source, its IDs and the data fields, so the form never mixes two sources.
import { useNavigate } from "@tanstack/react-router"
import { type ReactNode, useCallback, useMemo, useRef, useState } from "react"

import { type ProgramKind, useListPrograms } from "@/api"
import { LoadingState } from "@/components/LoadingState"
import { type CreatePrefill, singleId, useConsumedPrefill } from "@/lib/prefill"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { surface } from "@/lib/styles"
import { cn } from "@/lib/utils"
import type { RequestValues } from "./form-values"
import { PrefillAlert } from "./PrefillAlert"
import { SectionIcon } from "./SectionIcon"
import { type FormParts, type PrefillFills, ProgramForm } from "./ProgramForm"
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

/** The fields kept when the program is switched: what the two programs share and a user sets. */
const SHARED_FIELDS = [
  "scan_number",
  "reconstruction_id",
  "author",
  "input_path",
  "filename_prefixes",
  "scan_points",
  "depth_range",
  "geometry_file",
  "crystal_file",
  "mask_file",
  "output_path_template",
  "notes",
] as const

/** What survives a switch of program: the shared fields and a scan's or a reconstruction's source. */
export interface ProgramCarry {
  values: RequestValues
  source: CreatePrefill
}

/** A create page's choice of program, drawn beside its title. */
export interface ProgramSwitch {
  options: readonly { value: ProgramKind; label: string }[]
  onChange: (next: ProgramKind, carry: ProgramCarry) => void
}

/**
 * The create page of `kind`. `search` is the route's prefill (`validateCreatePrefill`); the page removes it
 * from the URL itself, with replace (a pushed entry would bring Back to the consumed URL again).
 */
export function CreateRunPage({
  kind,
  title,
  search,
  programSwitch,
  carried,
}: {
  kind: ProgramKind
  title: string
  /** The route's prefill parameters. */
  search: CreatePrefill
  programSwitch?: ProgramSwitch
  /** The values kept from the program this page was switched from. */
  carried?: RequestValues
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

  const valuesRef = useRef<(() => RequestValues) | null>(null)
  const switchProgram = (next: ProgramKind) => {
    const current = valuesRef.current?.() ?? {}
    const values: RequestValues = {}
    for (const name of SHARED_FIELDS) {
      if (name in current) values[name] = current[name]
    }
    // A clone is of this program's run: it does not carry over.
    programSwitch?.onChange(next, {
      values,
      source: mode === "clone" ? {} : source,
    })
  }
  const layout = (parts: FormParts) => (
    <CreateLayout
      title={title}
      toggle={
        programSwitch && (
          <ProgramToggle
            kind={kind}
            options={programSwitch.options}
            onChange={switchProgram}
          />
        )
      }
      nav={<SectionNav sections={spec?.sections ?? []} />}
      alert={
        error && (
          <PrefillAlert error={error} onRetry={retry} retrying={retrying} />
        )
      }
      {...parts}
    />
  )
  return (
    <section>
      {showForm ? (
        <ProgramForm
          kind={kind}
          values={applied.values}
          carried={carried}
          valuesRef={valuesRef}
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
          layout={layout}
        />
      ) : (
        layout({ sections: <LoadingState variant="cards" count={3} /> })
      )}
    </section>
  )
}

/**
 * The page's frame: the header (the title's row with the form's actions, a refused submission's message under
 * it, then the validation summary), sticky below the navbar from `md` up, over the full width of the canvas;
 * the Submitted answer under the header; then the sidebar and the form's column. The header's height is
 * `--form-header` on the frame, which the sidebar's sticky offset and the scroll margins add to the navbar's.
 */
function CreateLayout({
  title,
  toggle,
  nav,
  alert,
  actions,
  message,
  summary,
  answer,
  sections,
}: FormParts & {
  title: string
  toggle?: ReactNode
  nav: ReactNode
  alert: ReactNode
}) {
  return (
    <div className="flex flex-col gap-4">
      <div
        ref={measureHeader}
        data-slot="create-header"
        className="z-30 -mx-4 -mt-4 flex flex-col gap-3 border-b bg-canvas p-4 md:sticky md:top-12"
      >
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-xl font-semibold">{title}</h1>
          {toggle}
          <div className="mr-auto" />
          {actions}
        </div>
        {message}
        {summary}
      </div>
      {/* No box of its own: the panel is a flex item of the frame. */}
      <div className={cn("contents", BELOW_HEADER)}>{answer}</div>
      <div className="grid items-start gap-4 md:grid-cols-[13rem_minmax(0,1fr)]">
        {nav}
        <div className={cn("flex min-w-0 flex-col gap-4", BELOW_HEADER)}>
          {alert}
          {sections}
        </div>
      </div>
    </div>
  )
}

/**
 * The scroll margin of the focusable elements below the header (the Submitted answer's, the form's column's):
 * one that takes the focus (the first error after Submit, a control reached by Tab) scrolls to below the header.
 */
const BELOW_HEADER =
  "[&_:is(a,button,input,select,textarea,[tabindex])]:scroll-mt-[calc(var(--form-header,0px)+4rem)]"

/** The program of the form, as the Source switch draws its kinds. */
function ProgramToggle({
  kind,
  options,
  onChange,
}: {
  kind: ProgramKind
  options: ProgramSwitch["options"]
  onChange: (next: ProgramKind) => void
}) {
  return (
    <ToggleGroup
      aria-label="Program"
      variant="outline"
      spacing={0}
      value={[kind]}
      onValueChange={(value) => {
        const next = value[0] as ProgramKind | undefined
        if (next !== undefined && next !== kind) onChange(next)
      }}
    >
      {options.map((option) => (
        <ToggleGroupItem
          key={option.value}
          value={option.value}
          className="aria-pressed:bg-primary aria-pressed:text-primary-foreground aria-pressed:hover:bg-primary"
        >
          {option.label}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  )
}

/**
 * Keeps the header's height in `--form-header` on its parent, the frame. When the header grows over the
 * focused element (the summary lists the issues of a Submit that has just focused the first error), that
 * element scrolls to below the header again; nothing else moves the page (a header that shrinks, an element
 * the user scrolled under the header).
 */
function measureHeader(header: HTMLDivElement | null) {
  const frame = header?.parentElement
  if (!header || !frame || typeof ResizeObserver === "undefined") return
  let height = header.offsetHeight
  const observer = new ResizeObserver(() => {
    const grown = header.offsetHeight - height
    height = header.offsetHeight
    frame.style.setProperty("--form-header", `${String(height)}px`)
    if (grown <= 0) return
    const focused = document.activeElement
    if (!(focused instanceof HTMLElement) || !frame.contains(focused)) return
    if (header.contains(focused)) return
    // The page keeps its place (scroll anchoring) and the header's bottom edge moves down by `grown`: the
    // growth covered the element if its top was below the old edge and is above the new one.
    const edge = header.getBoundingClientRect().bottom
    const { top } = focused.getBoundingClientRect()
    if (top >= edge - grown && top < edge)
      focused.scrollIntoView({ block: "nearest" })
  })
  observer.observe(header)
  return () => {
    observer.disconnect()
  }
}

/** The sidebar: a link to each section of the form. */
function SectionNav({ sections }: { sections: readonly SectionSpec[] }) {
  if (sections.length === 0) return <div aria-hidden />
  return (
    <nav
      aria-label="Sections"
      className={cn(
        surface,
        "flex flex-col gap-0.5 p-2 md:sticky md:top-[calc(var(--form-header,0px)+4rem)]"
      )}
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
            <SectionIcon title={section.group} muted className="mt-0.5" />
            {section.group}
          </a>
        )
      })}
    </nav>
  )
}
