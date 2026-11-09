import { Link } from "@tanstack/react-router"
import {
  ActivityIcon,
  CircleAlertIcon,
  FileUpIcon,
  LayersIcon,
  ListIcon,
  RefreshCwIcon,
  ScanSearchIcon,
} from "lucide-react"

import { useRef, useState } from "react"

import { useGetSystemStatus, type SystemStatus } from "@/api"
import { DefinitionList } from "@/components/DefinitionList"
import { ErrorState } from "@/components/ErrorState"
import { LoadingState } from "@/components/LoadingState"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { useRetryFocus } from "@/lib/retry-focus"
import { cn } from "@/lib/utils"
import { ActiveRunsCard } from "./ActiveRunsCard"
import { QueueCard } from "./QueueCard"
import { StatusCard, ToneBadge } from "./StatusCard"
import { connectionView, STATUS_POLL_MS, systemAlerts } from "./system-status"
import { WorkerCard } from "./WorkerCard"

// The quick actions: the pages' names and URLs.
const QUICK_ACTIONS = [
  { to: "/scans", label: "Scans", icon: ListIcon },
  { to: "/scans/import", label: "Import Scans", icon: FileUpIcon },
  {
    to: "/reconstructions/new",
    label: "New Wire Reconstruction",
    icon: LayersIcon,
  },
  {
    to: "/indexing/new",
    label: "New Indexation",
    icon: ScanSearchIcon,
  },
  { to: "/runs", label: "Run Monitor", icon: ActivityIcon },
] as const

function formatTime(ms: number): string {
  return new Date(ms).toLocaleTimeString([], { hourCycle: "h23" })
}

/**
 * The status page: the database and the API, the worker and its threads, the queue and its next
 * items, quick links, and the queued and running runs, from GET /api/v1/system/status polled
 * every 5 s while the tab is visible. A part of the system that is down gets an alert on top.
 */
export function StatusPage() {
  const status = useGetSystemStatus({
    query: {
      refetchInterval: STATUS_POLL_MS,
      refetchIntervalInBackground: false,
    },
  })
  // TanStack Query clears the error of a query without data each time it fetches again, so while the
  // API keeps failing, every poll would swap the error for the loading skeleton (and the alert, made
  // anew, would be announced again): the last error stays until an answer comes.
  const [lastError, setLastError] = useState<unknown>(null)
  if (status.error !== null && status.error !== lastError) {
    setLastError(status.error)
  }
  const error = status.error ?? (status.data === undefined ? lastError : null)
  // Polls run in the background: the buttons stay usable (and keep the focus) while one runs, and
  // a click then waits for it rather than starting another.
  const refresh = () => void status.refetch({ cancelRefetch: false })
  // A Retry that loads takes its alert away: the heading gets the keyboard.
  const headingRef = useRef<HTMLHeadingElement>(null)
  const markRetry = useRetryFocus(error !== null, status.isFetching, headingRef)

  return (
    <section
      className="flex flex-col gap-4"
      aria-busy={status.isPending && error === null}
    >
      <div className="flex items-center gap-2">
        <h1
          ref={headingRef}
          tabIndex={-1}
          className="text-xl font-semibold outline-none"
        >
          Status
        </h1>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Refresh"
          onClick={refresh}
        >
          <RefreshCwIcon className={cn(status.isFetching && "animate-spin")} />
        </Button>
        {status.dataUpdatedAt > 0 && (
          <span className="text-xs text-muted-foreground">
            Updated {formatTime(status.dataUpdatedAt)}
          </span>
        )}
      </div>

      {error !== null && (
        <ErrorState
          error={error}
          onRetry={() => {
            markRetry()
            refresh()
          }}
          refreshing={status.isFetching}
          stale={status.data !== undefined}
        />
      )}
      {status.data ? (
        <StatusCards status={status.data} />
      ) : (
        error === null && (
          <LoadingState variant="cards" count={4} label="Loading the status" />
        )
      )}
    </section>
  )
}

function StatusCards({ status }: { status: SystemStatus }) {
  return (
    <>
      {systemAlerts(status).map((alert) => (
        <Alert variant="destructive" key={alert.title}>
          <CircleAlertIcon aria-hidden />
          <AlertTitle>{alert.title}</AlertTitle>
          {alert.detail && <AlertDescription>{alert.detail}</AlertDescription>}
        </Alert>
      ))}
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <ConnectionCard status={status} />
        <WorkerCard status={status} />
        <QueueCard status={status} />
        <QuickActionsCard />
      </div>
      <ActiveRunsCard status={status} />
    </>
  )
}

function ConnectionCard({ status }: { status: SystemStatus }) {
  return (
    <StatusCard title="Connection">
      {/* A row per service: its name, then its badge and, below it, its detail. */}
      <DefinitionList
        rows={connectionView(status).map((service) => ({
          label: service.name,
          value: (
            <span className="flex flex-col items-start gap-1">
              <ToneBadge tone={service.tone}>{service.state}</ToneBadge>
              {service.detail && (
                <span
                  className={cn(
                    "text-xs break-all text-muted-foreground",
                    service.name === "Database" && "font-mono"
                  )}
                >
                  {service.detail}
                </span>
              )}
            </span>
          ),
        }))}
      />
    </StatusCard>
  )
}

function QuickActionsCard() {
  return (
    <StatusCard title="Quick Actions">
      <ul className="-mx-2 flex flex-col">
        {QUICK_ACTIONS.map(({ to, label, icon: Icon }) => (
          <li key={to}>
            <Link
              to={to}
              className="flex items-center gap-2 rounded-md px-2 py-1.5 text-link underline-offset-4 outline-none hover:bg-muted hover:underline focus-visible:ring-[3px] focus-visible:ring-ring"
            >
              <Icon className="size-4 shrink-0" aria-hidden />
              {label}
            </Link>
          </li>
        ))}
      </ul>
    </StatusCard>
  )
}
