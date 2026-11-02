"""v1 ``3ad1557`` (``bdp_integration``): the five job columns of remote execution.

``backend``, ``submission_id``, ``external_id``, ``external_state`` and ``external_url``: nullable
``VARCHAR`` columns (a NULL ``backend`` is a local run, as in v1), placed where v1's ``create_all`` puts
them, after ``queue_job_id``. An empty database upgraded to this revision stores exactly v1
``3ad1557``'s DDL, object for object (``tests/fixtures/v1_schema_3ad1557.json``).

A database that already has v1 ``3ad1557``'s schema is stamped, not upgraded (``laueportal db stamp
0002``, or ``--detect``). That includes a ``dde1bde`` database that v1 ``3ad1557``'s own
``scripts/migrate_workflow_database.py`` upgraded: it appends the five columns at the end of ``job``
(``ALTER TABLE ... ADD COLUMN``). Column order is not part of the schema checks, and the downgrade takes
the columns out wherever they are.

SQLite adds a column only at the end, so ``job`` is rebuilt from **its own stored DDL** with the five
column definitions inserted after ``queue_job_id``; the downgrade removes them from the stored DDL
again. Editing the stored statement, instead of writing the table out here, keeps everything else as it
was: production's ``job`` (made by v1's migration script) has a quoted name and its two CHECK
constraints in the other order, and 0001 -> 0002 -> 0001 gives back its text unchanged.

The rows go through a temporary table: copied out, ``job`` dropped and created again under its own name,
copied back. SQLite's create-copy-drop-rename procedure (0004's ``_rebuild``) would store the renamed
table as ``CREATE TABLE "job"``, which is no longer ``create_all``'s DDL byte for byte. Foreign keys are
off during migrations, so dropping ``job`` changes nothing in the tables that reference it, and they
reference the new table. ``job`` has no indexes or triggers in v1; any that exist are recreated.

The downgrade refuses, changing nothing, while a job holds a remote run's identity (a ``backend`` other
than ``local``, or a submission or external value): v1 ``dde1bde`` has no columns for it.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16
"""

from __future__ import annotations

from typing import NamedTuple

from alembic import op
from alembic.util import CommandError

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TABLE = "job"
AFTER = "queue_job_id"
REMOTE_COLUMNS = ("backend", "submission_id", "external_id", "external_state", "external_url")
"""v1 ``3ad1557``'s ``Job`` columns, in its order: ``mapped_column(String, nullable=True)`` (``VARCHAR``)."""
COLUMN_TYPE = "VARCHAR"
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
    """Replace ``job`` by a table created with ``sql`` (its own name, exactly that text) holding the same
    rows; ``columns`` are copied back, the others are NULL."""
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


def upgrade() -> None:
    columns = _column_names()
    present = [column for column in REMOTE_COLUMNS if column in columns]
    if present:
        raise CommandError(
            f"revision {revision} adds {', '.join(REMOTE_COLUMNS)} to {TABLE}, but it already has "
            f"{', '.join(present)}. A database at v1 3ad1557's schema is stamped with 0002, not upgraded. "
            "Nothing was changed."
        )
    sql = _stored_sql()
    parts = _parts(sql)
    position = next((index for index, part in enumerate(parts) if part.name == AFTER), None)
    if position is None:
        raise CommandError(f"revision {revision}: {TABLE} has no column {AFTER}. Nothing was changed.")
    anchor = parts[position]
    # The separator that follows queue_job_id in the stored text (create_all writes ", \n\t").
    separator = sql[anchor.end : parts[position + 1].start] if position + 1 < len(parts) else ", "
    inserted = "".join(f"{separator}{column} {COLUMN_TYPE}" for column in REMOTE_COLUMNS)
    _rebuild(sql[: anchor.end] + inserted + sql[anchor.end :], columns)


def downgrade() -> None:
    bind = op.get_bind()
    held = " OR ".join(f"{column} IS NOT NULL" for column in REMOTE_COLUMNS[1:])
    remote = [
        row[0]
        for row in bind.exec_driver_sql(
            f"SELECT job_id FROM {TABLE} WHERE (backend IS NOT NULL AND backend != 'local') OR {held} ORDER BY job_id"
        )
    ]
    if remote:
        shown = ", ".join(str(job_id) for job_id in remote[:20])
        raise CommandError(
            f"revision {revision}'s downgrade drops {TABLE}'s remote-execution columns, but {len(remote)} jobs "
            f"are remote runs ({shown}{', ...' if len(remote) > 20 else ''}). Nothing was changed."
        )
    sql = _stored_sql()
    parts = _parts(sql)
    found = [index for index, part in enumerate(parts) if part.name in REMOTE_COLUMNS]
    missing = [column for column in REMOTE_COLUMNS if column not in {parts[index].name for index in found}]
    if missing:
        raise CommandError(
            f"revision {revision}'s downgrade: {TABLE} has no {', '.join(missing)}. Nothing was changed."
        )
    if found[0] == 0:  # each goes with the separator before it, so none can be the first part
        raise CommandError(f"revision {revision}'s downgrade: cannot read the {TABLE} table's DDL: {sql}")
    edited = sql
    for index in reversed(found):  # each with the separator before it; later ones first, so offsets hold
        edited = edited[: parts[index - 1].end] + edited[parts[index].end :]
    _rebuild(edited, [column for column in _column_names() if column not in REMOTE_COLUMNS])
