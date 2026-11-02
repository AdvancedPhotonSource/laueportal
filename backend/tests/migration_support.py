"""Helpers for the migration tests: v1's schema fixture, schema dumps, and databases at a revision."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from laueportal.core import migrations
from laueportal.core.settings import DatabaseSettings

FIXTURES = Path(__file__).parent / "fixtures"
V1_SCHEMAS = {"0001": "dde1bde", "0002": "3ad1557"}
"""The v1 commit whose ``create_all`` each v1 revision builds (``fixtures/v1_schema_<commit>.json``)."""
REMOTE_COLUMNS = ("backend", "submission_id", "external_id", "external_state", "external_url")
"""v1 ``3ad1557``'s job columns (revision 0002), in its order."""
QUEUE_COLUMNS = ("moved_to_front_at", "process_host", "process_pid", "process_since_boot")
"""Revision 0007's job columns, in their order."""
NEW_TABLES_0007 = ("conversion", "idempotency_key", "worker")
"""Revision 0007's tables."""

TIMESTAMP_COLUMNS = {
    "job": ("submit_time", "start_time", "finish_time", "updated_at", "heartbeat_at", "cancel_requested_at"),
    "indexing_run": ("created_at",),
    "reconstruction_run": ("created_at",),
    "metadata": ("time",),
}
"""Every DATETIME column of v1's schema (after 0003's renames): what revision 0005 converts."""


def v1_schema_fixture(commit: str = "dde1bde") -> dict[str, Any]:
    return json.loads((FIXTURES / f"v1_schema_{commit}.json").read_text())


def v1_schema_objects(commit: str = "dde1bde") -> list[dict[str, Any]]:
    """v1 ``commit``'s ``create_all``: ``sqlite_master`` rows (type, name, tbl_name, sql) by type and name."""
    return v1_schema_fixture(commit)["objects"]


def schema_objects(path: Path) -> list[dict[str, Any]]:
    """``sqlite_master`` rows of a database, as in the fixture, without Alembic's version table."""
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE tbl_name != ? ORDER BY type, name",
            (migrations.VERSION_TABLE,),
        ).fetchall()
    finally:
        connection.close()
    return [{"type": kind, "name": name, "tbl_name": table, "sql": sql} for kind, name, table, sql in rows]


def normalize_sql(sql: str | None) -> str | None:
    """Unquote lower-case identifiers. SQLite keeps an identifier's quotes when it renames a column or a
    table (``"scanNumber"`` becomes ``"scan_number"``) where ``create_all`` writes ``scan_number``."""
    return None if sql is None else re.sub(r'"([a-z_][a-z0-9_]*)"', r"\1", sql)


def normalized(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**item, "sql": normalize_sql(item["sql"])} for item in objects]


def create_v1_database(path: Path, commit: str = "dde1bde", *, without: tuple[str, ...] = ()) -> Path:
    """A database as v1 ``commit`` created it (its DDL, no Alembic revision): tables first, then indexes; ``without``
    the tables named there and their indexes (``create_v1_cb1159c_database``)."""
    objects = [
        item for item in v1_schema_objects(commit) if item["sql"] is not None and item["tbl_name"] not in without
    ]
    connection = sqlite3.connect(path)
    try:
        for kind in ("table", "index"):
            for item in objects:
                if item["type"] == kind:
                    connection.execute(item["sql"])
        connection.commit()
    finally:
        connection.close()
    return path


CB1159C_COLUMN = "remote_observation JSON"
"""v1 ``cb1159c``'s job column (revision 0008), as its ``create_all`` and ``scripts/workflow_migration.py`` write it."""
CB1159C_LIVE_TABLES = ("live_checkpoint", "live_point", "live_session")
"""v1 ``cb1159c``'s live tables (``fixtures/v1_schema_cb1159c.json``), which ``db stamp --detect`` drops."""


def create_v1_cb1159c_database(path: Path, *, appended: bool = False) -> Path:
    """v1 ``cb1159c``'s tables but its live tables, as v1 ``cb1159c``'s ``scripts/migrate_workflow_database.py`` leaves
    a database (it never creates them; ``db stamp --detect`` stamps it 0002, the column kept): ``cb1159c``'s own
    ``create_all`` (``fixtures/v1_schema_cb1159c.json``, ``job.remote_observation`` after ``external_url``; the
    script's ``job`` from a legacy database, which it rebuilds from ``cb1159c``'s models) or, ``appended``, v1
    ``3ad1557``'s schema with the column where the script adds it to an older database (``ALTER TABLE ... ADD
    COLUMN``, last)."""
    if appended:
        create_v1_database(path, "3ad1557")
        execute(DatabaseSettings(path=path), f'ALTER TABLE "job" ADD COLUMN {CB1159C_COLUMN}')
        return path
    return create_v1_database(path, "cb1159c", without=CB1159C_LIVE_TABLES)


