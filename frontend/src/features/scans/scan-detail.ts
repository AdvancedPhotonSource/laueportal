// The scan detail page's logic: its tab in the URL, its loader, the depth rule, the aperture's
// names, and what the Scan Info and Metadata tabs show.
import type { QueryClient } from "@tanstack/react-query"
import { notFound } from "@tanstack/react-router"

import {
  ApiError,
  getGetScanQueryOptions,
  type ScanDetail,
  type ScanDimension,
  type ScanMetadata,
} from "@/api"
import { formatDateTime } from "@/lib/format"

// ---------------------------------------------------------------------------
// The tab in the URL (only a value other than the default)

export const SCAN_TABS = ["info", "metadata", "catalog"] as const
export type ScanTab = (typeof SCAN_TABS)[number]

export function isScanTab(value: unknown): value is ScanTab {
  return SCAN_TABS.some((tab) => tab === value)
}

export const TAB_LABELS: Record<ScanTab, string> = {
  info: "Scan Info",
  metadata: "Metadata",
  catalog: "Catalog",
}

export interface ScanSearch {
  /** Not "info": the default tab is not written. */
  tab?: Exclude<ScanTab, "info">
}

/**
 * `validateSearch` of /scans/$scanId: `tab` is "metadata" or "catalog" (strings, as the URL gives
 * them); anything else, the default tab included, is dropped (no error page).
 */
export function validateScanSearch(
  search: Record<string, unknown>
): ScanSearch {
  const { tab } = search
  return { tab: tab === "metadata" || tab === "catalog" ? tab : undefined }
}

/** The tab a search shows. */
export function searchTab(search: ScanSearch): ScanTab {
  return search.tab ?? "info"
}

/** The search for a tab (the default one leaves the URL). */
export function tabSearch(tab: ScanTab): ScanSearch {
  return { tab: tab === "info" ? undefined : tab }
}

// ---------------------------------------------------------------------------
// The route's scan

/** The scan number of the URL's `scanId`, or undefined when it is not one (then the page is a 404). */
export function parseScanId(text: string): number | undefined {
  if (!/^\d+$/.test(text)) return undefined
  const value = Number(text)
  return Number.isSafeInteger(value) ? value : undefined
}

/**
 * The route's loader: the scan into the query cache (the page reads it with useGetScan), or
 * notFound() for a scan ID that is not one or a scan the API does not know (404). Other errors go to
 * the route's error boundary.
 */
export async function loadScan(
  queryClient: QueryClient,
  scanId: string
): Promise<void> {
  const scan = parseScanId(scanId)
  if (scan === undefined) throw notFound()
  const { queryKey, queryFn } = getGetScanQueryOptions(scan)
  try {
    // A cached scan is shown as it is (the page refreshes it when it is stale), else it is fetched.
    await queryClient.query({ queryKey, queryFn, staleTime: "static" })
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) throw notFound()
    throw error
  }
}

// ---------------------------------------------------------------------------
// Depth scans and the aperture

/**
 * Whether the scan's type has "depth": the rule for the Aperture field and the Reconstructions card,
 * which also shows for a scan of another type that has reconstructions (ScanRunCards).
 */
export function isDepthScan(technique: string): boolean {
  return technique.toLowerCase().includes("depth")
}

export const APERTURES = ["none", "wire", "mask"] as const
export type Aperture = (typeof APERTURES)[number]

export const APERTURE_LABELS: Record<Aperture, string> = {
  none: "None",
  wire: "Wire",
  mask: "Coded Aperture",
}

/**
 * The catalog form's aperture for a stored one: `none`, `wire` and `mask` as they are, a migrated
 * catalog's "None" (any case) as `none`; anything else (e.g. the demo's "synthetic") is not one the
 * form offers: null, to be chosen before saving.
 */
export function apertureValue(
  stored: string | null | undefined
): Aperture | null {
  const value = stored?.trim().toLowerCase()
  return APERTURES.find((aperture) => aperture === value) ?? null
}

/** How an aperture shows: its form label ("Coded Aperture"), else the stored text. */
export function apertureLabel(stored: string | null | undefined): string {
  const value = apertureValue(stored)
  return value ? APERTURE_LABELS[value] : (stored ?? "")
}

// ---------------------------------------------------------------------------
// Scan Info

export interface InfoField {
  label: string
  value: string
  /** Shown in a monospace font (paths). */
  mono?: boolean
}

/** The Scan Info fields, in order; the aperture only for a depth scan. Needs the catalog. */
export function scanInfoFields(scan: ScanDetail): InfoField[] {
  const { metadata, catalog } = scan
  const fields: InfoField[] = [
    { label: "User", value: metadata.user_name ?? "" },
    { label: "Date", value: formatDateTime(metadata.time) },
    { label: "Dimensions", value: `${String(scan.dimensions.length)}D` },
    { label: "Scan Type", value: scan.technique },
  ]
  if (isDepthScan(scan.technique)) {
    fields.push({ label: "Aperture", value: apertureLabel(catalog?.aperture) })
  }
  fields.push(
    { label: "Sample", value: catalog?.sample_name ?? "" },
    { label: "Data Folder", value: catalog?.filefolder ?? "", mono: true }
  )
  return fields
}

