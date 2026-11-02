import { useSelectionStats } from "@/api"
import { DefinitionList } from "@/components/DefinitionList"
import { ErrorState } from "@/components/ErrorState"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { formatCount, formatSignificant } from "@/lib/format"
import { subheading } from "@/lib/styles"
import { isTooManyPatterns, MAX_STATS_PATTERNS } from "./pole-figure"

const SKIPPED = `Misorientation stats skipped (more than ${String(MAX_STATS_PATTERNS)} patterns selected).`

const plural = (n: number, word: string) =>
  `${formatCount(n)} ${word}${n === 1 ? "" : "s"}`

/**
 * The selection's card: its poles and patterns, and their pairwise misorientation
 * (`selection-stats`: mean, min and max), or the skip notice above 700 patterns, where nothing
 * is asked.
 */
export function SelectionStatsCard({
  indexingId,
  poles,
  patterns,
  request,
}: {
  indexingId: number
  poles: number
  patterns: number
  /** The distinct patterns to send, or null when nothing is asked (statsRequest). */
  request: [number, number][] | null
}) {
  const query = useSelectionStats(indexingId, request)
  const stats = request ? query.data : undefined

  let misorientation
  if (patterns > MAX_STATS_PATTERNS) {
    misorientation = <p className="text-muted-foreground">{SKIPPED}</p>
  } else if (!request) {
    misorientation = null
  } else if (stats) {
    misorientation =
      stats.mean === null || stats.min === null || stats.max === null ? (
        <p className="text-muted-foreground">
          Fewer than two of these patterns have an orientation.
        </p>
      ) : (
        <DefinitionList
          aria-label="Misorientation"
          rows={[
            { label: "Mean", value: `${formatSignificant(stats.mean)}°` },
            { label: "Min", value: `${formatSignificant(stats.min)}°` },
            { label: "Max", value: `${formatSignificant(stats.max)}°` },
            {
              label: "Pairs",
              value: `${formatCount(stats.n_pairs)} (${stats.symmetry} symmetry)`,
            },
          ]}
        />
      )
  } else if (query.error) {
    misorientation = isTooManyPatterns(query.error) ? (
      <p className="text-muted-foreground">{SKIPPED}</p>
    ) : (
      <ErrorState
        error={query.error}
        onRetry={() => void query.refetch()}
        retrying={query.isFetching}
      />
    )
  } else {
    misorientation = (
      <p role="status" className="text-muted-foreground">
        Computing the misorientation…
      </p>
    )
  }

  return (
    <Card size="sm" role="region" aria-label="Selection">
      <CardHeader>
        <CardTitle>
          <h3>Selection</h3>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-1.5">
        {patterns === 0 ? (
          <p className="text-muted-foreground">
            No indexed patterns in the selection.
          </p>
        ) : (
          <p>
            {plural(poles, "pole")} from {plural(patterns, "pattern")}
          </p>
        )}
        {misorientation && (
          <div className="flex flex-col gap-1">
            <h4 className={subheading}>Misorientation</h4>
            {misorientation}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
