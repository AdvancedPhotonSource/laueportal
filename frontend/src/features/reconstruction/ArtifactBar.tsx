import { LoaderCircleIcon, RefreshCwIcon } from "lucide-react"

import type { ReconstructionArtifact } from "@/api"
import { Button } from "@/components/ui/button"
import { Switch } from "@/components/ui/switch"
import { RUN_STATUS_LABELS } from "@/lib/run-status"
import { cn } from "@/lib/utils"
import {
  artifactStatus,
  errorStatus,
  isRunActive,
  type ArtifactStatus,
} from "./artifact-bar"

const TONES: Record<ArtifactStatus["tone"], string> = {
  muted: "text-muted-foreground",
  warning: "text-warning",
  destructive: "text-destructive",
}

/**
 * The artifact bar: "Point <id>", "x of y points complete" (or the artifact's state, or why it
 * cannot be read), the run's status while it is active (the page polls then), "Follow newest point"
 * for an open live run, and Refresh. The page holds the queries; the bar only shows them.
 */
export function ArtifactBar({
  point,
  artifact,
  error,
  refreshing,
  onRefresh,
  follow,
}: {
  /** The point shown, or none. */
  point: string | undefined
  artifact: ReconstructionArtifact | undefined
  error: unknown
  refreshing: boolean
  onRefresh: () => void
  /** "Follow newest point": shown when given. */
  follow?: { checked: boolean; onChange: (checked: boolean) => void }
}) {
  const failed = error !== null && error !== undefined
  const status = failed
    ? errorStatus(error)
    : artifact && artifactStatus(artifact)
  const active = !failed && isRunActive(artifact)
  return (
    <div
      role="region"
      aria-label="Reconstruction"
      className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm"
    >
      {point === undefined ? (
        <span className="text-muted-foreground">No point selected</span>
      ) : (
        <span className="font-medium">Point {point}</span>
      )}
      {status && (
        <span
          role="status"
          title={status.hint}
          className={cn(TONES[status.tone], failed && "font-medium")}
        >
          {status.text}
        </span>
      )}
      {active && artifact && (
        <span className="flex items-center gap-1.5 text-muted-foreground">
          <LoaderCircleIcon
            aria-hidden
            className="size-3.5 motion-safe:animate-spin"
          />
          {RUN_STATUS_LABELS[artifact.run_status]}
        </span>
      )}
      {follow && (
        <label
          className="ml-auto flex items-center gap-2"
          title="Select each new completed point"
        >
          <Switch
            checked={follow.checked}
            onCheckedChange={follow.onChange}
            aria-label="Follow newest point"
          />
          Follow newest point
        </label>
      )}
      <Button
        variant="outline"
        size="sm"
        className={cn(!follow && "ml-auto")}
        title="Read the reconstruction again"
        onClick={onRefresh}
      >
        <RefreshCwIcon
          aria-hidden
          className={cn(refreshing && "motion-safe:animate-spin")}
        />
        Refresh
      </Button>
    </div>
  )
}
