"""``indexing_run.selected_xml``: the Output XML a person chose for the results viewer.

When a run directory holds several XML files and the run records none that exists, the artifact is ambiguous
and the viewer asks which one holds the results (``POST /api/v1/indexing-runs/{id}/artifact/select-xml``). The
choice, a file name in the run directory, is kept here rather than in ``lauego_indexing_parameters.output_xml``,
which records what the run was submitted with. A nullable ``VARCHAR``, placed where ``create_all`` puts it: after
``results_path``.

SQLite adds a column only at the end, so ``indexing_run`` is rebuilt from **its own stored DDL** with the column
definition inserted after ``results_path`` (0002's procedure for ``job``: the rows go through a temporary table,
the table is created again under its own name and its indexes are recreated; foreign keys are off during
migrations, so the tables that reference it keep referencing it). The downgrade takes the definition out of the
stored DDL again, so 0005 -> 0006 -> 0005 gives back the table's text unchanged. It refuses, changing nothing,
while a run holds a choice: it would be lost.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-21
"""

from __future__ import annotations

from typing import NamedTuple

from alembic import op
from alembic.util import CommandError

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

TABLE = "indexing_run"
AFTER = "results_path"
COLUMN = "selected_xml"
COLUMN_TYPE = "VARCHAR"
ROWS = "_alembic_indexing_run_rows"
"""The temporary table (in SQLite's ``temp`` schema) that holds the rows while ``indexing_run`` is rebuilt."""


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
    """Replace ``indexing_run`` by a table created with ``sql`` (its own name, exactly that text) holding the same
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
    if COLUMN in columns:
        raise CommandError(f"revision {revision}: {TABLE} already has {COLUMN}. Nothing was changed.")
    sql = _stored_sql()
    parts = _parts(sql)
    position = next((index for index, part in enumerate(parts) if part.name == AFTER), None)
    if position is None:
        raise CommandError(f"revision {revision}: {TABLE} has no column {AFTER}. Nothing was changed.")
    anchor = parts[position]
    # The separator that follows results_path in the stored text (create_all writes ", \n\t").
    separator = sql[anchor.end : parts[position + 1].start] if position + 1 < len(parts) else ", "
    _rebuild(sql[: anchor.end] + f"{separator}{COLUMN} {COLUMN_TYPE}" + sql[anchor.end :], columns)


def downgrade() -> None:
    bind = op.get_bind()
    chosen = [row[0] for row in bind.exec_driver_sql(f"SELECT id FROM {TABLE} WHERE {COLUMN} IS NOT NULL ORDER BY id")]
    if chosen:
        shown = ", ".join(f"I{run_id}" for run_id in chosen[:20])
        raise CommandError(
            f"revision {revision}'s downgrade drops {TABLE}.{COLUMN}, but {len(chosen)} runs hold a chosen XML "
            f"({shown}{', ...' if len(chosen) > 20 else ''}). Nothing was changed."
        )
    sql = _stored_sql()
    parts = _parts(sql)
    position = next((index for index, part in enumerate(parts) if part.name == COLUMN), None)
    if position is None:
        raise CommandError(f"revision {revision}'s downgrade: {TABLE} has no {COLUMN}. Nothing was changed.")
    if position == 0:  # it goes with the separator before it, so it cannot be the first part
        raise CommandError(f"revision {revision}'s downgrade: cannot read the {TABLE} table's DDL: {sql}")
    edited = sql[: parts[position - 1].end] + sql[parts[position].end :]
    _rebuild(edited, [column for column in _column_names() if column != COLUMN])
