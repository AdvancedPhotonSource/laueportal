// The Calibration page's cards: the inputs, the peaks, the indexing, the fit and the saved file.
import { useQueryClient, type UseQueryResult } from "@tanstack/react-query"
import { useState } from "react"

import {
  getGetCalibrationSetupQueryKey,
  useSaveCalibration,
  type CalibrationIndexing,
  type CalibrationIndexSettings,
  type CalibrationInputsInfo,
  type CalibrationPeakSettings,
  type CalibrationPeaks,
  type CalibrationRefineRequest,
  type CalibrationResult,
  type CalibrationSurface,
  type GeometryFolder,
  type useRefineCalibration,
} from "@/api"
import { DefinitionList, type Definition } from "@/components/DefinitionList"
import { ErrorState } from "@/components/ErrorState"
import { PathField } from "@/components/PathPicker"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardAction,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { NumberField } from "@/features/indexing/NumberField"
import {
  DETECTOR_LABEL_RANGE,
  isDetectorLabelSize,
} from "@/features/indexing/viewer-slices"
import { subheading } from "@/lib/styles"
import { toastError } from "@/lib/toast"
import { cn } from "@/lib/utils"

import { surfaceWarns, vector } from "./calibration"
import {
  useCalibrationDraftStore,
  type FrameDraft,
  type ListedPeak,
} from "./draft"

export type PeakTool = "add" | "remove" | null

const PRESSED =
  "aria-pressed:bg-primary aria-pressed:text-primary-foreground aria-pressed:hover:bg-primary aria-pressed:hover:text-primary-foreground"

const FOLDER_STATES: Record<GeometryFolder["state"], string> = {
  ok: "",
  not_set: "Not set",
  outside_roots: "Outside the file roots",
  not_found: "Not found",
  not_writable: "Not writable",
  not_responding: "Not responding",
}

/** A file name Save accepts (the API's pattern). */
const GEOMETRY_FILE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*\.xml$/

const deg = (value: number | null | undefined, digits = 2) =>
  value === null || value === undefined ? "—" : `${value.toFixed(digits)}°`

function hkl(s: Pick<CalibrationSurface, "h" | "k" | "l">) {
  return `${String(s.h)} ${String(s.k)} ${String(s.l)}`
}

// ---------------------------------------------------------------------------------------------
// Inputs

export function InputsCard({
  files,
  folder,
  info,
  error,
  loading,
  sampleFromDetector0,
}: {
  files: { frame: string; geometry: string; crystal: string }
  folder: GeometryFolder
  info: CalibrationInputsInfo | undefined
  error: unknown
  loading: boolean
  sampleFromDetector0: boolean
}) {
  const setFiles = useCalibrationDraftStore((state) => state.setFiles)
  const rows: Definition[] = info
    ? [
        {
          label: "Detector",
          value: `${info.detector.detector_id} (detector ${String(info.detector.slot)})`,
        },
        {
          label: "Frame",
          value: `${String(info.width)} × ${String(info.height)}`,
        },
        {
          label: "Crystal",
          value: `${info.crystal_name} (space group ${String(info.space_group)})`,
        },
        {
          label: "Sample rotation",
          value: info.fit_sample_rotation
            ? "Fitted"
            : sampleFromDetector0
              ? "Fixed: detector 0 calibration"
              : "Fixed: this frame's indexing",
        },
      ]
    : []
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle>Inputs</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <FileRow
          id="calibration-frame"
          label="Frame"
          value={files.frame}
          onChange={(frame) => {
            setFiles({ frame })
          }}
          dialogTitle="Choose the calibration frame"
        />
        <FileRow
          id="calibration-geometry"
          label="Starting geometry"
          value={files.geometry}
          onChange={(geometry) => {
            setFiles({ geometry })
          }}
          extensions={[".xml"]}
          startPath={folder.path ?? undefined}
          dialogTitle="Choose the starting geometry file"
        />
        <FileRow
          id="calibration-crystal"
          label="Crystal"
          value={files.crystal}
          onChange={(crystal) => {
            setFiles({ crystal })
          }}
          extensions={[".xml"]}
          dialogTitle="Choose the calibration crystal's file"
        />
        {error ? (
          <ErrorState error={error} />
        ) : info ? (
          <DefinitionList rows={rows} aria-label="Inputs" />
        ) : loading ? (
          <p className="text-sm text-muted-foreground">Checking</p>
        ) : null}
      </CardContent>
    </Card>
  )
}

