// The Peaks and Patterns tabs: "Indexed Peaks (N total)" and "Indexed Patterns (N total)", a
// DataGrid on the run's grid list within the applied scope, and the column checklist beside
// it. N is the scope's rows (a one-row query, keyed under the run's URL, so reloadResults
// reads it again); while a filter applies, the heading also says how many match.
import { useEffect, useRef, useState } from "react"

import {
  listIndexingPatterns,
  listIndexingPeaks,
  useListIndexingPatterns,
  useListIndexingPeaks,
  type PatternRow,
  type PeakRow,
} from "@/api"
import {
  DataGrid,
  type DataGridHandle,
  type FetchRows,
  type GridColumn,
  type GridRowCount,
} from "@/components/DataGrid"
import { Checkbox } from "@/components/ui/checkbox"
import { surface } from "@/lib/styles"
import { cn } from "@/lib/utils"
import { onResultsReload } from "./artifact-bar"
import { PATTERN_COLUMNS, PEAK_COLUMNS } from "./columns"
import { Section } from "./controls"
import {
  groupChoices,
  PATTERN_COLUMN_GROUPS,
  PATTERNS_GRID_ID,
  PEAK_COLUMN_GROUPS,
  PEAKS_GRID_ID,
  totalText,
  type ColumnGroup,
} from "./results-tables"
import { useScopeParams } from "./viewer-store"

// The grid's row ID: the row's position in the whole table (the API's `id`).
const rowId = (row: { id: number }) => String(row.id)

export function PeaksTab({ indexingId }: { indexingId: number }) {
  const scope = useScopeParams()
  const total = useListIndexingPeaks(indexingId, { ...scope, limit: 1 })
  return (
    <ResultsTable<PeakRow>
      indexingId={indexingId}
      title="Indexed Peaks"
      noun="peaks"
      gridId={PEAKS_GRID_ID}
      columns={PEAK_COLUMNS}
      groups={PEAK_COLUMN_GROUPS}
      listKey={`${String(scope.pattern0_only)}:${String(scope.min_peaks)}`}
      total={total.data?.total}
      fetchRows={(query, signal) =>
        listIndexingPeaks(indexingId, { ...query, ...scope }, { signal })
      }
    />
  )
}

export function PatternsTab({ indexingId }: { indexingId: number }) {
  const scope = useScopeParams()
  const total = useListIndexingPatterns(indexingId, { ...scope, limit: 1 })
  return (
    <ResultsTable<PatternRow>
      indexingId={indexingId}
      title="Indexed Patterns"
      noun="patterns"
      gridId={PATTERNS_GRID_ID}
      columns={PATTERN_COLUMNS}
      groups={PATTERN_COLUMN_GROUPS}
      listKey={`${String(scope.pattern0_only)}:${String(scope.min_peaks)}`}
      total={total.data?.total}
      fetchRows={(query, signal) =>
        listIndexingPatterns(indexingId, { ...query, ...scope }, { signal })
      }
    />
  )
}

function ResultsTable<TRow extends { id: number }>({
  indexingId,
  title,
  noun,
  gridId,
  columns,
  groups,
  listKey,
  total,
  fetchRows,
}: {
  indexingId: number
  title: string
  noun: string
  gridId: string
  columns: readonly GridColumn<TRow>[]
  groups: readonly ColumnGroup<TRow>[]
  /** The scope: another one is another list (the grid keeps its sort and filters). */
  listKey: string
  total: number | undefined
  fetchRows: FetchRows<TRow>
}) {
  const grid = useRef<DataGridHandle>(null)
  const [count, setCount] = useState<GridRowCount>()
  // The columns shown, as the grid tells them (its column state); unknown until it is made.
  const [shown, setShown] = useState<ReadonlySet<string>>()

  // Another XML or a finished conversion (reloadResults): load the rows shown again.
  useEffect(
    () =>
      onResultsReload((id) => {
        if (id === indexingId) grid.current?.refresh()
      }),
    [indexingId]
  )

  return (
    <div className="flex flex-col gap-2">
      <h2 className="text-lg font-semibold">
        {title}
        {totalText(total, count)}
      </h2>
      <div className="grid gap-4 md:grid-cols-[13rem_minmax(0,1fr)]">
        <aside
          aria-label={`${title} columns`}
          className={cn(
            surface,
            "flex h-fit flex-col gap-4 p-3 md:max-h-[max(400px,calc(100dvh-17rem))] md:overflow-y-auto"
          )}
        >
          {groupChoices(groups, columns).map(({ title: group, choices }) => (
            <Section key={group} title={group}>
              {choices.map(({ field, label }) => (
                <label key={field} className="flex items-center gap-2 text-sm">
                  <Checkbox
                    checked={shown?.has(field) ?? false}
                    disabled={shown === undefined}
                    onCheckedChange={(checked) => {
                      grid.current?.setColumnsShown([field], checked)
                    }}
                  />
                  <span>{label}</span>
                </label>
              ))}
            </Section>
          ))}
        </aside>
        <DataGrid<TRow>
          ref={grid}
          gridId={gridId}
          columns={columns}
          fetchRows={fetchRows}
          listKey={listKey}
          getRowId={rowId}
          noun={noun}
          onRowCount={setCount}
          onShownColumnsChange={(fields) => {
            setShown(new Set(fields))
          }}
          className="h-[max(400px,calc(100dvh-17rem))]"
        />
      </div>
    </div>
  )
}
