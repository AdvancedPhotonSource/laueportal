import {
  BanIcon,
  CircleCheckIcon,
  CircleDotIcon,
  CircleXIcon,
  CopyIcon,
  DatabaseIcon,
  type LucideIcon,
} from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import { IMPORT_STATUS_LABELS, type ImportStatus } from "./scan-import"

// As RunStatusBadge: a tint and a coloured icon, the label in the foreground colour.
const STYLES: Record<
  ImportStatus,
  { icon: LucideIcon; tint: string; iconColor: string }
> = {
  new: {
    icon: CircleDotIcon,
    tint: "border-link/50 bg-link/15",
    iconColor: "text-link",
  },
  exists: {
    icon: DatabaseIcon,
    tint: "border-border bg-muted",
    iconColor: "text-muted-foreground",
  },
  duplicate: {
    icon: CopyIcon,
    tint: "border-warning/50 bg-warning/15",
    iconColor: "text-warning",
  },
  invalid: {
    icon: BanIcon,
    tint: "border-destructive/50 bg-destructive/15",
    iconColor: "text-destructive",
  },
  imported: {
    icon: CircleCheckIcon,
    tint: "border-success/50 bg-success/15",
    iconColor: "text-success",
  },
  failed: {
    icon: CircleXIcon,
    tint: "border-destructive/50 bg-destructive/15",
    iconColor: "text-destructive",
  },
}

/** A scan-log row's status as a badge; a failed import's error is its tooltip. */
export function ImportStatusBadge({
  status,
  error,
}: {
  status: ImportStatus
  error?: string | null
}) {
  const style = STYLES[status]
  const Icon = style.icon
  return (
    <Badge
      variant="outline"
      data-status={status}
      title={error ?? undefined}
      className={cn("text-foreground", style.tint)}
    >
      <Icon data-icon="inline-start" aria-hidden className={style.iconColor} />
      {IMPORT_STATUS_LABELS[status]}
    </Badge>
  )
}
