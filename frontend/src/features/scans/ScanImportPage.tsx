import {
  CircleAlertIcon,
  FolderOpenIcon,
  LoaderCircleIcon,
  UploadIcon,
} from "lucide-react"
import { useEffect, useRef, useState, type ChangeEvent } from "react"
import { useForm, useWatch } from "react-hook-form"

import {
  useImportScans,
  useIndexScanLog,
  useListFileRoots,
  type ScanImportReport,
} from "@/api"
import { DataGrid, type DataGridHandle } from "@/components/DataGrid"
import { DefinitionList } from "@/components/DefinitionList"
import { ErrorState } from "@/components/ErrorState"
import { PathPickerDialog } from "@/components/PathPicker"
import { UnsavedChangesGuard } from "@/components/UnsavedChangesGuard"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  PathPickerField,
  SelectField,
  TextField,
  type FormValues,
} from "@/features/program-form"
import { describeError } from "@/lib/errors"
import { formatCount } from "@/lib/format"
import { SCAN_LOG_COLUMNS } from "./columns"
import { ScanCard } from "./ScanCard"
import { QuickSelect } from "./ScanImportQuickSelect"
import {
  applyImportResults,
  CATALOG_DEFAULT_FIELDS,
  CATALOG_DEFAULT_VALUES,
  catalogDefaultsOf,
  failuresOf,
  IMPORT_STATUS_LABELS,
  IMPORT_STATUSES,
  importRowId,
  importRowsOf,
  isImportable,
  scanLogSizeProblem,
  statusCounts,
  summaryText,
  type ImportRow,
} from "./scan-import"

/** The scan log indexed: where it came from, its staged copy's token, and its rows as imported since. */
interface IndexedLog {
  name: string
  token: string
  rows: ImportRow[]
}

/** A new log that was refused: its file's name or path, and why. */
interface Refusal {
  name: string
  message: string
}

/** The selected rows' IDs equal these (the quick select's, until the selection changes another way). */
function sameIds(rows: readonly ImportRow[], ids: ReadonlySet<string>) {
  return (
    rows.length === ids.size && rows.every((row) => ids.has(importRowId(row)))
  )
}

/** The time now, ticking every second while `running`. */
function useNow(running: boolean): number {
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(() => {
      setNow(Date.now())
    }, 1000)
    return () => {
      window.clearInterval(timer)
    }
  }, [running])
  return now
}

/**
 * Import scans: index a scan log from the server or the user's computer, choose its New scans (by
 * hand or quick select), give the catalog's defaults, and import them; the statuses follow the
 * import's results.
 */