function FileRow({
  id,
  label,
  value,
  onChange,
  extensions,
  startPath,
  dialogTitle,
  mode = "file",
}: {
  id: string
  label: string
  value: string
  onChange: (value: string) => void
  extensions?: readonly string[]
  startPath?: string
  dialogTitle: string
  mode?: "file" | "directory"
}) {
  // The typed path applies on leaving the field (each change asks the server).
  const [draft, setDraft] = useState(value)
  const [shown, setShown] = useState(value)
  if (shown !== value) {
    setShown(value)
    setDraft(value)
  }
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      <PathField
        id={id}
        value={draft}
        onChange={(next) => {
          setDraft(next)
        }}
        onBlur={() => {
          if (draft !== value) onChange(draft)
        }}
        mode={mode}
        extensions={extensions}
        startPath={startPath}
        dialogTitle={dialogTitle}
        fromBrowse={(path) => {
          // A path chosen in the dialog applies at once (a typed one on leaving the field).
          onChange(path)
          return path
        }}
      />
    </div>
  )
}

// ---------------------------------------------------------------------------------------------
// Peaks

export function PeaksCard({
  frame,
  peaks,
  listed,
  draft,
  settings,
  tool,
  onToolChange,
  labels,
  labelSize,
}: {
  frame: string
  peaks: UseQueryResult<CalibrationPeaks>
  listed: readonly ListedPeak[]
  draft: FrameDraft
  settings: CalibrationPeakSettings | null
  tool: PeakTool
  onToolChange: (tool: PeakTool) => void
  labels: boolean
  labelSize: number
}) {
  const updateFrame = useCalibrationDraftStore((state) => state.updateFrame)
  const setLabels = useCalibrationDraftStore((state) => state.setLabels)
  const found = listed.filter((peak) => peak.origin === "found").length
  const setSettings = (change: Partial<CalibrationPeakSettings>) => {
    if (!settings) return
    updateFrame(frame, (current) => ({
      ...current,
      peakSettings: { ...settings, ...change },
    }))
  }
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle>Peaks</CardTitle>
        <CardAction>
          <Button
            size="xs"
            variant="ghost"
            disabled={
              !frame || (draft.added.length === 0 && draft.removed.length === 0)
            }
            onClick={() => {
              updateFrame(frame, (current) => ({
                ...current,
                added: [],
                removed: [],
              }))
            }}
            title="Drop the peaks added and restore those removed"
          >
            Reset peaks
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {peaks.isError ? (
          <ErrorState
            error={peaks.error}
            onRetry={() => void peaks.refetch()}
          />
        ) : (
          <DefinitionList
            aria-label="Peak counts"
            rows={[
              { label: "Found", value: peaks.data ? String(found) : "—" },
              { label: "Added", value: String(draft.added.length) },
              { label: "Removed", value: String(draft.removed.length) },
              {
                label: "Threshold",
                value: peaks.data
                  ? `${peaks.data.threshold.toFixed(0)} counts`
                  : "—",
              },
            ]}
          />
        )}
        <div className="flex flex-wrap items-center gap-3">
          <ToggleGroup
            aria-label="Click on the frame"
            variant="outline"
            size="sm"
            spacing={0}
            value={tool ? [tool] : []}
            onValueChange={(value) => {
              const next = value[0]
              onToolChange(next === "add" || next === "remove" ? next : null)
            }}
          >
            <ToggleGroupItem
              value="add"
              disabled={!frame}
              className={cn("h-7 px-2 text-xs", PRESSED)}
              title="Click on the frame to fit and add a peak"
            >
              Add peak
            </ToggleGroupItem>
            <ToggleGroupItem
              value="remove"
              disabled={!frame}
              className={cn("h-7 px-2 text-xs", PRESSED)}
              title="Click on a peak to remove it"
            >
              Remove peak
            </ToggleGroupItem>
          </ToggleGroup>
          <div className="flex items-center gap-2">
            <Switch
              id="calibration-labels"
              size="sm"
              checked={labels}
              onCheckedChange={(showLabels) => {
                setLabels({ showLabels })
              }}
            />
            <Label htmlFor="calibration-labels" className="text-xs">
              hkl labels
            </Label>
            <NumberField
              aria-label="Label size"
              inputMode="numeric"
              value={labelSize}
              disabled={!labels}
              isValid={isDetectorLabelSize}
              onCommit={(next) => {
                setLabels({ labelSize: next })
              }}
              title={`Label size: ${String(DETECTOR_LABEL_RANGE[0])} to ${String(DETECTOR_LABEL_RANGE[1])} px`}
              className="w-12"
            />
            <span className="text-xs text-muted-foreground">px</span>
          </div>
        </div>
        {settings && (
          <details className="text-sm">
            <summary className={cn(subheading, "cursor-pointer")}>
              Peak search
            </summary>
            <div className="mt-2 grid grid-cols-2 gap-2">
              <Setting label="Threshold (counts)">
                <NumberField
                  aria-label="Threshold"
                  value={settings.threshold ?? null}
                  placeholder="automatic"
                  isValid={(v) => v >= 0}
                  onCommit={(threshold) => {
                    setSettings({ threshold })
                  }}
                />
              </Setting>
              <Setting label="Box size (px)">
                <NumberField
                  aria-label="Box size"
                  value={settings.box_size}
                  isValid={(v) => Number.isInteger(v) && v > 0}
                  onCommit={(box_size) => {
                    setSettings({ box_size })
                  }}
                />
              </Setting>
              <Setting label="Min size (px)">
                <NumberField
                  aria-label="Min size"
                  value={settings.min_size}
                  isValid={(v) => v > 0}
                  onCommit={(min_size) => {
                    setSettings({ min_size })
                  }}
                />
              </Setting>
              <Setting label="Min separation (px)">
                <NumberField
                  aria-label="Min separation"
                  value={settings.min_separation}
                  isValid={(v) => Number.isInteger(v) && v >= 0}
                  onCommit={(min_separation) => {
                    setSettings({ min_separation })
                  }}
                />
              </Setting>
              <Setting label="Max peaks">
                <NumberField
                  aria-label="Max peaks"
                  value={settings.max_number ?? null}
                  isValid={(v) => Number.isInteger(v) && v > 0 && v <= 1000}
                  onCommit={(max_number) => {
                    setSettings({ max_number })
                  }}
                />
              </Setting>
              <Setting label="Max R-factor">
                <NumberField
                  aria-label="Max R-factor"
                  value={settings.max_rfactor}
                  isValid={(v) => v >= 0 && v <= 1}
                  onCommit={(max_rfactor) => {
                    setSettings({ max_rfactor })
                  }}
                />
              </Setting>
            </div>
          </details>
        )}
      </CardContent>
    </Card>
  )
}

