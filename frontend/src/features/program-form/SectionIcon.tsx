import { cn } from "@/lib/utils"
import { sectionIdentity } from "./section-identity"

/**
 * A section's icon before its title in a card, a sub-section or the sidebar: in its accent, or (`muted`, the
 * sidebar) in the muted text colour, so that only the cards show the accents.
 */
export function SectionIcon({
  title,
  muted = false,
  className,
}: {
  title: string
  muted?: boolean
  className?: string
}) {
  const { icon: Icon, accent } = sectionIdentity(title)
  return (
    <Icon
      aria-hidden
      className={cn(
        "size-4 shrink-0",
        muted ? "text-muted-foreground" : accent,
        className
      )}
    />
  )
}
