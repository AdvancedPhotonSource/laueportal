import { useQueryClient } from "@tanstack/react-query"
import {
  FileCode2Icon,
  FileQuestionIcon,
  FilesIcon,
  FolderXIcon,
  LoaderCircleIcon,
  ServerOffIcon,
  TriangleAlertIcon,
} from "lucide-react"
import { useEffect, useId, useRef, useState, type ReactNode } from "react"
import { toast } from "sonner"

import {
  ArtifactState,
  ConversionStatus,
  getGetIndexingArtifactQueryKey,
  useConvertIndexingResults,
  useGetIndexingArtifact,
  useSelectIndexingXml,
  XmlSource,
  type IndexingArtifact,
} from "@/api"
import { ErrorState } from "@/components/ErrorState"
import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { failureStreak } from "@/lib/polling"
import { toastError } from "@/lib/toast"
import { cn } from "@/lib/utils"
import {
  artifactRefetchInterval,
  CONVERSION_LOST_TEXT,
  conversionText,
  fileName,
  isConverting,
  reloadResults,
  resultsReplaced,
  XML_SOURCE_TEXT,
} from "./artifact-bar"

/**
 * The artifact bar: nothing while the results file is ready; for an XML, where it came from and
 * "Convert to HDF5" (with the conversion's progress, polled, and the results read again once
 * converted); for several XMLs, the choice (which a chosen XML can change); while the run writes, a
 * status (polled); for a run directory not on this host or not responding, its status; for none, why.
 * An open live run's artifact is polled too, and its results read again when they are replaced. The
 * XML states and none list the API's problems (an unusable results file, a queue that cannot be
 * read).
 */
export function ArtifactBar({ indexingId }: { indexingId: number }) {
  const queryClient = useQueryClient()
  // The artifact's consecutive failed reads, for the back-off.
  const [streak] = useState(failureStreak)
  const artifact = useGetIndexingArtifact(indexingId, {
    query: {
      refetchInterval: (query) =>
        artifactRefetchInterval(
          query.state.data,
          query.state.error,
          streak(query.state)
        ),
    },
  })
  const data = artifact.data

  // Converted (or another XML chosen elsewhere), or a live run's results replaced: every product reads
  // the new results.
  const previous = useRef<IndexingArtifact | undefined>(undefined)
  useEffect(() => {
    const before = previous.current
    previous.current = data
    if (resultsReplaced(before, data)) {
      void reloadResults(queryClient, indexingId)
    } else if (
      before?.state === ArtifactState.xml_only &&
      data?.state === ArtifactState.ready
    ) {
      void reloadResults(queryClient, indexingId)
      toast.success("Converted to HDF5", {
        description: `The viewer now reads ${fileName(data.results_path ?? "output.h5")}.`,
      })
    }
  }, [data, indexingId, queryClient])

  if (artifact.error !== null) {
    return (
      <ErrorState
        error={artifact.error}
        onRetry={() => void artifact.refetch()}
        refreshing={artifact.isFetching}
        stale={data !== undefined}
      />
    )
  }
  if (!data || data.state === ArtifactState.ready) return null
  if (data.state === ArtifactState.xml_only) {
    return <XmlOnlyBar indexingId={indexingId} artifact={data} />
  }
  if (data.state === ArtifactState.writing) {
    return (
      <Bar
        icon={<LoaderCircleIcon className="motion-safe:animate-spin" />}
        tone="info"
      >
        <p className="font-medium">Writing results</p>
      </Bar>
    )
  }
  if (data.state === ArtifactState.not_on_host) {
    return (
      <Bar icon={<FolderXIcon />} tone="warning">
        <p className="font-medium" title="Absent or unreadable on this host">
          Run directory not on this host
        </p>
      </Bar>
    )
  }
  if (data.state === ArtifactState.not_responding) {
    return (
      <Bar icon={<ServerOffIcon />} tone="warning">
        <p className="font-medium" title="Its file system did not answer">
          Run directory not responding
        </p>
      </Bar>
    )
  }
  if (data.state === ArtifactState.ambiguous) {
    return (
      <Bar icon={<FilesIcon />} tone="warning">
        <div className="flex min-w-0 flex-col gap-1">
          <p>This run's folder has several XML files: choose one.</p>
          <Problems problems={data.problems} />
          <XmlChooser
            indexingId={indexingId}
            candidates={data.xml_candidates}
            label="Use this XML"
          />
        </div>
      </Bar>
    )
  }
  return (
    <Bar icon={<FileQuestionIcon />} tone="warning">
      <div className="flex flex-col gap-1">
        <p className="font-medium">No results for this run.</p>
        <Problems
          problems={data.problems}
          fallback="No results file or XML was found in the run's folder."
        />
      </div>
    </Bar>
  )
}

