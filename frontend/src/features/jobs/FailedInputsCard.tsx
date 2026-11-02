import { keepPreviousData } from "@tanstack/react-query"
import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react"
import { useState } from "react"

import {
  getListJobFailuresQueryKey,
  useListJobFailures,
  type FailureRow,
  type JobDetail,
} from "@/api"
import { Value } from "@/components/DefinitionList"
import { ErrorState } from "@/components/ErrorState"
import { LoadingState } from "@/components/LoadingState"
import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { isActiveStatus } from "@/lib/run-status"
import { cn } from "@/lib/utils"
import {
  categoryTone,
  FAILURE_PAGE_SIZE,
  failurePaging,
  failuresEmptyText,
  jobRevision,
  shortMessage,
} from "./job-page"
import { JobCard, ToneBadge } from "./JobCard"

/**
 * Failed Inputs: the run's failure report, paged on the server, 50 records a page (#, input,
 * category, error, message). It is read again when the job's status changes (the report is written
 * when the run ends); a report that is not on this host or not readable says so.
 */
export function FailedInputsCard({ job }: { job: JobDetail }) {
  const [offset, setOffset] = useState(0)
  const active = isActiveStatus(job.status)
  const params = { offset, limit: FAILURE_PAGE_SIZE }
  const failures = useListJobFailures(job.job_id, params, {
    query: {
      queryKey: [
        ...getListJobFailuresQueryKey(job.job_id, params),
        jobRevision(job),
      ],
      // A page changes only when the job does (its revision is in the key) or on Refresh.
      staleTime: Infinity,
      placeholderData: keepPreviousData,
    },
  })
  const page = failures.data
  // While another page loads, the last one stays (dimmed): its offset is the one it was asked for.
  const shownOffset = failures.isPlaceholderData ? undefined : offset
  const paging =
    page && shownOffset !== undefined
      ? failurePaging(page, shownOffset)
      : undefined
  // While the run is active its report is not written yet: the polled counter says how many failed.
  const empty = page
    ? failuresEmptyText(
        page,
        offset,
        active ? job.progress.n_failed : undefined
      )
    : undefined

  return (
    <JobCard
      title="Failed Inputs"
      actions={
        page && (page.rows.length > 0 || offset > 0) ? (
          <>
            <span
              className="text-xs text-muted-foreground"
              role="status"
              aria-live="polite"
            >
              {paging?.text}
            </span>
            {/* While a page loads both are off, but keep the keyboard focus (focusableWhenDisabled). */}
            <Button
              variant="outline"
              size="sm"
              disabled={paging?.previous == null}
              focusableWhenDisabled
              className="aria-disabled:opacity-50"
              onClick={() => {
                if (paging?.previous != null) setOffset(paging.previous)
              }}
            >
              <ChevronLeftIcon data-icon="inline-start" aria-hidden />
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={paging?.next == null}
              focusableWhenDisabled
              className="aria-disabled:opacity-50"
              onClick={() => {
                if (paging?.next != null) setOffset(paging.next)
              }}
            >
              Next
              <ChevronRightIcon data-icon="inline-end" aria-hidden />
            </Button>
          </>
        ) : undefined
      }
    >
      {failures.error !== null && (
        <ErrorState
          error={failures.error}
          onRetry={() => void failures.refetch({ cancelRefetch: false })}
          refreshing={failures.isFetching}
          stale={page !== undefined}
          className="mb-2"
        />
      )}
      {page ? (
        empty ? (
          <p className="text-muted-foreground" data-testid="failures-empty">
            {empty}
          </p>
        ) : (
          <FailureTable rows={page.rows} busy={failures.isPlaceholderData} />
        )
      ) : (
        failures.error === null && (
          <LoadingState
            variant="table"
            count={3}
            label="Loading the failed inputs"
          />
        )
      )}
    </JobCard>
  )
}

function FailureTable({
  rows,
  busy,
}: {
  rows: readonly FailureRow[]
  busy: boolean
}) {
  return (
    <div aria-busy={busy} className={cn(busy && "opacity-60")}>
      <Table aria-label="Failed inputs">
        <TableHeader>
          <TableRow>
            <TableHead className="w-16">#</TableHead>
            <TableHead>Input</TableHead>
            <TableHead>Category</TableHead>
            <TableHead>Error</TableHead>
            <TableHead>Message</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row, index) => (
            <TableRow
              key={`${String(row.index ?? "")}-${String(index)}`}
              data-index={row.index ?? undefined}
            >
              <TableCell className="tabular-nums">
                {row.index ?? <Value text={null} />}
              </TableCell>
              <TableCell title={row.source ?? undefined}>
                {row.input_id ? (
                  <code className="font-mono text-xs">{row.input_id}</code>
                ) : (
                  <Value text={null} />
                )}
              </TableCell>
              <TableCell>
                <ToneBadge tone={categoryTone(row.category)} icon={false}>
                  {row.category}
                </ToneBadge>
              </TableCell>
              <TableCell>
                <Value text={row.error_type} />
              </TableCell>
              {/* A blank message (a migrated run's queued inputs of a cancelled run) is a dash, as the others. */}
              <TableCell
                title={row.message.trim() ? row.message : undefined}
                className="min-w-48 wrap-break-word whitespace-normal"
              >
                {row.message.trim() ? (
                  shortMessage(row.message)
                ) : (
                  <Value text={null} />
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}
