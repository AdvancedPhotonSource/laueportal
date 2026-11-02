import {
  ArrowDownToLineIcon,
  ArrowUpFromLineIcon,
  ClockIcon,
  LoaderCircleIcon,
  LogInIcon,
  type LucideIcon,
} from "lucide-react"

import type { RemotePhase } from "@/api"
import { RUN_STATUS_STYLES } from "@/components/RunStatusBadge"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import type { RemotePhaseView, RemoteTone } from "./remote-status"

// The badge colours (warning for Queued and Login needed, info for the others): the run status
// badge's queued and running tints, an icon per phase.
const TONES: Record<RemoteTone, { tint: string; iconColor: string }> = {
  warning: RUN_STATUS_STYLES.queued,
  info: RUN_STATUS_STYLES.running,
}
const ICONS: Record<RemotePhase, { icon: LucideIcon; spin?: boolean }> = {
  uplink: { icon: ArrowUpFromLineIcon },
  queued: { icon: ClockIcon },
  running: { icon: LoaderCircleIcon, spin: true },
  downlink: { icon: ArrowDownToLineIcon },
  login_required: { icon: LogInIcon },
}

/** A crux run's remote phase as a badge (the Remote Run card's Phase, the run monitor's Status cell). */
export function RemotePhaseBadge({ view }: { view: RemotePhaseView }) {
  const { tint, iconColor } = TONES[view.tone]
  const { icon: Icon, spin } = ICONS[view.phase]
  return (
    <Badge
      variant="outline"
      data-phase={view.phase}
      className={cn("text-foreground", tint)}
    >
      <Icon
        data-icon="inline-start"
        aria-hidden
        className={cn(iconColor, spin && "motion-safe:animate-spin")}
      />
      {view.label}
    </Badge>
  )
}
