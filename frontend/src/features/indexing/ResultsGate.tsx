import { FileQuestionIcon } from "lucide-react"
import type { ReactNode } from "react"

import type { IndexingResultsSummary } from "@/api"
import { EmptyState } from "@/components/EmptyState"
import { ErrorState } from "@/components/ErrorState"
import { LoadingState } from "@/components/LoadingState"

/**
 * What every results tab (all but Parameters) shows before its own content: the results summary
 * (its colour modes, axes and steps) gates them all. A results file that cannot be read yet (missing,
 * several XMLs, or the artifact itself failed) points to the artifact bar above, which says why and
 * offers the way out; a summary that fails (409 results unreadable, 500, the network) is an error with
 * Retry; otherwise the tab renders with the summary.
 */
export function ResultsGate({
  readable,
  artifactSettled,
  summary,
  children,
}: {
  /** The artifact is ready or an XML that can be read. */
  readable: boolean
  /** The artifact has answered (or failed): not readable then means none to show (the bar says why), not "loading". */
  artifactSettled: boolean
  summary: {
    data: IndexingResultsSummary | undefined
    error: unknown
    isFetching: boolean
    refetch: () => unknown
  }
  children: (summary: IndexingResultsSummary) => ReactNode
}) {
  if (!readable) {
    return artifactSettled ? (
      <EmptyState icon={FileQuestionIcon} title="No results to show" />
    ) : (
      <LoadingState label="Loading the results" />
    )
  }
  if (summary.data) return children(summary.data)
  if (summary.error) {
    return (
      <ErrorState
        error={summary.error}
        onRetry={() => void summary.refetch()}
        retrying={summary.isFetching}
      />
    )
  }
  return <LoadingState label="Loading the results" />
}