function Setting({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <label className="flex flex-col gap-1 text-xs text-muted-foreground">
      {label}
      {children}
    </label>
  )
}

// ---------------------------------------------------------------------------------------------
// Index

export function IndexCard({
  frame,
  indexing,
  draft,
  settings,
  warningDeg,
}: {
  frame: string
  indexing: UseQueryResult<CalibrationIndexing>
  draft: FrameDraft
  settings: CalibrationIndexSettings | null
  warningDeg: number
}) {
  const updateFrame = useCalibrationDraftStore((state) => state.updateFrame)
  const data = indexing.data
  const setSettings = (change: Partial<CalibrationIndexSettings>) => {
    if (!settings) return
    updateFrame(frame, (current) => ({
      ...current,
      indexSettings: { ...settings, ...change },
    }))
  }
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle>Index</CardTitle>
        <CardAction>
          {draft.start && (
            <Button
              size="xs"
              variant="ghost"
              onClick={() => {
                updateFrame(frame, (current) => ({ ...current, start: null }))
              }}
              title="Index with the starting geometry file's detector again"
            >
              Use geometry file
            </Button>
          )}
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {indexing.isError ? (
          <ErrorState
            error={indexing.error}
            onRetry={() => void indexing.refetch()}
          />
        ) : !data ? (
          <p className="text-sm text-muted-foreground">
            {indexing.isFetching ? "Indexing" : "Not indexed"}
          </p>
        ) : !data.found ? (
          <p className="text-sm text-muted-foreground">No pattern found</p>
        ) : (
          <>
            <DefinitionList
              aria-label="Indexing"
              rows={[
                {
                  label: "Starting detector",
                  value: draft.start
                    ? "Refined (Match Again)"
                    : "Geometry file",
                },
                {
                  label: "Indexed",
                  value: `${String(data.n_indexed)} of ${String(data.n_peaks)} peaks`,
                },
                { label: "RMS error", value: deg(data.rms_error_deg, 3) },
                { label: "Patterns", value: String(data.n_patterns) },
              ]}
            />
            {data.surface && (
              <SurfaceCheck surface={data.surface} warningDeg={warningDeg} />
            )}
          </>
        )}
        {settings && (
          <details className="text-sm">
            <summary className={cn(subheading, "cursor-pointer")}>
              Indexing
            </summary>
            <div className="mt-2 grid grid-cols-2 gap-2">
              <Setting label="Max energy, search (keV)">
                <NumberField
                  aria-label="Max energy for searching"
                  value={settings.index_kev_max_calc}
                  isValid={(v) => v > 0 && v <= 200}
                  onCommit={(index_kev_max_calc) => {
                    setSettings({ index_kev_max_calc })
                  }}
                />
              </Setting>
              <Setting label="Max energy, match (keV)">
                <NumberField
                  aria-label="Max energy for matching"
                  value={settings.index_kev_max_test}
                  isValid={(v) => v > 0 && v <= 200}
                  onCommit={(index_kev_max_test) => {
                    setSettings({ index_kev_max_test })
                  }}
                />
              </Setting>
              <Setting label="Angle tolerance (°)">
                <NumberField
                  aria-label="Angle tolerance"
                  value={settings.index_angle_tolerance}
                  isValid={(v) => v > 0 && v <= 10}
                  onCommit={(index_angle_tolerance) => {
                    setSettings({ index_angle_tolerance })
                  }}
                />
              </Setting>
              <Setting label="Cone (°)">
                <NumberField
                  aria-label="Cone"
                  value={settings.index_cone}
                  isValid={(v) => v > 0 && v <= 180}
                  onCommit={(index_cone) => {
                    setSettings({ index_cone })
                  }}
                />
              </Setting>
            </div>
          </details>
        )}
      </CardContent>
    </Card>
  )
}

