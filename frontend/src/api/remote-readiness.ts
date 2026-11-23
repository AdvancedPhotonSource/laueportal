import { getGpuReadiness, getRemoteReadiness } from "./generated/endpoints"
import {
  GetGpuReadinessResponse,
  GetRemoteReadinessResponse,
} from "./generated/zod/system/system.zod"

/** The route serves cached status immediately. A hung API must not leave an old green dot forever. */
export async function getRemoteReadinessWithin(signal: AbortSignal) {
  return within(signal, async (inner) =>
    GetRemoteReadinessResponse.parse(
      await getRemoteReadiness({ signal: inner })
    )
  )
}

/** The Pioneer GPU target's readiness, bounded as `getRemoteReadinessWithin`. */
export async function getGpuReadinessWithin(signal: AbortSignal) {
  return within(signal, async (inner) =>
    GetGpuReadinessResponse.parse(await getGpuReadiness({ signal: inner }))
  )
}

async function within<T>(
  signal: AbortSignal,
  call: (inner: AbortSignal) => Promise<T>
): Promise<T> {
  const controller = new AbortController()
  const cancel = () => {
    controller.abort(signal.reason)
  }
  if (signal.aborted) cancel()
  signal.addEventListener("abort", cancel, { once: true })
  const timer = setTimeout(() => {
    controller.abort()
  }, 10_000)
  try {
    return await call(controller.signal)
  } finally {
    clearTimeout(timer)
    signal.removeEventListener("abort", cancel)
  }
}