export function ScanImportPage() {
  const grid = useRef<DataGridHandle>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const [browsing, setBrowsing] = useState(false)
  const [serverPath, setServerPath] = useState("")
  const [refusal, setRefusal] = useState<Refusal>()
  const [log, setLog] = useState<IndexedLog>()
  const [selected, setSelected] = useState<ImportRow[]>([])
  // The rows the last quick select picked: its count shows while the selection is still those rows.
  const [quickPick, setQuickPick] = useState<ReadonlySet<string>>()
  // Imports that succeeded: each starts Quick Select again (its criteria were for the old statuses).
  const [imports, setImports] = useState(0)
  // The scan-log settings the server publishes (limit, extensions, start folder); until they load, the
  // server's own checks decide.
  const scanLogs = useListFileRoots().data?.scan_logs
  const defaults = useForm<FormValues>({
    defaultValues: CATALOG_DEFAULT_VALUES,
  })
  // The catalog defaults the last import sent (consumed), as text.
  const [consumed, setConsumed] = useState(() =>
    JSON.stringify(CATALOG_DEFAULT_VALUES)
  )
  const typedDefaults = JSON.stringify({
    ...CATALOG_DEFAULT_VALUES,
    ...useWatch({ control: defaults.control }),
  })

  const indexing = useIndexScanLog()
  const importing = useImportScans()
  const now = useNow(importing.isPending)
  const elapsed = Math.max(0, Math.floor((now - importing.submittedAt) / 1000))
  const busy = indexing.isPending || importing.isPending

  // A new log, refused or not, ends the last import's summary.
  const index = (source: { file: File } | { path: string }, name: string) => {
    setRefusal(undefined)
    importing.reset()
    indexing.mutate(
      { data: source },
      {
        onSuccess: (answer) => {
          setLog({ name, token: answer.token, rows: importRowsOf(answer.rows) })
          setQuickPick(undefined)
        },
        onError: (error) => {
          const { title, detail } = describeError(error)
          setRefusal({ name, message: detail ?? title })
        },
      }
    )
  }

  const upload = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ""
    if (!file) return
    const problem =
      scanLogs && scanLogSizeProblem(file.size, scanLogs.max_bytes)
    if (problem) {
      indexing.reset()
      importing.reset()
      setRefusal({ name: file.name, message: problem })
      return
    }
    index({ file }, file.name)
  }

  const importSelected = () => {
    if (!log) return
    // In log order, whatever order they were selected in.
    const rows = selected
      .filter(isImportable)
      .sort((a, b) => a.scan_index - b.scan_index)
    const ids = rows.flatMap((row) =>
      row.scan_number === null ? [] : [row.scan_number]
    )
    if (ids.length === 0) return
    const sent = new Set(rows.map(importRowId))
    const token = log.token
    const sentDefaults = defaults.getValues()
    importing.mutate(
      {
        data: {
          token,
          scan_ids: ids,
          catalog_defaults: catalogDefaultsOf(sentDefaults),
        },
      },
      {
        onSuccess: (report) => {
          setLog((current) =>
            current?.token === token
              ? {
                  ...current,
                  rows: applyImportResults(current.rows, sent, report.results),
                }
              : current
          )
          grid.current?.clearSelection()
          setImports((count) => count + 1)
          setConsumed(
            JSON.stringify({ ...CATALOG_DEFAULT_VALUES, ...sentDefaults })
          )
        },
      }
    )
  }

  const counts = log ? statusCounts(log.rows) : undefined
  // Work an import has not consumed: New rows of the log, a selection, or defaults typed since.
  const unsaved =
    (log?.rows.some(isImportable) ?? false) ||
    selected.length > 0 ||
    typedDefaults !== consumed
  const toImport = selected.filter(isImportable).length

  return (
    <section className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold">Import Scans</h1>
      <UnsavedChangesGuard when={unsaved} />

      <ScanCard
        title="Scan Log"
        actions={
          <>
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => {
                setBrowsing(true)
              }}
            >
              <FolderOpenIcon data-icon="inline-start" aria-hidden />
              Browse Server Logs
            </Button>
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => fileInput.current?.click()}
            >
              <UploadIcon data-icon="inline-start" aria-hidden />
              Upload from Computer
            </Button>
            <input
              ref={fileInput}
              type="file"
              accept={scanLogs?.extensions.join(",")}
              className="hidden"
              data-testid="scan-log-upload"
              onChange={upload}
            />
          </>
        }
        contentClassName="flex flex-col gap-3"
      >
        {indexing.isPending ? (
          <p
            role="status"
            className="flex items-center gap-2 text-muted-foreground"
          >
            <LoaderCircleIcon
              aria-hidden
              className="size-4 motion-safe:animate-spin"
            />
            Indexing…
          </p>
        ) : !log ? (
          <p className="text-muted-foreground">No scan log</p>
        ) : (
          <DefinitionList
            aria-label="Scan log"
            rows={[
              { label: "Log", value: log.name, mono: true },
              {
                label: "Scans",
                value: [
                  formatCount(log.rows.length),
                  ...IMPORT_STATUSES.flatMap((status) => {
                    const count = counts?.get(status)
                    return count
                      ? [
                          `${IMPORT_STATUS_LABELS[status]} ${formatCount(count)}`,
                        ]
                      : []
                  }),
                ].join(" · "),
              },
            ]}
          />
        )}
        {refusal && (
          <Alert variant="destructive" aria-label="Scan log refused">
            <CircleAlertIcon aria-hidden />
            <AlertTitle className="font-mono">{refusal.name}</AlertTitle>
            <AlertDescription>{refusal.message}</AlertDescription>
          </Alert>
        )}
      </ScanCard>

      <PathPickerDialog
        open={browsing}
        onOpenChange={setBrowsing}
        mode="file"
        extensions={scanLogs?.extensions}
        maxFileSize={scanLogs?.max_bytes}
        value={serverPath}
        startPath={scanLogs?.start_folder ?? undefined}
        title="Browse Server Logs"
        onSelect={(path) => {
          setBrowsing(false)
          setServerPath(path)
          index({ path }, path)
        }}
      />

      {log && (
        <>
          <QuickSelect
            // A new log, or an import that succeeded, starts it again (criteria for the old statuses).
            key={`${log.token}:${String(imports)}`}
            rows={log.rows}
            disabled={importing.isPending}
            picked={
              quickPick && sameIds(selected, quickPick)
                ? quickPick.size
                : undefined
            }
            onSelect={(rows) => {
              const ids = rows.map(importRowId)
              setQuickPick(new Set(ids))
              grid.current?.selectRows(ids)
            }}
          />

          <ScanCard
            title="Catalog Defaults"
            contentClassName="grid items-start gap-4 md:grid-cols-3"
          >
            <SelectField
              field={CATALOG_DEFAULT_FIELDS.aperture}
              control={defaults.control}
            />
            <TextField
              field={CATALOG_DEFAULT_FIELDS.sample_name}
              control={defaults.control}
            />
            <TextField
              field={CATALOG_DEFAULT_FIELDS.filename_prefix}
              control={defaults.control}
              mono
            />
            <PathPickerField
              field={CATALOG_DEFAULT_FIELDS.filefolder}
              control={defaults.control}
              mode="directory"
              className="md:col-span-3"
            />
            <TextField
              field={CATALOG_DEFAULT_FIELDS.notes}
              control={defaults.control}
              className="md:col-span-3"
            />
          </ScanCard>

          <ScanCard
            title="Scans"
            actions={
              <>
                {selected.length > 0 && (
                  <>
                    <span className="text-sm text-muted-foreground">
                      {formatCount(selected.length)} selected
                    </span>
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={importing.isPending}
                      onClick={() => grid.current?.clearSelection()}
                    >
                      Clear selection
                    </Button>
                  </>
                )}
                <Button
                  disabled={toImport === 0 || importing.isPending}
                  onClick={importSelected}
                >
                  {importing.isPending && (
                    <LoaderCircleIcon
                      data-icon="inline-start"
                      aria-hidden
                      className="motion-safe:animate-spin"
                    />
                  )}
                  Import Selected ({formatCount(toImport)})
                </Button>
              </>
            }
            contentClassName="flex flex-col gap-3"
          >
            {importing.isPending && (
              <p
                role="status"
                className="flex items-center gap-2 text-muted-foreground"
              >
                <LoaderCircleIcon
                  aria-hidden
                  className="size-4 motion-safe:animate-spin"
                />
                Importing{" "}
                {formatCount(importing.variables.data.scan_ids.length)} scans ·{" "}
                {elapsed} s
              </p>
            )}
            {importing.data && <ImportSummary report={importing.data} />}
            {importing.error && <ErrorState error={importing.error} />}
            <div className="h-[32rem]">
              <DataGrid
                ref={grid}
                gridId="scan-import"
                columns={SCAN_LOG_COLUMNS}
                rows={log.rows}
                listKey={log.token}
                getRowId={importRowId}
                selection="multiRow"
                isRowSelectable={isImportable}
                onSelectionChange={setSelected}
                noun="scans"
                className="h-full"
              />
            </div>
          </ScanCard>
        </>
      )}
    </section>
  )
}

/** The failed scans listed under the summary (each row's badge has its error too). */
const FAILURES_LISTED = 10

/** The import's counts, and the failed scans with their errors. */
function ImportSummary({ report }: { report: ScanImportReport }) {
  const failures = failuresOf(report)
  const more = failures.length - FAILURES_LISTED
  return (
    <Alert role="status" aria-label="Import summary">
      <AlertTitle>{summaryText(report.summary)}</AlertTitle>
      {failures.length > 0 && (
        <AlertDescription>
          <ul className="flex flex-col gap-0.5 text-destructive">
            {failures.slice(0, FAILURES_LISTED).map((item) => (
              <li key={item.scan_number}>
                <span className="tabular-nums">{item.scan_number}</span>:{" "}
                {item.error}
              </li>
            ))}
            {more > 0 && <li>{formatCount(more)} more</li>}
          </ul>
        </AlertDescription>
      )}
    </Alert>
  )
}
