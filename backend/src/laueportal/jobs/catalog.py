"""The catalog thread: a live run's arrivals from DM's file catalog.

**Where it runs.** In the live run's job process, a thread of its own (``start_catalog``, from ``execute_run``), for a
run whose ``live.json`` source is ``daq`` (a live wire run, or an indexing-only live LaueGo run): never for a
``notify`` run or a follower, never in the api or the worker. So it runs only while its run is Running (a Queued run
has no job process; its files are found by the first scan), and it ends at the seal: the executor closes it before it
takes the arrivals lock for the seal (``LiveExecutorHooks.before_seal``, ``CatalogThread.before_seal``): the DM call
in flight is ended and the thread's next refused, then one last catalog read is made (one cycle,
one DM call within the usual deadline, its files admitted though the run's stop flag is set, since the seal follows it
in this process; the pages past its cap or deadline stay unread; a DM failure then is in ``arrival-status.json`` and the
seal proceeds), and the client is closed, refusing any later call, so no call is made after the seal. It also ends when
its run's monitor reads the stop flag or the service stop (its last read is the seal's), when an admission finds the run
closed (``ArrivalsClosed``), and with the run (``close`` in ``execute_run``'s end).

**One DM call per cycle** (``CatalogReader.step``): one subprocess (``remote.dm.DMClient.catalog``)
for the DAQ's experiment and the run's folder (relative to ``live.json``'s ``daq.storage``) reads every page of the
cycle's queries, in order, one request at a time of ``catalog_page_size`` rows (1,000), within ``catalog_max_rows``
rows in all (5,000), and, after the first request, none that the longest so far says would end past half of
``dm_timeout_seconds`` from the DM script's start (``DEADLINE_SHARE``):
- the cursor's query: the rows catalogued since the cursor, less an overlap (``fileProcessingTime`` at least the
  newest seen less ``OVERLAP_SECONDS``, 120 s), so a row catalogued late, or seen twice, is read again and admitted
  once;
- on the first cycle, every ``catalog_rescan_seconds`` (300, counted from the start of the last) and until a full scan
  was read to its end (no cursor yet), a full scan of the folder's rows, after the cursor's query;
- a query read to its end (a page shorter than asked) is done: the cursor moves to the newest processing time of the
  run's rows it showed. A query the row cap or the time stopped keeps its place (``skip``; the cursor does not move),
  and the next cycle goes on with it at once, still one call at a time;
- while no row of the run was seen, a full scan read to its end sets a provisional cursor: the earlier of the portal's
  clock at the scan's start and the newest processing time of any row it showed plus the overlap (the clock's alone
  for an empty folder), so the next cycles query from it, less the overlap, instead of reading the whole folder
  again (else a folder of other files would be read whole at every idle cycle): from the other
  files' newest time, or from the clock less the overlap while they are newer than that (a burst catalogued just
  before the scan is read again until the next re-scan). Another file's time can only lower it below
  the clock, never move it past (only the run's own rows advance a cursor). The run's first row replaces it,
  whatever its time; from then on the cursor only moves forward. Its cost is the two clocks: with the portal's more
  than 120 s ahead of DM's and no other file to bound it, the run's first files can wait for the next re-scan
  (``catalog_rescan_seconds``), and none is lost. With another file to bound it and the portal's clock ahead of DM's,
  a first frame catalogued late, with a time before the other files' newest row, is not read by the idle query (which
  starts at that row): it waits for the run's next frame or for the re-scan (64 s, or 309 s with no further frame
  before the re-scan, in a measured case), and none is lost;
- the cadence: ``catalog_seconds`` (5 s) after a cycle that admitted a file; after one that admitted nothing,
  the wait doubles, from ``catalog_seconds`` up to ``catalog_idle_seconds`` (30 s); a failed call or admission doubles
  it up to ``remote.dm.FAILED_WAIT_SECONDS`` (60 s) and is tried again (the same pages), never failing the run.

**Admission** goes through ``jobs.arrivals``' ``ArrivalWriter.admit`` only, source ``catalog``, with the
catalog's size. A row's checks, in order: its DAQ, its experiment and its path (relative, without ``..``,
directly in the run's folder: else it is not used, and reported); then the run's file name template (another file of
the folder is passed over: never admitted, its size never read and its time only bounding a provisional cursor, so it
is never in the status's error; ``rows_seen`` counts it); then its size (a positive whole number, else not admitted,
and reported) and its time (else the file is admitted but its time not used for the cursor, and reported). The file
is the storage directory joined to the row's ``experimentFilePath``. A file not there yet or without the catalog's
size is held back (``ArrivalNotReady``) and tried again at each cycle, by its own stat (no DM call), until it
matches; a file refused for good (changed since it was admitted, not a regular file) is not tried again.

**``arrival-status.json``**, replaced when its ``rows_seen`` or its error change, and for a newer ``checked_at`` alone
at most every ``STATUS_REFRESH_SECONDS`` (60 s): ``checked_at`` (the last query's time, aware UTC to the second),
``rows_seen`` (the folder's distinct rows the catalog has shown so far), ``error`` (the last query's: a failed call or
admission; else the rows it could not use, "N catalog row(s) not used: <the first>" (a row two queries of one cycle show
counted once), and the files not admitted, "N catalogued file(s) not admitted yet, M refused: <the latest reason>",
joined by "; "; null when it had none; a held file counts only once it has been held across a full re-scan, one that
started after it was first held, so a file the catalog shows a cycle or two before this host does is no error, while one
refused for good counts at once) and ``error_at`` (when that error was first seen in a row of queries with it; null with
no error). Each change of error is logged once. A cycle whose held files' retry fails before any query reports its error
with the last query's ``checked_at``; so does a cycle that raised (a defect: "the catalog's cycle failed: <type>:
<reason>"). A DAQ run whose thread cannot start (no DM settings, or anything else) fails at its setup
(``CatalogUnavailable``): it would have no source.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Protocol

from sqlalchemy import Engine

from laueportal.core.settings import LiveSettings
from laueportal.files.patterns import compile_filename_templates
from laueportal.jobs.arrivals import ArrivalNotReady, ArrivalRefused, ArrivalsClosed, ArrivalWriter
from laueportal.jobs.contract import RunRequest
from laueportal.jobs.daq import DM_ERRORS, DaqUnavailable, describe, live_client
from laueportal.jobs.live import LiveDaq, LiveDocument
from laueportal.jobs.rundir import ARRIVAL_STATUS_FILENAME, write_json_file
from laueportal.remote.dm import FAILED_WAIT_SECONDS, CallAborted, CatalogRead, CatalogRow, catalog_query

logger = logging.getLogger(__name__)

OVERLAP_SECONDS = 120.0
"""The overlap: an incremental query reads the rows catalogued since the newest seen less this."""

DEADLINE_SHARE = 0.5
"""The share of ``live.dm_timeout_seconds`` a cycle's call reads within: after its first request, the DM script starts
none that the longest request so far says would end past this share from the script's own start. The rest of the
timeout is the margin for what the script cannot time (the login shell, an SSH bridge, Python's start) and for a
request slower than those before it."""

CLOSE_WAIT_SECONDS = 10.0
"""How long ``CatalogThread.close`` waits for the thread to end (a call in flight is aborted at once; an admission's
stat on a hung mount could hold it longer, and no call can start after the close)."""

STATUS_REFRESH_SECONDS = 60.0
"""``arrival-status.json`` is replaced when its rows seen or its error change; a newer ``checked_at`` alone, at most
this often."""


class CatalogUnavailable(RuntimeError):
    """A DAQ run's catalog thread could not start: the run fails at its setup (it would have no source)."""


class CatalogClient(Protocol):
    def catalog(
        self,
        experiment: str,
        reads: Sequence[tuple[Mapping[str, Any], int]],
        *,
        limit: int,
        cap: int,
        seconds: float,
    ) -> tuple[CatalogRead, ...]: ...

    def abort(self, *, later: bool = False) -> None: ...

    def resume(self) -> None: ...


def status_path(run_directory: str | os.PathLike[str]) -> str:
    return os.path.join(run_directory, ARRIVAL_STATUS_FILENAME)


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat()


@dataclass
class _Query:
    """A catalog query being read: its ``fileProcessingTime`` bound (None: every row, a full scan), the row its next
    page starts at, the newest processing time of the run's rows its pages showed and of any row they showed; a full
    scan's number (the first is 1) and the portal's clock at its start (seconds)."""

    since: float | None
    skip: int = 0
    newest: float | None = None
    latest: float | None = None
    scan: int = 0
    started: float = 0.0


class _RowRefused(Exception):
    """A catalog row the run cannot use (another DAQ, outside its folder, an unsafe path)."""


class CatalogReader:
    """The catalog thread's rules (the module's docstring) for the run's ``live.json`` and its DAQ ``daq``, one cycle
    per ``step(now)`` (``now``: the thread's clock, ``time.monotonic``), which returns the seconds until the next cycle
    (0: a query goes on at once), or None once the run admits nothing more (closed) or the client was aborted.
    ``wall`` gives the times ``arrival-status.json`` records."""

    def __init__(
        self,
        engine: Engine,
        job_id: int,
        run_directory: str | os.PathLike[str],
        live: LiveDocument,
        daq: LiveDaq,
        settings: LiveSettings,
        client: CatalogClient,
        *,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._job_id = job_id
        self._daq = daq
        self._folder = PurePosixPath(os.path.relpath(live.folder, daq.storage))
        self._settings = settings
        self._client = client
        self._wall = wall
        self._writer = ArrivalWriter(engine, job_id, run_directory, live)
        self._matcher = compile_filename_templates([live.filename_template])
        self._status_path = status_path(run_directory)
        self._incremental: _Query | None = None
        self._full: _Query | None = None
        self._cursor: float | None = None
        self._provisional = False
        """The cursor is a full scan's (no row of the run seen yet; the module's docstring), not the run's rows'."""
        self._full_at: float | None = None
        self._scans = 0
        """The full scans started."""
        self._scanned = 0
        """The number of the last full scan read to its end."""
        self._wait = settings.catalog_seconds
        self._known: set[str] = set()
        """The files admitted, or refused for good: not tried again."""
        self._held: dict[str, int] = {}
        """The files not ready yet (``ArrivalNotReady``), with the catalog's size: tried again each cycle."""
        self._held_since: dict[str, int] = {}
        """The held files, with the number of full scans started when each was first held."""
        self._refused: set[str] = set()
        """The files ``admit`` refused for good."""
        self._unadmitted: dict[str, str] = {}
        """The held and refused files' reasons, the latest last (``arrival-status.json``'s error)."""
        self._seen: set[str] = set()
        self._logged: set[str] = set()
        self._checked_at: datetime | None = None
        self._error: str | None = None
        self._error_at: datetime | None = None
        self._status: dict[str, Any] | None = None
        self.admitted = 0
        """The files this reader admitted."""

    @property
    def idle_seconds(self) -> float:
        return self._settings.catalog_idle_seconds

    # -- one cycle

    def step(self, now: float) -> float | None:
        try:
            arrived = self._retry_held()
        except ArrivalsClosed:
            return None
        except Exception as error:  # the ledger, its lock, the database: the next cycle, never the run's failure
            return self._failed(f"admission failed: {type(error).__name__}: {error}")
        checked_at = self._wall()
        queries = self._queries(now, checked_at)
        try:
            reads = self._client.catalog(
                self._daq.experiment,
                [(catalog_query(self._daq.id, str(self._folder), query.since), query.skip) for query in queries],
                limit=self._settings.catalog_page_size,
                cap=self._settings.catalog_max_rows,
                seconds=self._settings.dm_timeout_seconds * DEADLINE_SHARE,
            )
        except CallAborted:
            return None
        except DM_ERRORS as error:
            return self._failed(describe(error), checked_at)
        try:
            admitted, problems = self._admit(queries, reads)
        except ArrivalsClosed:
            return None
        except Exception as error:  # as above; the same pages are read again, their admitted files known
            return self._failed(f"admission failed: {type(error).__name__}: {error}", checked_at)
        arrived += admitted
        more = False
        for query, read in zip(queries, reads, strict=True):
            if read.more:
                query.skip += read.count
                more = True
            else:
                self._done(query)
        parts = [f"{len(problems)} catalog row(s) not used: {problems[0]}"] if problems else []
        waiting = {path for path in self._held if self._held_since[path] < self._scanned}  # across a full re-scan
        reasons = [reason for path, reason in self._unadmitted.items() if path in waiting or path in self._refused]
        if reasons:
            parts.append(
                f"{len(waiting)} catalogued file(s) not admitted yet, {len(self._refused)} refused: {reasons[-1]}"
            )
        self._report(checked_at, "; ".join(parts) or None)
        if arrived:
            self._wait = self._settings.catalog_seconds
        if more:
            return 0.0
        if arrived:
            return self._wait
        return self._back_off(self._settings.catalog_idle_seconds)

    def _queries(self, now: float, clock: datetime) -> list[_Query]:
        """The cycle's queries, those in progress going on: the cursor's (once there is one, provisional or the run's),
        then a full scan on the first cycle, every ``catalog_rescan_seconds`` from the last one's start, and while
        there is no cursor; ``clock``: the portal's, a new full scan's start."""
        rescan = self._full_at is None or now - self._full_at >= self._settings.catalog_rescan_seconds
        if self._full is None and (self._cursor is None or rescan):
            self._full_at = now
            self._scans += 1
            self._full = _Query(since=None, scan=self._scans, started=clock.timestamp())
        if self._incremental is None and self._cursor is not None:
            self._incremental = _Query(since=max(0.0, self._cursor - OVERLAP_SECONDS))
        return [query for query in (self._incremental, self._full) if query is not None]

    def _done(self, query: _Query) -> None:
        """A query read to its end: the cursor moves to the newest of the run's rows it showed (the run's first row
        replaces a provisional cursor, whatever its time); a full scan that showed none while the run has no row yet
        sets the provisional cursor: the earlier of the portal's clock at its start and the newest time of any row it
        showed plus ``OVERLAP_SECONDS`` (the clock's for an empty folder), so the next query starts at that row."""
        if query.newest is not None:
            if self._cursor is None or self._provisional:
                self._cursor = query.newest
            else:
                self._cursor = max(self._cursor, query.newest)
            self._provisional = False
        elif query is self._full and (self._cursor is None or self._provisional):
            self._cursor = query.started if query.latest is None else min(query.started, query.latest + OVERLAP_SECONDS)
            self._provisional = True
        if query is self._incremental:
            self._incremental = None
        else:
            self._scanned = query.scan
            self._full = None

    def _back_off(self, longest: float) -> float:
        """The wait after a cycle that admitted nothing, or failed: the current one, then doubled up to
        ``longest``."""
        delay = min(self._wait, longest)
        self._wait = min(2 * delay, longest)
        return delay

    def _failed(self, reason: str, checked_at: datetime | None = None) -> float:
        self._report(checked_at, reason)
        return self._back_off(max(FAILED_WAIT_SECONDS, self._settings.catalog_idle_seconds))

    # -- admission

    def _admit(self, queries: list[_Query], reads: tuple[CatalogRead, ...]) -> tuple[int, list[str]]:
        """The cycle's rows, each read's in order: the files admitted now, and the rows not used, a row that two reads
        show (a re-scan cycle's, inside the overlap) once."""
        admitted = 0
        problems: Counter[str] = Counter()
        for query, read in zip(queries, reads, strict=True):
            found = [str(failure) for failure in read.failed]
            for row in read.rows:
                if row.processing_time is not None:
                    seen = row.processing_time
                    query.latest = seen if query.latest is None else max(query.latest, seen)
                admitted += self._admit_row(query, row, found)
            problems |= Counter(found)  # the most any one read showed of each
        return admitted, list(problems.elements())

    def _admit_row(self, query: _Query, row: CatalogRow, problems: list[str]) -> int:
        """One row, checked in order (the module's docstring): 1 when its file was admitted now."""
        try:
            path = self._path(row)
        except _RowRefused as refused:
            problems.append(str(refused))
            self._log_once(row.path, "Job %s: %s", refused)
            return 0
        self._seen.add(path)
        if self._matcher.match(os.path.basename(path)) is None:
            return 0  # another file of the folder: never the run's
        if row.processing_time is None:
            problems.append(f"catalog row {row.path}: fileProcessingTime is not a time")
        else:
            query.newest = row.processing_time if query.newest is None else max(query.newest, row.processing_time)
        if path in self._known:
            return 0
        if row.size is None:
            problems.append(f"catalog row {row.path}: fileSize is not a positive whole number")
            return 0
        return self._try(path, row.size)

    def _path(self, row: CatalogRow) -> str:
        """The file a row names, on this host: the DAQ's and its experiment's, directly in
        the run's folder, relative and without ``..``; joined to the storage directory."""
        if row.daq_id != self._daq.id:
            raise _RowRefused(f"catalog row {row.path} belongs to another DAQ")
        if row.experiment != self._daq.experiment:
            raise _RowRefused(f"catalog row {row.path} belongs to another experiment")
        relative = PurePosixPath(row.path)
        if relative.is_absolute() or ".." in relative.parts:
            raise _RowRefused(f"catalog row {row.path} has an unsafe path")
        if relative.parent != self._folder:
            raise _RowRefused(f"catalog row {row.path} lies outside the run's folder")
        return os.path.join(self._daq.storage, *relative.parts)

    def _retry_held(self) -> int:
        """The held files tried again, by their stat (no DM call)."""
        return sum(self._try(path, size) for path, size in list(self._held.items()))

    def _try(self, path: str, size: int) -> int:
        """Admit ``path`` with the catalog's ``size``: 1 when it was admitted now. A file not ready is held; one
        refused for good is known (not tried again); both are reported until admitted. ``ArrivalsClosed`` ends the
        thread."""
        try:
            admission = self._writer.admit(path, source="catalog", size=size)
        except ArrivalNotReady as reason:
            if path not in self._held:
                logger.info("Job %s: not admitted yet (%s); tried again each cycle", self._job_id, reason)
                self._held_since[path] = self._scans
            self._held[path] = size
            self._note(path, str(reason))
            return 0
        except ArrivalsClosed:
            raise
        except ArrivalRefused as reason:
            self._held.pop(path, None)
            self._held_since.pop(path, None)
            self._known.add(path)
            self._refused.add(path)
            self._note(path, str(reason))
            self._log_once(path, "Job %s: catalogued file not admitted: %s", reason)
            return 0
        self._held.pop(path, None)
        self._held_since.pop(path, None)
        self._unadmitted.pop(path, None)
        self._known.add(path)
        if admission.admitted:
            self.admitted += 1
            return 1
        return 0

    def _note(self, path: str, reason: str) -> None:
        self._unadmitted.pop(path, None)
        self._unadmitted[path] = reason

    def _log_once(self, key: str, message: str, *args: Any) -> None:
        if key not in self._logged:
            self._logged.add(key)
            logger.warning(message, self._job_id, *args)

    # -- arrival-status.json

    def _only_checked(self, status: dict[str, Any]) -> bool:
        """Whether ``status`` differs from the one written only by a ``checked_at`` less than
        ``STATUS_REFRESH_SECONDS`` newer (not written)."""
        written = self._status
        if written is None or any(status[key] != written[key] for key in status if key != "checked_at"):
            return False
        if written["checked_at"] is None or status["checked_at"] is None:
            return False
        newer = datetime.fromisoformat(status["checked_at"]) - datetime.fromisoformat(written["checked_at"])
        return newer.total_seconds() < STATUS_REFRESH_SECONDS

    def report_defect(self, reason: str) -> None:
        """A cycle that raised (a defect): its reason in ``arrival-status.json``, the last
        query's ``checked_at`` kept; never raises."""
        try:
            self._report(None, reason)
        except Exception:
            logger.exception("Job %s: %s could not be written", self._job_id, ARRIVAL_STATUS_FILENAME)

    def last_read(self, now: float) -> None:
        """One last catalog read at a Stop, before the seal: one cycle (``step``: one DM call,
        within the usual deadline; the pages past it stay unread), whose files the run admits though its stop flag is
        set (the seal follows in this job process: ``ArrivalWriter.last_read``); a DM failure then is in
        ``arrival-status.json`` and the seal proceeds. Never raises."""
        self._writer.last_read = True
        try:
            self.step(now)
        except Exception as error:
            logger.exception("Job %s: the catalog's last read failed", self._job_id)
            self.report_defect(f"the catalog's last read failed: {type(error).__name__}: {error}")
        finally:
            self._writer.last_read = False

    def _report(self, checked_at: datetime | None, error: str | None) -> None:
        """``arrival-status.json`` after a query at ``checked_at`` (None: no query ran; the last one's time stays)."""
        if checked_at is not None:
            self._checked_at = checked_at
        if error != self._error:
            if error is not None:
                logger.warning("Job %s: the DM catalog: %s", self._job_id, error)
            else:
                logger.info("Job %s: the DM catalog answers again", self._job_id)
            self._error_at = (checked_at or self._wall()) if error is not None else None
        self._error = error
        status = {
            "checked_at": None if self._checked_at is None else _utc_text(self._checked_at),
            "rows_seen": len(self._seen),
            "error": error,
            "error_at": None if self._error_at is None else _utc_text(self._error_at),
        }
        if status == self._status or self._only_checked(status):
            return
        try:
            write_json_file(self._status_path, status, overwrite=True)
        except OSError as failure:
            self._log_once(self._status_path, "Job %s: %s could not be written: %s", ARRIVAL_STATUS_FILENAME, failure)
            return
        self._status = status


class CatalogThread:
    """A ``CatalogReader`` in a daemon thread of the job process (the module's docstring): its first cycle at once,
    then each after the wait the last returned; it ends when ``stopping()`` is true at a cycle (the run's monitor read
    the stop flag or the service stop), when the reader returns None, or at ``close`` (the seal, the run's end), which
    aborts the call in flight and refuses any later one. An error that escapes a cycle (a defect) is waited out, never
    the run's, its traceback logged once per change of its reason (not at each retry)."""

    def __init__(
        self,
        reader: CatalogReader,
        client: CatalogClient,
        *,
        stopping: Callable[[], bool],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.reader = reader
        self._client = client
        self._stopping = stopping
        self._clock = clock
        self._closed = threading.Event()
        self._client_closed = False
        self._thread = threading.Thread(target=self._run, name="live-catalog", daemon=True)

    def start(self) -> CatalogThread:
        self._thread.start()
        return self

    def close(self) -> None:
        """End the thread: no call starts after this (``DMClient.abort``), and the one in flight is killed; waits at
        most ``CLOSE_WAIT_SECONDS`` for the thread. Idempotent."""
        self._closed.set()
        self._client_closed = True
        self._client.abort()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(CLOSE_WAIT_SECONDS)
            if self._thread.is_alive():
                logger.warning("The catalog thread is still running %g s after its close", CLOSE_WAIT_SECONDS)

    def before_seal(self) -> None:
        """At a Stop, before the seal takes the arrivals lock (``LiveExecutorHooks.before_seal``): no cycle starts any
        more, the call in flight is ended at once and the client refuses the thread's next until the last read
        (``DMClient.abort(later=True)``: a cycle past its stop check calls no DM); once the
        thread has ended (``CLOSE_WAIT_SECONDS`` at most: an admission's stat could hold it), one last catalog read in
        this thread (``CatalogReader.last_read``, one call within the usual deadline; ``DMClient.resume``), then
        ``close``, so no call follows the seal. A thread still running then gets no last read."""
        self._closed.set()
        if not self._client_closed:
            self._client.abort(later=True)
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(CLOSE_WAIT_SECONDS)
        if self._thread.is_alive():
            logger.warning("The catalog thread is still running at the seal: no last read")
        elif not self._client_closed:
            self._client.resume()
            self.reader.last_read(self._clock())
        self.close()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        delay: float | None = 0.0
        defect: str | None = None  # the last cycle's, logged with its traceback
        while delay is not None and not self._closed.wait(delay):
            if self._stopping():
                break
            try:
                delay = self.reader.step(self._clock())
            except Exception as error:  # a defect: its traceback once per change of its reason, never the run's failure
                if f"{type(error).__name__}: {error}" != defect:
                    defect = f"{type(error).__name__}: {error}"
                    logger.exception("The catalog thread's cycle failed")
                self.reader.report_defect(f"the catalog's cycle failed: {defect}")
                delay = self.reader.idle_seconds
            else:
                defect = None


def start_catalog(
    engine: Engine,
    request: RunRequest,
    settings: LiveSettings,
    *,
    stopping: Callable[[], bool],
) -> CatalogThread | None:
    """The catalog thread of the live run ``request`` (``execute_run``), started; None for a run whose source is not a
    DAQ (``notify``, a follower), which reads no catalog. A DAQ run whose thread cannot start (no DM settings,
    ``live.dm_setup_file``, which ``arrival-status.json`` then says; anything else, logged) would have no source:
    ``CatalogUnavailable``, which fails the run at its setup. Once started, the catalog never
    fails its run."""
    try:
        live = LiveDocument.model_validate_json(json.dumps(request.live))
        if live.source != "daq" or live.daq is None:  # the one source check: a notify run, a follower
            return None
        try:
            client = live_client(settings, own_session=True)
        except DaqUnavailable as unavailable:
            now = _utc_text(datetime.now(UTC))
            status = {"checked_at": None, "rows_seen": 0, "error": str(unavailable), "error_at": now}
            write_json_file(status_path(request.run_directory), status, overwrite=True)
            raise CatalogUnavailable(f"no DM catalog: {unavailable}") from None
        reader = CatalogReader(engine, request.job_id, request.run_directory, live, live.daq, settings, client)
        return CatalogThread(reader, client, stopping=stopping).start()
    except CatalogUnavailable:
        raise
    except Exception as error:
        logger.exception("Job %s: the catalog thread could not start", request.job_id)
        raise CatalogUnavailable(f"the DM catalog could not start: {type(error).__name__}: {error}") from error