/** Where the {001} reflection points: its angle to the expected surface normal, and whether it lies between the beams. */
function SurfaceCheck({
  surface,
  warningDeg,
}: {
  surface: CalibrationSurface
  warningDeg: number
}) {
  const warns = surfaceWarns(surface, warningDeg)
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-2">
        <span className={subheading}>Surface reflection</span>
        <Badge
          variant={warns ? "destructive" : "secondary"}
          title={`The {001} reflection nearest the expected surface normal: more than ${String(warningDeg)}° from it, or not between the incident and exit beams, suggests a wrong orientation or geometry`}
        >
          {warns ? "Check orientation" : "As expected"}
        </Badge>
      </div>
      <DefinitionList
        aria-label="Surface reflection"
        rows={[
          { label: "hkl", value: hkl(surface) },
          {
            label: "To surface normal",
            value: deg(surface.angle_to_expected_deg),
          },
          {
            label: "To incident beam",
            value: deg(surface.angle_to_incident_deg),
          },
          { label: "To exit beam", value: deg(surface.angle_to_exit_deg) },
          { label: "Between the beams", value: surface.between ? "Yes" : "No" },
        ]}
      />
    </div>
  )
}

// ---------------------------------------------------------------------------------------------
// Refine

const STATUS_LABELS: Record<CalibrationResult["status"], string> = {
  ok: "Refined",
  insufficient_data: "Insufficient data",
  energy_mismatch: "Energy mismatch",
  not_converged: "Not converged",
  not_indexed: "Not indexed",
}

