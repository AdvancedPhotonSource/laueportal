import { Link } from "@tanstack/react-router"
import {
  CircleXIcon,
  InfoIcon,
  TriangleAlertIcon,
  type LucideIcon,
} from "lucide-react"
import { useId, type ReactNode } from "react"

import type { JobDetail, JobLog, LogTail, RunFile } from "@/api"
import { DefinitionList, type Definition } from "@/components/DefinitionList"
import { ErrorState } from "@/components/ErrorState"
import { LoadingState } from "@/components/LoadingState"
import { Alert, AlertDescription } from "@/components/ui/alert"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { isActiveStatus } from "@/lib/run-status"
import { subheading } from "@/lib/styles"
import {
  artifactLink,
  executionRows,
  isCodeRow,
  fileStateView,
  historyRows,
  kindText,
  logTitle,
  partialResultsNote,
  requestRows,
  runFiles,
  type Row,
} from "./job-page"
import { JobCard, ToneBadge } from "./JobCard"

/** The job's log query, as the page has it. */
export interface LogQuery {
  data: JobLog | undefined
  error: unknown
  isFetching: boolean
  retry: () => void
}

/**
 * Run Configuration: what the run's files say. Problems, the output's validation error and warnings,
 * the partial-results note; the run directory and files with their states (missing on this host, not
 * recorded, unreadable and why); the submitted request; execution and versions; a migrated run's
 * history; the artifacts (linked where a viewer exists); the last 50 log lines, and a remote run's
 * PBS output.
 */
