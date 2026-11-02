import type { CustomCellRendererProps } from "ag-grid-react"
import type { ReactNode } from "react"

import type { RunReference } from "@/api"
import { RunProgressBar } from "@/components/RunProgressBar"
import {
  IndexingRunLink,
  JobLink,
  ReconstructionRunLink,
  RunLink,
  ScanLink,
  SourceLinks,
} from "@/components/RunLinks"
import { RunStatusBadge } from "@/components/RunStatusBadge"
import { HELD_HINT, isRunStatus, type RunCounts } from "@/lib/run-status"
import type { LinkKind } from "./columns"

// AG Grid cell renderers for the column types that render more than text, on the shared display
// components (src/components). A row still loading (an Infinite Row Model stub) has no data and
// renders nothing.

type Row = Record<string, unknown>

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null
}

function counts(row: Row): RunCounts {
  return {
    n_inputs: asNumber(row.n_inputs) ?? 0,
    n_succeeded: asNumber(row.n_succeeded) ?? 0,
    n_failed: asNumber(row.n_failed) ?? 0,
    n_not_run: asNumber(row.n_not_run) ?? 0,
  }
}

function isRunReference(value: unknown): value is RunReference {
  return (
    typeof value === "object" &&
    value !== null &&
    "kind" in value &&
    "id" in value &&
    typeof value.id === "number"
  )
}

/**
 * `type: "status"`: the run's status badge, with processed/total while it runs; a value it does not
 * know reads "Unknown (x)".
 */
export function StatusCell({ value, data }: CustomCellRendererProps<Row>) {
  if (!data || value == null) return null
  const { n_inputs } = counts(data)
  const processed =
    asNumber(data.n_processed) ??
    (asNumber(data.n_succeeded) ?? 0) + (asNumber(data.n_failed) ?? 0)
  return (
    <RunStatusValue
      status={String(value)}
      processed={processed}
      total={n_inputs}
      held={data.held === true}
    />
  )
}

/**
 * The status column's cell: the run's status badge (processed/total while it runs), and "Held" for a
 * queued batch run an open live session holds (`JobSummary.held`). A status column's own `cell` (the run
 * monitor's, which shows a crux run's remote phase instead) falls back to it.
 */
export function RunStatusValue({
  status,
  processed,
  total,
  held,
}: {
  status: string
  processed: number
  total: number
  held: boolean
}) {
  const badge = (
    <RunStatusBadge status={status} processed={processed} total={total} />
  )
  if (!held) return badge
  // A queued batch run an open live session holds (JobSummary.held).
  return (
    <span className="flex items-center gap-2">
      {badge}
      <span
        className="text-xs text-muted-foreground"
        data-held
        title={HELD_HINT}
      >
        Held
      </span>
    </span>
  )
}

/** `type: "custom"`: what the column's `cell` draws for the row. */
export function CustomCell({
  data,
  cell,
}: CustomCellRendererProps<Row> & { cell: (row: Row) => ReactNode }) {
  return data ? cell(data) : null
}

/** `type: "progress"`: the segmented run-progress bar. */
export function ProgressCell({ data }: CustomCellRendererProps<Row>) {
  if (!data) return null
  const status = isRunStatus(data.status) ? data.status : undefined
  return <RunProgressBar counts={counts(data)} status={status} />
}

/** `type: "link"`: the link of its kind, on the cell's value (and the row's sources). */
export function LinkCell({
  value,
  data,
  link,
}: CustomCellRendererProps<Row> & { link: LinkKind }) {
  if (!data) return null
  if (link === "source") {
    return (
      <SourceLinks
        scan={asNumber(data.scan_number)}
        reconstruction={asNumber(data.reconstruction_id)}
      />
    )
  }
  if (link === "run")
    return isRunReference(value) ? <RunLink run={value} /> : null
  const id = asNumber(value)
  if (link === "reconstruction") return <ReconstructionRunLink id={id} />
  if (id === null) return null
  switch (link) {
    case "scan":
      return <ScanLink scan={id} />
    case "indexing":
      return <IndexingRunLink id={id} />
    case "job":
      return <JobLink id={id} />
  }
}
