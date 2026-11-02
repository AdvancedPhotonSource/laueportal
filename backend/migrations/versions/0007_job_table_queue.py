"""The job table's queue: the idempotency keys, conversions and the worker in SQLite, and the job columns of the
queue that replaces RQ.

- ``idempotency_key``: a create endpoint's ``Idempotency-Key`` (``key``, ``<route>:<SHA-256 of the key>``, the
  primary key), the body's ``fingerprint``, its ``state`` (``pending``, ``kept``), ``claimed_at``, the kept
  ``answer`` (JSON, null while pending) and ``expires_at`` (indexed: the reconciler's purge). Used from this
  revision on (``jobs.idempotency``).
- ``conversion``: one results conversion per indexing run (``indexing_id``, primary key and foreign key to
  ``indexing_run``), its ``status`` (``queued``, ``running``, ``finished``, ``failed``), ``requested_at``,
  ``started_at``, ``ended_at``, ``message`` and its job process (``process_host``, ``process_pid``,
  ``process_since_boot``).
- ``worker``: the worker (``name``, primary key), its ``host``, ``pid``, ``started_at``, ``stopped_at`` and each
  thread's last cycle (``dispatcher_at``, ``reconciler_at``, ``poller_at``).
- ``job.moved_to_front_at`` (Move to Front), ``job.process_host``, ``job.process_pid``, ``job.process_since_boot``
  (the job process the dispatcher started), all nullable.

The new tables are created as ``create_all`` creates them. ``job`` is rebuilt from **its own stored DDL** with the
four definitions inserted after its last column definition, before the table constraints and separated as the
stored text separates its parts: where and as ``create_all`` writes them (``ALTER TABLE ... ADD COLUMN`` would
separate them with a bare ``", "``). The rebuild is 0006's procedure: the rows go through a temporary table, the
table is created again under its own name and its indexes are recreated; foreign keys are off during migrations,
so the tables that reference it keep referencing it. The downgrade takes the definitions out of the
stored DDL again, so 0006 -> 0007 -> 0006 gives back the table's text unchanged, and drops the three tables with
what they hold: the keys, conversion and worker records and the four columns' values are the worker's and the
create endpoints' working state, which the code of revision 0006 neither reads nor needs (its keys are in Redis).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-24
"""

from __future__ import annotations

from typing import NamedTuple

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

TABLE = "job"
COLUMNS = (
    ("moved_to_front_at", "DATETIME"),
    ("process_host", "VARCHAR"),
    ("process_pid", "INTEGER"),
    ("process_since_boot", "FLOAT"),
)
"""The job columns, in ``create_all``'s order, with their DDL types."""
NEW_TABLES = ("idempotency_key", "conversion", "worker")
CONSTRAINT_WORDS = frozenset({"CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN"})
"""The first word of a table constraint in a ``CREATE TABLE`` statement (a column named so would be quoted)."""
ROWS = "_alembic_job_rows"
"""The temporary table (in SQLite's ``temp`` schema) that holds the rows while ``job`` is rebuilt."""


class _Part(NamedTuple):  # not a dataclass: Alembic loads revisions outside sys.modules
    """A column definition or table constraint in a ``CREATE TABLE`` statement: ``sql[start:end]``."""

    name: str
    start: int
    end: int


def _stored_sql() -> str:
    rows = (
        op.get_bind()
        .exec_driver_sql("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,))
        .fetchall()
    )
    if len(rows) != 1:
        raise CommandError(f"revision {revision}: the database has no {TABLE} table")
    return str(rows[0][0])


def _unquoted(name: str) -> str:
    if len(name) >= 2 and (name[0], name[-1]) in {('"', '"'), ("`", "`"), ("[", "]")}:
        return name[1:-1]
    return name


def _parts(sql: str) -> list[_Part]:
    """The column definitions and table constraints of ``sql``, in order, without their separators."""
    opening = sql.find("(")
    if opening < 0 or not sql.rstrip().endswith(")"):
        raise CommandError(f"revision {revision}: cannot read the {TABLE} table's DDL: {sql}")
    closing = len(sql.rstrip()) - 1
    parts: list[_Part] = []
    depth, quote, start = 0, "", opening + 1
    for index in range(opening + 1, closing + 1):
        char = sql[index]
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "'\"`":
            quote = char
        elif char == "[":
            quote = "]"
        elif char == "(":
            depth += 1
        elif char == ")" and depth > 0:
            depth -= 1
        elif char in ",)" and depth == 0:  # the closing parenthesis ends the last part
            text = sql[start:index]
            if text.strip():
                first = len(text) - len(text.lstrip())
                begin, end = start + first, start + len(text.rstrip())
                parts.append(_Part(_unquoted(sql[begin:end].split()[0]), begin, end))
            start = index + 1
    return parts


def _rebuild(sql: str, columns: list[str]) -> None:
    """Replace ``job`` by a table created with ``sql`` (its own name, exactly that text) holding the same rows;
    ``columns`` are copied back, the others are NULL."""
    bind = op.get_bind()
    others = [
        str(row[0])
        for row in bind.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE tbl_name = ? AND type IN ('index', 'trigger') AND sql IS NOT NULL",
            (TABLE,),
        )
    ]
    names = ", ".join(f'"{column}"' for column in columns)
    bind.exec_driver_sql(f'CREATE TEMP TABLE "{ROWS}" AS SELECT * FROM "{TABLE}"')
    bind.exec_driver_sql(f'DROP TABLE "{TABLE}"')
    bind.exec_driver_sql(sql)
    bind.exec_driver_sql(f'INSERT INTO "{TABLE}" ({names}) SELECT {names} FROM temp."{ROWS}"')
    bind.exec_driver_sql(f'DROP TABLE temp."{ROWS}"')
    for statement in others:
        bind.exec_driver_sql(statement)


