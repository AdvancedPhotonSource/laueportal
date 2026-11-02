import { XIcon } from "lucide-react"

import { ApiError, useGetIndexingPoint, type PointDetails } from "@/api"
import { ErrorState } from "@/components/ErrorState"
import { LoadingState } from "@/components/LoadingState"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { formatSignificant } from "@/lib/format"
import type { ScopeParams } from "./viewer-store"

const fixed = (value: number | null, digits: number) =>
  value === null ? "—" : value.toFixed(digits)

/**
 * The clicked point's details (`GET …/points/{step}`): the step, its frame, motor position and depth,
 * its coordinates on the plotted axes, and each pattern, the clicked one marked.
 */
export function PointDetailsCard({
  indexingId,
  step,
  patternIndex,
  axes,
  scope,
  onClose,
}: {
  indexingId: number
  step: number
  /** The clicked record's pattern (-1: a frame-only record), when known. */
  patternIndex: number | null
  axes: readonly string[]
  scope: ScopeParams
  onClose: () => void
}) {
  const query = useGetIndexingPoint(indexingId, step, {
    axes: axes.join(","),
    ...scope,
  })
  return (
    <Card size="sm" role="region" aria-label="Clicked point details">
      <CardHeader className="flex items-center gap-2 py-1.5">
        <CardTitle>
          <h3>Clicked point details</h3>
        </CardTitle>
        <Button
          variant="ghost"
          size="icon-sm"
          className="ml-auto"
          aria-label="Close the point details"
          onClick={onClose}
        >
          <XIcon aria-hidden />
        </Button>
      </CardHeader>
      <CardContent>
        {query.data ? (
          <Details point={query.data} patternIndex={patternIndex} />
        ) : query.error ? (
          query.error instanceof ApiError && query.error.status === 404 ? (
            <p className="text-muted-foreground">
              Step {step} is not in these results.
            </p>
          ) : (
            <ErrorState
              error={query.error}
              onRetry={() => void query.refetch()}
              refreshing={query.isFetching}
            />
          )
        ) : (
          <LoadingState label="Loading the point's details" />
        )}
      </CardContent>
    </Card>
  )
}

function Details({
  point,
  patternIndex,
}: {
  point: PointDetails
  patternIndex: number | null
}) {
  const [x = null, y = null, z = null] = point.sample_position
  return (
    <div className="flex flex-col gap-2">
      <p>
        <strong>Step #{point.step}</strong>{" "}
        <span className="text-muted-foreground">(frame {point.frame_id})</span>{" "}
        · Motor position ({fixed(x, 1)}, {fixed(y, 1)}, {fixed(z, 1)}) µm
        {point.depth !== null && (
          <> · Depth {fixed(point.depth, 2)} µm</>
        )} · {point.n_peaks} detected peaks
        {!point.in_scope && (
          <span className="text-muted-foreground"> · outside the scope</span>
        )}
      </p>
      {point.coordinates.length > 0 && (
        <p className="font-mono text-xs tabular-nums">
          {point.coordinates
            .map(
              (axis) =>
                `${axis.label} ${axis.value === null ? "—" : formatSignificant(axis.value)}`
            )
            .join(" · ")}
        </p>
      )}
      {point.patterns.length === 0 ? (
        <p className="text-muted-foreground">No pattern at this step.</p>
      ) : (
        <table className="w-full text-xs tabular-nums">
          <thead className="text-left text-muted-foreground">
            <tr>
              <th className="font-normal">Pattern</th>
              <th className="font-normal">Indexed</th>
              <th className="font-normal">Goodness</th>
              <th className="font-normal">RMS error (°)</th>
              <th className="font-normal">In scope</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {point.patterns.map((pattern) => (
              <tr
                key={pattern.pattern_index}
                aria-current={
                  pattern.pattern_index === patternIndex ? "true" : undefined
                }
                className="aria-[current]:font-semibold"
              >
                <td>
                  {pattern.pattern_index}
                  {pattern.pattern_index === patternIndex && " (clicked)"}
                </td>
                <td>
                  {pattern.indexed
                    ? `${String(pattern.n_indexed)}/${String(point.n_peaks)}`
                    : "not indexed"}
                </td>
                <td>{fixed(pattern.goodness, 1)}</td>
                <td>{fixed(pattern.rms_error_deg, 5)}</td>
                <td>{pattern.in_scope ? "yes" : "no"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