def add_v1_live_tables(database: DatabaseSettings) -> None:
    """v1 ``cb1159c``'s live tables and their indexes (``fixtures/v1_schema_cb1159c.json``), as v1's app adds them to a
    database at its start (``init_db``'s ``create_all``; v1's migration script never creates them). With
    ``create_v1_cb1159c_database``: a database as v1 ``cb1159c``'s app leaves it, whose live tables ``db stamp
    --detect`` drops."""
    live = [item for item in v1_schema_objects("cb1159c") if item["tbl_name"] in CB1159C_LIVE_TABLES and item["sql"]]
    for kind in ("table", "index"):
        for item in live:
            if item["type"] == kind:
                execute(database, item["sql"])


def add_remote_columns_at_the_end(database: DatabaseSettings) -> str:
    """Upgrade a v1 ``dde1bde`` database to v1 ``3ad1557``'s schema as v1's own migration script does it (its
    stage "upgrade": ``ALTER TABLE "job" ADD COLUMN <name> VARCHAR``, appended after the last column).
    Returns the job table's DDL as SQLite stores it."""
    for column in REMOTE_COLUMNS:
        execute(database, f'ALTER TABLE "job" ADD COLUMN {column} VARCHAR')
    [(sql,)] = execute(database, "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'job'")
    return sql


def database_at(tmp_path: Path, revision: str, *, name: str = "migrated.db", **settings: Any) -> DatabaseSettings:
    """A new database upgraded to ``revision``."""
    database = DatabaseSettings(path=tmp_path / name, **settings)
    migrations.upgrade(database, revision)
    return database


def database_with_early_numbering(
    tmp_path: Path, recorded: str, schema: str, *, name: str = "early.db", **settings: Any
) -> DatabaseSettings:
    """A database migrated with the early numbering of the revisions: revision ``schema``'s tables without 0002's job
    columns, with ``recorded`` as its revision (a development database of that numbering: ``recorded="0004"``,
    ``schema="0005"``)."""
    database = database_at(tmp_path, schema, name=name, **settings)
    for column in REMOTE_COLUMNS:
        execute(database, f'ALTER TABLE "job" DROP COLUMN {column}')
    execute(database, "UPDATE alembic_version SET version_num = ?", (recorded,))
    return database


def execute(database: DatabaseSettings, sql: str, parameters: Any = ()) -> list[tuple[Any, ...]]:
    """Run one statement on a plain connection (foreign keys off, as v1's raw data would be) and commit."""
    connection = sqlite3.connect(database.path)
    try:
        rows = connection.execute(sql, parameters).fetchall()
        connection.commit()
    finally:
        connection.close()
    return rows


def columns(database: DatabaseSettings, table: str) -> list[str]:
    return [row[1] for row in execute(database, f'PRAGMA table_info("{table}")')]


def rebuild_table(database: DatabaseSettings, table: str, edit: Callable[[str], str]) -> str:
    """Rebuild ``table`` from its DDL changed by ``edit``, keeping its rows and indexes (SQLite's documented
    procedure: create, copy, drop, rename; foreign keys off). Returns the new DDL as SQLite stores it.

    It makes databases that a careless repair or another tool could leave behind: a schema that differs
    from head only where Alembic's ``compare_metadata`` does not look (CHECK constraints, server defaults).
    """
    connection = sqlite3.connect(database.path)
    try:
        [(sql,)] = connection.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,))
        indexes = [
            row[0]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL", (table,)
            )
        ]
        edited = edit(sql)
        assert edited != sql, "the edit changed nothing"
        rebuilt = f"{table}_rebuilt"
        created, count = re.subn(rf'^CREATE TABLE "?{table}"?', f'CREATE TABLE "{rebuilt}"', edited)
        assert count == 1, edited
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN")
        connection.execute(created)
        connection.execute(f'INSERT INTO "{rebuilt}" SELECT * FROM "{table}"')
        connection.execute(f'DROP TABLE "{table}"')
        connection.execute(f'ALTER TABLE "{rebuilt}" RENAME TO "{table}"')
        for index in indexes:
            connection.execute(index)
        connection.commit()
        [(stored,)] = connection.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,))
    finally:
        connection.close()
    return stored


def journal_header(path: Path) -> tuple[int, int]:
    """The file format write/read versions in the database header: (1, 1) rollback journal, (2, 2) WAL.

    Read from the file's bytes, so looking never changes anything (``PRAGMA journal_mode=...`` would)."""
    with path.open("rb") as stream:
        header = stream.read(20)
    return header[18], header[19]


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