export interface PositionerRow {
  key: string
  /** The motor group, capitalized ("Sample"). */
  group: string
  pv: string
  start: string | null
  end: string | null
  step: string | null
  /** The positioner's text as written, when it is not `start end step`. */
  text: string | null
}

export interface DimensionGroup {
  key: string
  /** "Dim 2" (the scan log's dimension number). */
  label: string
  /** "Completed 21/21": points completed of those programmed. */
  completed: string
  positioners: PositionerRow[]
}

function capitalize(text: string): string {
  return text ? `${text.charAt(0).toUpperCase()}${text.slice(1)}` : text
}

/** A scan row's four positioner slots, as stored (an unused slot has an empty PV). */
function positionerSlots(dimension: ScanDimension) {
  const d = dimension
  return [
    {
      index: 1,
      pv: d.scan_positioner1_pv,
      text: d.scan_positioner1,
      ar: d.scan_positioner1_ar,
      mode: d.scan_positioner1_mode,
    },
    {
      index: 2,
      pv: d.scan_positioner2_pv,
      text: d.scan_positioner2,
      ar: d.scan_positioner2_ar,
      mode: d.scan_positioner2_mode,
    },
    {
      index: 3,
      pv: d.scan_positioner3_pv,
      text: d.scan_positioner3,
      ar: d.scan_positioner3_ar,
      mode: d.scan_positioner3_mode,
    },
    {
      index: 4,
      pv: d.scan_positioner4_pv,
      text: d.scan_positioner4,
      ar: d.scan_positioner4_ar,
      mode: d.scan_positioner4_mode,
    },
  ]
}

/** A scan row's four detector-trigger slots, as stored. */
function triggerSlots(dimension: ScanDimension) {
  const d = dimension
  return [
    {
      index: 1,
      pv: d.scan_detector_trig1_pv,
      value: d.scan_detector_trig1_val,
    },
    {
      index: 2,
      pv: d.scan_detector_trig2_pv,
      value: d.scan_detector_trig2_val,
    },
    {
      index: 3,
      pv: d.scan_detector_trig3_pv,
      value: d.scan_detector_trig3_val,
    },
    {
      index: 4,
      pv: d.scan_detector_trig4_pv,
      value: d.scan_detector_trig4_val,
    },
  ]
}

/** The per-dimension table of Scan Info (the positioner table), in the log's order. */
export function dimensionGroups(
  dimensions: readonly ScanDimension[]
): DimensionGroup[] {
  return dimensions.map((dimension, position) => {
    const number = dimension.scan_dim ?? position + 1
    const programmed =
      dimension.scan_npts === null ? "?" : String(dimension.scan_npts)
    return {
      key: String(dimension.id),
      label: `Dim ${String(number)}`,
      completed: `Completed ${String(dimension.scan_cpt)}/${programmed}`,
      positioners: dimension.positioners.map((positioner) => {
        const split = positioner.start !== null
        return {
          key: `${String(dimension.id)}:${String(positioner.index)}`,
          group: capitalize(positioner.motor_group),
          pv: positioner.pv,
          start: positioner.start,
          end: positioner.end,
          step: positioner.step,
          text: split
            ? null
            : (positionerSlots(dimension).find(
                (slot) => slot.index === positioner.index
              )?.text ?? ""),
        }
      }),
    }
  })
}

// ---------------------------------------------------------------------------
// Metadata (read-only)

export interface MetadataItem {
  label: string
  /** "" when the log has no value. */
  value: string
  /** A file name or a PV: monospace. */
  mono?: boolean
}

export interface MetadataSection {
  title: string
  items: MetadataItem[]
}

/** A value with its unit ("20 keV"); "" without a value. */
export function withUnit(
  value: string | number | null | undefined,
  unit?: string | null
): string {
  if (value === null || value === undefined || value === "") return ""
  const text = String(value)
  return unit ? `${text} ${unit}` : text
}

const MOTOR_GROUPS = ["sample", "depth", "energy", "other"] as const

function totals(metadata: ScanMetadata): MetadataItem[] {
  const counts = {
    sample: [
      metadata.motor_group_sample_npts_total,
      metadata.motor_group_sample_cpt_total,
    ],
    depth: [
      metadata.motor_group_depth_npts_total,
      metadata.motor_group_depth_cpt_total,
    ],
    energy: [
      metadata.motor_group_energy_npts_total,
      metadata.motor_group_energy_cpt_total,
    ],
    other: [
      metadata.motor_group_other_npts_total,
      metadata.motor_group_other_cpt_total,
    ],
  } as const
  return MOTOR_GROUPS.flatMap((group) => {
    const [points, completed] = counts[group]
    if (points === null) return []
    return [
      { label: `${capitalize(group)} Points`, value: withUnit(points) },
      { label: `${capitalize(group)} Completed`, value: withUnit(completed) },
    ]
  })
}

