"""Alembic migrations of the portal database, driven from Python (``laueportal db ...``).

- ``upgrade``, ``downgrade``, ``stamp``, ``current_revision``, ``describe_current`` and ``check`` take the
  ``DatabaseSettings`` of the database to work on (``settings.database``, or one named on the command line).
- Alembic is configured here, not by an ``alembic.ini``: the scripts are ``backend/migrations`` of this
  checkout. The UTC revision reads ``database.legacy_timezone`` from the Alembic config
  (``config.attributes["legacy_timezone"]``).
- **Reading never changes the database.** ``current_revision``, ``describe_current``, ``check``, stamp's
  schema verification and what ``upgrade`` and ``downgrade`` refuse before migrating open the file read-only
  (``mode=ro``) and set no pragma, so they never write and never switch the journal mode (production's
  rollback-journal database on NFS stays as it is; a read-only file can be inspected). Only the commands
  that write connect through ``core.db.database_engine``, with the app's pragmas (``busy_timeout``, the
  configured journal mode).
- Every command that writes runs in **one** SQLite transaction (``BEGIN IMMEDIATE`` ... ``COMMIT``) with
  ``PRAGMA foreign_keys=OFF``. SQLite's DDL is transactional, so an upgrade that fails part-way (e.g. the
  ``'ca'`` guard of 0004) changes nothing; with foreign keys off, rebuilding a table (copy, drop, rename)
  never cascades deletes into its children. ``PRAGMA foreign_key_check`` must come back empty before the
  commit.
- ``upgrade`` only goes up: a target below the database's revision is refused (downgrades are tested
  through ``downgrade`` but not offered by the CLI).
- ``check`` compares the schema two ways: Alembic's ``compare_metadata`` against the models, and the
  database's DDL (``sqlite_master``) against a new database migrated to head, which also covers what
  ``compare_metadata`` ignores (CHECK constraints, server defaults). See ``ddl_differences``. It also runs
  SQLite's ``quick_check`` and ``foreign_key_check`` and, from revision 0005 on, verifies that every timestamp is
  stored as UTC (``timestamp_counts``); ``examine`` adds what it measured, for ``laueportal db check``.
- ``stamp`` refuses revision 0005 or later while a timestamp is not UTC (v1's naive local times: 0005's migration
  converts them), so a stamped database never fails on read.
- Nothing here creates a database except ``upgrade`` (which builds a new one from nothing).

How an existing v1 database moves to the portal: v1's ``scripts/migrate_workflow_database.py`` (legacy schema to
v1's schema), then ``laueportal db stamp`` with the revision of that v1 schema (``V1_REVISIONS``: 0001 for v1
``dde1bde``, 0002 for v1 ``3ad1557``; ``detect_v1_schema`` picks it, ``laueportal db stamp --detect``),
``laueportal db upgrade`` and ``laueportal db check``. ``laueportal db stamp --detect`` (``stamp_detected``) also
takes v1 ``cb1159c``'s schema (``3ad1557``'s with ``job.remote_observation`` and three live tables): it
refuses, changing nothing, while one of its live sessions has not ended (the portal does not continue v1-started live
sessions), else drops the live tables and records 0002 in one transaction; the column stays (revision 0008 adds it
only where absent). Without the live tables (v1 ``cb1159c``'s migration script never creates
them) it records 0002 and drops nothing.

A database migrated with the early numbering of the revisions (before v1 ``3ad1557``'s schema became 0002 and the
first cleanup revisions moved from 0002-0004 to 0003-0005) records a revision whose schema it does not have.
Every command refuses to migrate it, ``check`` and ``describe_current`` report it, and the message says to
rebuild it (``_early_numbering``).
"""

from __future__ import annotations

import re
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import Connection, create_engine, event, inspect
from sqlalchemy.pool import NullPool

from laueportal.core.db import BUSY_TIMEOUT_MS, database_engine, quick_check_errors
from laueportal.core.settings import DatabaseSettings
from laueportal.core.text import quoted

T = TypeVar("T")

MIGRATIONS_DIRECTORY = Path(__file__).resolve().parents[3] / "migrations"
"""``backend/migrations`` of the checkout this package runs from (the portal is deployed from a checkout)."""

BASELINE_REVISION = "0001"
"""v1 ``dde1bde``'s schema (the baseline)."""

V1_REVISIONS: dict[str, str] = {"0001": "v1 dde1bde", "0002": "v1 3ad1557"}
"""The revisions whose schema is a deployed v1's; an existing v1 database is stamped with one of them."""

STAMP_V1 = "`laueportal db stamp --detect` (0001 for v1 dde1bde's schema, 0002 for v1 3ad1557's)"
"""How a v1 database comes under Alembic, for messages."""

