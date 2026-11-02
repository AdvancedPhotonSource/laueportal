import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"

/**
 * Skeletons in place of content that is loading, in the shape of what comes: a page (heading, text,
 * a block), a table (header and rows) or a row of cards. Screen readers hear the label.
 */
export function LoadingState({
  label = "Loading",
  variant = "page",
  count = 4,
  className,
}: {
  label?: string
  variant?: "page" | "table" | "cards"
  /** Table rows or cards. */
  count?: number
  className?: string
}) {
  const items = Array.from({ length: count }, (_, index) => index)
  return (
    <div
      role="status"
      aria-busy="true"
      data-slot="loading-state"
      className={cn("flex flex-col gap-3", className)}
    >
      <span className="sr-only">{label}</span>
      {variant === "cards" && (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {items.map((index) => (
            <Skeleton key={index} className="h-24" />
          ))}
        </div>
      )}
      {variant === "table" && (
        <>
          <Skeleton className="h-8 w-full" />
          {items.map((index) => (
            <Skeleton key={index} className="h-6 w-full" />
          ))}
        </>
      )}
      {variant === "page" && (
        <>
          <Skeleton className="h-7 w-48" />
          <Skeleton className="h-4 w-full max-w-xl" />
          <Skeleton className="h-4 w-full max-w-md" />
          <Skeleton className="h-32 w-full" />
        </>
      )}
    </div>
  )
}
