import {
  CircleAlertIcon,
  CircleCheckIcon,
  CircleDashedIcon,
  CircleXIcon,
  type LucideIcon,
} from "lucide-react"
import { useId, type ReactNode } from "react"

import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { cn } from "@/lib/utils"
import type { CategoryTone, FileTone } from "./job-page"

/** A card of the job page, named by its title (a region screen readers can jump to). */
export function JobCard({
  title,
  actions,
  className,
  children,
}: {
  title: string
  /** On the title's row, at its end. */
  actions?: ReactNode
  className?: string
  children: ReactNode
}) {
  const id = useId()
  return (
    <Card size="sm" role="region" aria-labelledby={id} className={className}>
      <CardHeader className="flex flex-wrap items-center gap-2">
        <CardTitle>
          <h2 id={id}>{title}</h2>
        </CardTitle>
        {actions && (
          <div className="ml-auto flex flex-wrap items-center gap-2">
            {actions}
          </div>
        )}
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  )
}

// As RunStatusBadge: a tint and a coloured icon, the label in the foreground colour for contrast.
const TONES: Record<
  FileTone | CategoryTone,
  { tint: string; icon: string; Icon: LucideIcon }
> = {
  ok: {
    tint: "border-success/50 bg-success/15",
    icon: "text-success",
    Icon: CircleCheckIcon,
  },
  muted: {
    tint: "border-border bg-muted",
    icon: "text-muted-foreground",
    Icon: CircleDashedIcon,
  },
  warning: {
    tint: "border-warning/50 bg-warning/15",
    icon: "text-warning",
    Icon: CircleAlertIcon,
  },
  error: {
    tint: "border-destructive/50 bg-destructive/15",
    icon: "text-destructive",
    Icon: CircleXIcon,
  },
}

/** A state as a tinted badge: a run file's ("Not available on this host"), a failure's category. */
export function ToneBadge({
  tone,
  icon = true,
  children,
}: {
  tone: FileTone | CategoryTone
  /** The tone's icon before the text. */
  icon?: boolean
  children: ReactNode
}) {
  const { tint, icon: iconColour, Icon } = TONES[tone]
  return (
    <Badge
      variant="outline"
      data-tone={tone}
      className={cn("text-foreground", tint)}
    >
      {icon && (
        <Icon data-icon="inline-start" aria-hidden className={iconColour} />
      )}
      {children}
    </Badge>
  )
}