function XmlOnlyBar({
  indexingId,
  artifact,
}: {
  indexingId: number
  artifact: IndexingArtifact
}) {
  const queryClient = useQueryClient()
  const convert = useConvertIndexingResults({
    mutation: {
      onSuccess: (job) => {
        // Show the job at once; the artifact's polling follows it.
        queryClient.setQueryData<IndexingArtifact>(
          getGetIndexingArtifactQueryKey(indexingId),
          (current) =>
            current && {
              ...current,
              conversion: job,
              conversion_job_id: job.id,
            }
        )
        void queryClient.invalidateQueries({
          queryKey: getGetIndexingArtifactQueryKey(indexingId),
        })
      },
      onError: (error) => {
        toastError(error, "The conversion did not start")
      },
    },
  })
  const conversion = artifact.conversion
  const converting = isConverting(artifact) || convert.isPending
  const failed = conversion?.status === ConversionStatus.failed
  const ended = failed || conversion?.status === ConversionStatus.finished
  // Polling follows a queued or running conversion. When the artifact then reports none, polling stops:
  // say so, and offer to check again.
  const [following, setFollowing] = useState(isConverting(artifact))
  if (
    isConverting(artifact) !== following &&
    (isConverting(artifact) || conversion !== null)
  ) {
    setFollowing(isConverting(artifact))
  }
  const lost = following && conversion === null
  const [changing, setChanging] = useState(false)
  const canChange =
    artifact.xml_source === XmlSource.chosen &&
    artifact.xml_candidates.length > 1

  return (
    <Bar icon={<FileCode2Icon />} tone="info">
      <div className="flex min-w-0 flex-col gap-1">
        <p>
          <span className="font-medium">XML only:</span> reading{" "}
          <code className="font-mono text-xs break-all">
            {artifact.xml_path ? fileName(artifact.xml_path) : "the XML"}
          </code>
          {artifact.xml_source && <>, {XML_SOURCE_TEXT[artifact.xml_source]}</>}
          .
        </p>
        {conversion && (
          <p
            role="status"
            className={cn(
              "flex items-center gap-1.5",
              failed ? "text-destructive" : "text-muted-foreground"
            )}
          >
            {isConverting(artifact) && (
              <LoaderCircleIcon className="size-3.5 animate-spin" aria-hidden />
            )}
            {ended && <TriangleAlertIcon className="size-3.5" aria-hidden />}
            {conversionText(conversion)}
          </p>
        )}
        {lost && (
          <p
            role="status"
            className="flex items-center gap-1.5 text-destructive"
          >
            <TriangleAlertIcon className="size-3.5" aria-hidden />
            {CONVERSION_LOST_TEXT}
          </p>
        )}
        <Problems problems={artifact.problems} />
        {changing && (
          <XmlChooser
            indexingId={indexingId}
            candidates={artifact.xml_candidates}
            current={
              artifact.xml_path ? fileName(artifact.xml_path) : undefined
            }
            label="Use this XML"
            onChosen={() => {
              setChanging(false)
            }}
            onCancel={() => {
              setChanging(false)
            }}
          />
        )}
      </div>
      <div className="ml-auto flex shrink-0 gap-2">
        {lost && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              void queryClient.invalidateQueries({
                queryKey: getGetIndexingArtifactQueryKey(indexingId),
              })
            }}
          >
            Check again
          </Button>
        )}
        {canChange && !changing && (
          <Button
            variant="outline"
            size="sm"
            disabled={converting}
            onClick={() => {
              setChanging(true)
            }}
          >
            Change XML
          </Button>
        )}
        <Button
          size="sm"
          disabled={converting}
          title="HDF5 results make the viewer faster"
          onClick={() => {
            convert.mutate({ indexingId })
          }}
        >
          {converting && (
            <LoaderCircleIcon className="animate-spin" aria-hidden />
          )}
          {ended ? "Convert to HDF5 again" : "Convert to HDF5"}
        </Button>
      </div>
    </Bar>
  )
}

