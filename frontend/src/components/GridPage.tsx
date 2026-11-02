import type { ReactNode } from "react"

import { Button } from "@/components/ui/button"

/**
 * A page around a DataGrid (the scans, the reconstructions, the indexations): its title, the
 * selection's count with Clear selection, the page's buttons, and the grid filling the rest of the
 * window's height (below the navbar and the page's padding).
 */
export function GridPage({
  title,
  selected,
  onClearSelection,
  actions,
  children,
}: {
  title: string
  /** Rows selected: their count and Clear selection show when there are some. */
  selected: number
  onClearSelection: () => void
  /** The page's buttons, on the title's row. */
  actions: ReactNode
  children: ReactNode
}) {
  return (
    <section className="flex h-[calc(100svh-var(--spacing)*20-1px)] min-h-96 flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-xl font-semibold">{title}</h1>
        <div className="ml-auto flex items-center gap-2">
          {selected > 0 && (
            <>
              <span className="text-sm text-muted-foreground" role="status">
                {selected} selected
              </span>
              <Button variant="ghost" size="sm" onClick={onClearSelection}>
                Clear selection
              </Button>
            </>
          )}
          {actions}
        </div>
      </div>
      {children}
    </section>
  )
}
