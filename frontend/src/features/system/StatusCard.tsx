import {
  CircleCheckIcon,
  CircleDashedIcon,
  CircleXIcon,
  TriangleAlertIcon,
  type LucideIcon,
} from "lucide-react"
import { useId, type ReactNode } from "react"

import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { cn } from "@/lib/utils"
import type { Tone } from "./system-status"

const TONE_ICON: Record<Tone, LucideIcon> = {
  ok: CircleCheckIcon,
  warning: TriangleAlertIcon,
  error: CircleXIcon,
  unknown: CircleDashedIcon,
}

// As RunStatusBadge: a tint and a coloured icon, the text in the foreground colour for contrast.
const TONE_CLASS: Record<Tone, { tint: string; icon: string }> = {
  ok: { tint: "border-success/50 bg-success/15", icon: "text-success" },
  warning: { tint: "border-warning/50 bg-warning/15", icon: "text-warning" },
  error: {
    tint: "border-destructive/50 bg-destructive/15",
    icon: "text-destructive",
  },
  unknown: { tint: "border-border bg-muted", icon: "text-muted-foreground" },
}

/** A card of the page, named by its title (a region screen readers can jump to). */
export function StatusCard({
  title,
  action,
  className,
  children,
}: {
  title: string
  action?: ReactNode
  className?: string
  children: ReactNode
}) {
  const id = useId()
  return (
    <Card size="sm" role="region" aria-labelledby={id} className={className}>
      <CardHeader className="flex items-center gap-2">
        <CardTitle>
          <h2 id={id}>{title}</h2>
        </CardTitle>
        {action}
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  )
}

/** A state as a badge: "OK", "Error", "Running", with the tone's icon; `title` is its tooltip. */
export function ToneBadge({
  tone,
  title,
  children,
}: {
  tone: Tone
  title?: string
  children: ReactNode
}) {
  const Icon = TONE_ICON[tone]
  return (
    <Badge
      variant="outline"
      data-tone={tone}
      title={title}
      className={cn("text-foreground", TONE_CLASS[tone].tint)}
    >
      <Icon
        data-icon="inline-start"
        aria-hidden
        className={TONE_CLASS[tone].icon}
      />
      {children}
    </Badge>
  )
}
