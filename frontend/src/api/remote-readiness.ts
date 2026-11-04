import { getRemoteReadiness } from "./generated/endpoints"
import { GetRemoteReadinessResponse } from "./generated/zod/system/system.zod"

/** The route serves cached status immediately. A hung API must not leave an old green dot forever. */
export async function getRemoteReadinessWithin(signal: AbortSignal) {
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
    return GetRemoteReadinessResponse.parse(
      await getRemoteReadiness({ signal: controller.signal })
    )
  } finally {
    clearTimeout(timer)
    signal.removeEventListener("abort", cancel)
  }
}