export function RunConfigurationCard({
  job,
  log,
}: {
  job: JobDetail
  log: LogQuery
}) {
  const configuration = job.configuration
  const active = isActiveStatus(job.status)
  const fileNamed = (name: string) =>
    configuration.files.find((file) => file.name === name)
  const kind = kindText(configuration.kind)
  const partial = partialResultsNote(configuration)
  const { request, execution, history } = configuration
  const historical = history ? historyRows(history) : []
  // The note, when the run's files give nothing to show (an ended run).
  const nothingRecorded =
    !active && request === null && execution === null && historical.length === 0
  return (
    <JobCard title="Run Configuration">
      <div className="flex flex-col gap-4">
        {kind && <DefinitionList rows={[{ label: "Kind", value: kind }]} />}
        {(configuration.problems.length > 0 ||
          configuration.validation_error ||
          configuration.warnings.length > 0 ||
          partial) && (
          <div className="flex flex-col gap-2">
            {configuration.problems.map((problem) => (
              <Note key={problem} tone="warning">
                {problem}
              </Note>
            ))}
            {configuration.validation_error && (
              <Note tone="error">
                <strong className="font-medium">
                  Output validation failed:
                </strong>{" "}
                {configuration.validation_error}
              </Note>
            )}
            {configuration.warnings.map((warning) => (
              <Note key={warning} tone="warning">
                {warning}
              </Note>
            ))}
            {partial && <Note tone="info">{partial}</Note>}
          </div>
        )}

        <Section title="Run files">
          <FileTable files={runFiles(configuration)} active={active} />
        </Section>

        {nothingRecorded && (
          <p className="text-muted-foreground">
            No request or run summary recorded.
          </p>
        )}

        <div className="grid gap-4 lg:grid-cols-2">
          <Section title="Submitted request">
            {request ? (
              <DefinitionList rows={definitions(requestRows(request))} />
            ) : (
              <FileStateNote file={fileNamed("request.json")} active={active} />
            )}
          </Section>
          <Section title="Execution and versions">
            {execution ? (
              <DefinitionList rows={definitions(executionRows(execution))} />
            ) : (
              <FileStateNote file={fileNamed("run.json")} active={active} />
            )}
          </Section>
          {historical.length > 0 && (
            <Section title="Historical per-input records">
              <DefinitionList rows={definitions(historical)} />
            </Section>
          )}
        </div>

        <Section title="Artifacts">
          {configuration.artifacts.length === 0 ? (
            <p className="text-muted-foreground">
              {active ? "None yet." : "No artifacts recorded."}
            </p>
          ) : (
            <ul className="flex flex-col gap-1">
              {configuration.artifacts.map((artifact) => {
                const link = artifactLink(artifact, job.run)
                return (
                  <li
                    key={artifact.name}
                    data-artifact={artifact.name}
                    className="flex flex-wrap items-baseline gap-x-2"
                  >
                    <span className="font-medium">{artifact.name}</span>
                    <span className="font-mono text-xs wrap-anywhere">
                      {artifact.path}
                    </span>
                    {link && (
                      <Link
                        {...link}
                        className="text-link underline-offset-4 hover:underline"
                      >
                        View {job.run?.label}
                      </Link>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
        </Section>

        <Logs job={job} log={log} active={active} />
      </div>
    </JobCard>
  )
}

function definitions(rows: readonly Row[]): Definition[] {
  return rows.map((row) => ({
    label: row[0],
    value: row[1],
    mono: isCodeRow(row),
  }))
}

/** A part of the card, a group named by its heading. */
function Section({ title, children }: { title: string; children: ReactNode }) {
  const id = useId()
  return (
    <div
      role="group"
      aria-labelledby={id}
      className="flex min-w-0 flex-col gap-1.5"
    >
      <h3 id={id} className={subheading}>
        {title}
      </h3>
      {children}
    </div>
  )
}

const NOTE_TONES: Record<
  "info" | "warning" | "error",
  { Icon: LucideIcon; className: string; icon: string }
> = {
  info: {
    Icon: InfoIcon,
    className: "border-link/40 bg-link/10",
    icon: "text-link!",
  },
  warning: {
    Icon: TriangleAlertIcon,
    className: "border-warning/50 bg-warning/10",
    icon: "text-warning!",
  },
  error: {
    Icon: CircleXIcon,
    className: "border-destructive/50 bg-destructive/10",
    icon: "text-destructive!",
  },
}

/**
 * A note about the run's files: a tinted box with its tone's icon (`!`: the alert colours its icon
 * with the text's colour otherwise), the text in the foreground colour (for contrast).
 */
function Note({
  tone,
  children,
}: {
  tone: "info" | "warning" | "error"
  children: ReactNode
}) {
  const { Icon, className, icon } = NOTE_TONES[tone]
  return (
    <Alert role="note" data-tone={tone} className={className}>
      <Icon aria-hidden className={icon} />
      <AlertDescription className="text-foreground">
        {children}
      </AlertDescription>
    </Alert>
  )
}

/** The run directory and the files read, with their states. */
function FileTable({
  files,
  active,
}: {
  files: readonly RunFile[]
  active: boolean
}) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="w-36">File</TableHead>
          <TableHead className="w-56">State</TableHead>
          <TableHead>Path</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {files.map((file) => {
          const view = fileStateView(file, active)
          return (
            <TableRow key={file.name} data-file={file.name}>
              <TableCell className="align-top font-medium">
                {file.name}
              </TableCell>
              <TableCell className="align-top whitespace-normal">
                <div className="flex flex-col items-start gap-1">
                  <ToneBadge tone={view.tone}>{view.label}</ToneBadge>
                  {view.detail && (
                    <span className="text-xs text-muted-foreground">
                      {view.detail}
                    </span>
                  )}
                </div>
              </TableCell>
              <TableCell className="align-top font-mono text-xs wrap-anywhere whitespace-normal">
                {file.path ?? (
                  <span className="font-sans text-muted-foreground">
                    No path recorded
                  </span>
                )}
              </TableCell>
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
  )
}

/** Why a file's content is not shown: its state, and what that means. */
function FileStateNote({
  file,
  active,
}: {
  file: RunFile | undefined
  active: boolean
}) {
  if (!file) return <p className="text-muted-foreground">Not read.</p>
  const view = fileStateView(file, active)
  return (
    <div
      className="flex flex-col items-start gap-1"
      data-file-state={file.name}
    >
      <span className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs">{file.name}</span>
        <ToneBadge tone={view.tone}>{view.label}</ToneBadge>
      </span>
      {view.detail && (
        <span className="text-xs text-muted-foreground">{view.detail}</span>
      )}
    </div>
  )
}

/** The run log's last lines, and a remote run's PBS output. */
function Logs({
  job,
  log,
  active,
}: {
  job: JobDetail
  log: LogQuery
  active: boolean
}) {
  if (!log.data) {
    return log.error ? (
      <Section title="Run log">
        <ErrorState
          error={log.error}
          onRetry={log.retry}
          refreshing={log.isFetching}
        />
      </Section>
    ) : (
      <Section title="Run log">
        <LoadingState variant="table" count={2} label="Loading the run log" />
      </Section>
    )
  }
  return (
    <>
      <LogBlock name="Run log" tail={log.data.run_log} active={active} />
      {job.remote && log.data.pbs_output && (
        <LogBlock
          name="PBS output"
          tail={log.data.pbs_output}
          active={active}
        />
      )}
    </>
  )
}

function LogBlock({
  name,
  tail,
  active,
}: {
  name: string
  tail: LogTail
  active: boolean
}) {
  const title = logTitle(name, tail.lines.length, tail.n_lines, tail.truncated)
  return (
    <div
      role="group"
      aria-label={name}
      className="flex min-w-0 flex-col gap-1.5"
    >
      <h3 className={subheading}>{title}</h3>
      {tail.file.state === "available" ? (
        tail.lines.length > 0 ? (
          <pre
            tabIndex={0}
            aria-label={title}
            className="max-h-72 overflow-auto rounded-lg border bg-muted/40 p-2 font-mono text-xs/5 whitespace-pre-wrap"
          >
            {tail.lines.join("\n")}
          </pre>
        ) : (
          <p className="text-muted-foreground">
            The log is empty
            {tail.file.path && (
              <>
                {" "}
                (<span className="font-mono text-xs">{tail.file.path}</span>)
              </>
            )}
            .
          </p>
        )
      ) : (
        <FileStateNote file={tail.file} active={active} />
      )}
    </div>
  )
}
