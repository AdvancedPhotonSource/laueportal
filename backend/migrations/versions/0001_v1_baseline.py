"""v1 baseline: exactly the schema of v1 ``dde1bde``'s ``Base.metadata.create_all``.

Same tables, columns, types, constraints (and their order) and the five named indexes, so the DDL that
SQLite stores is identical to v1's, object for object (``tests/fixtures/v1_schema_dde1bde.json``).
Written out by hand on purpose: it must not follow the models, which the later revisions change.

Existing v1 databases (after v1's own ``scripts/workflow_migration.py``) already have this schema:
``laueportal db stamp 0001`` records it without running this revision.

v1 had no naming convention: its primary keys, foreign keys and UNIQUE constraints are unnamed, and the
CHECK constraint and index names are spelled out (``op.f``) so no convention can change them.

Revision ID: 0001
Revises:
Create Date: 2026-10-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job",
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("computer_name", sa.String(), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("submit_time", sa.DateTime(), nullable=True),
        sa.Column("start_time", sa.DateTime(), nullable=True),
        sa.Column("finish_time", sa.DateTime(), nullable=True),
        sa.Column("messages", sa.String(), nullable=True),
        sa.Column("phase", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(), nullable=True),
        sa.Column("queue_job_id", sa.String(), nullable=True),
        sa.Column("n_inputs", sa.Integer(), server_default="0", nullable=False),
        sa.Column("n_succeeded", sa.Integer(), server_default="0", nullable=False),
        sa.Column("n_failed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("n_not_run", sa.Integer(), server_default="0", nullable=False),
        sa.Column("run_directory", sa.String(), nullable=True),
        sa.Column("manifest_path", sa.String(), nullable=True),
        sa.Column("manifest_digest", sa.String(), nullable=True),
        sa.Column("request_path", sa.String(), nullable=True),
        sa.Column("failure_report_path", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("job_id"),
        sa.CheckConstraint(
            "n_inputs >= 0 AND n_succeeded >= 0 AND n_failed >= 0 AND n_not_run >= 0",
            name=op.f("ck_job_counters_non_negative"),
        ),
        sa.CheckConstraint("n_succeeded + n_failed + n_not_run <= n_inputs", name=op.f("ck_job_counters_bounded")),
    )
    op.create_table(
        "metadata",
        sa.Column("scanNumber", sa.Integer(), nullable=False),
        sa.Column("time_epoch", sa.Integer(), nullable=True),
        sa.Column("time", sa.DateTime(), nullable=False),
        sa.Column("user_name", sa.String(), nullable=True),
        sa.Column("source_beamBad", sa.String(), nullable=True),
        sa.Column("source_CCDshutter", sa.String(), nullable=True),
        sa.Column("source_monoTransStatus", sa.String(), nullable=True),
        sa.Column("source_energy_unit", sa.String(), nullable=True),
        sa.Column("source_energy", sa.Float(), nullable=True),
        sa.Column("source_IDgap_unit", sa.String(), nullable=True),
        sa.Column("source_IDgap", sa.Float(), nullable=True),
        sa.Column("source_IDtaper_unit", sa.String(), nullable=True),
        sa.Column("source_IDtaper", sa.Float(), nullable=True),
        sa.Column("source_ringCurrent_unit", sa.String(), nullable=True),
        sa.Column("source_ringCurrent", sa.Float(), nullable=True),
        sa.Column("sample_XYZ_unit", sa.String(), nullable=True),
        sa.Column("sample_XYZ_desc", sa.String(), nullable=True),
        sa.Column("sample_XYZ", sa.String(), nullable=True),
        sa.Column("knifeEdge_XYZ_unit", sa.String(), nullable=True),
        sa.Column("knifeEdge_XYZ_desc", sa.String(), nullable=True),
        sa.Column("knifeEdge_XYZ", sa.String(), nullable=True),
        sa.Column("knifeEdge_knifeScan_unit", sa.String(), nullable=True),
        sa.Column("knifeEdge_knifeScan", sa.Float(), nullable=True),
        sa.Column("mda_file", sa.String(), nullable=True),
        sa.Column("scanEnd_abort", sa.String(), nullable=True),
        sa.Column("scanEnd_time_epoch", sa.Integer(), nullable=True),
        sa.Column("scanEnd_time", sa.String(), nullable=True),
        sa.Column("scanEnd_scanDuration_unit", sa.String(), nullable=True),
        sa.Column("scanEnd_scanDuration", sa.Float(), nullable=True),
        sa.Column("scanEnd_source_beamBad", sa.String(), nullable=True),
        sa.Column("scanEnd_source_ringCurrent_unit", sa.String(), nullable=True),
        sa.Column("scanEnd_source_ringCurrent", sa.Float(), nullable=True),
        sa.Column("motorGroup_sample_npts_total", sa.Integer(), nullable=True),
        sa.Column("motorGroup_sample_cpt_total", sa.Integer(), nullable=True),
        sa.Column("motorGroup_energy_npts_total", sa.Integer(), nullable=True),
        sa.Column("motorGroup_energy_cpt_total", sa.Integer(), nullable=True),
        sa.Column("motorGroup_depth_npts_total", sa.Integer(), nullable=True),
        sa.Column("motorGroup_depth_cpt_total", sa.Integer(), nullable=True),
        sa.Column("motorGroup_other_npts_total", sa.Integer(), nullable=True),
        sa.Column("motorGroup_other_cpt_total", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("scanNumber"),
    )
    op.create_table(
        "calib",
        sa.Column("calib_id", sa.Integer(), nullable=False),
        sa.Column("scanNumber", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("filefolder", sa.String(), nullable=False),
        sa.Column("filenamePrefix", sa.JSON(), nullable=False),
        sa.Column("author", sa.String(), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("calib_config", sa.String(), nullable=False),
        sa.Column("cenx", sa.Float(), nullable=False),
        sa.Column("dist", sa.Float(), nullable=False),
        sa.Column("anglez", sa.Float(), nullable=False),
        sa.Column("angley", sa.Float(), nullable=False),
        sa.Column("anglex", sa.Float(), nullable=False),
        sa.Column("cenz", sa.Float(), nullable=False),
        sa.Column("shift_parameter", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("calib_id"),
        sa.ForeignKeyConstraint(["scanNumber"], ["metadata.scanNumber"]),
        sa.UniqueConstraint("job_id"),
        sa.ForeignKeyConstraint(["job_id"], ["job.job_id"]),
    )
    op.create_table(
        "catalog",
        sa.Column("catalog_id", sa.Integer(), nullable=False),
        sa.Column("scanNumber", sa.Integer(), nullable=False),
        sa.Column("filefolder", sa.String(), nullable=False),
        sa.Column("filenamePrefix", sa.JSON(), nullable=False),
        sa.Column("aperture", sa.String(), nullable=False),
        sa.Column("sample_name", sa.String(), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("catalog_id"),
        sa.UniqueConstraint("scanNumber"),
        sa.ForeignKeyConstraint(["scanNumber"], ["metadata.scanNumber"]),
    )
    op.create_table(
        "reconstruction_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scan_number", sa.Integer(), nullable=True),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("input_path", sa.String(), nullable=False),
        sa.Column("output_path", sa.String(), nullable=True),
        sa.Column("author", sa.String(), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("algorithm_version", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("method IN ('wire', 'ca')", name=op.f("ck_reconstruction_run_method")),
        sa.ForeignKeyConstraint(["scan_number"], ["metadata.scanNumber"]),
        sa.UniqueConstraint("job_id"),
        sa.ForeignKeyConstraint(["job_id"], ["job.job_id"]),
    )
    op.create_index(op.f("ix_reconstruction_run_scan_number"), "reconstruction_run", ["scan_number"])
    op.create_index(op.f("ix_reconstruction_run_method"), "reconstruction_run", ["method"])
    op.create_table(
        "scan",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scanNumber", sa.Integer(), nullable=False),
        sa.Column("scan_dim", sa.Integer(), nullable=True),
        sa.Column("scan_npts", sa.Integer(), nullable=True),
        sa.Column("scan_after", sa.String(), nullable=False),
        sa.Column("scan_positioner1_PV", sa.String(), nullable=False),
        sa.Column("scan_positioner1_ar", sa.String(), nullable=False),
        sa.Column("scan_positioner1_mode", sa.String(), nullable=False),
        sa.Column("scan_positioner1", sa.String(), nullable=False),
        sa.Column("scan_positioner2_PV", sa.String(), nullable=False),
        sa.Column("scan_positioner2_ar", sa.String(), nullable=False),
        sa.Column("scan_positioner2_mode", sa.String(), nullable=False),
        sa.Column("scan_positioner2", sa.String(), nullable=False),
        sa.Column("scan_positioner3_PV", sa.String(), nullable=False),
        sa.Column("scan_positioner3_ar", sa.String(), nullable=False),
        sa.Column("scan_positioner3_mode", sa.String(), nullable=False),
        sa.Column("scan_positioner3", sa.String(), nullable=False),
        sa.Column("scan_positioner4_PV", sa.String(), nullable=False),
        sa.Column("scan_positioner4_ar", sa.String(), nullable=False),
        sa.Column("scan_positioner4_mode", sa.String(), nullable=False),
        sa.Column("scan_positioner4", sa.String(), nullable=False),
        sa.Column("scan_detectorTrig1_PV", sa.String(), nullable=False),
        sa.Column("scan_detectorTrig1_VAL", sa.String(), nullable=False),
        sa.Column("scan_detectorTrig2_PV", sa.String(), nullable=False),
        sa.Column("scan_detectorTrig2_VAL", sa.String(), nullable=False),
        sa.Column("scan_detectorTrig3_PV", sa.String(), nullable=False),
        sa.Column("scan_detectorTrig3_VAL", sa.String(), nullable=False),
        sa.Column("scan_detectorTrig4_PV", sa.String(), nullable=False),
        sa.Column("scan_detectorTrig4_VAL", sa.String(), nullable=False),
        sa.Column("scan_cpt", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["scanNumber"], ["metadata.scanNumber"]),
    )
    op.create_table(
        "indexing_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scan_number", sa.Integer(), nullable=True),
        sa.Column("reconstruction_id", sa.Integer(), nullable=True),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("input_path", sa.String(), nullable=False),
        sa.Column("output_path", sa.String(), nullable=True),
        sa.Column("author", sa.String(), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("algorithm_version", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("n_frames_indexed", sa.Integer(), nullable=True),
        sa.Column("results_path", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("method IN ('lauego', 'laue_matching')", name=op.f("ck_indexing_run_method")),
        sa.ForeignKeyConstraint(["scan_number"], ["metadata.scanNumber"]),
        sa.ForeignKeyConstraint(["reconstruction_id"], ["reconstruction_run.id"]),
        sa.UniqueConstraint("job_id"),
        sa.ForeignKeyConstraint(["job_id"], ["job.job_id"]),
    )
    op.create_index(op.f("ix_indexing_run_scan_number"), "indexing_run", ["scan_number"])
    op.create_index(op.f("ix_indexing_run_reconstruction_id"), "indexing_run", ["reconstruction_id"])
    op.create_index(op.f("ix_indexing_run_method"), "indexing_run", ["method"])
    op.create_table(
        "wire_reconstruction_parameters",
        sa.Column("reconstruction_id", sa.Integer(), nullable=False),
        sa.Column("filename_prefixes", sa.JSON(), nullable=False),
        sa.Column("geometry_file", sa.String(), nullable=False),
        sa.Column("percent_brightest", sa.Float(), nullable=False),
        sa.Column("wire_edges", sa.String(), nullable=False),
        sa.Column("depth_start", sa.Float(), nullable=False),
        sa.Column("depth_end", sa.Float(), nullable=False),
        sa.Column("depth_resolution", sa.Float(), nullable=False),
        sa.Column("frame_skip", sa.Integer(), nullable=True),
        sa.Column("wire_skip", sa.Integer(), nullable=True),
        sa.Column("num_threads", sa.Integer(), nullable=False),
        sa.Column("memory_limit_mb", sa.Integer(), nullable=False),
        sa.Column("scan_points", sa.String(), nullable=False),
        sa.Column("scan_points_len", sa.Integer(), nullable=False),
        sa.Column("verbose", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("reconstruction_id"),
        sa.ForeignKeyConstraint(["reconstruction_id"], ["reconstruction_run.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "lauego_indexing_parameters",
        sa.Column("indexing_id", sa.Integer(), nullable=False),
        sa.Column("filename_prefixes", sa.JSON(), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=True),
        sa.Column("threshold_ratio", sa.Integer(), nullable=True),
        sa.Column("max_rfactor", sa.Float(), nullable=False),
        sa.Column("box_size", sa.Integer(), nullable=False),
        sa.Column("max_number", sa.Integer(), nullable=True),
        sa.Column("min_separation", sa.Integer(), nullable=False),
        sa.Column("peak_shape", sa.String(), nullable=False),
        sa.Column("scan_points", sa.String(), nullable=False),
        sa.Column("scan_points_len", sa.Integer(), nullable=False),
        sa.Column("depth_range", sa.String(), nullable=True),
        sa.Column("depth_range_len", sa.Integer(), nullable=True),
        sa.Column("detector_crop_x1", sa.Integer(), nullable=False),
        sa.Column("detector_crop_x2", sa.Integer(), nullable=False),
        sa.Column("detector_crop_y1", sa.Integer(), nullable=False),
        sa.Column("detector_crop_y2", sa.Integer(), nullable=False),
        sa.Column("min_size", sa.Float(), nullable=False),
        sa.Column("max_peaks", sa.Integer(), nullable=False),
        sa.Column("smooth", sa.Boolean(), nullable=False),
        sa.Column("mask_file", sa.String(), nullable=True),
        sa.Column("index_kev_max_calc", sa.Float(), nullable=False),
        sa.Column("index_kev_max_test", sa.Float(), nullable=False),
        sa.Column("index_angle_tolerance", sa.Float(), nullable=False),
        sa.Column("index_h", sa.Integer(), nullable=False),
        sa.Column("index_k", sa.Integer(), nullable=False),
        sa.Column("index_l", sa.Integer(), nullable=False),
        sa.Column("index_cone", sa.Float(), nullable=False),
        sa.Column("energy_unit", sa.String(), nullable=False),
        sa.Column("exposure_unit", sa.String(), nullable=False),
        sa.Column("cosmic_filter", sa.Boolean(), nullable=False),
        sa.Column("reciprocal_lattice_unit", sa.String(), nullable=False),
        sa.Column("lattice_parameters_unit", sa.String(), nullable=False),
        sa.Column("output_xml", sa.String(), nullable=True),
        sa.Column("geometry_file", sa.String(), nullable=False),
        sa.Column("crystal_file", sa.String(), nullable=False),
        sa.Column("depth", sa.String(), nullable=True),
        sa.Column("beamline", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("indexing_id"),
        sa.ForeignKeyConstraint(["indexing_id"], ["indexing_run.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("lauego_indexing_parameters")
    op.drop_table("wire_reconstruction_parameters")
    op.drop_index(op.f("ix_indexing_run_method"), table_name="indexing_run")
    op.drop_index(op.f("ix_indexing_run_reconstruction_id"), table_name="indexing_run")
    op.drop_index(op.f("ix_indexing_run_scan_number"), table_name="indexing_run")
    op.drop_table("indexing_run")
    op.drop_table("scan")
    op.drop_index(op.f("ix_reconstruction_run_method"), table_name="reconstruction_run")
    op.drop_index(op.f("ix_reconstruction_run_scan_number"), table_name="reconstruction_run")
    op.drop_table("reconstruction_run")
    op.drop_table("catalog")
    op.drop_table("calib")
    op.drop_table("metadata")
    op.drop_table("job")
