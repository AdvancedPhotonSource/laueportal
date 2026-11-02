import type { CustomOverlayProps } from "ag-grid-react"
import { FilterXIcon, SearchXIcon } from "lucide-react"
import { use, type ReactNode } from "react"

import { EmptyState } from "@/components/EmptyState"
import { ErrorState } from "@/components/ErrorState"
import { LoadingState } from "@/components/LoadingState"
import { Button } from "@/components/ui/button"
import { GridOverlayContext, GridRetryContext } from "./overlay-context"

/** What the grid shows over its rows while it has none to show. */
export type OverlayContent =
  | { kind: "loading"; label: string }
  | {
      kind: "empty"
      title: string
      /** Filters hide every row: offer to clear them. */
      filtered: boolean
    }
  | {
      kind: "error"
      error: unknown
      /** A retry is loading: Retry spins, and stays enabled to keep the keyboard focus. */
      retrying: boolean
    }

/**
 * The grid's loading, empty and error states, shown by AG Grid over the rows area (its
 * non-exclusive overlay, below the header, so the header and its filters stay usable). AG Grid's
 * overlay layer ignores the pointer; the content takes it back for its buttons, which act on the
 * grid through AG Grid's API. The skeleton fades in after a moment, so a quick answer does not
 * flash it.
 */
export function GridOverlay({ api }: CustomOverlayProps) {
  const content = use(GridOverlayContext)
  const onRetry = use(GridRetryContext)
  if (!content) return null
  switch (content.kind) {
    case "loading":
      return (
        <LoadingState
          variant="table"
          count={6}
          label={content.label}
          className="h-full w-full animate-in self-start bg-background p-3 delay-200 fill-mode-both fade-in"
        />
      )
    case "empty":
      return (
        <Backdrop>
          <EmptyState
            icon={content.filtered ? SearchXIcon : undefined}
            title={content.title}
            className="pointer-events-auto border-dashed"
          >
            {content.filtered && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  api.setFilterModel(null)
                }}
              >
                <FilterXIcon data-icon="inline-start" aria-hidden />
                Clear filters
              </Button>
            )}
          </EmptyState>
        </Backdrop>
      )
    case "error":
      return (
        <Backdrop>
          <ErrorState
            error={content.error}
            onRetry={onRetry}
            refreshing={content.retrying}
            className="pointer-events-auto max-w-xl text-left"
          />
        </Backdrop>
      )
  }
}

/** Covers the rows area (a failed block's empty row too) and centres the state in it. */
function Backdrop({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-full w-full items-center justify-center bg-background p-3">
      {children}
    </div>
  )
}