def _column_names() -> list[str]:
    return [str(row[1]) for row in op.get_bind().exec_driver_sql(f'PRAGMA table_info("{TABLE}")')]


def _table_names() -> set[str]:
    return {str(row[0]) for row in op.get_bind().exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _add_job_columns() -> None:
    columns = _column_names()
    present = [name for name, _ in COLUMNS if name in columns]
    if present:
        raise CommandError(f"revision {revision}: {TABLE} already has {', '.join(present)}. Nothing was changed.")
    sql = _stored_sql()
    parts = _parts(sql)
    definitions = [
        index
        for index, part in enumerate(parts)
        if sql[part.start : part.end].split()[0].upper() not in CONSTRAINT_WORDS
    ]
    if not definitions:
        raise CommandError(f"revision {revision}: cannot read the {TABLE} table's DDL: {sql}")
    position = definitions[-1]
    anchor = parts[position]
    # The separator that follows the last column in the stored text (create_all writes ", \n\t").
    separator = sql[anchor.end : parts[position + 1].start] if position + 1 < len(parts) else ", "
    added = "".join(f"{separator}{name} {kind}" for name, kind in COLUMNS)
    _rebuild(sql[: anchor.end] + added + sql[anchor.end :], columns)


def _drop_job_columns() -> None:
    sql = _stored_sql()
    edited = sql
    for name, _ in reversed(COLUMNS):  # each goes with the separator before it
        parts = _parts(edited)
        position = next((index for index, part in enumerate(parts) if part.name == name), None)
        if position is None:
            raise CommandError(f"revision {revision}'s downgrade: {TABLE} has no {name}. Nothing was changed.")
        if position == 0:
            raise CommandError(f"revision {revision}'s downgrade: cannot read the {TABLE} table's DDL: {sql}")
        edited = edited[: parts[position - 1].end] + edited[parts[position].end :]
    dropped = {name for name, _ in COLUMNS}
    _rebuild(edited, [column for column in _column_names() if column not in dropped])


def upgrade() -> None:
    existing = sorted(_table_names() & set(NEW_TABLES))
    if existing:
        raise CommandError(f"revision {revision}: the database already has {', '.join(existing)}. Nothing was changed.")
    _add_job_columns()
    op.create_table(
        "idempotency_key",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(), nullable=False),
        sa.Column("answer", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index(op.f("ix_idempotency_key_expires_at"), "idempotency_key", ["expires_at"])
    op.create_table(
        "conversion",
        sa.Column("indexing_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("message", sa.String(), nullable=True),
        sa.Column("process_host", sa.String(), nullable=True),
        sa.Column("process_pid", sa.Integer(), nullable=True),
        sa.Column("process_since_boot", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("indexing_id"),
        sa.ForeignKeyConstraint(["indexing_id"], ["indexing_run.id"]),
    )
    op.create_table(
        "worker",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("stopped_at", sa.DateTime(), nullable=True),
        sa.Column("dispatcher_at", sa.DateTime(), nullable=True),
        sa.Column("reconciler_at", sa.DateTime(), nullable=True),
        sa.Column("poller_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    missing = sorted(set(NEW_TABLES) - _table_names())
    if missing:
        raise CommandError(
            f"revision {revision}'s downgrade: the database has no {', '.join(missing)}. Nothing was changed."
        )
    _drop_job_columns()
    op.drop_table("worker")
    op.drop_table("conversion")
    op.drop_index(op.f("ix_idempotency_key_expires_at"), table_name="idempotency_key")
    op.drop_table("idempotency_key")
