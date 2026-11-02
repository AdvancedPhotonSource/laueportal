// The colorbar's distribution of a large frame, computed off the main thread (compute.ts).
import { distributionOf, type DistributionInput } from "./distribution"

self.onmessage = (e: MessageEvent<DistributionInput>) => {
  const dist = distributionOf(e.data)
  const buffer = (dist.kind === "counts" ? dist.cumulative : dist.values)
    .buffer as ArrayBuffer
  self.postMessage(dist, { transfer: [buffer] })
}
