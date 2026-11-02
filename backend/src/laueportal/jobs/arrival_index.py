"""The admission index of a ``notify`` run: what ``laueportal live notify`` looks up
under the arrivals lock instead of reading the whole ``arrivals.jsonl``, so an admission's cost does not grow with the
ledger.

**Why.** A ``notify`` is a process of its own per file, with no memory of the ledger: without an index its writer read
and parsed every line under the exclusive lock (9 µs a line: 0.97 s at 100,000 lines), so the lock served at most
about 1/(9 µs × lines) admissions a second. The long-lived writers (the catalog thread, a follower's thread) keep the
ledger in memory and read only the lines written since their last look; they use no index.

**What it is.** A SQLite file per run beside the portal's database, on its local disk
(``<database>.notify-index/<job id>.sqlite``; never in the run directory): each admitted path with its arrival number
and its line's place in the ledger (offset and length), and how far the index has read (the ledger's device and inode,
the end of its last indexed whole line, the count and that line's offset). It is only ever used under the run's
arrivals lock (``jobs.arrivals``' ``_locked``), so it needs no lock of its own (SQLite's are its own business; every
writer runs on the portal host, the lock's assumption).

**The ledger stays the source of truth.** At each admission the index is checked against the ledger, then brought
up to it (``sync``): it is stale, and rebuilt from the ledger's first line, when it is missing or unreadable (a
``sqlite3.DatabaseError``: the file is replaced), names another run directory or another ledger file (device,
inode), reaches past the ledger's end, or its last indexed line is not, at its recorded place, that arrival's whole
line. Then the whole lines after its end are read and indexed (none, normally: each admission indexes its own line
after its append; a writer that died between its append and its index's commit leaves its line for the next one).
A path found is read back from the ledger at its recorded place (one line) and must be that arrival of that path,
else the index is rebuilt and the path looked up again. So one admission reads a bounded number of lines however
long the ledger is, but for a rebuild (once, after a stale or lost index: ``rebuilds``).

It is SQLite in WAL mode with ``synchronous = NORMAL``: a commit is not fsynced, so a crash may lose the last commits
(the index then lags the ledger, and the next admission catches it up) but never corrupts it; the ledger's own line
is fsynced. One admission holds the arrivals lock about 20 ms on this host (the ledger's fsync most of it),
whatever the ledger's length (``docs/runbooks/live-deployment-test.md``, "Which source to keep").

**Kept** while its run may still be notified: an index whose files have not changed for ``INDEX_KEEP_SECONDS`` (seven
days) is removed when another run's index is made (a lost one is rebuilt, so this is never wrong, only a rebuild).
The folder is a cache: deleting it is safe, and a backup leaves it out.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from typing import Protocol

from laueportal.jobs.rundir import ARRIVALS_FILENAME

logger = logging.getLogger(__name__)

INDEX_SUFFIX = ".notify-index"
"""The index folder's name: the database file's name and this, beside it."""
INDEX_KEEP_SECONDS = 7 * 24 * 3600
"""An index unchanged this long is removed when another run's index is made (the module's docstring)."""
INDEX_VERSION = 1
_FILES = ("", "-wal", "-shm", "-journal")
"""An index's files: SQLite's database and its write-ahead log, shared memory and journal."""

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS ledger (one INTEGER PRIMARY KEY CHECK (one = 1), directory TEXT NOT NULL,"
    " device INTEGER NOT NULL, inode INTEGER NOT NULL, end_offset INTEGER NOT NULL, count INTEGER NOT NULL,"
    " last_offset INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS arrival (path TEXT PRIMARY KEY, n INTEGER NOT NULL, line_offset INTEGER NOT NULL,"
    " line_length INTEGER NOT NULL) WITHOUT ROWID",
)


def index_path(database: str | os.PathLike[str], job_id: int) -> str:
    """The index file of run ``job_id`` for the database file ``database``."""
    return os.path.join(os.fspath(database) + INDEX_SUFFIX, f"{job_id}.sqlite")


class Line(Protocol):
    """What the index reads of a ledger line (``jobs.arrivals.Arrival``)."""

    @property
    def n(self) -> int: ...
    @property
    def path(self) -> str: ...


Parse = Callable[[bytes, int], Line]
"""``jobs.arrivals.Arrival.from_line``: the arrival a whole line holds, which must be arrival ``n``; it raises
``ValueError`` or ``RuntimeError`` (``ArrivalsUnreadable``) for anything else."""


class IndexStale(Exception):
    """The index does not agree with the ledger: it is rebuilt."""


class ArrivalIndex:
    """One run's admission index (the module's docstring), opened for one admission under the arrivals lock:
    ``sync`` with the ledger first, then ``find`` and ``add``; ``close`` at the end. ``parse`` reads a ledger line
    (``Arrival.from_line``). ``rebuilds`` counts the rebuilds from the ledger's first line (one after a stale or lost
    index)."""

    def __init__(self, path: str, run_directory: str, parse: Parse) -> None:
        self.path = path
        self._directory = run_directory
        self._parse = parse
        self._connection: sqlite3.Connection | None = None
        self.end = 0
        """The end of the last indexed whole line (the ledger's offset for the next line)."""
        self.count = 0
        self.last = 0
        """The offset of the last indexed line."""
        self.tail = 0
        """The bytes after ``end`` (a torn last line) at the last ``sync``."""
        self.rebuilds = 0

    # -- the file

    def _connect(self) -> sqlite3.Connection:
        if self._connection is None:
            folder = os.path.dirname(self.path)
            made = not os.path.exists(self.path)
            os.makedirs(folder, mode=0o700, exist_ok=True)
            if made:
                _remove_old(folder, keep=self.path)
            connection = sqlite3.connect(self.path, isolation_level=None)
            try:
                # WAL with NORMAL: a commit is not fsynced (a crash may lose the last ones, never corrupt it), so
                # the index may only lag the ledger, which the next sync catches up (the module's docstring).
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = NORMAL")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, INDEX_VERSION):
                    raise sqlite3.DatabaseError(f"index version {version}")
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {INDEX_VERSION}")
            except BaseException:
                connection.close()
                raise
            self._connection = connection
        return self._connection

    def _replace(self) -> None:
        """Remove an index file SQLite cannot read (it is rebuilt)."""
        self.close()
        for suffix in _FILES:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.path + suffix)

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    # -- one admission

    def sync(self, ledger: int) -> None:
        """Check the index against the ledger open on descriptor ``ledger`` and bring it up to the ledger's last
        whole line (the module's docstring), rebuilding it when it is stale or unreadable. ``ArrivalsUnreadable``
        (``jobs.arrivals``) for a ledger line that is not the next arrival."""
        try:
            self._sync(ledger)
        except sqlite3.DatabaseError as error:
            logger.warning("The admission index %s is unreadable (%s); it is rebuilt", self.path, error)
            self._replace()
            self._sync(ledger)

    def _sync(self, ledger: int) -> None:
        connection = self._connect()
        status = os.fstat(ledger)
        row = connection.execute(
            "SELECT directory, device, inode, end_offset, count, last_offset FROM ledger WHERE one = 1"
        ).fetchone()
        if row is None or not self._agrees(row, ledger, status):
            if row is not None:
                logger.warning(
                    "The admission index %s does not agree with %s; it is rebuilt", self.path, ARRIVALS_FILENAME
                )
            self._rebuild(status)
        else:
            self.end, self.count, self.last = row[3], row[4], row[5]
        self._catch_up(ledger, status.st_size)

    def rebuild(self, ledger: int) -> None:
        """Rebuild the index from the ledger's first line (a stale entry ``find`` met)."""
        status = os.fstat(ledger)
        self._rebuild(status)
        self._catch_up(ledger, status.st_size)

    def _agrees(self, row: tuple, ledger: int, status: os.stat_result) -> bool:
        directory, device, inode, end, count, last = row
        if (directory, device, inode) != (self._directory, status.st_dev, status.st_ino) or end > status.st_size:
            return False
        if count == 0:
            return end == 0
        line = os.pread(ledger, end - last, last)
        return line.endswith(b"\n") and self._line(line[:-1], count - 1) is not None

    def _rebuild(self, status: os.stat_result) -> None:
        self.rebuilds += 1
        self.end = self.count = self.last = 0
        connection = self._connect()
        with _transaction(connection):
            connection.execute("DELETE FROM arrival")
            connection.execute(
                "INSERT OR REPLACE INTO ledger VALUES (1, ?, ?, ?, 0, 0, 0)",
                (self._directory, status.st_dev, status.st_ino),
            )

    def _catch_up(self, ledger: int, size: int) -> None:
        """Index the whole lines after ``end`` (the ledger's ``size`` bytes, read in pieces); a line that is not the
        next arrival raises the parser's error (the ledger cannot be trusted)."""
        connection = self._connect()
        offset = self.end
        pending = b""
        rows: list[tuple[str, int, int, int]] = []
        while offset + len(pending) < size:
            chunk = os.pread(ledger, min(1 << 20, size - offset - len(pending)), offset + len(pending))
            if not chunk:
                break
            pending += chunk
            while (newline := pending.find(b"\n")) >= 0:
                line, pending = pending[:newline], pending[newline + 1 :]
                arrival = self._parse(line, self.count + len(rows))
                rows.append((arrival.path, arrival.n, offset, newline + 1))
                offset += newline + 1
        self.tail = len(pending)
        if rows:
            with _transaction(connection):
                connection.executemany("INSERT OR REPLACE INTO arrival VALUES (?, ?, ?, ?)", rows)
                self.count += len(rows)
                self.end, self.last = offset, rows[-1][2]
                self._save()

    def find(self, path: str, ledger: int) -> Line | None:
        """The arrival of ``path``, read from the ledger at its indexed place (one line); ``IndexStale`` when that
        line is not that arrival of that path."""
        row = (
            self._connect()
            .execute("SELECT n, line_offset, line_length FROM arrival WHERE path = ?", (path,))
            .fetchone()
        )
        if row is None:
            return None
        n, offset, length = row
        if n >= self.count or offset + length > self.end:
            raise IndexStale(f"{path} is indexed past the index's end")
        line = os.pread(ledger, length, offset)
        arrival = self._line(line[:-1], n) if line.endswith(b"\n") else None
        if arrival is None or arrival.path != path:
            raise IndexStale(f"{path} is not arrival {n} at its indexed place")
        return arrival

    def add(self, path: str, n: int, offset: int, length: int) -> None:
        """Index the line just appended (arrival ``n`` of ``path`` at ``offset``)."""
        connection = self._connect()
        with _transaction(connection):
            connection.execute("INSERT OR REPLACE INTO arrival VALUES (?, ?, ?, ?)", (path, n, offset, length))
            self.count, self.end, self.last = n + 1, offset + length, offset
            self._save()

    def _line(self, line: bytes, n: int) -> Line | None:
        try:
            return self._parse(line, n)
        except (ValueError, RuntimeError):
            return None

    def _save(self) -> None:
        assert self._connection is not None
        self._connection.execute(
            "UPDATE ledger SET end_offset = ?, count = ?, last_offset = ? WHERE one = 1",
            (self.end, self.count, self.last),
        )


@contextlib.contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def _remove_old(folder: str, *, keep: str) -> None:
    """Remove the indexes of ``folder`` (each with its SQLite files) whose files are all unchanged for
    ``INDEX_KEEP_SECONDS``, but ``keep``; never raises."""
    before = time.time() - INDEX_KEEP_SECONDS
    try:
        names = [entry.path for entry in os.scandir(folder) if entry.name.endswith(".sqlite")]
    except OSError:
        return
    for name in names:
        if name == keep:
            continue
        files = [name + suffix for suffix in _FILES]
        changed = []
        for path in files:
            with contextlib.suppress(OSError):
                changed.append(os.stat(path).st_mtime)
        if changed and max(changed) < before:
            for path in files:
                with contextlib.suppress(OSError):
                    os.unlink(path)
