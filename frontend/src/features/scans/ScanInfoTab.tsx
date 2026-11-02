import { useQueryClient } from "@tanstack/react-query"
import { ClipboardListIcon } from "lucide-react"
import { useEffect, useId, useRef, useState, type SubmitEvent } from "react"
import { toast } from "sonner"

import {
  getGetScanQueryKey,
  useAppendScanNote,
  type ScanCatalog,
  type ScanDetail,
  type ScanDimension,
} from "@/api"
import { DefinitionList, Value } from "@/components/DefinitionList"
import { EmptyState } from "@/components/EmptyState"
import { Button } from "@/components/ui/button"
import { Field, FieldLabel } from "@/components/ui/field"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Textarea } from "@/components/ui/textarea"
import { surface } from "@/lib/styles"
import { toastError } from "@/lib/toast"
import { cn } from "@/lib/utils"
import { dimensionGroups, scanInfoFields } from "./scan-detail"

/**
 * The Scan Info tab: the scan's user, date, dimensions, type, aperture (depth scans), sample and
 * data folder, its per-dimension table, and a note to append to the catalog. Without a catalog
 * entry, a prompt to fill it in.
 */
export function ScanInfoTab({
  scan,
  onFillCatalog,
  onNoteAdded,
  onNoteDirtyChange,
}: {
  scan: ScanDetail
  /** Shows the Catalog tab. */
  onFillCatalog: () => void
  /** A note was appended to the catalog's notes (`catalog`, the answer). */
  onNoteAdded?: (text: string, catalog: ScanCatalog) => void
  /** Whether the note box holds text not added yet (the page's unsaved-changes guard); stable. */
  onNoteDirtyChange?: (dirty: boolean) => void
}) {
  if (!scan.catalog) {
    return (
      <EmptyState
        icon={ClipboardListIcon}
        title="This scan has no catalog entry yet"
      >
        <Button onClick={onFillCatalog}>Fill in the catalog</Button>
      </EmptyState>
    )
  }
  return (
    <div className={cn(surface, "flex flex-col gap-4 p-4")}>
      <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <DefinitionList rows={scanInfoFields(scan)} />
        <DimensionTable dimensions={scan.dimensions} />
      </div>
      <NoteForm
        scanId={scan.scan_number}
        onNoteAdded={onNoteAdded}
        onDirtyChange={onNoteDirtyChange}
      />
    </div>
  )
}

/** The positioner table: a group per dimension ("Dim 2 · Completed 80/81"), a row per positioner. */
function DimensionTable({
  dimensions,
}: {
  dimensions: readonly ScanDimension[]
}) {
  const groups = dimensionGroups(dimensions)
  if (groups.length === 0) {
    return (
      <p className="text-muted-foreground">The scan log lists no dimensions.</p>
    )
  }
  return (
    <Table aria-label="Scan dimensions" className="tabular-nums">
      <TableHeader>
        <TableRow>
          <TableHead>Motor Group</TableHead>
          <TableHead className="text-right">Start</TableHead>
          <TableHead className="text-right">End</TableHead>
          <TableHead className="text-right">Step</TableHead>
        </TableRow>
      </TableHeader>
      {groups.map((group) => (
        <TableBody key={group.key}>
          <TableRow className="bg-muted/50 hover:bg-muted/50">
            <TableHead scope="rowgroup" colSpan={4}>
              {group.label}
              <span className="font-normal text-muted-foreground">
                {" · "}
                {group.completed}
              </span>
            </TableHead>
          </TableRow>
          {group.positioners.map((row) => (
            <TableRow key={row.key}>
              <TableCell title={row.pv}>{row.group}</TableCell>
              {row.text === null ? (
                <>
                  <TableCell className="text-right font-mono">
                    {row.start}
                  </TableCell>
                  <TableCell className="text-right font-mono">
                    {row.end}
                  </TableCell>
                  <TableCell className="text-right font-mono">
                    {row.step}
                  </TableCell>
                </>
              ) : (
                <TableCell colSpan={3} className="text-right font-mono">
                  <Value text={row.text} mono />
                </TableCell>
              )}
            </TableRow>
          ))}
          {group.positioners.length === 0 && (
            <TableRow>
              <TableCell colSpan={4} className="text-muted-foreground">
                No positioner
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      ))}
    </Table>
  )
}

/**
 * A note appended to the catalog's notes, empty to start with. After it is added, the box is empty
 * again and keeps the focus.
 */
function NoteForm({
  scanId,
  onNoteAdded,
  onDirtyChange,
}: {
  scanId: number
  onNoteAdded?: (text: string, catalog: ScanCatalog) => void
  onDirtyChange?: (dirty: boolean) => void
}) {
  const id = useId()
  const textRef = useRef<HTMLTextAreaElement>(null)
  const [text, setText] = useState("")
  const queryClient = useQueryClient()
  const append = useAppendScanNote()
  const blank = text.trim() === ""
  useEffect(() => {
    onDirtyChange?.(!blank)
  }, [blank, onDirtyChange])

  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (blank || append.isPending) return
    append.mutate(
      { scanId, data: { text } },
      {
        onSuccess: (catalog) => {
          // Before the stored notes change: the catalog form keeps the note if Notes has edits.
          onNoteAdded?.(text, catalog)
          queryClient.setQueryData<ScanDetail>(
            getGetScanQueryKey(scanId),
            (old) => old && { ...old, catalog }
          )
          void queryClient.invalidateQueries({
            queryKey: getGetScanQueryKey(scanId),
          })
          setText("")
          textRef.current?.focus()
          toast.success(`Note added to scan ${String(scanId)}`)
        },
        onError: (error) => {
          toastError(error, "The note was not added")
        },
      }
    )
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-2">
      <Field>
        <FieldLabel htmlFor={`${id}-note`}>Note</FieldLabel>
        <Textarea
          id={`${id}-note`}
          ref={textRef}
          value={text}
          onChange={(event) => {
            setText(event.target.value)
          }}
          className="min-h-24"
        />
      </Field>
      <div>
        <Button
          type="submit"
          disabled={blank}
          title="Appends to the catalog's notes, on a new line"
        >
          {append.isPending ? "Adding…" : "Add to DB"}
        </Button>
      </div>
    </form>
  )
}
