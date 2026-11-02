import { useQueryErrorResetBoundary } from "@tanstack/react-query"
import { useRouter, type ErrorComponentProps } from "@tanstack/react-router"
import { RefreshCwIcon, TriangleAlertIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { describeError } from "@/lib/errors"

/**
 * The error boundary of every route (the router's defaultErrorComponent): each route match has its
 * own, so a failing page leaves the navbar in place. A problem+json error shows its title and detail.
 * Retry clears the error and reloads the route. The title comes from the router: a loader error
 * sets the match status, and titleHead turns that into "Error". An error thrown while rendering does
 * not change the match, so the tab keeps the page's own title.
 */
export function RouteError({ error, reset }: ErrorComponentProps) {
  const router = useRouter()
  const queryErrorReset = useQueryErrorResetBoundary()
  const { title, detail, instance } = describeError(error)

  const retry = () => {
    queryErrorReset.reset()
    reset()
    void router.invalidate()
  }

  return (
    <section
      role="alert"
      className="mx-auto flex max-w-lg flex-col items-center gap-3 py-16 text-center"
    >
      <TriangleAlertIcon className="size-10 text-destructive" aria-hidden />
      <h1 className="text-xl font-semibold">{title}</h1>
      {detail && <p className="text-muted-foreground">{detail}</p>}
      {instance && (
        <p className="text-xs text-muted-foreground">
          Reference <span className="font-mono">{instance}</span>
        </p>
      )}
      <Button variant="outline" onClick={retry}>
        <RefreshCwIcon data-icon="inline-start" aria-hidden />
        Retry
      </Button>
    </section>
  )
}
