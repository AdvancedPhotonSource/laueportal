// The artifact bar: what the viewer reads (results file, an XML, nothing) and what can be done about
// it: convert an XML to HDF5 (a worker job the page follows by polling the artifact), choose between
// XMLs, or read why there are no results.
import type { QueryClient } from "@tanstack/react-query"

import {
  ArtifactState,
  ConversionStatus,
  getGetIndexingArtifactQueryKey,
  XmlSource,
  type ConversionJob,
  type IndexingArtifact,
} from "@/api"
import { backoff, isGone } from "@/lib/polling"

/** How often the page reads the artifact while its conversion is queued or running. */
export const ARTIFACT_POLL_MS = 2000
/**
 * … while the run writes its results, its run directory does not respond (a hung mount), or it is an
 * open live run (its results grow; revalidated by the artifact's ETag, a 304 the same answer).
 */
export const WAITING_POLL_MS = 5000

export function isConverting(artifact: IndexingArtifact | undefined): boolean {
  const status = artifact?.conversion?.status
  return (
    status === ConversionStatus.queued || status === ConversionStatus.running
  )
}

/**
 * Poll every 2 s while a conversion is queued or running, every 5 s while the run writes its results,
 * its run directory does not respond or it is an open live run, doubled per consecutive failed read;
 * not once the run is gone (a 404; src/lib/polling.ts).
 */
export function artifactRefetchInterval(
  artifact: IndexingArtifact | undefined,
  error: unknown,
  failures: number
): number | false {
  if (isGone(error)) return false
  if (isConverting(artifact)) return backoff(ARTIFACT_POLL_MS, failures)
  const state = artifact?.state
  if (
    state === ArtifactState.writing ||
    state === ArtifactState.not_responding ||
    artifact?.live_open === true
  ) {
    return backoff(WAITING_POLL_MS, failures)
  }
  return false
}

/**
 * Whether the results the page shows were replaced: ready before and after, another results file
 * (`results_version`), as an open live run publishes its preview. The page then reads its results
 * again (`reloadResults`); each product revalidates by its ETag.
 */
export function resultsReplaced(
  before: IndexingArtifact | undefined,
  after: IndexingArtifact | undefined
): boolean {
  return (
    before?.state === ArtifactState.ready &&
    after?.state === ArtifactState.ready &&
    before.results_version !== after.results_version
  )
}

/** A path's last part. */
export function fileName(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).at(-1) ?? path
}

export const XML_SOURCE_TEXT: Record<XmlSource, string> = {
  [XmlSource.recorded]: "the XML recorded with the run",
  [XmlSource.chosen]: "the XML chosen on this page",
  [XmlSource.only]: "the only XML in the run's folder",
}

/** The conversion's progress as one line. */
export function conversionText(conversion: ConversionJob): string {
  switch (conversion.status) {
    case ConversionStatus.queued:
      return conversion.held
        ? "Conversion queued: held by the live session…"
        : "Conversion queued: waiting for a worker…"
    case ConversionStatus.running:
      return "Converting to HDF5…"
    case ConversionStatus.finished:
      // Still XML only: the converted file did not validate (the problems say why).
      return "The conversion finished, but its results file cannot be used: the viewer still reads the XML."
    case ConversionStatus.failed:
      return conversion.message
        ? `The conversion failed: ${conversion.message}`
        : "The conversion failed."
  }
}

/** The progress line when polling stopped without an outcome: the conversion's state cannot be read. */
export const CONVERSION_LOST_TEXT =
  "The conversion's progress cannot be followed: its state cannot be read."

type ResultsReloadListener = (indexingId: number) => void

const resultsReloadListeners = new Set<ResultsReloadListener>()

/**
 * Hear `reloadResults`: what reads a run's results outside React Query (the Peaks and Patterns
 * grids, whose DataGrid fetches its own blocks) loads them again when it is told the run's ID.
 * Returns the function that stops listening.
 */
export function onResultsReload(listener: ResultsReloadListener): () => void {
  resultsReloadListeners.add(listener)
  return () => {
    resultsReloadListeners.delete(listener)
  }
}

/**
 * Read every product of this run again (the results it reads changed: converted, or another XML
 * chosen), except the artifact, which the caller has just read; the grids hear it through
 * `onResultsReload`.
 */
export function reloadResults(queryClient: QueryClient, indexingId: number) {
  for (const listener of resultsReloadListeners) listener(indexingId)
  const prefix = `/api/v1/indexing-runs/${String(indexingId)}/`
  const artifactKey = getGetIndexingArtifactQueryKey(indexingId)[0]
  return queryClient.invalidateQueries({
    predicate: ({ queryKey }) => {
      const [first] = queryKey
      return (
        typeof first === "string" &&
        first.startsWith(prefix) &&
        first !== artifactKey
      )
    },
  })
}
