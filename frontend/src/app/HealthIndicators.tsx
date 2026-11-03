import { Link } from "@tanstack/react-router"
import type { ReactNode } from "react"

import { useGetHealth } from "@/api"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import {
  getHealthWithin,
  HEALTH_POLL_MS,
  retryHealth,
  shellHealth,
  type DotView,
  type IndicatorView,
  type PillView,
} from "./health-indicators"

/** The dot alone: filled in the quiet success colour while the worker runs, a muted ring while checking. */
const DOT_TONE: Record<DotView["tone"], string> = {
  ok: "bg-success-quiet",
  unknown: "border-[1.5px] border-muted-foreground",
}

// The tint and the dot carry the tone; the words stay in the foreground colour (red text on the red tint is
// below 4.5:1 in the light theme).
const PILL_TONE: Record<PillView["tone"], { pill: string; dot: string }> = {
  warning: {
    pill: "border-warning/35 bg-warning/15 hover:bg-warning/25",
    dot: "bg-warning",
  },
  error: {
    pill: "border-destructive/35 bg-destructive/15 hover:bg-destructive/25",
    dot: "bg-destructive",
  },
}

const DOT = "size-[7px] shrink-0 rounded-full"

function formatTime(ms: number): string {
  return new Date(ms).toLocaleTimeString([], { hourCycle: "h23" })
}

/**
 * The live Worker indicator for the navbar, from GET /api/v1/health polled every 10 s (only while
 * the tab is visible; TanStack Query refetches when it shows again), with a tooltip of the details
 * and a link to the status page. When the API itself does not answer, within a poll interval, it
 * says so instead.
 */
export function HealthIndicators() {
  const health = useGetHealth({
    query: {
      queryFn: ({ signal }) => getHealthWithin(signal),
      retry: retryHealth,
      refetchInterval: HEALTH_POLL_MS,
      refetchIntervalInBackground: false,
    },
  })
  const shell = shellHealth(health)

  return (
    <div
      role="group"
      aria-label="Live system status"
      className="flex min-w-0 items-center"
    >
      <Indicator view={shell.view}>
        {shell.kind === "checking" && <p>Checking the worker…</p>}
        {shell.kind === "unreachable" && (
          <>
            <p className="font-medium">{shell.error.title}</p>
            {shell.error.detail && <p>{shell.error.detail}</p>}
            <p>Checking again every {HEALTH_POLL_MS / 1000} s</p>
            <p className="opacity-70">
              {health.dataUpdatedAt
                ? `Last answer at ${formatTime(health.dataUpdatedAt)}`
                : "No answer yet"}
            </p>
          </>
        )}
        {shell.kind === "live" && (
          <>
            <p className="font-medium">{shell.view.state}</p>
            {shell.view.form === "pill" && shell.view.note && (
              <p>{shell.view.note}</p>
            )}
            {health.dataUpdatedAt > 0 && (
              <p className="opacity-70">
                Checked at {formatTime(health.dataUpdatedAt)}
              </p>
            )}
          </>
        )}
      </Indicator>
    </div>
  )
}

/**
 * The link to the status page: a dot alone while all is well (its name says the state), a pill with
 * the words otherwise. On a narrow navbar the pill's subject ("Worker", "API") goes before its
 * state's words do; its name and tooltip keep the full text.
 */
function Indicator({
  view,
  children,
}: {
  view: IndicatorView
  children: ReactNode
}) {
  const link =
    "inline-flex h-7 min-w-0 items-center rounded-md text-xs font-medium whitespace-nowrap text-foreground outline-none"
  return (
    <Tooltip>
      <TooltipTrigger
        render={<Link to="/" activeOptions={{ exact: true }} />}
        data-tone={view.tone}
        className={
          view.form === "dot"
            ? // The dot is small, its link is not: as wide as a link with a label (w-21), shrinking to the
              // chip that shows hover and focus.
              cn(link, "group w-21 min-w-7")
            : cn(
                link,
                "gap-1.5 border px-2.5 transition-colors focus-visible:ring-3 focus-visible:ring-ring",
                PILL_TONE[view.tone].pill
              )
        }
      >
        {view.form === "dot" ? (
          <>
            <span className="inline-flex size-7 shrink-0 items-center justify-center rounded-md transition-colors group-hover:bg-muted group-focus-visible:ring-3 group-focus-visible:ring-ring">
              <span
                data-slot="status-dot"
                aria-hidden
                className={cn(DOT, DOT_TONE[view.tone])}
              />
            </span>
            <span className="sr-only">{view.state}</span>
          </>
        ) : (
          <>
            <span
              data-slot="status-dot"
              aria-hidden
              className={cn(DOT, PILL_TONE[view.tone].dot)}
            />
            {/* One line, one line high: the state's words first in layout (order-first), then the subject,
                the row reversed so the subject still reads first. When both do not fit, the subject wraps
                to the hidden second line: it is whole or gone, never cut to a letter ("A unreachable").
                Narrower than 768 px, the words alone shorten to an ellipsis. The space between them is a
                text node for the name: a flex container does not draw it, the gap does. */}
            <span className="flex h-lh min-w-0 flex-row-reverse flex-wrap content-start justify-end gap-x-1 overflow-hidden">
              <span data-slot="status-subject">{view.subject}</span>{" "}
              <span
                data-slot="status-words"
                className="order-first min-w-0 truncate"
              >
                {view.words}
              </span>
            </span>
          </>
        )}
      </TooltipTrigger>
      <TooltipContent
        side="bottom"
        align="start"
        className="flex-col items-start gap-1 text-left"
      >
        {children}
      </TooltipContent>
    </Tooltip>
  )
}
