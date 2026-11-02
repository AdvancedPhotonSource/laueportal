// Scan details as GET /api/v1/scans/{id} answers them, for tests: a depth scan (wire, with a
// catalog) and its variants (no catalog, another technique, a migrated catalog's aperture).
import type {
  ScanCatalog,
  ScanDetail,
  ScanDimension,
  ScanMetadata,
} from "@/api"

function emptySlots(): Omit<
  ScanDimension,
  "id" | "scan_dim" | "scan_npts" | "scan_cpt" | "scan_after" | "positioners"
> {
  return {
    scan_positioner1_pv: "",
    scan_positioner1_ar: "",
    scan_positioner1_mode: "",
    scan_positioner1: "",
    scan_positioner2_pv: "",
    scan_positioner2_ar: "",
    scan_positioner2_mode: "",
    scan_positioner2: "",
    scan_positioner3_pv: "",
    scan_positioner3_ar: "",
    scan_positioner3_mode: "",
    scan_positioner3: "",
    scan_positioner4_pv: "",
    scan_positioner4_ar: "",
    scan_positioner4_mode: "",
    scan_positioner4: "",
    scan_detector_trig1_pv: "34ide:det:Acquire",
    scan_detector_trig1_val: "1",
    scan_detector_trig2_pv: "",
    scan_detector_trig2_val: "",
    scan_detector_trig3_pv: "",
    scan_detector_trig3_val: "",
    scan_detector_trig4_pv: "",
    scan_detector_trig4_val: "",
  }
}

export function makeMetadata(
  scanNumber: number,
  overrides: Partial<ScanMetadata> = {}
): ScanMetadata {
  return {
    scan_number: scanNumber,
    time_epoch: 1_767_600_000,
    time: "2026-01-05T08:00:00Z",
    user_name: "adoe",
    source_beam_bad: "false",
    source_ccd_shutter: "open",
    source_mono_trans_status: "ok",
    source_energy_unit: "keV",
    source_energy: 20,
    source_id_gap_unit: "mm",
    source_id_gap: 11.5,
    source_id_taper_unit: "mrad",
    source_id_taper: 0.25,
    source_ring_current_unit: "mA",
    source_ring_current: 101.2,
    sample_xyz_unit: "micron",
    sample_xyz_desc: "fine stage",
    sample_xyz: "1,2,3",
    knife_edge_xyz_unit: "micron",
    knife_edge_xyz_desc: "wire",
    knife_edge_xyz: "4,5,6",
    knife_edge_knife_scan_unit: "micron",
    knife_edge_knife_scan: 120,
    mda_file: "34ide_0042.mda",
    scan_end_abort: "false",
    scan_end_time_epoch: 1_767_603_600,
    scan_end_time: "2026-01-05T03:00:00",
    scan_end_scan_duration_unit: "s",
    scan_end_scan_duration: 3600,
    scan_end_source_beam_bad: "false",
    scan_end_source_ring_current_unit: "mA",
    scan_end_source_ring_current: 100.9,
    motor_group_sample_npts_total: 441,
    motor_group_sample_cpt_total: 441,
    motor_group_energy_npts_total: null,
    motor_group_energy_cpt_total: null,
    motor_group_depth_npts_total: 81,
    motor_group_depth_cpt_total: 80,
    motor_group_other_npts_total: null,
    motor_group_other_cpt_total: null,
    ...overrides,
  }
}

/** A depth dimension (the wire, outer) and a sample area (inner), as the log lists them. */
export function depthDimensions(): ScanDimension[] {
  return [
    {
      ...emptySlots(),
      id: 11,
      scan_dim: 2,
      scan_npts: 81,
      scan_cpt: 80,
      scan_after: "Prior",
      scan_positioner1_pv: "34ide:t80:c0:m1.VAL",
      scan_positioner1_ar: "relative",
      scan_positioner1_mode: "linear",
      scan_positioner1: "-50 50 1.25",
      positioners: [
        {
          index: 1,
          pv: "34ide:t80:c0:m1.VAL",
          motor_group: "depth",
          start: "-50",
          end: "50",
          step: "1.25",
        },
      ],
    },
    {
      ...emptySlots(),
      id: 12,
      scan_dim: 1,
      scan_npts: 21,
      scan_cpt: 21,
      scan_after: "Prior",
      scan_positioner1_pv: "34ide:mxv:c0:m1.VAL",
      scan_positioner1_ar: "relative",
      scan_positioner1_mode: "linear",
      scan_positioner1: "-10 10 1",
      scan_positioner2_pv: "34ide:mxv:c0:m3.VAL",
      scan_positioner2_ar: "absolute",
      scan_positioner2_mode: "table",
      scan_positioner2: "0,5",
      positioners: [
        {
          index: 1,
          pv: "34ide:mxv:c0:m1.VAL",
          motor_group: "sample",
          start: "-10",
          end: "10",
          step: "1",
        },
        {
          index: 2,
          pv: "34ide:mxv:c0:m3.VAL",
          motor_group: "sample",
          start: null,
          end: null,
          step: null,
        },
      ],
    },
  ]
}

export function makeCatalog(overrides: Partial<ScanCatalog> = {}): ScanCatalog {
  return {
    filefolder: "/net/data/2026/scan_42",
    filename_prefix: ["Ni_%d.h5", "Si_%d.h5"],
    aperture: "wire",
    sample_name: "Ni film",
    notes: "first note",
    version: 'W/"catalog-1"',
    ...overrides,
  }
}

/** Scan 42: a depth scan with a wire catalog. */
export function makeScanDetail(
  overrides: Partial<ScanDetail> = {}
): ScanDetail {
  const scanNumber = overrides.scan_number ?? 42
  return {
    scan_number: scanNumber,
    technique: "depth + area",
    metadata: makeMetadata(scanNumber),
    dimensions: depthDimensions(),
    catalog: makeCatalog(),
    ...overrides,
  }
}