/**
 * The run folder's XML files to choose from, and the button that records the choice; `onCancel`
 * adds a Cancel (changing a chosen XML).
 */
function XmlChooser({
  indexingId,
  candidates,
  current,
  label,
  onChosen,
  onCancel,
}: {
  indexingId: number
  candidates: string[]
  current?: string
  label: string
  onChosen?: () => void
  onCancel?: () => void
}) {
  const id = useId()
  const queryClient = useQueryClient()
  const [name, setName] = useState<string | null>(current ?? null)
  const select = useSelectIndexingXml({
    mutation: {
      onSuccess: (artifact) => {
        queryClient.setQueryData(
          getGetIndexingArtifactQueryKey(indexingId),
          artifact
        )
        void reloadResults(queryClient, indexingId)
        onChosen?.()
      },
      onError: (error) => {
        toastError(error, "The XML was not chosen")
      },
    },
  })
  return (
    <div className="flex flex-wrap items-center gap-2">
      <label htmlFor={id} className="sr-only">
        XML file
      </label>
      <Select
        items={Object.fromEntries(candidates.map((file) => [file, file]))}
        value={name}
        onValueChange={(value) => {
          setName(value)
        }}
      >
        <SelectTrigger id={id} size="sm" className="min-w-56">
          <SelectValue placeholder="Choose an XML file" />
        </SelectTrigger>
        <SelectContent>
          {candidates.map((file) => (
            <SelectItem key={file} value={file}>
              {file}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Button
        size="sm"
        disabled={name === null || name === current || select.isPending}
        onClick={() => {
          if (name !== null) select.mutate({ indexingId, data: { name } })
        }}
      >
        {select.isPending && (
          <LoaderCircleIcon className="animate-spin" aria-hidden />
        )}
        {label}
      </Button>
      {onCancel && (
        <Button
          variant="ghost"
          size="sm"
          disabled={select.isPending}
          onClick={onCancel}
        >
          Cancel
        </Button>
      )}
    </div>
  )
}

/** The API's notes on the artifact; `fallback` (or nothing) when there are none. */
function Problems({
  problems,
  fallback,
}: {
  problems: string[]
  fallback?: string
}) {
  if (problems.length === 0) {
    return fallback ? <p className="text-muted-foreground">{fallback}</p> : null
  }
  return (
    <ul className="list-disc pl-5 text-muted-foreground">
      {problems.map((problem) => (
        <li key={problem}>{problem}</li>
      ))}
    </ul>
  )
}

function Bar({
  icon,
  tone,
  children,
}: {
  icon: ReactNode
  tone: "info" | "warning"
  children: ReactNode
}) {
  return (
    <div
      role="region"
      aria-label="Results file"
      className={cn(
        "flex flex-wrap items-start gap-x-3 gap-y-2 rounded-lg border px-3 py-2 text-sm",
        tone === "warning" ? "border-warning/50 bg-warning/5" : "bg-muted/40"
      )}
    >
      <span className="mt-0.5 text-muted-foreground [&>svg]:size-4" aria-hidden>
        {icon}
      </span>
      {children}
    </div>
  )
}
