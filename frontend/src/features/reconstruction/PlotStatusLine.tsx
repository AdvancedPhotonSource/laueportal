import { RefreshCwIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import type { PlotStatus } from "@/lib/busy"

/** A status over a plot (centred at its top), its detail as the tooltip; Retry when it offers one. */
export function PlotStatusLine({
  status,
  onRetry,
}: {
  status: PlotStatus
  onRetry?: () => void
}) {
  return (
    <div className="absolute top-1 left-1/2 flex w-max max-w-[80%] -translate-x-1/2 items-center gap-2 rounded bg-background/80 px-2 py-0.5 text-xs text-muted-foreground">
      <p role="status" title={status.hint}>
        {status.text}
      </p>
      {status.retry && onRetry && (
        <Button variant="outline" size="xs" onClick={onRetry}>
          <RefreshCwIcon data-icon="inline-start" aria-hidden />
          Retry
        </Button>
      )}
    </div>
  )
}
