import {
  BanIcon,
  CircleCheckIcon,
  CircleHelpIcon,
  CircleXIcon,
  ClockIcon,
  LoaderCircleIcon,
  type LucideIcon,
} from "lucide-react"

import type { RunStatus } from "@/api"
import { Badge } from "@/components/ui/badge"
import { isRunStatus, runStatusText } from "@/lib/run-status"
import { cn } from "@/lib/utils"

export interface StatusStyle {
  icon: LucideIcon
  /** The badge's tint and border. */
  tint: string
  /** The icon's colour (the text stays in the foreground colour, for contrast). */
  iconColor: string
  /** The icon turns (a running run's), unless the user asked for reduced motion. */
  spin?: boolean
}

// The badge colours (warning, link, success, destructive, muted) as theme tokens: a tint and a
// coloured icon, with the label in the foreground colour so it stays readable in both themes. A
// crux run's remote phase badge shares them (RemoteRunCard).
export const RUN_STATUS_STYLES: Record<RunStatus, StatusStyle> = {
  queued: {
    icon: ClockIcon,
    tint: "border-warning/50 bg-warning/15",
    iconColor: "text-warning",
  },
  running: {
    icon: LoaderCircleIcon,
    tint: "border-link/50 bg-link/15",
    iconColor: "text-link",
    spin: true,
  },
  finished: {
    icon: CircleCheckIcon,
    tint: "border-success/50 bg-success/15",
    iconColor: "text-success",
  },
  failed: {
    icon: CircleXIcon,
    tint: "border-destructive/50 bg-destructive/15",
    iconColor: "text-destructive",
  },
  cancelled: {
    icon: BanIcon,
    tint: "border-border bg-muted",
    iconColor: "text-muted-foreground",
  },
}

const UNKNOWN: StatusStyle = {
  icon: CircleHelpIcon,
  tint: "border-border bg-muted",
  iconColor: "text-muted-foreground",
}

/**
 * A run's status as a badge: Queued, Running (with processed/total when the counters are given),
 * Finished, Failed or Cancelled. Used in grids and on cards.
 */
export function RunStatusBadge({
  status,
  processed,
  total,
  className,
}: {
  /** The API's status (RunStatus); another value reads "Unknown (x)". */
  status: RunStatus | (string & Record<never, never>)
  /** Inputs processed so far; with `total`, a running run shows `Running 3/10`. */
  processed?: number
  total?: number
  className?: string
}) {
  const style = isRunStatus(status) ? RUN_STATUS_STYLES[status] : UNKNOWN
  const Icon = style.icon
  const progress =
    processed !== undefined && total !== undefined
      ? { processed, total }
      : undefined
  return (
    <Badge
      variant="outline"
      data-status={status}
      className={cn("text-foreground", style.tint, className)}
    >
      <Icon
        data-icon="inline-start"
        aria-hidden
        className={cn(
          style.iconColor,
          style.spin && "motion-safe:animate-spin"
        )}
      />
      {runStatusText(status, progress)}
    </Badge>
  )
}
