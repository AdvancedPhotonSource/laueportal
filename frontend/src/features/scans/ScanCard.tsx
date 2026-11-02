import { useId, type ReactNode } from "react"

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { cn } from "@/lib/utils"

/** A card of the scan page, named by its title (a region screen readers can jump to). */
export function ScanCard({
  title,
  actions,
  className,
  contentClassName,
  children,
}: {
  title: string
  /** Buttons on the title's row. */
  actions?: ReactNode
  className?: string
  contentClassName?: string
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
      <CardContent className={cn(contentClassName)}>{children}</CardContent>
    </Card>
  )
}
