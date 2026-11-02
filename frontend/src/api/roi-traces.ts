// The wire viewer's ROI traces (`POST …/points/{point}/roi-traces`). The endpoint is a POST that
// changes nothing, so it is read as a query keyed on the ROIs and `normalize`: asking for the same ROIs
// again asks nothing. The key starts with the endpoint's URL (the point's ID encoded, as in every call
// of a point's route). The route is capped (a 503 with `Retry-After` when busy): the caller passes its
// retry rule.
import { useQuery, type UseQueryResult } from "@tanstack/react-query"

import type { ErrorType } from "./fetcher"
import {
  computeReconstructionRoiTraces,
  getComputeReconstructionRoiTracesUrl,
} from "./generated/endpoints"
import type {
  ReconstructionRoi,
  ReconstructionRoiTraces,
} from "./generated/model"

/** An answer with the ROIs it was asked for (its traces are in their order). */
export interface RoiTracesAnswer {
  rois: readonly ReconstructionRoi[]
  answer: ReconstructionRoiTraces
}

export interface RetryRule {
  retry?: (failureCount: number, error: ErrorType<unknown>) => boolean
  retryDelay?: (failureCount: number, error: ErrorType<unknown>) => number
}

/**
 * The traces of `rois` (at most 32, each inside the image: the caller keeps them so) on a point, sums
 * or `normalize`d; nothing while there are none. While other ROIs of the same point load, the last
 * answer stays (with its own ROIs), so a chart does not blank.
 */
export function useReconstructionRoiTraces(
  reconstructionId: number,
  pointId: string,
  rois: readonly ReconstructionRoi[],
  normalize: boolean,
  rule: RetryRule = {}
): UseQueryResult<RoiTracesAnswer, ErrorType<unknown>> {
  const point = encodeURIComponent(pointId)
  const url = getComputeReconstructionRoiTracesUrl(reconstructionId, point)
  return useQuery({
    queryKey: [url, { rois, normalize }] as const,
    queryFn: async ({ signal }) => ({
      rois,
      answer: await computeReconstructionRoiTraces(
        reconstructionId,
        point,
        { rois: rois.map(({ x, y, size }) => ({ x, y, size })), normalize },
        { signal }
      ),
    }),
    enabled: rois.length > 0,
    staleTime: Infinity,
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[0] === url ? previous : undefined,
    ...rule,
  })
}
