// The misorientation statistics of a selection (`POST …/selection-stats`). The endpoint is a POST
// that changes nothing, so it is read as a query keyed on the patterns: selecting the same patterns
// again asks nothing, and only the latest selection's answer is shown. The key starts with the
// endpoint's URL, under the run's `/api/v1/indexing-runs/{id}/`, so reading the run's results again
// (another XML, a conversion: `reloadResults`) refreshes it too.
import { useQuery, type UseQueryResult } from "@tanstack/react-query"

import type { ErrorType } from "./fetcher"
import {
  computeIndexingSelectionStats,
  getComputeIndexingSelectionStatsUrl,
} from "./generated/endpoints"
import type { SelectionStats } from "./generated/model"

type Patterns = readonly (readonly [number, number])[]

/** The statistics query's key: the endpoint's URL (under the run's prefix), then the patterns. */
export function getSelectionStatsQueryKey(
  indexingId: number,
  patterns: Patterns | null
) {
  return [getComputeIndexingSelectionStatsUrl(indexingId), patterns] as const
}

/**
 * The statistics of `patterns` (`[step, pattern_index]` pairs, each ≥ 0), or nothing while
 * `patterns` is null. The caller keeps the request within the API's limits (at most 700 distinct
 * oriented patterns, 33,600 entries); a 422 is not retried.
 */
export function useSelectionStats(
  indexingId: number,
  patterns: Patterns | null
): UseQueryResult<SelectionStats, ErrorType<unknown>> {
  return useQuery({
    queryKey: getSelectionStatsQueryKey(indexingId, patterns),
    queryFn: ({ signal }) =>
      computeIndexingSelectionStats(
        indexingId,
        { patterns: (patterns ?? []).map(([step, index]) => [step, index]) },
        { signal }
      ),
    enabled: patterns !== null,
    staleTime: Infinity,
  })
}
