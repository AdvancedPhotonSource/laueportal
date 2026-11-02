"""``job.remote_observation``: a ``crux`` run's latest remote observation, v1 ``cb1159c``'s column with its name,
type and place: a nullable ``JSON`` after ``external_url``, where ``create_all`` puts it.

**Added only where absent.** A v1 ``cb1159c`` database already has the column: ``cb1159c``'s ``create_all`` writes it
after ``external_url``, and v1's ``scripts/workflow_migration.py`` appends it to an older database
(``ALTER TABLE ... ADD COLUMN``). ``db stamp --detect`` stamps such a database 0002 with the column kept, and the chain
keeps it to here: 0003 renames columns of other tables in place, 0004 and 0006 rebuild ``reconstruction_run`` and
``indexing_run`` only, 0005 converts the values of its named DATETIME columns, and 0007 rebuilds ``job`` from its own
stored DDL, copying every column (``tests/test_migrations.py`` follows such databases through the chain). So this
revision leaves a ``job`` that has the column as it is, with its observations (refused, changing nothing, when the
column is there with another type or NOT NULL), and otherwise adds it, NULL in every row.

SQLite adds a column only at the end, so ``job`` is rebuilt from **its own stored DDL** with the definition inserted
after ``external_url`` (0006's and 0007's procedure: the rows go through a temporary table, the table is created
again under its own name and its indexes are recreated; foreign keys are off during migrations, so the tables that
reference it keep referencing it). The downgrade takes the definition out of the stored DDL again, so 0007 -> 0008
-> 0007 gives back the table's text unchanged. It drops the observations, a display of where an active run was that
the code of revision 0007 neither reads nor needs, also those of a ``cb1159c`` database (0007's schema has no such
column).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-25
"""

from __future__ import annotations

from typing import NamedTuple

from alembic import op
from alembic.util import CommandError

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

TABLE = "job"
AFTER = "external_url"
COLUMN = "remote_observation"
COLUMN_TYPE = "JSON"
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


def _columns() -> dict[str, tuple[str, bool]]:
    """``job``'s columns: their declared type (upper case) and whether they are NOT NULL."""
    rows = op.get_bind().exec_driver_sql(f'PRAGMA table_info("{TABLE}")').fetchall()
    return {str(row[1]): (str(row[2]).upper(), bool(row[3])) for row in rows}


def upgrade() -> None:
    columns = _columns()
    if COLUMN in columns:  # a v1 cb1159c database: its column and observations stay
        if columns[COLUMN] != (COLUMN_TYPE, False):
            kind, not_null = columns[COLUMN]
            raise CommandError(
                f"revision {revision}: {TABLE}.{COLUMN} is {kind}{' NOT NULL' if not_null else ''}, not v1 cb1159c's "
                f"nullable {COLUMN_TYPE}. Nothing was changed."
            )
        return
    sql = _stored_sql()
    parts = _parts(sql)
    position = next((index for index, part in enumerate(parts) if part.name == AFTER), None)
    if position is None:
        raise CommandError(f"revision {revision}: {TABLE} has no column {AFTER}. Nothing was changed.")
    anchor = parts[position]
    # The separator that follows external_url in the stored text (create_all writes ", \n\t").
    separator = sql[anchor.end : parts[position + 1].start] if position + 1 < len(parts) else ", "
    _rebuild(sql[: anchor.end] + f"{separator}{COLUMN} {COLUMN_TYPE}" + sql[anchor.end :], list(columns))


def downgrade() -> None:
    sql = _stored_sql()
    parts = _parts(sql)
    position = next((index for index, part in enumerate(parts) if part.name == COLUMN), None)
    if position is None:
        raise CommandError(f"revision {revision}'s downgrade: {TABLE} has no {COLUMN}. Nothing was changed.")
    if position == 0:  # it goes with the separator before it, so it cannot be the first part
        raise CommandError(f"revision {revision}'s downgrade: cannot read the {TABLE} table's DDL: {sql}")
    edited = sql[: parts[position - 1].end] + sql[parts[position].end :]
    _rebuild(edited, [column for column in _columns() if column != COLUMN])
