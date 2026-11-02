// A prefill the API refused (an unknown or mismatched source) or that failed: the API's message, with
// a Retry when a retry may help.
import { CircleAlertIcon, RefreshCwIcon } from "lucide-react"

import { ApiError } from "@/api"
import {
  Alert,
  AlertAction,
  AlertDescription,
  AlertTitle,
} from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { describeError } from "@/lib/errors"
import { cn } from "@/lib/utils"

export function PrefillAlert({
  error,
  onRetry,
  retrying,
}: {
  error: unknown
  onRetry: () => void
  retrying: boolean
}) {
  const { title, detail } = describeError(error)
  // A refused source (4xx) does not change on a retry; a network or server failure may.
  const retryable = !(error instanceof ApiError && error.status < 500)
  return (
    <Alert variant="destructive" aria-label="Prefill">
      <CircleAlertIcon aria-hidden />
      <AlertTitle>{title}</AlertTitle>
      {detail && <AlertDescription>{detail}</AlertDescription>}
      {retryable && (
        <AlertAction>
          <Button
            variant="outline"
            size="sm"
            onClick={onRetry}
            disabled={retrying}
          >
            <RefreshCwIcon
              data-icon="inline-start"
              className={cn(retrying && "animate-spin")}
              aria-hidden
            />
            Retry
          </Button>
        </AlertAction>
      )}
    </Alert>
  )
}
