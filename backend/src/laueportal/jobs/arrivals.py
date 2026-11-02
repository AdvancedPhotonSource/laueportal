"""A live run's arrivals: ``arrivals.jsonl``, its lock ``arrivals.lock``, admission (``ArrivalWriter.admit``, the
ledger's only writer), and the executor's reading and seal.

**The ledger.** ``arrivals.jsonl`` holds one compact JSON line per admitted input, in arrival order: ``n`` (0, 1, 2,
…), ``path`` (absolute, in ``live.json``'s folder), the file's identity when it was admitted (``size``, ``mtime_ns``,
``inode``, ``device``), ``source`` (``catalog``, ``notify`` or ``run``), ``admitted_at`` (aware UTC to the second)
and ``scan_point`` (the template's ``%d``); a follower's line names a point and adds ``point_id`` and ``n_depths``.
It and an empty ``arrivals.lock`` are made with the run (``create_ledger``); the lock is never removed.

**Admission** (every source writes through it, so either the catalog or ``notify`` can go without the other or the
compute). The file must be a regular file directly in the run's folder whose name matches the run's template
(one number, its scan point); the catalog's must have the catalog's size. A follower's point (``admit_point``, source
``run``, from its thread: ``jobs.follower``) is its point file, a regular file inside the followed run's directory
(by its text and by its real path), with the scan point, ``point_id`` and ``n_depths`` of its line in that run's
``completed.jsonl``: its ``point_id`` the file's stem and no other admitted file's, at most ``MAX_POINT_DEPTHS``
depths. Then, under an exclusive ``flock`` on ``arrivals.lock``, held for the check and the append only: the writer
reads the lines after its last offset (``notify``, a process with no memory of the ledger, looks the path up in the
run's admission index instead, ``jobs.arrival_index``: its cost does not grow with the ledger);
the same path with the same identity is a no-op (a repeated notification);
another identity is refused and logged; otherwise the run must be open, read inside the lock (not ended and no stop
flag: a Queued run admits), and the line is appended with one ``write`` and an ``fsync``, once a torn last line
(a writer that died while writing it) is truncated, logged.

**The lock** (what the code assumes). Every writer (the catalog thread, ``notify``, a follower's thread) and the
executor run on the portal host, so ``arrivals.jsonl`` and ``arrivals.lock`` have one NFS client (its close-to-open
and attribute caching are coherent for them), and the lock is the kernel's ``flock(LOCK_EX)`` on the runs root's file
system, waited for without a time limit (a writer holds it for its check and its append only). On NFS the client
emulates ``flock`` as a whole-file lock on the server, which needs a descriptor open for writing (EBADF otherwise), so
each acquisition opens ``arrivals.lock`` read-write (it is created with the run: every writer may already write
``arrivals.jsonl`` beside it); a mount with ``local_lock=flock`` keeps it on the client, which is enough with one
host. Each acquisition opens a descriptor of its own, so two threads of one process (the catalog thread and the
seal) exclude each other too: never share one. It is unlocked explicitly (``LOCK_UN``) before its descriptor is
closed, so a child forked without exec while it was held (whose copy shares the open file) cannot keep it. A lock
that cannot be taken (an ``OSError``: a lock file the caller
may not write, a server without locking) admits nothing: ``admit`` raises it (``notify``: its one line, exit 1), and
the seal's raises out of the executor's ``arrivals()``, a run-level error (the run Failed, what it published kept),
since without the lock the executor cannot know it read the whole input.

**Reading and the seal** (the executor, ``ArrivalReader``): no lock; whole lines only, from its offset (a last line
without its newline is still being written: it is read again from its start next time). The file is on the runs
root, so the executor looks at it at a bounded rate (``LiveExecutorHooks``: at most every
``live.arrivals_seconds``): a stat first (``changed``: its size or modification time differs from the file's when the
last read was used), and a read only when it changed. A write that keeps the size (a torn last line truncated, then
a line of the same length appended) shows by its modification time alone; within one tick of the file system's
timestamps it is read at the next change or at the seal, never lost or counted twice. Once the executor sees the
stop flag it seals (``seal``): it takes the lock once and lets it go, so a writer that read the flag before the Stop
has appended and any later one is refused; then it reads to the end at once, and a last line still without its
newline is torn: ignored, logged.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import Engine, select

from laueportal.core.text import quoted
from laueportal.files.patterns import compile_filename_templates
from laueportal.jobs import state
from laueportal.jobs.arrival_index import ArrivalIndex, IndexStale, index_path
from laueportal.jobs.live import LiveDocument, inside, live_queue_id, read_live_document
from laueportal.jobs.models import Job
from laueportal.jobs.rundir import ARRIVALS_FILENAME, ARRIVALS_LOCK_FILENAME, CompletedPoint, write_empty_file

logger = logging.getLogger(__name__)

ArrivalSource = Literal["catalog", "notify", "run"]
FILE_SOURCES = ("catalog", "notify")
"""The sources whose arrivals are files in the run's folder (a follower's, ``run``, are points: ``admit_point``)."""

MAX_POINT_DEPTHS = 100_000
"""The most depths a followed point may name (its ``n_depths``; ``admit_point``). A followed point is one input per
selected depth, and with no ``depth_range`` every depth: the executor builds one ``ManifestEntry`` each, so a corrupt
line's number must never be built unchecked. A wire point holds one image per depth of its reconstruction's range (the
default -50 to 150 µm at 1 µm: 201); 100,000 is 500 times that, a point file of 0.4 TB at 1024 x 1024 float32 images,
and about 25 MB of entries for the executor to build (measured). The portal cannot know the wire run's own count without
lauelab (its grid rounds the range's ends to the resolution), so the bound is a number, not that count. So a wire run of
more depths (e.g. -0.5 to 999.5 µm at 0.01 µm: 100,001; its form only warns, a span over 500 µm and a resolution under
0.1 µm) is not refused when it is created: its follower ends Failed at its first point ("has 100,001 depths; at most
100,000"), and the wire run goes on (documented in ``docs/runbooks/deploy.md`` §8 and the user's workflows; not refused
at validation, which would need lauelab's count)."""


class ArrivalRefused(Exception):
    """A file that was not admitted; ``str()`` is the one-line reason."""


class ArrivalNotReady(ArrivalRefused):
    """A file not admitted yet: it is not there, cannot be read, or has not the catalog's size. The catalog thread
    tries it again (``jobs.catalog``); ``notify`` reports it as any refusal."""


class ArrivalsClosed(ArrivalRefused):
    """The run admits nothing more: it has ended, or it was stopped (sealed)."""


class ArrivalsUnreadable(RuntimeError):
    """A whole line of ``arrivals.jsonl`` that is not the next arrival: the ledger cannot be trusted."""


def arrivals_path(run_directory: str | os.PathLike[str]) -> str:
    return os.path.join(run_directory, ARRIVALS_FILENAME)


def lock_path(run_directory: str | os.PathLike[str]) -> str:
    return os.path.join(run_directory, ARRIVALS_LOCK_FILENAME)


def create_ledger(run_directory: str | os.PathLike[str]) -> None:
    """Make a new live run's empty ``arrivals.lock`` and ``arrivals.jsonl`` (exclusively: ``FileExistsError``)."""
    write_empty_file(lock_path(run_directory))
    write_empty_file(arrivals_path(run_directory))


def _whole(value: Any) -> int:
    if type(value) is not int:
        raise TypeError("not a whole number")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("not text")
    return value


@dataclass(frozen=True)
class Arrival:
    """One line of ``arrivals.jsonl`` (the module's docstring)."""

    n: int
    path: str
    size: int
    mtime_ns: int
    inode: int
    device: int
    source: str
    admitted_at: str
    scan_point: int
    point_id: str | None = None
    n_depths: int | None = None

    @property
    def identity(self) -> tuple[int, int, int, int]:
        return (self.size, self.mtime_ns, self.inode, self.device)

    def line(self) -> bytes:
        """Its line: compact JSON and a newline (JSONL), a file's without the point keys."""
        values = asdict(self)
        if self.point_id is None:
            del values["point_id"], values["n_depths"]
        return (json.dumps(values, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")

    @classmethod
    def from_line(cls, line: bytes, n: int) -> Arrival:
        """The arrival a whole line holds, which must be arrival ``n``; ``ArrivalsUnreadable`` (naming the line, never
        its values) for anything else."""
        try:
            values = json.loads(line)
            if not isinstance(values, dict):
                raise TypeError("not an object")
            point_id = values.get("point_id")
            arrival = cls(
                n=_whole(values["n"]),
                path=_text(values["path"]),
                size=_whole(values["size"]),
                mtime_ns=_whole(values["mtime_ns"]),
                inode=_whole(values["inode"]),
                device=_whole(values["device"]),
                source=_text(values["source"]),
                admitted_at=_text(values["admitted_at"]),
                scan_point=_whole(values["scan_point"]),
                point_id=None if point_id is None else _text(point_id),
                n_depths=None if point_id is None else _whole(values["n_depths"]),
            )
        except (ValueError, KeyError, TypeError) as error:
            reason = f"{type(error).__name__}: {error}" if not isinstance(error, KeyError) else f"no {error}"
            raise ArrivalsUnreadable(f"line {n + 1} of {ARRIVALS_FILENAME} is not an arrival ({reason})") from None
        if arrival.n != n:
            raise ArrivalsUnreadable(f"line {n + 1} of {ARRIVALS_FILENAME} is arrival {arrival.n}, not {n}")
        return arrival


@dataclass(frozen=True)
class Admission:
    """What ``admit`` did: ``admitted`` False when the file was admitted before with the same identity (a no-op)."""

    arrival: Arrival
    admitted: bool


def whole_lines(path: str, offset: int) -> tuple[list[bytes], int, bytes, tuple[int, int]]:
    """The whole lines of ``path`` after byte ``offset`` (without their newlines), the offset after the last of them,
    what follows it (a last line without its newline, or nothing), and the file's size and modification time before
    the read (so what is appended after them is a change the next time)."""
    with open(path, "rb") as handle:
        status = os.fstat(handle.fileno())
        handle.seek(offset)
        data = handle.read()
    end = data.rfind(b"\n") + 1
    return data[:end].split(b"\n")[:-1], offset + end, data[end:], (status.st_size, status.st_mtime_ns)


@contextmanager
def _locked(run_directory: str) -> Iterator[None]:
    """Hold the exclusive ``flock`` on the run's ``arrivals.lock`` (``FileNotFoundError`` without one), on a
    descriptor of its own, open for writing as flock needs on NFS (the module's docstring, "The lock")."""
    descriptor = os.open(lock_path(run_directory), os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            # Unlocked explicitly: a child forked without exec while it was held shares the open file, and closing
            # this descriptor alone would leave the lock held by the child's copy.
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def seal(run_directory: str | os.PathLike[str]) -> None:
    """Take the run's arrivals lock once and let it go: a writer that read the run open has appended by then, and
    any later one finds the stop flag (the module's docstring)."""
    with _locked(os.fspath(run_directory)):
        pass


class ArrivalReader:
    """The executor's reading of ``arrivals.jsonl`` from its offset (no lock): ``read`` the arrivals after it, then
    ``advance`` once they are counted; ``tail`` is what follows the last whole line read; ``changed`` (one stat) says
    whether a read could find anything new."""

    def __init__(self, run_directory: str | os.PathLike[str]) -> None:
        self.path = arrivals_path(run_directory)
        self.offset = 0
        self.count = 0
        self.tail = b""
        self._read: tuple[int, int] | None = None
        self._used: tuple[int, int] | None = None
        """The file's size and modification time before the last read, and before the last read advanced past."""

    def changed(self) -> bool:
        """Whether the file's size or modification time differs from the file's when the last read was used (one
        ``stat``; a read not advanced past, its arrivals not counted, leaves it changed; a rewrite that keeps the size
        changes the modification time, unless within one tick of the file system's timestamps)."""
        status = os.stat(self.path)
        return (status.st_size, status.st_mtime_ns) != self._used

    def read(self) -> tuple[list[Arrival], int]:
        """The arrivals after the offset (whole lines only) and the offset after them; nothing moves until
        ``advance``. ``ArrivalsUnreadable`` for a line that is not the next arrival."""
        lines, end, self.tail, self._read = whole_lines(self.path, self.offset)
        return [Arrival.from_line(line, self.count + index) for index, line in enumerate(lines)], end

    def advance(self, offset: int, count: int) -> None:
        self.offset = offset
        self.count += count
        self._used = self._read


class ArrivalWriter:
    """One writer of a live run's ``arrivals.jsonl`` (the catalog thread keeps one; ``notify`` makes one): its offset,
    the files admitted so far, by path, and their count. ``quiet``: a refused identity is not logged (the caller
    reports it: ``laueportal live notify``'s one line). ``index``: the run's admission index file
    (``jobs.arrival_index``; ``notify``'s), looked up under the lock instead of reading the ledger from its first
    line, so a writer with no memory of the ledger admits at a cost independent of its length; a writer without one
    keeps the ledger's paths in memory and reads only the lines written since its last look. A writer with an index
    admits files only (``admit_point`` is a follower's thread's, which keeps one writer)."""

    def __init__(
        self,
        engine: Engine,
        job_id: int,
        run_directory: str | os.PathLike[str],
        live: LiveDocument,
        *,
        quiet: bool = False,
        index: str | None = None,
    ) -> None:
        self._engine = engine
        self._job_id = job_id
        self._directory = os.fspath(run_directory)
        self._live = live
        self._matcher = compile_filename_templates([live.filename_template])
        self._quiet = quiet
        self._offset = 0
        self._count = 0
        self._tail = 0
        self._known: dict[str, Arrival] = {}
        self._points: dict[str, str] = {}
        """The followed points admitted so far: each ``point_id``'s path."""
        self._index = None if index is None else ArrivalIndex(index, self._directory, Arrival.from_line)
        self.last_read = False
        """The catalog's last read at a Stop (``jobs.catalog``): while set, a stopped run still
        admits (not an ended one). Only the run's own job process sets it, before its seal takes the lock, so nothing is
        appended after the seal."""

    @property
    def run_directory(self) -> str:
        return self._directory

    def admit(self, path: str | os.PathLike[str], *, source: ArrivalSource, size: int | None = None) -> Admission:
        """Admit the file ``path`` (absolute) from ``source`` (``catalog``, whose ``size`` the file must have, or
        ``notify``): the module's rules. ``ArrivalRefused`` with the reason when it is not admitted."""
        if source not in FILE_SOURCES:
            raise ValueError(f"{source!r} does not admit files")
        target, scan_point = self._candidate(os.fspath(path))
        identity = _identity(target)
        if size is not None and identity[0] != size:
            raise ArrivalNotReady(f"{target} has {identity[0]} bytes; the catalog says {size}")
        return self._admit(target, identity, source=source, scan_point=scan_point)

    def admit_point(self, point: CompletedPoint) -> Admission:
        """Admit a completed point of the followed wire run (a follower's thread, ``jobs.follower``; source ``run``):
        its point file, a regular file inside the followed run's directory (``live.json``'s ``follows``), with the
        point's ``scan_point``, ``point_id`` and ``n_depths`` from its line of ``completed.jsonl``; then the module's
        rules (the same path with the same identity a no-op, the run open). ``ArrivalRefused`` with the reason when it
        is not admitted (``ArrivalNotReady`` while its file cannot be read). Refused for good: a file outside the
        followed run's directory by its text or by its real path (a link inside it that leads elsewhere, as the run's
        folder is checked for a file), or not a regular file; a ``point_id`` that is not its file's stem (lauelab names
        a point file after its point) or that an admitted point of another file has; more than ``MAX_POINT_DEPTHS``
        depths; no scan point."""
        follows = self._live.follows
        if follows is None:
            raise ValueError(f"job {self._job_id} follows no run: it admits no point")
        if self._index is not None:
            raise ValueError("a writer with an admission index admits files only")
        target = os.path.normpath(point.path)
        if (
            "\0" in target
            or not os.path.isabs(target)
            or not inside(target, follows.directory)
            or not inside(os.path.realpath(target), os.path.realpath(follows.directory))
        ):
            raise ArrivalRefused(f"point {quoted(point.point_id)}'s file is not in the followed run's directory")
        if point.point_id in (".", "..") or "/" in point.point_id or "\0" in point.point_id:
            raise ArrivalRefused(f"{quoted(point.point_id)} is not a point ID (a file's stem)")  # names a shard folder
        stem = os.path.splitext(os.path.basename(target))[0]
        if point.point_id != stem:
            raise ArrivalRefused(f"point {quoted(point.point_id)} is not its file's stem {quoted(stem)}")
        if point.scan_point is None:
            raise ArrivalRefused(f"point {quoted(point.point_id)} has no scan point")
        if point.n_depths > MAX_POINT_DEPTHS:
            raise ArrivalRefused(
                f"point {quoted(point.point_id)} has {point.n_depths:,} depths; at most {MAX_POINT_DEPTHS:,}"
            )
        identity = _identity(target)
        return self._admit(
            target,
            identity,
            source="run",
            scan_point=point.scan_point,
            point_id=point.point_id,
            n_depths=point.n_depths,
        )

    def _admit(
        self,
        target: str,
        identity: tuple[int, int, int, int],
        *,
        source: ArrivalSource,
        scan_point: int,
        point_id: str | None = None,
        n_depths: int | None = None,
    ) -> Admission:
        """Under the lock: the no-op for a known path with its identity, the refusal of a changed one, the open
        check, then the line appended."""
        try:
            with _locked(self._directory), self._ledger() as ledger:
                known = self._known.get(target) if ledger is None else self._find(target, ledger)
                if known is not None:
                    if known.identity == identity:
                        return Admission(known, admitted=False)
                    reason = f"{target} changed since it was admitted as arrival {known.n}; it is not admitted again"
                    if not self._quiet:
                        logger.warning("Job %s: %s", self._job_id, reason)
                    raise ArrivalRefused(reason)
                if point_id is not None and point_id in self._points:  # another file: the same path is known above
                    raise ArrivalRefused(
                        f"point {quoted(point_id)} was admitted as {self._points[point_id]}; {target} is another file"
                    )
                closed = self._closed()
                if closed is not None:
                    raise ArrivalsClosed(closed)
                arrival = Arrival(
                    n=self._count,
                    path=target,
                    size=identity[0],
                    mtime_ns=identity[1],
                    inode=identity[2],
                    device=identity[3],
                    source=source,
                    admitted_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
                    scan_point=scan_point,
                    point_id=point_id,
                    n_depths=n_depths,
                )
                offset = self._offset
                self._append(arrival)
                if ledger is not None:
                    self._indexed(arrival, offset)
        except FileNotFoundError as error:
            ledger = {lock_path(self._directory), arrivals_path(self._directory)}
            if error.filename in ledger:
                raise ArrivalRefused(f"job {self._job_id} has no {os.path.basename(error.filename)}") from None
            raise
        return Admission(arrival, admitted=True)

    def _candidate(self, text: str) -> tuple[str, int]:
        """The path as the ledger records it (in ``live.json``'s folder) and its scan point; refused when the path is
        not one of the run's files."""
        if "\0" in text or not os.path.isabs(text):
            raise ArrivalRefused(f"{quoted(text)} is not an absolute path")
        directory, name = os.path.split(os.path.normpath(text))
        folder = self._live.folder
        if directory != folder and os.path.realpath(directory) != os.path.realpath(folder):
            raise ArrivalRefused(f"{quoted(text)} is not directly in the run's folder {folder}")
        match = self._matcher.match(name)
        if match is None or len(match.indices) != 1:
            raise ArrivalRefused(f"{quoted(name)} does not match the run's file name {self._live.filename_template}")
        return os.path.join(folder, name), match.indices[0]

    @contextmanager
    def _ledger(self) -> Iterator[int | None]:
        """Under the lock, the writer brought up to the ledger: without an index, the lines written since its last
        look read (None); with one, the index checked and brought up to it (``jobs.arrival_index``) and the ledger's
        descriptor, open while the admission reads it."""
        if self._index is None:
            self._catch_up()
            yield None
            return
        descriptor = os.open(arrivals_path(self._directory), os.O_RDONLY | os.O_CLOEXEC)
        try:
            self._index.sync(descriptor)
            self._offset, self._count, self._tail = self._index.end, self._index.count, self._index.tail
            yield descriptor
        finally:
            os.close(descriptor)
            self._index.close()

    def _find(self, target: str, ledger: int) -> Arrival | None:
        """The arrival of ``target`` the index finds (its line read back from the ledger); a stale index is rebuilt
        from the ledger and asked again."""
        assert self._index is not None
        try:
            found = self._index.find(target, ledger)
        except IndexStale as stale:
            logger.warning("Job %s: the admission index is rebuilt: %s", self._job_id, stale)
            self._index.rebuild(ledger)
            self._offset, self._count, self._tail = self._index.end, self._index.count, self._index.tail
            found = self._index.find(target, ledger)
        assert found is None or isinstance(found, Arrival)
        return found

    def _indexed(self, arrival: Arrival, offset: int) -> None:
        """Index the line just appended at ``offset``; an index that cannot be written is caught up by the next
        admission (the ledger is the source of truth)."""
        assert self._index is not None
        try:
            self._index.add(arrival.path, arrival.n, offset, self._offset - offset)
        except sqlite3.Error as error:
            logger.warning("Job %s: arrival %d not indexed yet: %s", self._job_id, arrival.n, error)

    def _catch_up(self) -> None:
        """Read the lines written since this writer's last look (under the lock)."""
        lines, end, tail, _ = whole_lines(arrivals_path(self._directory), self._offset)
        for line in lines:
            arrival = Arrival.from_line(line, self._count)
            self._known[arrival.path] = arrival
            if arrival.point_id is not None:
                self._points[arrival.point_id] = arrival.path
            self._count += 1
        self._offset = end
        self._tail = len(tail)

    def _closed(self) -> str | None:
        """Why the run admits nothing (ended, or stopped: sealed), read inside the lock; None while it is open. Read
        with SQL alone (``_job_row``), as ``notify`` reads the run."""
        row = _job_row(self._engine, self._job_id)
        if row is None:
            return f"job {self._job_id} does not exist"
        status, phase, cancel_requested_at = row[0], row[1], row[2]
        if status in state.TERMINAL_STATUSES or phase in state.TERMINAL_PHASES:
            return f"job {self._job_id} has ended ({state.STATUS_NAMES[state.JobStatus(status)]})"
        if cancel_requested_at is not None and not self.last_read:
            return f"job {self._job_id} was stopped: it admits no more files"
        return None

    def _append(self, arrival: Arrival) -> None:
        """Append ``arrival``'s line: one write and an fsync, after truncating a torn last line (under the lock)."""
        path = arrivals_path(self._directory)
        if self._tail:
            os.truncate(path, self._offset)
            logger.warning(
                "Job %s: truncated a torn last line of %s (%d bytes after arrival %d)",
                self._job_id,
                ARRIVALS_FILENAME,
                self._tail,
                self._count - 1,
            )
            self._tail = 0
        line = arrival.line()
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC)
        try:
            written = os.write(descriptor, line)
            if written != len(line):  # torn: the next writer truncates it
                raise OSError(f"wrote {written} of {len(line)} bytes of arrival {arrival.n}")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._offset += len(line)
        self._count += 1
        self._known[arrival.path] = arrival
        if arrival.point_id is not None:
            self._points[arrival.point_id] = arrival.path


def _identity(path: str) -> tuple[int, int, int, int]:
    """A regular file's identity (size, modification time, inode, device), not following a final link."""
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        raise ArrivalNotReady(f"{path} does not exist") from None
    except OSError as error:
        raise ArrivalNotReady(f"{path} cannot be read: {error.strerror}") from None
    if not stat.S_ISREG(status.st_mode):
        raise ArrivalRefused(f"{path} is not a regular file")
    return (status.st_size, status.st_mtime_ns, status.st_ino, status.st_dev)


def _job_row(engine: Engine, job_id: int) -> Any:
    """The job row's status, phase, stop flag, queue identity and run directory, by SQL on the table alone: no ORM
    mapper is configured (about 40 ms of a ``notify`` process's start)."""
    table = Job.__table__.c
    columns = (table.status, table.phase, table.cancel_requested_at, table.queue_job_id, table.run_directory)
    with engine.connect() as connection:
        return connection.execute(select(*columns).where(table.job_id == job_id)).first()


def notify(engine: Engine, job_id: int, path: str | os.PathLike[str]) -> Admission:
    """``laueportal live notify``: admit the file ``path`` (made absolute) into live run ``job_id``, which must be an
    open live run whose source is ``notify`` (``ArrivalRefused`` with the one-line reason otherwise). A file admitted
    before with the same identity is a no-op (before the run's state is looked at). The
    admission looks the path up in the run's admission index beside the database (``jobs.arrival_index``), so its
    cost does not grow with the ledger (an engine on no database file reads the ledger whole)."""
    row = _job_row(engine, job_id)
    if row is None:
        raise ArrivalRefused(f"job {job_id} does not exist")
    queue_job_id, run_directory = row[3], row[4]
    if queue_job_id != live_queue_id(job_id) or not run_directory:
        raise ArrivalRefused(f"job {job_id} is not a live run")
    try:
        live = read_live_document(run_directory)
    except (OSError, ValueError):
        raise ArrivalRefused(f"job {job_id} is not a live run: its live.json cannot be read") from None
    if live.source != "notify":
        raise ArrivalRefused(f"job {job_id} takes its files from {live.source!r}, not from notify")
    database = engine.url.database
    index = None if not database or database == ":memory:" else index_path(database, job_id)
    writer = ArrivalWriter(engine, job_id, run_directory, live, quiet=True, index=index)
    return writer.admit(os.path.abspath(path), source="notify")
