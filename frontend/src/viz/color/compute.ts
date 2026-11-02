// The distribution off the main thread: a frame (millions of values) is counted or sorted in a
// worker so the page stays responsive; small data (a map's scalars) is done in place.
import { useCallback, useEffect, useState } from "react"

import {
  distributionOf,
  type Distribution,
  type DistributionInput,
} from "./distribution"

/** Above this many values, the work goes to a worker. */
const WORKER_ABOVE = 1 << 18

/**
 * The distribution of `data`, in a worker for large data unless `inPage`. Rejects with an
 * AbortError once `signal` aborts, and with the worker's error when it fails (it could not start,
 * or ran out of memory).
 */
export function computeDistribution(
  data: DistributionInput,
  signal?: AbortSignal,
  inPage = false
): Promise<Distribution> {
  if (inPage || typeof Worker === "undefined" || data.length <= WORKER_ABOVE) {
    return Promise.resolve().then(() => {
      if (signal?.aborted) throw aborted()
      return distributionOf(data)
    })
  }
  return new Promise((resolve, reject) => {
    const worker = new Worker(
      new URL("./distribution.worker.ts", import.meta.url),
      { type: "module" }
    )
    const stop = () => {
      worker.terminate()
    }
    worker.onmessage = (e: MessageEvent<Distribution>) => {
      stop()
      resolve(e.data)
    }
    // A worker that cannot load sends a plain Event, without a message.
    worker.onerror = (e: Event) => {
      stop()
      const message = e instanceof ErrorEvent ? e.message : ""
      reject(new Error(message || "the worker failed"))
    }
    signal?.addEventListener("abort", () => {
      stop()
      reject(aborted())
    })
    // Posting copies the data: the caller keeps (and draws) its own array.
    worker.postMessage(data)
  })
}

function aborted(): DOMException {
  return new DOMException("Aborted", "AbortError")
}

export interface DistributionState {
  /** The distribution: null before the first one is ready, without data, and after a failure. */
  distribution: Distribution | null
  /** Why the current data's computation failed, else null. Pass it to the ColorBar (`error`). */
  error: string | null
  /** Computes the current data's distribution again, in the page. The ColorBar's `onRetry`. */
  retry: () => void
}

/**
 * The distribution of `data`. While a new array's is computed the previous one stays, so a new
 * frame (another step, the next depth) is drawn at once with the previous range instead of
 * blanking, and recoloured when its own arrives. The computation for a replaced array is dropped.
 * A failure drops the previous distribution and sets `error`; `retry` computes it in the page.
 */
export function useDistribution(
  data: DistributionInput | null | undefined
): DistributionState {
  const [state, setState] = useState<{
    distribution: Distribution | null
    error: string | null
    of: DistributionInput | null
  }>({ distribution: null, error: null, of: null })
  // The array a retry is for (a new object per retry, so each one runs).
  const [retried, setRetried] = useState<{ of: DistributionInput | null }>({
    of: null,
  })
  useEffect(() => {
    if (!data) return
    const controller = new AbortController()
    computeDistribution(data, controller.signal, retried.of === data).then(
      (distribution) => {
        setState({ distribution, error: null, of: data })
      },
      (e: unknown) => {
        // Aborted by a newer array or an unmount: nothing to show.
        if (controller.signal.aborted) return
        const error = e instanceof Error ? e.message : String(e)
        setState({ distribution: null, error, of: data })
      }
    )
    return () => {
      controller.abort()
    }
  }, [data, retried])
  const retry = useCallback(() => {
    setRetried({ of: data ?? null })
  }, [data])
  return {
    distribution: data ? state.distribution : null,
    error: data && state.of === data ? state.error : null,
    retry,
  }
}
