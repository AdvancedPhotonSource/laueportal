"""snake_case: rename every camelCase (mixed-case) column of v1's schema.

51 columns in four tables (34 in ``metadata``, 13 in ``scan``, 2 in ``catalog``, 2 in ``calib``, which
0004 drops). ``RENAMES`` is the complete map.

SQLite's own ``ALTER TABLE ... RENAME COLUMN`` does the work, so no table is copied: it rewrites the
column in the table, its primary key, UNIQUE constraints and indexes, and the foreign keys of the
tables that reference it (``REFERENCES metadata ("scan_number")`` in ``scan``, ``catalog``, ``calib``,
``indexing_run`` and ``reconstruction_run``). Data and column order are untouched. The downgrade
renames everything back.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

RENAMES: dict[str, dict[str, str]] = {
    "metadata": {
        "scanNumber": "scan_number",
        "source_beamBad": "source_beam_bad",
        "source_CCDshutter": "source_ccd_shutter",
        "source_monoTransStatus": "source_mono_trans_status",
        "source_IDgap_unit": "source_id_gap_unit",
        "source_IDgap": "source_id_gap",
        "source_IDtaper_unit": "source_id_taper_unit",
        "source_IDtaper": "source_id_taper",
        "source_ringCurrent_unit": "source_ring_current_unit",
        "source_ringCurrent": "source_ring_current",
        "sample_XYZ_unit": "sample_xyz_unit",
        "sample_XYZ_desc": "sample_xyz_desc",
        "sample_XYZ": "sample_xyz",
        "knifeEdge_XYZ_unit": "knife_edge_xyz_unit",
        "knifeEdge_XYZ_desc": "knife_edge_xyz_desc",
        "knifeEdge_XYZ": "knife_edge_xyz",
        "knifeEdge_knifeScan_unit": "knife_edge_knife_scan_unit",
        "knifeEdge_knifeScan": "knife_edge_knife_scan",
        "scanEnd_abort": "scan_end_abort",
        "scanEnd_time_epoch": "scan_end_time_epoch",
        "scanEnd_time": "scan_end_time",
        "scanEnd_scanDuration_unit": "scan_end_scan_duration_unit",
        "scanEnd_scanDuration": "scan_end_scan_duration",
        "scanEnd_source_beamBad": "scan_end_source_beam_bad",
        "scanEnd_source_ringCurrent_unit": "scan_end_source_ring_current_unit",
        "scanEnd_source_ringCurrent": "scan_end_source_ring_current",
        "motorGroup_sample_npts_total": "motor_group_sample_npts_total",
        "motorGroup_sample_cpt_total": "motor_group_sample_cpt_total",
        "motorGroup_energy_npts_total": "motor_group_energy_npts_total",
        "motorGroup_energy_cpt_total": "motor_group_energy_cpt_total",
        "motorGroup_depth_npts_total": "motor_group_depth_npts_total",
        "motorGroup_depth_cpt_total": "motor_group_depth_cpt_total",
        "motorGroup_other_npts_total": "motor_group_other_npts_total",
        "motorGroup_other_cpt_total": "motor_group_other_cpt_total",
    },
    "scan": {
        "scanNumber": "scan_number",
        "scan_positioner1_PV": "scan_positioner1_pv",
        "scan_positioner2_PV": "scan_positioner2_pv",
        "scan_positioner3_PV": "scan_positioner3_pv",
        "scan_positioner4_PV": "scan_positioner4_pv",
        "scan_detectorTrig1_PV": "scan_detector_trig1_pv",
        "scan_detectorTrig1_VAL": "scan_detector_trig1_val",
        "scan_detectorTrig2_PV": "scan_detector_trig2_pv",
        "scan_detectorTrig2_VAL": "scan_detector_trig2_val",
        "scan_detectorTrig3_PV": "scan_detector_trig3_pv",
        "scan_detectorTrig3_VAL": "scan_detector_trig3_val",
        "scan_detectorTrig4_PV": "scan_detector_trig4_pv",
        "scan_detectorTrig4_VAL": "scan_detector_trig4_val",
    },
    "catalog": {
        "scanNumber": "scan_number",
        "filenamePrefix": "filename_prefix",
    },
    "calib": {
        "scanNumber": "scan_number",
        "filenamePrefix": "filename_prefix",
    },
}


def upgrade() -> None:
    for table, columns in RENAMES.items():
        for old, new in columns.items():
            op.alter_column(table, old, new_column_name=new)


def downgrade() -> None:
    for table, columns in reversed(RENAMES.items()):
        for old, new in reversed(columns.items()):
            op.alter_column(table, new, new_column_name=old)
