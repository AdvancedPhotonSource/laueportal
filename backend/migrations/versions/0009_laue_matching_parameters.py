"""``laue_matching_indexing_parameters``: the parameters of a LaueMatching indexing run, one row per run.

``indexing_run.method`` has allowed ``laue_matching`` since the baseline, so ``indexing_run`` is not rebuilt. The
upgrade refuses a database that already has the table. The downgrade refuses, changing nothing, while the table holds a
run's parameters: a clone and the Parameters tab read them from here.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

TABLE = "laue_matching_indexing_parameters"


def _has_table() -> bool:
    return bool(
        op.get_bind()
        .exec_driver_sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,))
        .fetchone()
    )


def upgrade() -> None:
    if _has_table():
        raise CommandError(f"revision {revision}: the database already has {TABLE}. Nothing was changed.")
    op.create_table(
        TABLE,
        sa.Column("indexing_id", sa.Integer(), nullable=False),
        sa.Column("filename_prefixes", sa.JSON(), nullable=False),
        sa.Column("scan_points", sa.String(), nullable=False),
        sa.Column("scan_points_len", sa.Integer(), nullable=False),
        sa.Column("depth_range", sa.String(), nullable=True),
        sa.Column("depth_range_len", sa.Integer(), nullable=True),
        sa.Column("geometry_file", sa.String(), nullable=False),
        sa.Column("crystal_file", sa.String(), nullable=False),
        sa.Column("mask_file", sa.String(), nullable=True),
        sa.Column("background_file", sa.String(), nullable=True),
        sa.Column("forward_cache", sa.String(), nullable=False),
        sa.Column("create_forward_cache", sa.Boolean(), nullable=False),
        sa.Column("threshold_method", sa.String(), nullable=False),
        sa.Column("threshold_percentile", sa.Float(), nullable=False),
        sa.Column("threshold_value", sa.Float(), nullable=False),
        sa.Column("min_area", sa.Integer(), nullable=False),
        sa.Column("watershed_image", sa.Boolean(), nullable=False),
        sa.Column("elo", sa.Float(), nullable=False),
        sa.Column("ehi", sa.Float(), nullable=False),
        sa.Column("max_nr_laue_spots", sa.Integer(), nullable=False),
        sa.Column("min_nr_spots", sa.Integer(), nullable=False),
        sa.Column("min_good_spots", sa.Integer(), nullable=False),
        sa.Column("min_intensity", sa.Float(), nullable=False),
        sa.Column("max_angle", sa.Float(), nullable=False),
        sa.Column("robust_filter", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("indexing_id"),
        sa.ForeignKeyConstraint(["indexing_id"], ["indexing_run.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    if not _has_table():
        raise CommandError(f"revision {revision}'s downgrade: the database has no {TABLE}. Nothing was changed.")
    count = op.get_bind().exec_driver_sql(f"SELECT COUNT(*) FROM {TABLE}").scalar()
    if count:
        raise CommandError(
            f"revision {revision}'s downgrade: {TABLE} holds the parameters of {count} run(s). Nothing was changed."
        )
    op.drop_table(TABLE)