export function RefineCard({
  frame,
  request,
  indexing,
  refine,
  result,
  stale,
  warningDeg,
}: {
  frame: string
  request: CalibrationRefineRequest | null
  indexing: CalibrationIndexing | undefined
  refine: ReturnType<typeof useRefineCalibration>
  result: CalibrationResult | undefined
  stale: boolean
  warningDeg: number
}) {
  const updateFrame = useCalibrationDraftStore((state) => state.updateFrame)
  const setDetector0 = useCalibrationDraftStore((state) => state.setDetector0)
  const energies =
    request?.measurements?.filter((m) => m.energy_kev != null).length ?? 0
  const blocked = !request
    ? "Choose the inputs"
    : !indexing?.found
      ? "Index the peaks"
      : indexing.fit_sample_rotation && energies === 0
        ? "Enter at least one measured energy"
        : null
  const shown = result ?? (stale ? undefined : refine.data)
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle>Refine</CardTitle>
        <CardAction className="flex gap-1">
          <Button
            size="xs"
            variant="outline"
            disabled={result?.status !== "ok"}
            title="Index the peaks again with the refined detector"
            onClick={() => {
              if (result?.status !== "ok") return
              updateFrame(frame, (current) => ({
                ...current,
                start: result.refined,
              }))
            }}
          >
            Match Again
          </Button>
          <Button
            size="xs"
            disabled={blocked !== null || refine.isPending}
            onClick={() => {
              if (!request) return
              refine.mutate(
                { data: request },
                {
                  onSuccess: (answer) => {
                    if (answer.status === "ok" && answer.fit_sample_rotation) {
                      setDetector0({
                        frame,
                        reciprocal: answer.refined_reciprocal,
                      })
                    }
                  },
                  onError: (error) => toastError(error, "Refine"),
                }
              )
            }}
          >
            {refine.isPending ? "Refining" : "Refine"}
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {blocked && <p className="text-sm text-muted-foreground">{blocked}</p>}
        {stale && !refine.isPending && (
          <p className="text-sm text-muted-foreground">
            Inputs changed: refine again
          </p>
        )}
        {shown && <FitResult result={shown} warningDeg={warningDeg} />}
      </CardContent>
    </Card>
  )
}

