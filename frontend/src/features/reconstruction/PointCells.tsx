import {
  CircleAlertIcon,
  CircleCheckIcon,
  CircleDashedIcon,
  CircleDotIcon,
  CircleMinusIcon,
  CircleXIcon,
  type LucideIcon,
} from "lucide-react"

import {
  OutputPathState,
  ReconstructionPointStatus,
  type ReconstructionPoint,
} from "@/api"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import { POINT_STATUS_LABELS, SOURCE_STATE_LABELS } from "./points"

// As RunStatusBadge: a tint and a coloured icon, the label in the foreground colour.
const STYLES: Record<
  ReconstructionPointStatus,
  { icon: LucideIcon; tint: string; iconColor: string }
> = {
  [ReconstructionPointStatus.complete]: {
    icon: CircleCheckIcon,
    tint: "border-success/50 bg-success/15",
    iconColor: "text-success",
  },
  [ReconstructionPointStatus.writing]: {
    icon: CircleDotIcon,
    tint: "border-link/50 bg-link/15",
    iconColor: "text-link",
  },
  [ReconstructionPointStatus.pending]: {
    icon: CircleDashedIcon,
    tint: "border-border bg-muted",
    iconColor: "text-muted-foreground",
  },
  [ReconstructionPointStatus.unattempted]: {
    icon: CircleMinusIcon,
    tint: "border-border bg-muted",
    iconColor: "text-muted-foreground",
  },
  [ReconstructionPointStatus.interrupted]: {
    icon: CircleAlertIcon,
    tint: "border-warning/50 bg-warning/15",
    iconColor: "text-warning",
  },
  [ReconstructionPointStatus.no_frames]: {
    icon: CircleAlertIcon,
    tint: "border-warning/50 bg-warning/15",
    iconColor: "text-warning",
  },
  [ReconstructionPointStatus.failed]: {
    icon: CircleXIcon,
    tint: "border-destructive/50 bg-destructive/15",
    iconColor: "text-destructive",
  },
}

/** A point's status as a badge; a point that is not complete has the API's explanation as its tooltip. */
export function PointStatusBadge({ point }: { point: ReconstructionPoint }) {
  const style = STYLES[point.status]
  const Icon = style.icon
  return (
    <Badge
      variant="outline"
      data-status={point.status}
      title={point.explanation ?? undefined}
      className={cn("text-foreground", style.tint)}
    >
      <Icon data-icon="inline-start" aria-hidden className={style.iconColor} />
      {POINT_STATUS_LABELS[point.status]}
    </Badge>
  )
}

/**
 * A point's input file as the portal reads it (a remote run's mapped); its state when it is not
 * available here, the path as written in the tooltip.
 */
export function PointSource({ point }: { point: ReconstructionPoint }) {
  if (point.source === null) return null
  const state = point.source_state
  const shown = point.source_path ?? point.source
  return (
    <span title={point.source}>
      {state !== OutputPathState.unavailable && (
        <span className="font-mono text-xs">{shown}</span>
      )}
      {state !== null && state !== OutputPathState.available && (
        <span className="ml-2 text-xs text-muted-foreground">
          {SOURCE_STATE_LABELS[state]}
        </span>
      )}
    </span>
  )
}
