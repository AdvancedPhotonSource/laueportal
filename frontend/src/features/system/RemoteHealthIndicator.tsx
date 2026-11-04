import { RefreshCwIcon } from "lucide-react"

import { getRemoteReadinessWithin, useGetRemoteReadiness } from "@/api"
import { DefinitionList } from "@/components/DefinitionList"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import {
  CHECK_LABELS,
  CHECK_STATES,
  checkTone,
  readinessView,
  REMOTE_POLL_MS,
} from "./remote-readiness"
import { ToneBadge } from "./StatusCard"

const DOT = {
  ok: "bg-success-quiet",
  warning: "bg-warning",
  error: "bg-destructive",
  unknown: "border-[1.5px] border-muted-foreground",
}

/** Shared read-only readiness, independent of the local worker's indicator. Never submits a job. */
export function RemoteHealthIndicator() {
  const query = useGetRemoteReadiness({
    query: {
      queryFn: ({ signal }) => getRemoteReadinessWithin(signal),
      retry: false,
      staleTime: REMOTE_POLL_MS,
      refetchInterval: (q) =>
        q.state.data?.enabled === false
          ? false
          : q.state.data?.checking
            ? 2_000
            : REMOTE_POLL_MS,
      refetchIntervalInBackground: false,
    },
  })
  const data = query.data
  const view = readinessView(data, query.isError)
  const issue = data?.checks.find((check) => check.state !== "ok")
  const crux = data?.checks.find((check) => check.name === "crux")
  if (data?.enabled === false) return null

  return (
    <Dialog>
      <Tooltip>
        <TooltipTrigger
          render={<DialogTrigger />}
          aria-label={`ALCF: ${view.label}`}
          data-tone={view.tone}
          className="inline-flex h-7 shrink-0 items-center justify-center gap-1.5 rounded-md px-2 text-xs text-foreground outline-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring"
        >
          <span
            aria-hidden
            data-slot="remote-status-dot"
            className={cn("size-[7px] rounded-full", DOT[view.tone])}
          />
          <span className="hidden xl:inline">ALCF</span>
        </TooltipTrigger>
        <TooltipContent
          side="bottom"
          align="start"
          className="flex-col items-start gap-1 text-left"
        >
          <p className="font-medium">ALCF: {view.label}</p>
          {data && !data.stale && !query.isError && (
            <p>
              {issue
                ? `${CHECK_LABELS[issue.name]}: ${CHECK_STATES[issue.state]}`
                : crux
                  ? `Crux: ${crux.detail} · ${String(data.checks.length)} checks passed`
                  : "Checking connectivity…"}
            </p>
          )}
          <p className="opacity-70">
            {data?.checked_at
              ? `Checked at ${new Date(data.checked_at).toLocaleTimeString([], { hourCycle: "h23" })}`
              : "No check completed"}
          </p>
        </TooltipContent>
      </Tooltip>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>ALCF status</DialogTitle>
          <DialogDescription>
            Connectivity checks do not guarantee worker startup or PBS capacity.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-wrap items-center gap-2">
          <ToneBadge tone={view.tone}>{view.label}</ToneBadge>
          <Button
            size="sm"
            variant="outline"
            disabled={query.isFetching}
            onClick={() => void query.refetch({ cancelRefetch: false })}
          >
            <RefreshCwIcon
              className={cn("size-3.5", query.isFetching && "animate-spin")}
            />
            Refresh
          </Button>
          {data?.checking && (
            <span role="status" className="text-xs text-muted-foreground">
              Checking…
            </span>
          )}
        </div>
        <p className="text-xs text-muted-foreground">
          {data?.checked_at
            ? `Last checked ${new Date(data.checked_at).toLocaleString()}`
            : "No check completed"}
          {data &&
            data.retry_after_seconds > 0 &&
            ` · Next check in ${String(Math.ceil(data.retry_after_seconds))} s`}
        </p>
        {query.isError && (
          <p role="status" className="text-sm text-muted-foreground">
            Status check unavailable
          </p>
        )}
        {data && (
          <DefinitionList
            rows={data.checks.map((check) => ({
              label: CHECK_LABELS[check.name],
              value: (
                <span className="flex flex-col items-start gap-1">
                  <ToneBadge
                    tone={
                      data.stale || query.isError
                        ? "unknown"
                        : checkTone(check.state)
                    }
                  >
                    {data.stale || query.isError
                      ? "Unknown"
                      : check.state === "ok"
                        ? check.detail
                        : CHECK_STATES[check.state]}
                  </ToneBadge>
                  {check.state !== "ok" && (
                    <span className="text-xs text-muted-foreground">
                      {check.detail}
                    </span>
                  )}
                </span>
              ),
            }))}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}