function FitResult({
  result,
  warningDeg,
}: {
  result: CalibrationResult
  warningDeg: number
}) {
  const ok = result.status === "ok"
  const { initial, refined } = result
  const dP = refined.translation_um.map(
    (v, i) => v - (initial.translation_um[i] ?? 0)
  )
  const dR = refined.rotation_vector.map(
    (v, i) => v - (initial.rotation_vector[i] ?? 0)
  )
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <Badge variant={ok ? "secondary" : "destructive"}>
          {STATUS_LABELS[result.status]}
        </Badge>
        {!ok && (
          <span className="text-sm text-muted-foreground">
            {result.message}
          </span>
        )}
      </div>
      {ok && (
        <>
          <DefinitionList
            aria-label="Detector"
            rows={[
              {
                label: "R (rad)",
                value: vector(refined.rotation_vector, 8),
                mono: true,
              },
              { label: "ΔR (mrad)", value: vector(dR, 4, 1000), mono: true },
              {
                label: "± R (mrad)",
                value: vector(result.rotation_vector_sigma, 4, 1000),
                mono: true,
              },
              {
                label: "P (mm)",
                value: vector(refined.translation_um, 3, 0.001),
                mono: true,
              },
              { label: "ΔP (µm)", value: vector(dP, 1), mono: true },
              {
                label: "± P (µm)",
                value: vector(result.translation_sigma_um, 1),
                mono: true,
              },
              {
                label: "Sample rotation",
                value: result.fit_sample_rotation
                  ? `moved ${deg(result.sample_rotation_change_deg, 4)}`
                  : result.sample_rotation_from === "detector0"
                    ? "Fixed: detector 0 calibration"
                    : "Fixed: this frame's indexing",
              },
              {
                label: "Error",
                value: `${result.error_start.toPrecision(3)} → ${result.error_end.toPrecision(3)}`,
              },
              {
                label: "RMS pixel error",
                value:
                  result.rms_pixel_error === null
                    ? "—"
                    : `${result.rms_pixel_error.toFixed(3)} px`,
              },
              {
                label: "RMS energy error",
                value:
                  result.rms_energy_error_ev === null
                    ? "—"
                    : `${result.rms_energy_error_ev.toFixed(2)} eV`,
              },
              {
                label: "Reflections",
                value: `${String(result.n_rows)} (${String(result.n_energies)} with energy)`,
              },
            ]}
          />
          {result.surface && (
            <SurfaceCheck surface={result.surface} warningDeg={warningDeg} />
          )}
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------------------------
// Save

export function SaveCard({
  folder,
  request,
  result,
}: {
  folder: GeometryFolder
  request: CalibrationRefineRequest | null
  result: CalibrationResult | undefined
}) {
  const queryClient = useQueryClient()
  const setFiles = useCalibrationDraftStore((state) => state.setFiles)
  const saveFolder = useCalibrationDraftStore((state) => state.saveFolder)
  const setSaveFolder = useCalibrationDraftStore((state) => state.setSaveFolder)
  const save = useSaveCalibration()
  const [name, setName] = useState("")
  // The folder chosen, else the geometry folder; the request names it only when it is another.
  const target = saveFolder || folder.path || ""
  const chosen = saveFolder !== "" && saveFolder !== folder.path
  const nameInvalid =
    name.trim() !== "" && !GEOMETRY_FILE_NAME.test(name.trim())
  const suggested = result?.note ?? ""
  const [note, setNote] = useState(suggested)
  const [shown, setShown] = useState(suggested)
  if (shown !== suggested) {
    setShown(suggested)
    setNote(suggested)
  }
  const folderProblem = FOLDER_STATES[folder.state]
  const blocked =
    target === ""
      ? "Choose a folder"
      : !chosen && folder.state !== "ok"
        ? `Geometry folder: ${folderProblem}`
        : nameInvalid
          ? "File name: letters, digits, . _ - and .xml"
          : result?.status !== "ok"
            ? "Refine first"
            : null
  const saved = save.data
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle>Save</CardTitle>
        <CardAction>
          <Button
            size="xs"
            disabled={
              blocked !== null ||
              !request ||
              note.trim() === "" ||
              save.isPending
            }
            onClick={() => {
              if (!request) return
              save.mutate(
                {
                  data: {
                    ...request,
                    note: note.trim(),
                    inputs_version: result?.inputs_version ?? "",
                    folder: chosen ? saveFolder : null,
                    name: name.trim() || null,
                  },
                },
                {
                  onSuccess: () => {
                    // Keep calibrating from the file chosen, not the newest one just written.
                    setFiles({ geometry: request.geometry })
                    void queryClient.invalidateQueries({
                      queryKey: getGetCalibrationSetupQueryKey(),
                    })
                  },
                  onError: (error) => toastError(error, "Save"),
                }
              )
            }}
          >
            {save.isPending ? "Saving" : "Save"}
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <FileRow
          id="calibration-save-folder"
          label="Folder"
          value={target}
          onChange={(next) => {
            setSaveFolder(next.trim() === folder.path ? "" : next.trim())
          }}
          mode="directory"
          startPath={folder.path ?? undefined}
          dialogTitle="Choose the folder to save the geometry file in"
        />
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="calibration-save-name">File name</Label>
          <Input
            id="calibration-save-name"
            value={name}
            maxLength={255}
            placeholder="geoN_<date>_<time>.xml"
            aria-invalid={nameInvalid || undefined}
            onChange={(event) => {
              setName(event.target.value)
            }}
            title="Empty: geoN_ with the date and time of the save. An existing file is never replaced."
            className="font-mono text-xs"
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="calibration-note">Note</Label>
          <Input
            id="calibration-note"
            value={note}
            maxLength={99}
            onChange={(event) => {
              setNote(event.target.value)
            }}
            title="The detector's <note> in the geometry file: printable ASCII without <, > or &"
            className="font-mono text-xs"
          />
        </div>
        {blocked && <p className="text-sm text-muted-foreground">{blocked}</p>}
        {saved && (
          <DefinitionList
            aria-label="Saved"
            rows={[{ label: "Saved", value: saved.path, mono: true }]}
          />
        )}
      </CardContent>
    </Card>
  )
}
