import { Link } from "@tanstack/react-router"
import {
  CircleCheckIcon,
  CircleDashedIcon,
  CircleXIcon,
  LoaderCircleIcon,
  TriangleAlertIcon,
  UnplugIcon,
  type LucideIcon,
} from "lucide-react"
import type { ReactNode } from "react"

import { useGetHealth } from "@/api"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import {
  HEALTH_POLL_MS,
  shellHealth,
  type IndicatorView,
  type Tone,
} from "./health-indicators"

const TONE_ICON: Record<Tone, LucideIcon> = {
  ok: CircleCheckIcon,
  warning: TriangleAlertIcon,
  error: CircleXIcon,
  unknown: CircleDashedIcon,
}

const TONE_CLASS: Record<Tone, string> = {
  ok: "text-success",
  warning: "text-warning",
  error: "text-destructive",
  unknown: "text-muted-foreground",
}

function formatTime(ms: number): string {
  return new Date(ms).toLocaleTimeString([], { hourCycle: "h23" })
}

/**
 * The live Worker indicator for the navbar, from GET /api/v1/health polled every 10 s (only while
 * the tab is visible; TanStack Query refetches when it shows again), with a tooltip of the details
 * and a link to the status page. When the API itself does not answer, it says so instead.
 */
export function HealthIndicators() {
  const health = useGetHealth({
    query: {
      refetchInterval: HEALTH_POLL_MS,
      refetchIntervalInBackground: false,
    },
  })
  const view = shellHealth(health)
  const checked = health.dataUpdatedAt
    ? `Checked at ${formatTime(health.dataUpdatedAt)}`
    : undefined

  return (
    <div
      role="group"
      aria-label="Live system status"
      className="flex min-w-0 items-center gap-0.5"
    >
      {view.kind === "checking" && (
        <Indicator
          view={{ tone: "unknown", label: "Worker", state: "checking" }}
          pending
          shrink
        >
          Checking the worker…
        </Indicator>
      )}
      {view.kind === "unreachable" && (
        // The label stays in the foreground colour (red text on the red tint is below 4.5:1 in
        // the light theme); the tint and the icon carry the red.
        <Indicator
          view={{ tone: "error", label: view.label }}
          icon={UnplugIcon}
          shrink
          className="bg-destructive/10 text-foreground hover:bg-destructive/20 hover:text-foreground dark:bg-destructive/20 dark:hover:bg-destructive/30"
        >
          <p className="font-medium">{view.error.title}</p>
          {view.error.detail && <p>{view.error.detail}</p>}
          <p>Checking again every {HEALTH_POLL_MS / 1000} s</p>
          <p className="opacity-70">
            {health.dataUpdatedAt
              ? `Last answer at ${formatTime(health.dataUpdatedAt)}`
              : "No answer yet"}
          </p>
        </Indicator>
      )}
      {view.kind === "live" && (
        <Indicator view={view.worker} shrink>
          <IndicatorDetails view={view.worker} checked={checked} />
        </Indicator>
      )}
    </div>
  )
}

function Indicator({
  view,
  icon,
  pending = false,
  shrink = false,
  className,
  children,
}: {
  view: IndicatorView
  icon?: LucideIcon
  pending?: boolean
  /** On a narrow navbar, the label may shrink to an ellipsis; its name and tooltip keep the full text. */
  shrink?: boolean
  className?: string
  children: ReactNode
}) {
  const Icon = pending ? LoaderCircleIcon : (icon ?? TONE_ICON[view.tone])
  return (
    <Tooltip>
      <TooltipTrigger
        render={<Link to="/" activeOptions={{ exact: true }} />}
        data-tone={view.tone}
        className={cn(
          "inline-flex h-7 items-center gap-1.5 rounded-md px-2 text-xs font-medium whitespace-nowrap text-muted-foreground transition-colors outline-none hover:bg-muted hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring",
          shrink ? "min-w-0" : "shrink-0",
          className
        )}
      >
        <Icon
          aria-hidden
          className={cn(
            "size-3.5 shrink-0",
            TONE_CLASS[view.tone],
            pending && "animate-spin"
          )}
        />
        <span className="truncate">{view.label}</span>
        {view.state && <span className="sr-only">: {view.state}</span>}
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

function IndicatorDetails({
  view,
  checked,
}: {
  view: IndicatorView
  checked: string | undefined
}) {
  return (
    <>
      <p className="font-medium">
        {view.state ? `${view.label}: ${view.state}` : view.label}
      </p>
      {view.note && <p>{view.note}</p>}
      {checked && <p className="opacity-70">{checked}</p>}
    </>
  )
}
