import type { RunStatus } from "@/api"
import {
  runProgress,
  type ProgressSegmentKind,
  type RunCounts,
} from "@/lib/run-status"
import { cn } from "@/lib/utils"

// The segments' colours, as theme tokens.
const SEGMENT_COLOURS: Record<ProgressSegmentKind, string> = {
  succeeded: "bg-success",
  failed: "bg-destructive",
  notRun: "bg-muted-foreground",
  pending: "bg-warning",
}

/**
 * A run's progress as a segmented bar: `3/10 processed, 1 failed, 2 not run` over a bar of
 * succeeded, failed, not-run and pending inputs (pending ones striped and moving while the run
 * runs); "No inputs" for a run without inputs. `sm` fits a grid row, `md` a card.
 */
export function RunProgressBar({
  counts,
  status,
  size = "sm",
  className,
}: {
  counts: RunCounts
  status?: RunStatus
  size?: "sm" | "md"
  className?: string
}) {
  const progress = runProgress(counts, status)
  if (progress.total === 0) {
    return (
      <span className={cn("text-muted-foreground", className)}>
        {progress.text}
      </span>
    )
  }
  return (
    <div
      className={cn(
        "flex w-full min-w-0 flex-col",
        size === "sm" ? "gap-1" : "gap-1.5",
        className
      )}
      title={progress.text}
    >
      <span
        className={cn(
          "truncate text-center",
          size === "sm" ? "text-xs leading-none" : "text-sm"
        )}
      >
        {progress.text}
      </span>
      <div
        role="progressbar"
        aria-label="Run progress"
        aria-valuemin={0}
        aria-valuemax={progress.total}
        aria-valuenow={progress.processed}
        aria-valuetext={progress.text}
        className={cn(
          "flex w-full overflow-hidden rounded-full bg-muted",
          size === "sm" ? "h-1.5" : "h-3"
        )}
      >
        {progress.segments.map((segment) => (
          <div
            key={segment.kind}
            data-segment={segment.kind}
            title={segment.title}
            style={{ width: `${String(segment.percent)}%` }}
            className={cn(
              SEGMENT_COLOURS[segment.kind],
              segment.animated && "progress-stripes"
            )}
          />
        ))}
      </div>
    </div>
  )
}