CB1159C = "v1 cb1159c"
"""v1's live processing and remote monitoring: ``3ad1557``'s schema with ``job.remote_observation`` and the
live tables (``tests/fixtures/v1_schema_cb1159c.json``), or without the live tables: its
``scripts/migrate_workflow_database.py`` gives a database the column and never the tables (only its app creates them,
at its start). No revision's: ``stamp_detected`` drops the live tables and records ``CB1159C_REVISION``."""
CB1159C_REVISION = "0002"
CB1159C_OBSERVATION = ("job", "remote_observation", ("JSON", False, 0))
"""v1 ``cb1159c``'s job column (table, name, ``table_columns``' entry): kept, revision 0008 adds it only where absent."""
CB1159C_LIVE_TABLES = ("live_point", "live_checkpoint", "live_session")
"""v1 ``cb1159c``'s live tables, in the order the stamp drops them (``live_session`` last: the others reference it).
Nothing in the portal reads them (its live runs are ordinary runs and jobs); the chain needs them gone (head has no
such table)."""
CB1159C_LIVE_DDL = (
    "CREATE TABLE live_session (id INTEGER NOT NULL, active_slot INTEGER, reconstruction_id INTEGER, indexing_id "
    "INTEGER, reconstruction_job_id INTEGER, indexing_job_id INTEGER, state VARCHAR NOT NULL, action VARCHAR, "
    "control_directory VARCHAR NOT NULL, recipe JSON NOT NULL, revision INTEGER NOT NULL, published_at DATETIME, "
    "heartbeat_at DATETIME, created_at DATETIME NOT NULL, error VARCHAR, PRIMARY KEY (id), UNIQUE (active_slot), "
    "UNIQUE (reconstruction_id), FOREIGN KEY(reconstruction_id) REFERENCES reconstruction_run (id), UNIQUE "
    "(indexing_id), FOREIGN KEY(indexing_id) REFERENCES indexing_run (id), UNIQUE (reconstruction_job_id), FOREIGN "
    "KEY(reconstruction_job_id) REFERENCES job (job_id), UNIQUE (indexing_job_id), FOREIGN KEY(indexing_job_id) "
    "REFERENCES job (job_id))",
    "CREATE TABLE live_checkpoint (session_id INTEGER NOT NULL, state JSON NOT NULL, PRIMARY KEY (session_id), "
    "FOREIGN KEY(session_id) REFERENCES live_session (id))",
    "CREATE TABLE live_point (id INTEGER NOT NULL, session_id INTEGER NOT NULL, point_number INTEGER NOT NULL, "
    "point_id VARCHAR NOT NULL, source VARCHAR NOT NULL, source_identity JSON NOT NULL, arrived_at DATETIME NOT NULL, "
    "task JSON, reconstruction_state VARCHAR NOT NULL, indexing_state VARCHAR NOT NULL, indexing_order INTEGER, "
    "indexing_inputs INTEGER NOT NULL, indexing_succeeded INTEGER NOT NULL, indexing_failed INTEGER NOT NULL, "
    "indexed_frames INTEGER NOT NULL, reconstruction_receipt JSON, indexing_receipt JSON, error VARCHAR, PRIMARY KEY "
    "(id), UNIQUE (session_id, source), UNIQUE (session_id, point_id), UNIQUE (session_id, point_number), FOREIGN "
    "KEY(session_id) REFERENCES live_session (id))",
    "CREATE INDEX ix_live_point_session_id ON live_point (session_id)",
)
"""The live tables and their index as v1 ``cb1159c``'s ``create_all`` makes them (``tests/test_migrations.py``
compares them with the fixture): what ``stamp_detected`` knows it drops."""
LIVE_SESSION_ENDED = ("finished", "failed", "cancelled")
"""An ended v1 live session's states (v1 ``cb1159c``'s ``workflows/live.py`` ``TERMINAL``); every other one (queued,
running, waiting, finishing, stopping, and recovery, which v1 can resume) is open."""

REMOTE_EXECUTION_REVISION = "0002"
REMOTE_EXECUTION_COLUMNS = ("backend", "submission_id", "external_id", "external_state", "external_url")
"""The ``job`` columns revision 0002 adds (v1 ``3ad1557``'s remote execution); every later revision keeps them."""

REBUILD = "rebuild it with `laueportal seed demo --force` (a demo database)"
"""What to do with a database migrated with the early numbering (``_early_numbering``), for messages."""

UTC_REVISION = "0005"
"""The revision that stores every ``UTCDateTime`` column as UTC text with ``Z`` (``0005_utc_timestamps``): a database
at it or later holds no other form (``timestamp_counts``)."""

UTC_STORAGE_GLOB = (
    "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z"
)
"""``core.timeutil.UTCDateTime``'s storage format on SQLite (``%Y-%m-%dT%H:%M:%S.%fZ``) as a GLOB pattern."""
UTC_STORAGE_EXAMPLE = "2026-10-15T12:34:56.000000Z"
"""A value in that format, for messages."""

VERSION_TABLE = "alembic_version"


class MigrationError(Exception):
    """A migration command refused or failed. The message says why and what to do."""


# ---------------------------------------------------------------------------
# Alembic configuration


def alembic_config(database: DatabaseSettings, connection: Connection | None = None) -> Config:
    """An Alembic ``Config`` for ``database`` (no ini file). ``connection`` is what ``env.py`` migrates."""
    if not (MIGRATIONS_DIRECTORY / "env.py").is_file():
        raise MigrationError(
            f"the Alembic scripts are missing: {MIGRATIONS_DIRECTORY} (run from a repository checkout)"
        )
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIRECTORY).replace("%", "%%"))
    config.attributes["connection"] = connection
    config.attributes["legacy_timezone"] = database.legacy_timezone
    return config


def script_directory() -> ScriptDirectory:
    return ScriptDirectory(str(MIGRATIONS_DIRECTORY))


def head_revision() -> str:
    head = script_directory().get_current_head()
    if head is None:
        raise MigrationError(f"no revisions in {MIGRATIONS_DIRECTORY / 'versions'}")
    return head


def revision_ids() -> list[str]:
    """Every revision, oldest first (the history is linear)."""
    return [script.revision for script in reversed(list(script_directory().walk_revisions()))]


def resolve_revision(revision: str) -> str | None:
    """The revision id that ``revision`` names (``head``, ``base``, ``0003``, ...); None for ``base``."""
    if revision == "base":
        return None
    try:
        script = script_directory().get_revision(revision)
    except CommandError as error:
        raise MigrationError(f"unknown revision {revision!r}: {error}") from None
    if script is None:
        raise MigrationError(f"unknown revision {revision!r}")
    return script.revision


# ---------------------------------------------------------------------------
# Connections


def _require_file(database: DatabaseSettings) -> None:
    if not database.path.is_file():  # SQLite would create an empty database on connect
        raise MigrationError(f"database {database.path} does not exist")


