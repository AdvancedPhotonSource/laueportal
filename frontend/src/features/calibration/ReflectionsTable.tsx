// The indexed reflections: each peak's hkl (its harmonic chosen when a measured energy belongs to
// another), position, calculated energy, the measured energy with where it was measured, and after a
// fit its energy and pixel errors. A small fixed table (one row per indexed peak), so shadcn's Table.
import type { CalibrationIndexing, CalibrationResult } from "@/api"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { NumberField } from "@/features/indexing/NumberField"
import { cn } from "@/lib/utils"

import { energyMismatch } from "./calibration"
import {
  useCalibrationDraftStore,
  EMPTY_DRAFT,
  type ListedPeak,
  type MeasurementDraft,
} from "./draft"

/** A value to `digits` decimals ("0.0", never "-0.0"), or a dash. */
const fixed = (value: number | null | undefined, digits: number) => {
  if (value === null || value === undefined) return "—"
  const text = value.toFixed(digits)
  return Number(text) === 0 ? (0).toFixed(digits) : text
}

export function ReflectionsTable({
  frame,
  peaks,
  indexing,
  result,
  reindexing,
}: {
  frame: string
  peaks: readonly ListedPeak[]
  indexing: CalibrationIndexing | undefined
  result: CalibrationResult | undefined
  /** The indexing shown is of an earlier peak list (a new one loads): its rows' peak numbers do not fit `peaks`. */
  reindexing: boolean
}) {
  const draft = useCalibrationDraftStore(
    (state) => state.frames[frame] ?? EMPTY_DRAFT
  )
  const updateFrame = useCalibrationDraftStore((state) => state.updateFrame)
  const rows = indexing?.found ? indexing.rows : []
  const fitted = new Map((result?.rows ?? []).map((row) => [row.peak, row]))

  const setMeasurement = (key: string, change: Partial<MeasurementDraft>) => {
    updateFrame(frame, (current) => {
      const previous = current.measurements[key] ?? {
        energyKev: null,
        source: "",
        order: null,
      }
      return {
        ...current,
        measurements: {
          ...current.measurements,
          [key]: { ...previous, ...change },
        },
      }
    })
  }

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle>Reflections</CardTitle>
      </CardHeader>
      <CardContent>
        {reindexing ? (
          <p role="status" className="text-sm text-muted-foreground">
            Indexing
          </p>
        ) : rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No indexed reflections
          </p>
        ) : (
          <Table aria-label="Reflections" className="text-xs">
            <TableHeader>
              <TableRow>
                <TableHead>hkl</TableHead>
                <TableHead className="text-right">x</TableHead>
                <TableHead className="text-right">y</TableHead>
                <TableHead className="text-right">Calculated (keV)</TableHead>
                <TableHead>Measured (keV)</TableHead>
                <TableHead>Measured in</TableHead>
                <TableHead className="text-right">ΔE (eV)</TableHead>
                <TableHead className="text-right">Δ (px)</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => {
                const peak = peaks[row.peak]
                if (!peak) return null
                const measurement = draft.measurements[peak.key]
                const order = measurement?.order ?? row.order
                const harmonic =
                  row.harmonics.find((h) => h.order === order) ??
                  row.harmonics[0]
                const calculated =
                  harmonic?.energy_kev ?? row.calculated_energy_kev
                const mismatch = energyMismatch(
                  measurement?.energyKev,
                  calculated
                )
                const fit = fitted.get(row.peak)
                const label = `${String(row.h)} ${String(row.k)} ${String(row.l)}`
                const items = Object.fromEntries(
                  row.harmonics.map((h) => [
                    String(h.order),
                    `${String(h.h)} ${String(h.k)} ${String(h.l)}`,
                  ])
                )
                return (
                  <TableRow
                    key={peak.key}
                    data-mismatch={mismatch || undefined}
                  >
                    <TableCell>
                      <Select
                        items={items}
                        value={String(order)}
                        onValueChange={(next) => {
                          const value = Number(next)
                          setMeasurement(peak.key, {
                            order: value === row.order ? null : value,
                          })
                        }}
                      >
                        <SelectTrigger
                          size="sm"
                          aria-label={`Harmonic of ${label}`}
                          className="h-7 w-28 font-mono text-xs"
                          title="The harmonic the measured energy belongs to"
                        >
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent className="w-auto">
                          {row.harmonics.map((h) => (
                            <SelectItem key={h.order} value={String(h.order)}>
                              <span className="font-mono">{`${String(h.h)} ${String(h.k)} ${String(h.l)}`}</span>
                              <span className="ml-2 text-muted-foreground">
                                {fixed(h.energy_kev, 3)} keV
                              </span>
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </TableCell>
                    <TableCell className="text-right font-mono tabular-nums">
                      {row.x.toFixed(1)}
                    </TableCell>
                    <TableCell className="text-right font-mono tabular-nums">
                      {row.y.toFixed(1)}
                    </TableCell>
                    <TableCell className="text-right font-mono tabular-nums">
                      {fixed(calculated, 4)}
                    </TableCell>
                    <TableCell>
                      <NumberField
                        aria-label={`Measured energy of ${label}`}
                        value={measurement?.energyKev ?? null}
                        isValid={(v) => v > 0 && v <= 200}
                        onCommit={(energyKev) => {
                          setMeasurement(peak.key, { energyKev })
                        }}
                        onClear={() => {
                          setMeasurement(peak.key, { energyKev: null })
                        }}
                        className={cn("w-24", mismatch && "border-destructive")}
                        title={
                          mismatch
                            ? "Far from the calculated energy: choose the harmonic it belongs to"
                            : "The measured energy (keV); empty for none"
                        }
                      />
                    </TableCell>
                    <TableCell>
                      <Input
                        aria-label={`Where the energy of ${label} was measured`}
                        value={measurement?.source ?? ""}
                        disabled={measurement?.energyKev == null}
                        placeholder={
                          measurement?.energyKev == null ? "" : "scan"
                        }
                        maxLength={200}
                        onChange={(event) => {
                          setMeasurement(peak.key, {
                            source: event.target.value,
                          })
                        }}
                        className="h-7 w-28 text-xs"
                      />
                    </TableCell>
                    <TableCell className="text-right font-mono tabular-nums">
                      {fixed(fit?.energy_error_ev, 1)}
                    </TableCell>
                    <TableCell className="text-right font-mono tabular-nums">
                      {fixed(fit?.pixel_error, 3)}
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  )
}
