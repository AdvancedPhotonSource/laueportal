"""SQLite engine, sessions and the declarative ``Base``. This module never imports FastAPI.

- ``Base``: the declarative base of every ORM model; ``laueportal.models`` imports them all.
- ``make_engine(settings)`` (or ``database_engine(settings.database)``): every connection gets ``busy_timeout=30000``,
  ``foreign_keys=ON`` and the journal mode from settings. Making an engine applies the journal-mode guard first
  (``guard_journal_mode``), so the API, the worker, its job processes and every command that opens the database refuse
  WAL on a network file system before they start. No process forks with an engine open: the worker starts its job
  processes fresh (``jobs.dispatch``), and each opens its own. With ``create=False`` (the API's engine) the engine never
  creates the database file: a missing file is ``DatabaseMissing`` (a 503 problem). Only ``laueportal db upgrade`` and
  the seed commands create a database; the worker, ``remote poll``, ``results convert`` and the other ``db`` commands
  refuse a missing one.
- ``session_scope(engine_or_sessionmaker)``: commit on success, roll back on error, always close.
- ``begin_write(session)``: take the write lock at the start of a transaction, so its checks and writes are atomic.
- ``get_session(sessions)``: the generator behind the FastAPI session dependency. ``api.py`` modules
  use it through the adapter ``laueportal.core.api.SessionDep``.
- ``guard_journal_mode(database)``: the file system of ``database.path`` (its real
  location: links resolved, the mount from ``/proc/self/mountinfo``, ``database_filesystem``); a network file
  system (``NETWORK_FILESYSTEMS``) with ``journal_mode = "wal"`` is ``UnsafeJournalMode``, a ``ConfigError`` (the
  CLI exits 2). ``journal_mode_problem`` is the same check as text (``laueportal db check``).
- ``backup(database, dest)``: a consistent copy of a database in use (SQLite's online backup API), checked with
  ``quick_check`` before it takes its name (``laueportal db backup``).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
from collections.abc import Generator, Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, MetaData, create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from laueportal.core.mounts import MOUNTINFO, Mount, mount_holding, read_mountinfo
from laueportal.core.problems import ProblemError
from laueportal.core.settings import ConfigError, DatabaseSettings, Settings

logger = logging.getLogger(__name__)

BUSY_TIMEOUT_MS = 30_000

SQLITE_INTEGER_MAX = 2**63 - 1
"""The largest SQLite INTEGER: bind a larger Python int and sqlite3 raises ``OverflowError``."""


NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
}
"""Constraint and index names (``Base.metadata`` and therefore Alembic's operations).

It reproduces v1's names exactly: v1 named its indexes ``ix_<table>_<column>`` and its CHECK constraints
``ck_<table>_<name>`` (models write the short name: ``CheckConstraint(..., name="method")`` is
``ck_indexing_run_method``). v1's primary keys, foreign keys and UNIQUE constraints are unnamed, and they
stay unnamed: a ``pk``/``fk``/``uq`` convention would name them in ``create_all`` and in new migrations
but not in existing databases, so the two would drift apart. A migration that adds such a constraint
names it explicitly (``op.f("uq_<table>_<column>")``)."""


class Base(DeclarativeBase):
    """Declarative base of every ORM model.

    For queries, importing any domain's models module is enough: the domain model modules import each
    other, so every mapper is registered. ``laueportal.models`` gathers them all; Alembic's ``env.py``
    imports it so that ``Base.metadata`` is complete."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class DatabaseMissing(ProblemError):
    """The database file does not exist, and the engine that needs it never creates one (the API's): a 503."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        super().__init__(
            503,
            "Database unavailable",
            f"The database file {os.fspath(path)} does not exist; create it with `laueportal db upgrade`.",
        )
        self.path = Path(path)


# ---------------------------------------------------------------------------
# The journal-mode guard: WAL needs shared memory and reliable locks, which a network file system does not give
# (https://sqlite.org/wal.html, "WAL does not work over a network filesystem").

NETWORK_FILESYSTEMS = frozenset(
    {
        # network file systems
        "nfs",
        "nfs4",
        "cifs",
        "smb3",
        "smbfs",
        "ncpfs",
        "afs",
        "9p",
        "davfs",
        "vboxsf",
        # cluster and parallel file systems (a shared disk counts: another host writes it)
        "ceph",
        "glusterfs",
        "lustre",
        "gpfs",
        "beegfs",
        "gfs2",
        "ocfs2",
        "panfs",
        "pvfs2",
        "orangefs",
        # FUSE clients of the above (FUSE's type is fuse.<program>)
        "fuse.sshfs",
        "sshfs",
        "fuse.ceph-fuse",
        "fuse.glusterfs",
        "fuse.davfs2",
        "fuse.s3fs",
        "fuse.rclone",
        "fuse.gcsfuse",
        "fuse.juicefs",
        # the automounter's trigger (/net)
        "autofs",
    }
)
"""File-system types (``/proc/self/mountinfo``) whose files another host may share: WAL is refused there. ``autofs``
is the automounter's trigger (``/net``): a path still under it once its links are resolved is on a network mount
that is not mounted yet."""


class UnsafeJournalMode(ConfigError):
    """``journal_mode = "wal"`` for a database on a network file system: the process refuses to start. Its message
    names the database's path and its mount (not secrets) and what to change."""


def database_filesystem(path: str | os.PathLike[str]) -> tuple[str, Mount] | None:
    """``(real path, mount)``: where ``path`` really is (its links resolved; a file that does not exist yet, its
    folder's) and the mount that holds it, from the mount table (``MOUNTINFO``). A bind mount is in the table with
    its source's file-system type. None when the mount table cannot be read (no ``/proc``) or holds nothing."""
    real = os.path.realpath(path)
    try:
        mounts = read_mountinfo(MOUNTINFO)
    except OSError:
        return None
    mount = mount_holding(real, mounts)
    return None if mount is None else (real, mount)


def journal_mode_problem(database: DatabaseSettings) -> str | None:
    """Why ``database``'s journal mode is unsafe where it is (WAL on a network file system); None when it is safe,
    or when its file system cannot be told (no mount table: nothing is refused)."""
    if database.journal_mode != "wal":
        return None
    found = database_filesystem(database.path)
    if found is None or found[1].fstype not in NETWORK_FILESYSTEMS:
        return None
    real, mount = found
    shown = f"{database.path}" if real == str(database.path) else f"{database.path} (really {real})"
    return (
        f"database.path {shown} is on a network file system ({mount.fstype}, mounted at {mount.point}), where "
        'SQLite\'s WAL journal is unsafe (journal_mode = "wal"): put the database on a local disk (e.g. '
        '/local/laueportal/laueportal.db) or set journal_mode = "delete" in [database]'
    )


def guard_journal_mode(database: DatabaseSettings) -> None:
    """Raise ``UnsafeJournalMode`` when ``database``'s journal mode is unsafe where it is (``journal_mode_problem``)."""
    if (problem := journal_mode_problem(database)) is not None:
        raise UnsafeJournalMode(problem)


def journal_mode_report(database: DatabaseSettings) -> str:
    """One line for ``laueportal db check``: the journal mode and the file system it is on."""
    found = database_filesystem(database.path)
    if found is None:
        return f"journal: {database.journal_mode}, file system unknown (no mount table)"
    real, mount = found
    kind = "network" if mount.fstype in NETWORK_FILESYSTEMS else "local"
    really = "" if real == str(database.path) else f"; the database is {real}"
    return f"journal: {database.journal_mode} on {mount.fstype} ({kind}, mounted at {mount.point}{really})"


def make_engine(settings: Settings, *, create: bool = True) -> Engine:
    """The SQLite engine for ``settings.database``. Connecting is lazy: nothing touches the file here (the
    journal-mode guard reads the mount table and resolves the path's links). Raises ``UnsafeJournalMode``."""
    return database_engine(settings.database, create=create)


def database_engine(database: DatabaseSettings, *, create: bool = True) -> Engine:
    """The SQLite engine for one database (e.g. a copy named on the command line); see ``make_engine``.

    ``create=False`` makes an engine that never creates the database file (the API's, ``app.create_app``): each
    new connection raises ``DatabaseMissing`` when the file does not exist, and opens an existing one through a
    SQLite URI with ``mode=rw``, so a file removed after that check is not recreated either (SQLite refuses to
    open it). By default, as SQLite does, a missing file is created on the first connection: ``db upgrade`` and
    the seed rely on it; the worker and the other commands check that the file exists first and refuse a missing
    one.
    """
    guard_journal_mode(database)
    engine = create_engine(URL.create("sqlite", database=str(database.path)))
    journal_mode = database.journal_mode

    if not create:

        @event.listens_for(engine, "do_connect")
        def _open_existing_only(
            _dialect: Any, _connection_record: Any, cargs: list[Any], cparams: dict[str, Any]
        ) -> None:
            path = Path(cargs[0])
            if not path.is_file():
                raise DatabaseMissing(path)
            cargs[0] = f"{path.resolve().as_uri()}?mode=rw"
            cparams["uri"] = True

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")  # first: changing the journal mode may wait
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA journal_mode={journal_mode}")
            row = cursor.fetchone()
            actual = str(row[0]).lower() if row else "?"
            if actual != journal_mode:
                logger.warning("SQLite kept journal_mode=%s for %s (requested %s)", actual, database.path, journal_mode)
        finally:
            cursor.close()

    return engine


# ---------------------------------------------------------------------------
# Sessions


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False: objects stay readable after the commit (e.g. to build a response).
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope(bind: Engine | sessionmaker[Session]) -> Iterator[Session]:
    """A session that commits when the block succeeds and rolls back when it raises."""
    factory = bind if isinstance(bind, sessionmaker) else make_sessionmaker(bind)
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def begin_write(session: Session) -> None:
    """Take the database's write lock now: ``BEGIN IMMEDIATE`` as the first statement of ``session``'s transaction.

    SQLite (pysqlite) otherwise begins a transaction only at its first write, so what a transaction read before
    its first write may have changed by the time it commits. After ``begin_write``, every read sees the latest
    committed state and no other writer commits until this transaction ends: a check and the write it guards are
    one atomic step (``jobs.controls.cancel_run`` and ``remote.submission.submit_run`` rely on it). Other writers
    wait meanwhile (``busy_timeout``), so keep the transaction short: no slow I/O in it. Call it before anything
    else in the transaction (``with Session(engine) as session, session.begin(): begin_write(session)``).
    """
    session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def get_session(sessions: sessionmaker[Session]) -> Generator[Session, None, None]:
    """Generator behind the FastAPI session dependency: commit after the endpoint, roll back if it raises."""
    with session_scope(sessions) as session:
        yield session


def database_path(engine: Engine) -> Path | None:
    return Path(engine.url.database) if engine.url.database else None


def ping(engine: Engine, *, timeout_ms: int = 1000) -> None:
    """Raise unless the database file exists and answers a read within about ``timeout_ms``.

    The probe opens its own short-lived SQLite connection with the timeout set from the start. A
    pooled connection would first run the engine's connect pragmas (``journal_mode``) under the
    30 s busy timeout, so a database locked by a writer could hold the probe for 30 s. The probe
    never creates the file (``mode=rw``) and never changes the journal mode.
    """
    path = database_path(engine)
    if path is None or not path.is_file():
        raise FileNotFoundError(f"database file {path} does not exist")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True, timeout=timeout_ms / 1000)
    try:
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Backup


