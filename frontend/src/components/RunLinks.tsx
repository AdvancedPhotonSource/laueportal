import { Link } from "@tanstack/react-router"
import type { ReactNode } from "react"

import { RunKind, type RunReference } from "@/api"
import { cn } from "@/lib/utils"

// Links to scans, runs and jobs, used in grids and on cards.

const LINK_CLASS = "text-link underline-offset-4 hover:underline"

function Muted({ children }: { children: ReactNode }) {
  return <span className="text-muted-foreground">{children}</span>
}

/** A scan by its number; `children` replaces the number as the text. */
export function ScanLink({
  scan,
  children,
  className,
}: {
  scan: number
  children?: ReactNode
  className?: string
}) {
  return (
    <Link
      to="/scans/$scanId"
      params={{ scanId: String(scan) }}
      className={cn(LINK_CLASS, className)}
    >
      {children ?? scan}
    </Link>
  )
}

/** An indexing run as `I12`. */
export function IndexingRunLink({ id }: { id: number }) {
  return (
    <Link
      to="/indexing/$indexingId"
      params={{ indexingId: String(id) }}
      className={LINK_CLASS}
    >
      I{id}
    </Link>
  )
}

/**
 * A reconstruction run as `R3`; "Direct" when there is none (an indexation of the scan's own
 * images).
 */
export function ReconstructionRunLink({ id }: { id: number | null }) {
  if (id == null) return <Muted>Direct</Muted>
  return (
    <Link
      to="/reconstructions/$reconstructionId"
      params={{ reconstructionId: String(id) }}
      className={LINK_CLASS}
    >
      R{id}
    </Link>
  )
}

/**
 * A run's sources: its scan as `SN5` and its reconstruction as `R3`, or "Unlinked" when it has
 * neither.
 */
export function SourceLinks({
  scan,
  reconstruction,
}: {
  scan: number | null | undefined
  reconstruction?: number | null
}) {
  if (scan == null && reconstruction == null) return <Muted>Unlinked</Muted>
  return (
    <span className="inline-flex gap-2">
      {scan != null && <ScanLink scan={scan}>SN{scan}</ScanLink>}
      {reconstruction != null && <ReconstructionRunLink id={reconstruction} />}
    </span>
  )
}

/** A job by its ID. */
export function JobLink({ id }: { id: number }) {
  return (
    <Link
      to="/jobs/$jobId"
      params={{ jobId: String(id) }}
      className={LINK_CLASS}
    >
      {id}
    </Link>
  )
}

/** The run a job executes, by the API's reference (`I12`, `R3`); nothing for a job without one. */
export function RunLink({ run }: { run: RunReference | null | undefined }) {
  if (!run) return null
  return run.kind === RunKind.indexing ? (
    <IndexingRunLink id={run.id} />
  ) : (
    <ReconstructionRunLink id={run.id} />
  )
}
