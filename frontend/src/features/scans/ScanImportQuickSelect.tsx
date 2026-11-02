import { useId, useState } from "react"

import { Button } from "@/components/ui/button"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { formatCount } from "@/lib/format"
import { ScanCard } from "./ScanCard"
import {
  localInstant,
  quickSelect,
  scanIdThreshold,
  type ImportRow,
} from "./scan-import"

/**
 * The quick select: the New rows with a scan ID greater than N, or created at or after a local date
 * and time; each Select replaces the selection.
 */
export function QuickSelect({
  rows,
  disabled,
  picked,
  onSelect,
}: {
  rows: readonly ImportRow[]
  disabled: boolean
  /** The rows the last Select picked, while they are still the selection (else undefined). */
  picked: number | undefined
  onSelect: (rows: ImportRow[]) => void
}) {
  const id = useId()
  const [scanId, setScanId] = useState("")
  const [after, setAfter] = useState("")
  const threshold = scanIdThreshold(scanId)
  const instant = localInstant(after)

  return (
    <ScanCard
      title="Quick Select"
      contentClassName="flex flex-wrap items-end gap-x-6 gap-y-3"
    >
      <form
        className="flex items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          if (threshold !== undefined) {
            onSelect(quickSelect(rows, { scanIdGreaterThan: threshold }))
          }
        }}
      >
        <Field className="w-48 gap-1.5">
          <FieldLabel
            htmlFor={`${id}-scan`}
            title="Select the New scans whose scan ID is greater than this."
          >
            Scan ID greater than
          </FieldLabel>
          <Input
            id={`${id}-scan`}
            inputMode="numeric"
            autoComplete="off"
            value={scanId}
            onChange={(event) => {
              setScanId(event.target.value)
            }}
          />
        </Field>
        <Button
          type="submit"
          variant="outline"
          disabled={disabled || threshold === undefined}
          aria-label="Select by scan ID"
        >
          Select
        </Button>
      </form>
      <form
        className="flex items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          if (instant !== undefined) {
            onSelect(quickSelect(rows, { createdAtOrAfter: instant }))
          }
        }}
      >
        <Field className="w-60 gap-1.5">
          <FieldLabel
            htmlFor={`${id}-time`}
            title="Select the New scans started at or after this local date and time."
          >
            Created at or after
          </FieldLabel>
          <Input
            id={`${id}-time`}
            type="datetime-local"
            step={1}
            value={after}
            onChange={(event) => {
              setAfter(event.target.value)
            }}
          />
        </Field>
        <Button
          type="submit"
          variant="outline"
          disabled={disabled || instant === undefined}
          aria-label="Select by date"
        >
          Select
        </Button>
      </form>
      {picked !== undefined && (
        <p role="status" className="pb-2 text-sm text-muted-foreground">
          {picked === 0
            ? "No New scans match"
            : `${formatCount(picked)} New ${picked === 1 ? "scan" : "scans"} selected`}
        </p>
      )}
    </ScanCard>
  )
}
