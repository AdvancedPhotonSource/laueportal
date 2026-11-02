import { cn } from "@/lib/utils"
import { sectionIdentity } from "./section-identity"

/** A section's icon in its accent, before its title in a card, a sub-section or the sidebar. */
export function SectionIcon({
  title,
  className,
}: {
  title: string
  className?: string
}) {
  const { icon: Icon, accent } = sectionIdentity(title)
  return (
    <Icon aria-hidden className={cn("size-4 shrink-0", accent, className)} />
  )
}