class BackupError(Exception):
    """A backup failed; nothing took the destination's name. The message says why and which file it was."""


DATABASE_SUFFIXES = ("", "-journal", "-wal", "-shm")
"""A database's files: the database, and beside it its rollback journal, its WAL and the WAL's shared memory. They
belong together: SQLite finds a journal or a WAL by the database's name, so they are copied, moved and protected
together (``laueportal db backup`` and the protected originals, ``cli``)."""


def database_files(path: Path) -> list[Path]:
    """``path`` and the files SQLite keeps beside it (``DATABASE_SUFFIXES``), in that order."""
    return [path.with_name(path.name + suffix) for suffix in DATABASE_SUFFIXES]


def quick_check_errors(rows: Iterable[Sequence[Any]]) -> list[str]:
    """``PRAGMA quick_check``'s answer as one message per error (its ``*** in database main ***`` header left out);
    ``["ok"]`` when the database passed."""
    return [
        line for row in rows for line in str(row[0]).splitlines() if line and not line.startswith("*** in database")
    ]


def backup(database: DatabaseSettings, dest: Path) -> int:
    """Copy ``database`` to ``dest`` with SQLite's online backup API and return the copy's pages.

    The copy is consistent while the app runs: one backup step reads the whole database in one read transaction
    (in WAL mode the writers go on meanwhile; in rollback-journal mode they wait for it, ``busy_timeout``). The
    source is opened read-only (``mode=ro``, no pragma), so its journal mode is left as it is. The copy is written
    beside ``dest`` (``.<name>.partial-<pid>``), put in rollback-journal mode (one self-contained file, safe on
    NFS: a WAL source's copy would otherwise be WAL), ``quick_check``ed on a connection of its own, given the
    source's permission bits, and only then renamed to ``dest`` (replacing a file there). A failure removes the
    partial copy and raises ``BackupError``, naming the side that failed (the database, or the copy). What may be
    overwritten is the caller's to decide (``laueportal db backup``).
    """
    source = database.path
    if not source.is_file():
        raise BackupError(f"database {source} does not exist")
    partial = dest.with_name(f".{dest.name}.partial-{os.getpid()}")
    side = f"database {source}"
    try:
        reader = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
        try:
            side = f"the copy {dest}"
            writer = sqlite3.connect(partial)
            try:
                side = f"the backup of {source} to {dest}"
                reader.backup(writer)
                side = f"the copy {dest}"
                writer.execute("PRAGMA journal_mode=DELETE")
                pages = int(writer.execute("PRAGMA page_count").fetchone()[0])
            finally:
                writer.close()
        finally:
            reader.close()
        check = sqlite3.connect(f"{partial.resolve().as_uri()}?mode=ro", uri=True)
        try:
            found = quick_check_errors(check.execute("PRAGMA quick_check(10)").fetchall())
        finally:
            check.close()
        if found != ["ok"]:
            raise BackupError(f"the copy of {source} failed quick_check, so {dest} was not written: {'; '.join(found)}")
        os.chmod(partial, stat.S_IMODE(source.stat().st_mode))
        os.replace(partial, dest)
    except (sqlite3.Error, OSError) as error:
        raise BackupError(f"{side}: {error}") from error
    finally:
        for path in database_files(partial):
            path.unlink(missing_ok=True)
    return pages


class DatabaseErrors:
    """A worker thread's database errors (``OperationalError``: the database write-locked past its busy timeout, a
    disk error), logged once per streak: its first error as one line without a traceback, its end as
    another. The dispatcher's, the reconciler's and the remote poller's cycles: the next cycle tries again."""

    def __init__(self, log: logging.Logger, who: str) -> None:
        self._log = log
        self._who = who
        self.failing = False

    def failed(self, error: OperationalError) -> None:
        if not self.failing:
            self._log.warning(
                "The %s could not use the database (%s); its next cycle tries again", self._who, error.orig
            )
        self.failing = True

    def succeeded(self) -> None:
        if self.failing:
            self.failing = False
            self._log.info("The %s can use the database again", self._who)
