"""Drop calib and the coded-aperture (CA) reconstruction method.

- ``calib`` is dropped. It belonged to CA reconstructions, which the portal does not have.
- ``reconstruction_run.method`` loses ``'ca'``: the CHECK constraint becomes ``method IN ('wire')``.

Both are refused, and nothing is changed, while data would be lost: a ``calib`` row, or a reconstruction
run with method ``'ca'``.

SQLite cannot alter a CHECK constraint, so ``reconstruction_run`` is rebuilt from the explicit definition
below (new table, copy, drop, rename); its rows, ids, indexes and the foreign keys pointing at it are kept.

``method`` stays extensible. A future reconstruction kind is added the same way: add the value to
``RECONSTRUCTION_METHODS`` in ``laueportal/reconstruction/models.py`` and write a migration that rebuilds
the table with the longer list (copy ``_rebuild`` from here). The indexing methods
(``INDEXING_METHODS``, ``ck_indexing_run_method``) work the same way.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

METHODS_BEFORE = ("wire", "ca")
METHODS_AFTER = ("wire",)

COLUMNS = (
    "id",
    "scan_number",
    "job_id",
    "method",
    "input_path",
    "output_path",
    "author",
    "notes",
    "algorithm_version",
    "created_at",
)


def _rebuild(methods: tuple[str, ...]) -> None:
    """Rebuild ``reconstruction_run`` with ``method`` limited to ``methods``.

    SQLite's procedure for a changed constraint: create the new table, copy the rows, drop the old table,
    rename the new one, recreate the indexes. v1's columns and constraint order, so the stored DDL is what
    the models' ``create_all`` writes. Foreign keys are off during migrations, so dropping the old table
    does not cascade into ``wire_reconstruction_parameters``, and the tables that reference
    ``reconstruction_run`` by name reference the new one.
    """
    allowed = ", ".join(f"'{method}'" for method in methods)
    temporary = "_alembic_tmp_reconstruction_run"
    op.create_table(
        temporary,
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
        sa.CheckConstraint(f"method IN ({allowed})", name=op.f("ck_reconstruction_run_method")),
        sa.ForeignKeyConstraint(["scan_number"], ["metadata.scan_number"]),
        sa.UniqueConstraint("job_id"),
        sa.ForeignKeyConstraint(["job_id"], ["job.job_id"]),
    )
    names = ", ".join(COLUMNS)
    op.execute(f"INSERT INTO {temporary} ({names}) SELECT {names} FROM reconstruction_run")
    op.drop_table("reconstruction_run")  # its indexes go with it
    op.rename_table(temporary, "reconstruction_run")
    op.create_index(op.f("ix_reconstruction_run_scan_number"), "reconstruction_run", ["scan_number"])
    op.create_index(op.f("ix_reconstruction_run_method"), "reconstruction_run", ["method"])


def _refuse_if_any(sql: str, what: str) -> None:
    rows = op.get_bind().exec_driver_sql(sql).fetchall()
    if rows:
        shown = ", ".join(str(row[0]) for row in rows[:20])
        raise CommandError(
            f"revision 0004 drops CA, but the database has {len(rows)} {what} ({shown}{', ...' if len(rows) > 20 else ''}). "
            "Nothing was changed. Export or delete them in v1 first."
        )


def upgrade() -> None:
    _refuse_if_any(
        "SELECT id FROM reconstruction_run WHERE method = 'ca' ORDER BY id", "reconstruction runs with method 'ca'"
    )
    _refuse_if_any("SELECT calib_id FROM calib ORDER BY calib_id", "calib rows")
    op.drop_table("calib")
    _rebuild(METHODS_AFTER)


def downgrade() -> None:
    _rebuild(METHODS_BEFORE)
    op.create_table(  # empty, as the upgrade found it; 0003's downgrade restores the camelCase names
        "calib",
        sa.Column("calib_id", sa.Integer(), nullable=False),
        sa.Column("scan_number", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("filefolder", sa.String(), nullable=False),
        sa.Column("filename_prefix", sa.JSON(), nullable=False),
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
        sa.ForeignKeyConstraint(["scan_number"], ["metadata.scan_number"]),
        sa.UniqueConstraint("job_id"),
        sa.ForeignKeyConstraint(["job_id"], ["job.job_id"]),
    )
