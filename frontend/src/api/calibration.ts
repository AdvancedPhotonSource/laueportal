// The Calibration page's reads. Its POST routes change nothing, so they are read as queries keyed on
// their body (as the ROI traces): asking for the same peaks or indexing again asks nothing, and the
// last answer stays on screen while a changed one loads. Refine and Save are explicit actions (the
// generated mutations). The routes are capped (a 503 with `Retry-After` when busy): the caller passes
// its retry rule.
import {
  keepPreviousData,
  useQuery,
  type UseQueryResult,
} from "@tanstack/react-query"

import type { ErrorType } from "./fetcher"
import {
  findCalibrationPeaks,
  getFindCalibrationPeaksMutationKey,
  getIndexCalibrationPeaksMutationKey,
  getInspectCalibrationInputsMutationKey,
  indexCalibrationPeaks,
  inspectCalibrationInputs,
} from "./generated/endpoints"
import type {
  CalibrationIndexing,
  CalibrationIndexRequest,
  CalibrationInputs,
  CalibrationInputsInfo,
  CalibrationPeakSearchRequest,
  CalibrationPeaks,
} from "./generated/model"
import type { RetryRule } from "./roi-traces"

/** The frame, geometry and crystal checked together (null while any is missing). */
export function useCalibrationInputs(
  inputs: CalibrationInputs | null,
  rule: RetryRule = {}
): UseQueryResult<CalibrationInputsInfo, ErrorType<unknown>> {
  return useQuery({
    queryKey: [...getInspectCalibrationInputsMutationKey(), inputs] as const,
    queryFn: ({ signal }) =>
      inspectCalibrationInputs(inputs as CalibrationInputs, { signal }),
    enabled: inputs !== null,
    staleTime: Infinity,
    retry: false,
    ...rule,
  })
}

/** The frame's peaks with these settings (null while there is no frame). */
export function useCalibrationPeaks(
  request: CalibrationPeakSearchRequest | null,
  rule: RetryRule = {}
): UseQueryResult<CalibrationPeaks, ErrorType<unknown>> {
  return useQuery({
    queryKey: [...getFindCalibrationPeaksMutationKey(), request] as const,
    queryFn: ({ signal }) =>
      findCalibrationPeaks(request as CalibrationPeakSearchRequest, { signal }),
    enabled: request !== null,
    staleTime: Infinity,
    retry: false,
    placeholderData: keepPreviousData,
    ...rule,
  })
}

/** The peaks indexed with the starting detector (null while the inputs or the peaks are not ready). */
export function useCalibrationIndexing(
  request: CalibrationIndexRequest | null,
  rule: RetryRule = {}
): UseQueryResult<CalibrationIndexing, ErrorType<unknown>> {
  return useQuery({
    queryKey: [...getIndexCalibrationPeaksMutationKey(), request] as const,
    queryFn: ({ signal }) =>
      indexCalibrationPeaks(request as CalibrationIndexRequest, { signal }),
    enabled: request !== null,
    staleTime: Infinity,
    retry: false,
    placeholderData: keepPreviousData,
    ...rule,
  })
}
