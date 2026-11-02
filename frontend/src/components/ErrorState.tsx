import { CircleAlertIcon, RefreshCwIcon } from "lucide-react"

import {
  Alert,
  AlertAction,
  AlertDescription,
  AlertTitle,
} from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { describeError } from "@/lib/errors"
import { cn } from "@/lib/utils"

/**
 * An error in place of a page's or a section's content: a problem+json error shows its title and
 * detail (and its issues and reference when it has them), anything else its message.
 */
export function ErrorState({
  error,
  onRetry,
  retrying = false,
  refreshing = false,
  stale = false,
  className,
}: {
  error: unknown
  /** Shows a Retry button. */
  onRetry?: () => void
  /** A retry is running: the button spins and is disabled. */
  retrying?: boolean
  /**
   * The page is fetching again on its own (it polls): the button spins but stays enabled, so it
   * keeps the keyboard focus; onRetry must then allow a click while a fetch runs.
   */
  refreshing?: boolean
  /** The last data loaded is still shown. */
  stale?: boolean
  className?: string
}) {
  const { title, detail, issues, instance } = describeError(error)
  return (
    <Alert variant="destructive" className={className}>
      <CircleAlertIcon aria-hidden />
      <AlertTitle>{title}</AlertTitle>
      <AlertDescription className="flex flex-col gap-1">
        {detail && <p>{detail}</p>}
        {issues && (
          <ul className="list-disc pl-4">
            {issues.map((issue, index) => (
              <li key={index}>{issue}</li>
            ))}
          </ul>
        )}
        {stale && <p>Showing the last data loaded.</p>}
        {instance && (
          <p className="text-xs">
            Reference <span className="font-mono">{instance}</span>
          </p>
        )}
      </AlertDescription>
      {onRetry && (
        <AlertAction>
          <Button
            variant="outline"
            size="sm"
            onClick={onRetry}
            disabled={retrying}
          >
            <RefreshCwIcon
              data-icon="inline-start"
              className={cn((retrying || refreshing) && "animate-spin")}
              aria-hidden
            />
            Retry
          </Button>
        </AlertAction>
      )}
    </Alert>
  )
}
