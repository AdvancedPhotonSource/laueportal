import { createContext } from "react"

import type { OverlayContent } from "./GridOverlay"

/**
 * What the grid's overlay shows. The overlay reads it from context (AG Grid renders it through a
 * portal inside the grid's React tree) rather than from AG Grid's overlay params: params that change
 * while AG Grid is still mounting the overlay are lost, and a fast answer can do that.
 */
export const GridOverlayContext = createContext<OverlayContent | undefined>(
  undefined
)

/** What the error overlay's Retry does: the grid reloads, and then gives the rows the keyboard. */
export const GridRetryContext = createContext<() => void>(() => undefined)