/**
 * The Metadata tab's sections, in order, from the scan log's record: identifiers, source, positioners,
 * then each scan row's (`dimensionSections`), the scan's end and the motor groups' totals.
 */
export function metadataSections(
  metadata: ScanMetadata,
  dimensions: readonly ScanDimension[] = []
): MetadataSection[] {
  const m = metadata
  const [identifiers, source, sample, aperture, ...end] = [
    {
      title: "Identifiers",
      items: [
        { label: "Scan Number", value: String(m.scan_number) },
        { label: "Time", value: formatDateTime(m.time) },
        { label: "User Name", value: withUnit(m.user_name) },
      ],
    },
    {
      title: "Source",
      items: [
        { label: "Beam Bad", value: withUnit(m.source_beam_bad) },
        { label: "CCD Shutter", value: withUnit(m.source_ccd_shutter) },
        {
          label: "Mono Trans Status",
          value: withUnit(m.source_mono_trans_status),
        },
        {
          label: "Energy",
          value: withUnit(m.source_energy, m.source_energy_unit),
        },
        {
          label: "Ring Current",
          value: withUnit(m.source_ring_current, m.source_ring_current_unit),
        },
        {
          label: "ID Gap",
          value: withUnit(m.source_id_gap, m.source_id_gap_unit),
        },
        // The ID taper's own values (not the gap).
        {
          label: "ID Taper",
          value: withUnit(m.source_id_taper, m.source_id_taper_unit),
        },
      ],
    },
    {
      title: "Sample Positioner",
      items: [
        {
          label: "Sample XYZ",
          value: withUnit(m.sample_xyz, m.sample_xyz_unit),
        },
        { label: "Description", value: withUnit(m.sample_xyz_desc) },
      ],
    },
    {
      title: "Aperture Positioner",
      items: [
        {
          label: "Aperture XYZ",
          value: withUnit(m.knife_edge_xyz, m.knife_edge_xyz_unit),
        },
        { label: "Description", value: withUnit(m.knife_edge_xyz_desc) },
        {
          label: "Knife Scan",
          value: withUnit(
            m.knife_edge_knife_scan,
            m.knife_edge_knife_scan_unit
          ),
        },
      ],
    },
    {
      title: "Scan End",
      items: [
        { label: "MDA File", value: withUnit(m.mda_file), mono: !!m.mda_file },
        { label: "Abort", value: withUnit(m.scan_end_abort) },
        { label: "Time", value: withUnit(m.scan_end_time) },
        {
          label: "Scan Duration",
          value: withUnit(
            m.scan_end_scan_duration,
            m.scan_end_scan_duration_unit
          ),
        },
        { label: "Beam Bad", value: withUnit(m.scan_end_source_beam_bad) },
        {
          label: "Ring Current",
          value: withUnit(
            m.scan_end_source_ring_current,
            m.scan_end_source_ring_current_unit
          ),
        },
      ],
    },
    { title: "Motor Group Totals", items: totals(m) },
  ]
  return [
    identifiers,
    source,
    sample,
    aperture,
    ...dimensionSections(dimensions),
    ...end,
  ]
}

/** The scan accordions: one section per scan row, with the slots that are set. */
export function dimensionSections(
  dimensions: readonly ScanDimension[]
): MetadataSection[] {
  return dimensions.map((dimension, position) => {
    const items: MetadataItem[] = [
      { label: "Points", value: withUnit(dimension.scan_npts) },
      { label: "Points Completed", value: withUnit(dimension.scan_cpt) },
      { label: "After", value: withUnit(dimension.scan_after) },
    ]
    for (const slot of positionerSlots(dimension)) {
      if (!slot.pv) continue
      const name = `Positioner ${String(slot.index)}`
      const group = dimension.positioners.find(
        (positioner) => positioner.index === slot.index
      )?.motor_group
      items.push(
        {
          label: `${name} PV`,
          value: group ? `${slot.pv} (${capitalize(group)})` : slot.pv,
          mono: true,
        },
        { label: name, value: slot.text },
        { label: `${name} Abs/Rel`, value: slot.ar },
        { label: `${name} Mode`, value: slot.mode }
      )
    }
    for (const slot of triggerSlots(dimension)) {
      if (!slot.pv) continue
      const name = `Detector Trigger ${String(slot.index)}`
      items.push(
        { label: `${name} PV`, value: slot.pv, mono: true },
        { label: `${name} Value`, value: slot.value }
      )
    }
    return {
      title: `Scan Dim ${String(dimension.scan_dim ?? position + 1)}`,
      items,
    }
  })
}