@contextmanager
def read_connection(database: DatabaseSettings) -> Iterator[Connection]:
    """A read-only connection (``mode=ro``) with no pragmas: nothing it does can change the file.

    Not ``core.db.database_engine``: its connect pragmas set the journal mode, which writes the file (a
    rollback-journal database would become WAL) and fails on a read-only file. A WAL database read this
    way may keep its ``-wal``/``-shm`` files afterwards, as SQLite documents for read-only connections.
    """
    _require_file(database)
    uri = f"{database.path.resolve().as_uri()}?mode=ro"

    def connect() -> sqlite3.Connection:
        # ``timeout`` is the busy timeout: a reader waits for a writer's lock instead of failing at once.
        return sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000, check_same_thread=False)

    engine = create_engine("sqlite://", creator=connect, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


@contextmanager
def _write_connection(database: DatabaseSettings) -> Iterator[Connection]:
    """One transaction for the whole command, foreign keys off, foreign-key check before the commit."""
    engine = database_engine(database)

    @event.listens_for(engine, "connect")
    def _migration_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        # After core.db's pragmas. foreign_keys can only change outside a transaction, so here.
        dbapi_connection.execute("PRAGMA foreign_keys=OFF")
        # pysqlite would commit before DDL on its own; with no implicit transactions, BEGIN below is ours.
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin(connection: Connection) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    try:
        with engine.begin() as connection:
            yield connection
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                shown = ", ".join(f"{table} row {rowid} -> {parent}" for table, rowid, parent, _ in violations[:10])
                raise MigrationError(
                    f"foreign key check failed after the migration ({len(violations)} rows: {shown}); nothing was changed"
                )
    finally:
        engine.dispose()


def _current(connection: Connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def _user_tables(connection: Connection) -> set[str]:
    return {name for name in inspect(connection).get_table_names() if name != VERSION_TABLE}


def _missing_remote_columns(connection: Connection, current: str | None) -> list[str]:
    """0002's ``job`` columns that a database at ``current`` lacks: empty unless the early numbering migrated it.

    The early numbering had the first cleanup revisions at 0002-0004; they are now 0003-0005, and 0002 is v1
    ``3ad1557``'s schema. A database that the early chain migrated (early development, e2e and snapshot-copy
    databases) records 0002, 0003 or 0004 but has none of the remote-execution columns. Under the new numbering its
    revision names a schema it does not have, and upgrading it would run the wrong migrations: 0005 again on the early
    head (recording head without the columns), 0003 or 0004 on its older revisions (failing on columns
    and tables they already changed). No database this checkout migrated is at 0002 or later without
    them: 0002 adds all five or nothing. A database without a ``job`` table is left to the other checks.
    """
    known = revision_ids()
    if current not in known or known.index(current) < known.index(REMOTE_EXECUTION_REVISION):
        return []
    if "job" not in _user_tables(connection):
        return []
    have = {row[1] for row in connection.exec_driver_sql('PRAGMA table_info("job")').fetchall()}
    return [column for column in REMOTE_EXECUTION_COLUMNS if column not in have]


def _early_numbering(database: DatabaseSettings, connection: Connection, current: str | None) -> str | None:
    """Why ``database`` (at ``current``) cannot be migrated because the early numbering did it; None when it can."""
    missing = _missing_remote_columns(connection, current)
    if not missing:
        return None
    return (
        f"database {database.path} is at revision {current}, but its job table lacks {', '.join(missing)}, "
        f"the columns of revision {REMOTE_EXECUTION_REVISION}: it was migrated with the early numbering of the "
        "revisions, before their 0002-0004 moved to 0003-0005 (0002 is now v1 3ad1557's remote-execution "
        f"columns). Its revision does not name its schema, so it cannot be migrated: {REBUILD}"
    )


# ---------------------------------------------------------------------------
# Commands


def current_revision(database: DatabaseSettings) -> str | None:
    """The database's revision, or None when it is not under Alembic (new, or a v1 database not stamped)."""
    with read_connection(database) as connection:
        return _current(connection)


def describe_current(database: DatabaseSettings) -> str:
    """The database's revision and what to do next (``laueportal db current``). Read-only."""
    head = head_revision()
    with read_connection(database) as connection:
        current = _current(connection)
        if current is None:
            if _user_tables(connection):
                return f"none (not under Alembic; head is {head}): a v1 database is stamped with {STAMP_V1}"
            return f"none (not under Alembic; head is {head})"
        missing = _missing_remote_columns(connection, current)
    if missing:
        return (
            f"{current} (the early numbering of the revisions: job lacks revision {REMOTE_EXECUTION_REVISION}'s "
            f"columns; {REBUILD})"
        )
    if current == head:
        return f"{current} (head)"
    return f"{current} (head is {head}: run `laueportal db upgrade`)"


def upgrade(database: DatabaseSettings, revision: str = "head") -> tuple[str | None, str | None]:
    """Upgrade to ``revision`` (default head); returns (before, after). Creates a new database from nothing.

    Refused, with the database left exactly as it was (checked on a read-only connection first):
    - a database with tables but no revision (a v1 database): stamp it with its v1 revision first;
    - a database migrated with the early numbering (``_early_numbering``): rebuild it;
    - a ``revision`` below the database's (``upgrade`` never goes down; the CLI offers no downgrade).
    """
    target = resolve_revision(revision)
    if not database.path.parent.is_dir():
        raise MigrationError(f"directory {database.path.parent} does not exist")
    if database.path.exists():
        with read_connection(database) as connection:
            _refuse_upgrade(database, connection, target)
    with _write_connection(database) as connection:
        before = _refuse_upgrade(database, connection, target)  # again, inside the transaction
        _run(command.upgrade, database, connection, revision)
        return before, _current(connection)


def _refuse_upgrade(database: DatabaseSettings, connection: Connection, target: str | None) -> str | None:
    """Raise if ``database`` must not be upgraded to ``target`` (None is ``base``); returns its revision."""
    current = _current(connection)
    if current is None:
        if _user_tables(connection):
            raise MigrationError(
                f"database {database.path} has tables but no Alembic revision. A v1 database (after v1's "
                f"scripts/migrate_workflow_database.py) is stamped first: {STAMP_V1}"
            )
        return None
    known = revision_ids()
    if current not in known:
        raise MigrationError(
            f"database {database.path} is at revision {current}, which this checkout does not have "
            f"(its revisions are {', '.join(known)})"
        )
    if (problem := _early_numbering(database, connection, current)) is not None:
        raise MigrationError(f"{problem} (nothing was changed)")
    if target is None or known.index(target) < known.index(current):
        raise MigrationError(
            f"database {database.path} is at revision {current}, past the requested {target or 'base'}: "
            "upgrade does not go down, and downgrades are not available from the command line"
        )
    return current


def downgrade(database: DatabaseSettings, revision: str) -> tuple[str | None, str | None]:
    """Downgrade to ``revision`` (``base`` drops everything); returns (before, after). A database migrated
    with the early numbering is refused and left as it was (``_early_numbering``)."""
    resolve_revision(revision)
    with read_connection(database) as connection:
        if (problem := _early_numbering(database, connection, _current(connection))) is not None:
            raise MigrationError(f"{problem} (nothing was changed)")
    with _write_connection(database) as connection:
        before = _current(connection)
        _run(command.downgrade, database, connection, revision)
        return before, _current(connection)


def stamp(database: DatabaseSettings, revision: str, *, verify: bool = True) -> None:
    """Record ``revision`` without running migrations (an existing v1 database: ``stamp 0001`` or ``0002``).

    With ``verify`` (the default), the database's tables and columns must be those that revision creates;
    otherwise nothing is recorded and the differences are reported, with the v1 revision whose schema the
    database has, if any (``_stamp_hint``). A revision at or after ``UTC_REVISION`` is refused while a timestamp
    column holds a value that is not UTC (``timestamp_counts``: v1's naive local times), with or without
    ``verify``: stamped there, the values would fail on read.
    """
    target = resolve_revision(revision)
    _require_file(database)
    with read_connection(database) as connection:
        if verify and target is not None:
            differences = schema_differences(connection, target)
            if differences:
                raise MigrationError(
                    f"database {database.path} does not have the schema of revision {target}, so it was not "
                    "stamped:\n  " + "\n  ".join(differences) + _stamp_hint(database, connection, target)
                )
        utc = target is not None and _at_or_after(target, UTC_REVISION)
        wrong = timestamp_counts(connection)[1] if utc else {}
    if wrong:
        raise MigrationError(
            f"database {database.path} holds timestamps that are not UTC ({_shown_counts(wrong)}), so it was not "
            f"stamped {target}: from revision {UTC_REVISION} on every timestamp is stored as UTC, and revision "
            f"{UTC_REVISION}'s migration converts v1's naive local times. Stamp the revision whose schema and values "
            f"the database has (a v1 database: {STAMP_V1}), then run `laueportal db upgrade`"
        )
    with _write_connection(database) as connection:
        _run(command.stamp, database, connection, revision)


def _stamp_hint(database: DatabaseSettings, connection: Connection, target: str) -> str:
    """What a refused ``stamp target`` adds: the other v1 revision whose schema the database has. Only a
    database not under Alembic is told to stamp it; one under Alembic is told its revision (and, if the early
    numbering migrated it, that)."""
    matches = [other for other in V1_REVISIONS if other != target and not schema_differences(connection, other)]
    current = _current(connection)
    if current is None:
        actual = table_columns(connection)
        live = _has_live_tables(actual)
        if _cb1159c_shaped(actual) and not cb1159c_differences(
            connection, actual, _at_reference_revision(CB1159C_REVISION, table_columns), live=live
        ):
            stamps = (
                f"drops its live tables ({', '.join(CB1159C_LIVE_TABLES)}) and records {CB1159C_REVISION}"
                if live
                else f"records {CB1159C_REVISION} and keeps job.remote_observation"
            )
            return f"\nIt has {_cb1159c_schema(live)}: stamp it with `laueportal db stamp --detect`, which {stamps}."
        return "".join(
            f"\nIt has the schema of revision {other} ({V1_REVISIONS[other]}): stamp {other}." for other in matches
        )
    hint = "".join(
        f"\nIts tables match revision {other} ({V1_REVISIONS[other]}); it is at revision {current}."
        for other in matches
    )
    numbering = _early_numbering(database, connection, current)
    return hint + (f"\n{numbering}" if numbering is not None else "")


@dataclass(frozen=True)
class V1Schema:
    """The v1 schema a database has (``detect_v1_schema``)."""

    revision: str
    """The revision it is stamped with."""
    name: str
    """``V1_REVISIONS``' name of it, or ``CB1159C``."""
    drops: tuple[str, ...] = ()
    """The tables the stamp drops first (v1 ``cb1159c``'s live tables; none for its schema without them)."""


def detect_v1_schema(database: DatabaseSettings) -> V1Schema:
    """The v1 schema ``database`` has: one of ``V1_REVISIONS``' (tables and columns, any column order), or v1
    ``cb1159c``'s with or without its live tables (``cb1159c_differences``). Read-only.

    Raises when the database is already under Alembic, or has none of them. A database with a live table of
    ``cb1159c`` is compared with ``cb1159c``'s schema alone, one with its ``job.remote_observation`` and no live table
    with that schema without the live tables alone (what v1 ``cb1159c``'s migration script leaves; nothing to drop,
    and no v1 live session without ``live_session``): the message lists how it differs. Any other database is
    compared with each of ``V1_REVISIONS``.
    """
    with read_connection(database) as connection:
        current = _current(connection)
        if current is not None:
            numbering = _early_numbering(database, connection, current)
            raise MigrationError(
                f"database {database.path} is already at revision {current}; detection is for a v1 database "
                "that is not under Alembic yet" + (f"\n{numbering}" if numbering is not None else "")
            )
        actual = table_columns(connection)
        references = {candidate: _at_reference_revision(candidate, table_columns) for candidate in V1_REVISIONS}
        if _cb1159c_shaped(actual):
            live = _has_live_tables(actual)
            found = cb1159c_differences(connection, actual, references[CB1159C_REVISION], live=live)
            if found:
                raise MigrationError(
                    f"database {database.path} has {CB1159C}'s live tables or job.remote_observation but not its "
                    f"schema, so it is not stamped and nothing was changed:\n  compared with {_cb1159c_schema(live)}:"
                    "\n    " + "\n    ".join(found)
                )
            return V1Schema(CB1159C_REVISION, CB1159C, CB1159C_LIVE_TABLES if live else ())
    differences = {candidate: _compare_tables(actual, reference) for candidate, reference in references.items()}
    matches = [candidate for candidate, found in differences.items() if not found]
    if not matches:
        raise MigrationError(
            f"database {database.path} has neither v1 schema, so its revision cannot be detected:"
            + "".join(
                f"\n  compared with revision {candidate} ({V1_REVISIONS[candidate]}):\n    " + "\n    ".join(found)
                for candidate, found in differences.items()
            )
        )
    return V1Schema(matches[0], V1_REVISIONS[matches[0]])


def _cb1159c_shaped(actual: dict[str, dict[str, tuple[str, bool, int]]]) -> bool:
    """Whether a database's tables (``table_columns``) have anything that only v1 ``cb1159c``'s schema has."""
    table, column, _ = CB1159C_OBSERVATION
    return _has_live_tables(actual) or column in actual.get(table, {})


def _has_live_tables(actual: dict[str, dict[str, tuple[str, bool, int]]]) -> bool:
    return any(name in actual for name in CB1159C_LIVE_TABLES)


def _cb1159c_schema(live: bool) -> str:
    """v1 ``cb1159c``'s schema with its live tables (``live``) or without them, for messages."""
    return f"{CB1159C}'s schema" + ("" if live else " without its live tables")


def _cb1159c_live_reference() -> tuple[dict[str, dict[str, tuple[str, bool, int]]], dict[tuple[str, str], str | None]]:
    """The live tables' columns (``table_columns``) and their indexes (``_live_indexes``) as ``CB1159C_LIVE_DDL``
    makes them, in a database in memory."""
    engine = create_engine("sqlite://", poolclass=NullPool)
    try:
        with engine.connect() as connection:
            for statement in CB1159C_LIVE_DDL:
                connection.exec_driver_sql(statement)
            return table_columns(connection), _live_indexes(connection)
    finally:
        engine.dispose()


def _live_indexes(connection: Connection) -> dict[tuple[str, str], str | None]:
    """``{("index", name): normalized SQL, None for a constraint's own}`` of the indexes on the live tables."""
    marks = ", ".join("?" for _ in CB1159C_LIVE_TABLES)
    rows = connection.exec_driver_sql(
        f"SELECT type, name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name IN ({marks})",
        CB1159C_LIVE_TABLES,
    ).fetchall()
    return {(str(kind), str(name)): None if sql is None else normalize_ddl(str(sql)) for kind, name, sql in rows}


def cb1159c_differences(
    connection: Connection,
    actual: dict[str, dict[str, tuple[str, bool, int]]],
    reference: dict[str, dict[str, tuple[str, bool, int]]],
    *,
    live: bool = True,
) -> list[str]:
    """How a database (``actual``: its ``table_columns``) differs from v1 ``cb1159c``'s schema; empty when it has it.

    Stricter than the other v1 schemas' comparison, since the stamp drops tables: the tables and columns are
    ``reference``'s (revision 0002's), ``job.remote_observation`` and the live tables' (any column order); the indexes
    on the live tables exactly ``cb1159c``'s (no other, none missing, the named one's SQL); and no view or trigger
    anywhere (v1 makes none, and one could use a live table). Not ``live``: the same without the live tables (and so
    without their indexes), as v1 ``cb1159c``'s ``scripts/migrate_workflow_database.py`` leaves a database.
    """
    expected = {name: dict(columns) for name, columns in reference.items()}
    table, column, definition = CB1159C_OBSERVATION
    expected[table][column] = definition
    live_columns, live_indexes = _cb1159c_live_reference() if live else ({}, {})
    expected.update(live_columns)
    differences = _compare_tables(actual, expected)
    have = _live_indexes(connection)
    differences += [f"missing {kind} {name}" for kind, name in sorted(live_indexes.keys() - have.keys())]
    differences += [f"unexpected {kind} {name}" for kind, name in sorted(have.keys() - live_indexes.keys())]
    differences += [
        f"{kind} {name} is `{have[kind, name]}`, expected `{live_indexes[kind, name]}`"
        for kind, name in sorted(live_indexes.keys() & have.keys())
        if have[kind, name] != live_indexes[kind, name]
    ]
    others = connection.exec_driver_sql(
        "SELECT type, name FROM sqlite_master WHERE type IN ('view', 'trigger') ORDER BY type, name"
    ).fetchall()
    differences += [f"unexpected {kind} {name}" for kind, name in others]
    return differences


def open_live_sessions(connection: Connection) -> list[str]:
    """v1 ``cb1159c``'s live sessions that have not ended (a state not in ``LIVE_SESSION_ENDED``), one line each:
    the session, its state and its runs. Empty when every session has ended."""
    marks = ", ".join("?" for _ in LIVE_SESSION_ENDED)
    rows = connection.exec_driver_sql(
        "SELECT id, state, reconstruction_id, indexing_id FROM live_session "
        f"WHERE state IS NULL OR state NOT IN ({marks}) ORDER BY id",
        LIVE_SESSION_ENDED,
    ).fetchall()
    lines = []
    for session_id, state, reconstruction_id, indexing_id in rows:
        runs = [
            f"{prefix}{run_id}"
            for prefix, run_id in (("R", reconstruction_id), ("I", indexing_id))
            if run_id is not None
        ]
        lines.append(f"live session {session_id}: {quoted(state)}" + (f" ({', '.join(runs)})" if runs else ""))
    return lines


def _refuse_open_live_sessions(database: DatabaseSettings, connection: Connection, unchanged: str) -> None:
    """Raise while a v1 live session has not ended (``open_live_sessions``); ``unchanged`` says what the refusal left
    as it was."""
    lines = open_live_sessions(connection)
    if lines:
        raise MigrationError(
            f"database {database.path} has {CB1159C}'s live sessions that have not ended, so it is not stamped and "
            f"{unchanged}:\n  " + "\n  ".join(lines) + "\nv2 does not continue v1-started live sessions. Before "
            "migrating, end each in v1 (its Live page's Stop; a session in recovery is abandoned by Stop), stop v1, "
            "and copy its database again; a rehearsal's copy: take another once no session is open "
            "(docs/runbooks/deploy.md step 4)"
        )


def _journal_mode(connection: Connection) -> str:
    return str(connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()).lower()


def stamp_detected(database: DatabaseSettings) -> V1Schema:
    """``laueportal db stamp --detect``: record the revision of the v1 schema ``database`` has (``detect_v1_schema``).

    A v1 ``cb1159c`` database is refused, changing nothing (read-only), while one of its live
    sessions has not ended (``open_live_sessions``; the portal does not continue v1-started live sessions); otherwise
    its live tables are dropped and ``CB1159C_REVISION`` recorded in one transaction (the sessions checked again in it),
    and ``job.remote_observation`` stays, with its values. Without live tables (v1 ``cb1159c``'s migration script's
    output) there is nothing to drop and no session: ``CB1159C_REVISION`` is recorded, the column kept. The sessions'
    runs and jobs stay as history: their queue identities (``live_<session>`` or ``live_<session>_<hex>``) and their
    run directories without ``live.json`` never read as the portal's live runs (``jobs.live.is_live_run``).
    """
    found = detect_v1_schema(database)
    if not found.drops:
        stamp(database, found.revision, verify=False)  # detection compared the schema already
        return found
    with read_connection(database) as connection:
        _refuse_open_live_sessions(database, connection, "nothing was changed")
        journal = _journal_mode(connection)
    with _write_connection(database) as connection:
        # Again, inside the transaction. Its connection has set the journal mode the settings name (core.db), which a
        # rollback cannot undo: the refusal says so when that changed it.
        now = _journal_mode(connection)
        unchanged = (
            "nothing was changed"
            if now == journal
            else f"nothing was changed but its journal mode ({journal}, now {now})"
        )
        _refuse_open_live_sessions(database, connection, unchanged)
        for table in found.drops:
            connection.exec_driver_sql(f'DROP TABLE "{table}"')
        _run(command.stamp, database, connection, found.revision)
    return found


@dataclass
class Examination:
    """What ``examine`` found (``laueportal db check``)."""

    problems: list[str] = field(default_factory=list)
    """``check``'s answer: why the database is not fit for this code."""
    report: list[str] = field(default_factory=list)
    """What was measured, one line each, with its counts."""
    readable: bool = False
    """The models can read every row: the database is at head, matches the models and head's DDL, and its
    timestamps are UTC (what a check that reads rows through the ORM needs: the runs stuck in creation)."""


def examine(database: DatabaseSettings) -> Examination:
    """Whether ``database`` is fit for this code, with what was measured: its revision (not under Alembic, migrated
    with the early numbering, or not at head is a problem), at head its schema against the models and head's DDL,
    SQLite's ``quick_check`` and ``foreign_key_check``, from ``UTC_REVISION`` on the storage format of every
    timestamp (``timestamp_counts``; before it, v1's naive local times are expected), and every table's rows.
    Read-only: the database is never written."""
    import laueportal.models  # noqa: F401  (registers every model on Base.metadata)
    from laueportal.core.db import Base

    head = head_revision()
    found = Examination()
    actual: dict[tuple[str, str], str] | None = None
    model_differences: list[str] = []
    wrong: dict[str, int] = {}
    with read_connection(database) as connection:
        current = _current(connection)
        if (problem := _revision_problem(database, connection, current, head)) is not None:
            found.problems.append(problem)
            found.report.append(f"revision: {current or 'none'} (head is {head})")
        else:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            model_differences = [
                f"differs from the models: {describe_difference(diff)}"
                for diff in compare_metadata(context, Base.metadata)
            ]
            found.problems += model_differences
            actual = schema_ddl(connection)
            found.report.append(f"revision: {current} (head)")
        integrity = quick_check_errors(connection.exec_driver_sql("PRAGMA quick_check(10)").fetchall())
        intact = integrity == ["ok"]
        found.report.append(f"quick_check: {'ok' if intact else f'{len(integrity)} errors'}")
        if not intact:
            found.problems.append(
                f"quick_check failed: {'; '.join(integrity[:5])}" + ("; ..." if len(integrity) > 5 else "")
            )
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        found.report.append(f"foreign_key_check: {len(violations)} violations")
        if violations:
            shown = ", ".join(f"{table} row {rowid} -> {parent}" for table, rowid, parent, _ in violations[:10])
            found.problems.append(f"foreign key check failed ({len(violations)} rows: {shown})")
        if not intact:
            found.report.append("timestamps and rows: not counted (quick_check failed)")
        else:
            if current in revision_ids() and _at_or_after(current, UTC_REVISION):
                values, wrong = timestamp_counts(connection)
                found.report.append(f"timestamps: {values} values, {sum(wrong.values())} not UTC")
                if wrong:
                    found.problems.append(
                        f"timestamps not in UTC's storage format ({UTC_STORAGE_EXAMPLE}): {_shown_counts(wrong)}; "
                        "the models cannot read them"
                    )
            rows = {
                table: int(connection.exec_driver_sql(f'SELECT count(*) FROM "{table}"').scalar_one())
                for table in sorted(_user_tables(connection))
            }
            found.report.append("rows: " + (", ".join(f"{table} {count}" for table, count in rows.items()) or "none"))
    if actual is not None:
        differences = ddl_differences(actual, _at_reference_revision(head, schema_ddl))
        found.problems += [f"differs from head's DDL: {difference}" for difference in differences]
        found.readable = intact and not model_differences and not differences and not wrong
    return found


def check(database: DatabaseSettings) -> list[str]:
    """Why ``database`` is not fit for this code (``examine``): not under Alembic, migrated with the early numbering,
    not at head, different from the models or from head's DDL, damaged (``quick_check``, ``foreign_key_check``),
    or holding timestamps that are not UTC. Empty when fine. Read-only: the database is never written."""
    return examine(database).problems


def _revision_problem(database: DatabaseSettings, connection: Connection, current: str | None, head: str) -> str | None:
    """Why ``database`` (at ``current``) is not at head; None when it is."""
    if current is None:
        return (
            f"database {database.path} is not under Alembic (no revision): a v1 database is stamped with "
            f"{STAMP_V1}, then upgraded"
        )
    if (problem := _early_numbering(database, connection, current)) is not None:
        return problem
    if current != head:
        return f"database {database.path} is at revision {current}, head is {head}: run `laueportal db upgrade`"
    return None


def _at_or_after(revision: str, reference: str) -> bool:
    known = revision_ids()
    return known.index(revision) >= known.index(reference)


def timestamp_columns(connection: Connection) -> list[tuple[str, str]]:
    """``(table, column)`` of every ``UTCDateTime`` column of the models that the database has."""
    import laueportal.models  # noqa: F401  (registers every model on Base.metadata)
    from laueportal.core.db import Base
    from laueportal.core.timeutil import UTCDateTime

    present = table_columns(connection)
    return [
        (table.name, column.name)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, UTCDateTime) and column.name in present.get(table.name, {})
    ]


def timestamp_counts(connection: Connection) -> tuple[int, dict[str, int]]:
    """``(values, {"table.column": values not UTC})`` over ``timestamp_columns`` (NULLs not counted). Not UTC is
    any value but text in ``UTCDateTime``'s storage format (``UTC_STORAGE_GLOB``): v1's naive local time
    (``2026-01-23 11:32:08.491324``), another offset, or anything else the models cannot read."""
    values, wrong = 0, {}
    for table, column in timestamp_columns(connection):
        count, bad = connection.exec_driver_sql(
            f'SELECT count("{column}"), coalesce(sum("{column}" IS NOT NULL AND NOT '
            f'(typeof("{column}") = \'text\' AND "{column}" GLOB ?)), 0) FROM "{table}"',
            (UTC_STORAGE_GLOB,),
        ).one()
        values += int(count)
        if bad:
            wrong[f"{table}.{column}"] = int(bad)
    return values, wrong


def _shown_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{name} {count}" for name, count in counts.items())


def _run(operation: Any, database: DatabaseSettings, connection: Connection, revision: str) -> None:
    try:
        operation(alembic_config(database, connection), revision)
    except CommandError as error:
        raise MigrationError(str(error)) from None


# ---------------------------------------------------------------------------
# Schema comparison (stamp verification)


def table_columns(connection: Connection) -> dict[str, dict[str, tuple[str, bool, int]]]:
    """``{table: {column: (declared type, not null, primary-key position)}}``, Alembic's table left out."""
    tables: dict[str, dict[str, tuple[str, bool, int]]] = {}
    for name in sorted(_user_tables(connection)):
        rows = connection.exec_driver_sql(f'PRAGMA table_info("{name}")').fetchall()
        tables[name] = {row[1]: (str(row[2]).upper(), bool(row[3]), int(row[5])) for row in rows}
    return tables


def _at_reference_revision(revision: str, read: Callable[[Connection], T]) -> T:
    """``read`` applied to a new database migrated to ``revision`` (built in ``TMPDIR``, then deleted)."""
    with tempfile.TemporaryDirectory(prefix="laueportal-schema-") as directory:
        reference = DatabaseSettings(path=Path(directory) / "reference.db", journal_mode="delete")
        upgrade(reference, revision)
        with read_connection(reference) as reference_connection:
            return read(reference_connection)


def schema_differences(connection: Connection, revision: str) -> list[str]:
    """How the tables and columns behind ``connection`` differ from those ``revision`` creates (any column order)."""
    return _compare_tables(table_columns(connection), _at_reference_revision(revision, table_columns))


def _compare_tables(actual: dict[str, dict[str, Any]], expected: dict[str, dict[str, Any]]) -> list[str]:
    differences = [f"missing table {name}" for name in sorted(expected.keys() - actual.keys())]
    differences += [f"unexpected table {name}" for name in sorted(actual.keys() - expected.keys())]
    for name in sorted(expected.keys() & actual.keys()):
        want, have = expected[name], actual[name]
        differences += [f"{name}: missing column {column}" for column in sorted(want.keys() - have.keys())]
        differences += [f"{name}: unexpected column {column}" for column in sorted(have.keys() - want.keys())]
        for column in sorted(want.keys() & have.keys()):
            if want[column] != have[column]:
                differences.append(
                    f"{name}.{column}: {_column_text(have[column])}, expected {_column_text(want[column])}"
                )
    return differences


def _column_text(column: tuple[str, bool, int]) -> str:
    kind, not_null, primary_key = column
    return " ".join([kind, *(["NOT NULL"] if not_null else []), *(["PRIMARY KEY"] if primary_key else [])])


def describe_difference(diff: Any) -> str:
    """One line for an entry of Alembic's ``compare_metadata`` (a tuple, or a list of column changes)."""
    if isinstance(diff, list):
        return "; ".join(describe_difference(item) for item in diff)
    if not isinstance(diff, Sequence) or not diff:
        return repr(diff)
    kind, *rest = diff
    if kind in {"add_table", "remove_table"}:
        return f"{kind} {rest[0].name}"
    if kind in {"add_column", "remove_column"}:
        return f"{kind} {rest[1]}.{rest[2].name}"
    if kind in {"add_index", "remove_index", "add_constraint", "remove_constraint", "add_fk", "remove_fk"}:
        item = rest[0]
        table = getattr(getattr(item, "table", None), "name", "?")
        return f"{kind} {item.name or '(unnamed)'} on {table}"
    if kind.startswith("modify_"):
        _schema, table, column, _info, old, new = rest
        return f"{kind} {table}.{column}: {old!r} -> {new!r}"
    return " ".join(str(part) for part in diff)


# ---------------------------------------------------------------------------
# DDL comparison (check)

_DDL_TOKEN = re.compile(r"""('(?:[^']|'')*')|"([a-z_][a-z0-9_]*)"|(\s+)""")
"""A string literal (kept as written), a quoted lower-case identifier (unquoted), or a run of whitespace."""
_CREATE_TABLE = re.compile(
    r"^(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?\S+)\s*\((.*)\)([^)]*)$", re.IGNORECASE | re.DOTALL
)
_TABLE_CONSTRAINT = re.compile(r"(CONSTRAINT|PRIMARY\s+KEY|UNIQUE|CHECK|FOREIGN\s+KEY)\b", re.IGNORECASE)
_CONSTRAINT_NAME = re.compile(r"CONSTRAINT\s+(\S+)\s", re.IGNORECASE)
_LEADING_NAME = re.compile(r"""("(?:[^"]|"")*"|`[^`]*`|\[[^\]]*\]|\S+)""")


def normalize_ddl(sql: str) -> str:
    """One spelling of a DDL statement: whitespace runs as one space, lower-case identifiers unquoted.

    SQLite keeps an identifier's quotes when it renames a column or a table (``"scanNumber"`` becomes
    ``"scan_number"``; v1's migration script left production's ``CREATE TABLE "job"``) where ``create_all``
    writes ``scan_number``. String literals are left exactly as written. The head-versus-models test
    normalizes the same way (``tests/migration_support.normalize_sql``).
    """

    def replace(match: re.Match[str]) -> str:
        literal, identifier, _space = match.groups()
        if literal is not None:
            return literal
        return identifier if identifier is not None else " "

    return _DDL_TOKEN.sub(replace, sql).strip()


def schema_ddl(connection: Connection) -> dict[tuple[str, str], str]:
    """``{(type, name): normalized SQL}`` of every table, index, view and trigger with stored SQL.

    Left out: Alembic's version table, SQLite's own ``sqlite_*`` objects, and the automatic indexes of
    PRIMARY KEY/UNIQUE constraints (no SQL: their table's DDL defines them).
    """
    rows = connection.exec_driver_sql(
        "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL AND tbl_name != ? AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'",
        (VERSION_TABLE,),
    ).fetchall()
    return {(str(kind), str(name)): normalize_ddl(str(sql)) for kind, name, sql in rows}


def _split_top_level(text: str) -> list[str]:
    """``text`` cut at the commas outside parentheses and quotes, each part stripped."""
    parts: list[str] = []
    depth, quote, start = 0, "", 0
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
        elif char == "[":
            quote = "]"
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return [part.strip() for part in parts if part.strip()]


@dataclass(frozen=True)
class _TableDDL:
    """A ``CREATE TABLE`` statement in parts: column and constraint order do not count."""

    head: str
    columns: dict[str, str] = field(default_factory=dict)
    constraints: Counter[str] = field(default_factory=Counter)
    options: str = ""


def _parse_table(sql: str) -> _TableDDL | None:
    match = _CREATE_TABLE.match(sql)
    if match is None:
        return None
    head, body, options = match.groups()
    table = _TableDDL(head=head, options=options.strip())
    for part in _split_top_level(body):
        if _TABLE_CONSTRAINT.match(part):
            table.constraints[part] += 1
        else:
            name = _LEADING_NAME.match(part)
            table.columns[name.group(1) if name else part] = part
    return table


def _constraint_name(constraint: str) -> str | None:
    match = _CONSTRAINT_NAME.match(constraint)
    return match.group(1) if match else None


def _table_differences(actual: str, expected: str) -> list[str]:
    have, want = _parse_table(actual), _parse_table(expected)
    if have is None or want is None:
        return [] if actual == expected else [f"is `{actual}`, expected `{expected}`"]
    differences: list[str] = []
    if have.head != want.head:
        differences.append(f"is created by `{have.head}`, expected `{want.head}`")
    differences += [
        f"missing column `{want.columns[name]}`" for name in sorted(want.columns.keys() - have.columns.keys())
    ]
    differences += [
        f"unexpected column `{have.columns[name]}`" for name in sorted(have.columns.keys() - want.columns.keys())
    ]
    differences += [
        f"column `{have.columns[name]}`, expected `{want.columns[name]}`"
        for name in sorted(want.columns.keys() & have.columns.keys())
        if have.columns[name] != want.columns[name]
    ]
    missing = sorted((want.constraints - have.constraints).elements())
    unexpected = sorted((have.constraints - want.constraints).elements())
    for constraint in list(missing):  # the same named constraint with another definition: one line
        name = _constraint_name(constraint)
        changed = next((other for other in unexpected if name and _constraint_name(other) == name), None)
        if changed is not None:
            differences.append(f"constraint `{changed}`, expected `{constraint}`")
            missing.remove(constraint)
            unexpected.remove(changed)
    differences += [f"missing constraint `{constraint}`" for constraint in missing]
    differences += [f"unexpected constraint `{constraint}`" for constraint in unexpected]
    if have.options != want.options:
        differences.append(f"table options `{have.options}`, expected `{want.options}`")
    return differences


def ddl_differences(actual: dict[tuple[str, str], str], expected: dict[tuple[str, str], str]) -> list[str]:
    """How the schema ``actual`` differs from ``expected`` (both from ``schema_ddl``).

    Tables are compared column by column (by name) and constraint by constraint (the definitions as
    written: CHECK expressions, defaults, NOT NULL, keys); the order of columns and of constraints does
    not count. Production's ``job`` table, made by v1's migration script, has its two CHECK constraints in
    the other order than ``create_all``. Indexes, views and triggers are compared statement by statement.
    """
    differences = [f"missing {kind} {name}" for kind, name in sorted(expected.keys() - actual.keys())]
    differences += [f"unexpected {kind} {name}" for kind, name in sorted(actual.keys() - expected.keys())]
    for kind, name in sorted(expected.keys() & actual.keys()):
        have, want = actual[kind, name], expected[kind, name]
        if kind == "table":
            differences += [f"table {name}: {difference}" for difference in _table_differences(have, want)]
        elif have != want:
            differences.append(f"{kind} {name} is `{have}`, expected `{want}`")
    return differences
