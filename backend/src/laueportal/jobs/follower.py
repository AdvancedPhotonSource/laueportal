"""The follower's thread: a live LaueGo run following a live wire run.

**Where it runs.** In the follower's job process, a thread of its own (``start_follower``, from ``execute_run``), for
a run whose ``live.json`` source is ``run`` only. It reads the followed wire run's ``completed.jsonl`` (in
``live.json``'s ``follows.directory``) and admits each completed point into its own ``arrivals.jsonl`` through
``ArrivalWriter.admit_point`` (every source writes through ``jobs.arrivals``), in the order of that file's lines:
the wire run's publication order, neither its arrival nor its scan order (so a follower's results are matched to a
batch run's by scan point). It reads the file from its first line (a follower that starts late misses
nothing). It never writes the wire run's directory: it stats and reads that one file, and opens nothing there for
writing.

**The cadence**: every ``live.follow_seconds`` (5 s), only while its run is open (its first look
at its start): a stat of ``completed.jsonl``, and a read only when its size or modification time changed since the
last read (a missing file is empty: the wire run has written no line yet); whole lines only (a last line without its
newline is read again, from its start, next time).

**The seal**. At each look the thread first asks whether the wire run has ended
(``wire_run_ended``: the executor's read of the wire run's job row, on the local database), then looks at the file.
Once the row is terminal (Finished, Failed or Cancelled), the file is read to its end at once (the wire run writes its
last lines before its row turns terminal), every point is admitted, and the thread is done: the executor's live hooks
then seal the run (``LiveExecutorHooks.arrivals``), and the compute drains every admitted input. A last line still
without its newline then (a wire run that died while writing it) is torn: ignored, logged. So a wire run that ends
Failed or Cancelled is followed to its end like a Finished one: the follower seals with what it has.

**Its ends.** Done (above); at the stop flag or the service stop (``stopping``: its run's monitor); at the seal
(``FollowerThread.close``, the hooks' ``before_seal``, so that no look follows it); when an admission finds the run
closed (``ArrivalsClosed``: stopped or ended); at the run's end (``close`` again). A Stop of the follower alone seals
only it: the wire run goes on.

**Errors.** A whole line of ``completed.jsonl`` that is not the next point (not JSON, a key missing or of another
type, ``n`` out of order: ``CompletedUnreadable``) and a point refused for good (``ArrivalRefused``: its file outside
the wire run's directory by its text or its real path, or not a regular file; its ``point_id`` not its file's stem or
another admitted file's; more than ``MAX_POINT_DEPTHS`` depths) are a run-level error of the follower, as an
unreadable line of its own ledger is (its input cannot be known): the thread keeps it and ends, and the hooks' next
look at the arrivals raises it (Failed at once, what was published kept). A file that cannot be read now (an ``OSError``; a point file not
there: ``ArrivalNotReady``) and a database error (the wire run's row, the admission's open check) are tried again at
the next look, the wait doubling from ``follow_seconds`` up to 60 s while they fail, logged once per change;
the points after a held one wait for it (publication order). While a completed point cannot be admitted the follower
does not seal: a Stop seals it with what it has. Like ``jobs.catalog`` it is a portal-side module (it reads the
database); the compute never imports it.

**Its status**: the follower's own run's ``arrival-status.json`` (the catalog thread's file, its keys)
says how its looks go: ``checked_at`` (the look it was written at), ``rows_seen`` (the lines of ``completed.jsonl``
read so far), ``error`` (while it waits, why: "point '<point_id>' (line <n> of the followed run's completed.jsonl) not
admitted yet: <why>" for a held point, else "the follower's look failed: <type>", with an ``OSError``'s text; a
database error or a defect by its type only, its text in the job's log; else null) and ``error_at`` (since when: the
first look that failed with that reason; else null). It is written at once when the waiting reason changes (a wait
starts, another reason, the look that admits the held point clears it), and otherwise at most every
``status_seconds`` (``live.publish_seconds``) while it has something to say: while it waits (its last check), or once
it has read new points; with nothing new it writes nothing (the job's heartbeat says it is alive), and never at each
look (a look is a stat); a write that failed is tried again at the next look. Its end
(``finish``: the thread's last act, and again at ``FollowerThread.close`` while that write fails) writes the last
look's status with no waiting reason, so nothing stale stays once it has ended: not a held point it later refused for
good (that is the run's error), nor a wait whose clearing write failed at its last look. A follower that wrote none
(it ended before its first) leaves none.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from laueportal.core.settings import LiveSettings
from laueportal.core.text import quoted
from laueportal.jobs import state
from laueportal.jobs.arrivals import ArrivalNotReady, ArrivalRefused, ArrivalsClosed, ArrivalWriter, whole_lines
from laueportal.jobs.contract import RunRequest
from laueportal.jobs.live import LiveDocument
from laueportal.jobs.models import Job
from laueportal.jobs.rundir import ARRIVAL_STATUS_FILENAME, COMPLETED_FILENAME, CompletedPoint, write_json_file
from laueportal.remote.dm import FAILED_WAIT_SECONDS

logger = logging.getLogger(__name__)

CLOSE_WAIT_SECONDS = 10.0
"""How long ``FollowerThread.close`` waits for the thread to end (a stat or a read on a hung mount could hold it
longer; no look starts after the close)."""


class CompletedUnreadable(RuntimeError):
    """A whole line of the followed run's ``completed.jsonl`` that is not its next point: the follower's run-level
    error (the module's docstring)."""


def wire_run_ended(engine: Engine, job_id: int) -> bool:
    """Whether the followed wire run ``job_id`` has ended (its job row terminal; a row that is gone counts as ended)."""
    with Session(engine) as session:
        job = session.get(Job, job_id)
        return job is None or state.is_terminal(job)


def _whole(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError(f"{name} is not a whole number")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{name} is not a text")
    return value


def _point(line: bytes, n: int) -> CompletedPoint:
    """The point a whole line holds, which must be point ``n``; ``CompletedUnreadable`` (naming the line, never its
    values) for anything else."""
    try:
        values = json.loads(line)
        if not isinstance(values, dict):
            raise TypeError("not an object")
        scan_point = values["scan_point"]
        point = CompletedPoint(
            n=_whole(values["n"], "n"),
            point_id=_text(values["point_id"], "point_id"),
            scan_point=None if scan_point is None else _whole(scan_point, "scan_point"),
            path=_text(values["path"], "path"),
            n_depths=_whole(values["n_depths"], "n_depths"),
            completed_at=_text(values["completed_at"], "completed_at"),
        )
    except (ValueError, KeyError, TypeError) as error:
        reason = f"no {error}" if isinstance(error, KeyError) else f"{type(error).__name__}: {error}"
        raise CompletedUnreadable(
            f"line {n + 1} of the followed run's {COMPLETED_FILENAME} is not a point ({reason})"
        ) from None
    if point.n != n:
        raise CompletedUnreadable(
            f"line {n + 1} of the followed run's {COMPLETED_FILENAME} is point {point.n}, not {n}"
        )
    return point


class CompletedReader:
    """The follower's reading of the wire run's ``completed.jsonl`` from its offset (no lock; whole lines only):
    ``changed`` is one stat (a missing file has not changed: no line yet), ``read`` the points after the offset."""

    def __init__(self, wire_directory: str | os.PathLike[str]) -> None:
        self.path = os.path.join(wire_directory, COMPLETED_FILENAME)
        self.offset = 0
        self.count = 0
        self.tail = b""
        """What follows the last whole line read: a line still being written, or one torn."""
        self._used: tuple[int, int] | None = None
        """The file's size and modification time before the last read."""

    def changed(self) -> bool:
        """Whether a read could find anything new: the file's size or modification time differs from the file's
        before the last read (one stat). A missing file has not changed."""
        try:
            status = os.stat(self.path)
        except FileNotFoundError:
            return False
        return (status.st_size, status.st_mtime_ns) != self._used

    def read(self) -> list[CompletedPoint]:
        """The points of the whole lines after the offset, the offset moved past them (none while the file is
        missing). ``CompletedUnreadable`` for a line that is not the next point (nothing moves then)."""
        try:
            lines, end, tail, used = whole_lines(self.path, self.offset)
        except FileNotFoundError:
            return []
        points = [_point(line, self.count + index) for index, line in enumerate(lines)]
        self.offset, self.tail, self._used = end, tail, used
        self.count += len(points)
        return points


class Follower:
    """One follower's looks (the module's docstring): ``step`` is one look, and returns the wait before the next, or
    None once there is none (done, the run closed, a run-level error)."""

    def __init__(
        self,
        writer: ArrivalWriter,
        reader: CompletedReader,
        *,
        ended: Callable[[], bool],
        interval: float,
        job_id: int,
        status_seconds: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.writer = writer
        self.reader = reader
        self._ended = ended
        self.interval = interval
        self._job_id = job_id
        self.pending: deque[CompletedPoint] = deque()
        """Points read and not admitted yet (one that cannot be admitted now holds those after it)."""
        self.admitted = 0
        self.done = threading.Event()
        """Set once the wire run has ended and every point of its ``completed.jsonl`` is admitted."""
        self.error: Exception | None = None
        """The follower's run-level error, for the hooks to raise."""
        self._failures = 0
        self._failing: str | None = None
        self._status_path = os.path.join(writer.run_directory, ARRIVAL_STATUS_FILENAME)
        self.status_seconds = status_seconds
        """The most often its status is written while its waiting reason stays (``live.publish_seconds``)."""
        self._clock = clock
        self._status_due = clock() + status_seconds
        self._looked_at: str | None = None
        """The time of the last look (its start)."""
        self._waiting: str | None = None
        """The waiting reason ``arrival-status.json`` says (None: not waiting)."""
        self._error_at: str | None = None
        self._unwritten: dict[str, Any] | None = None
        """The status still to write (its write failed, or it was just made)."""
        self._written = False
        self._rows_written: int | None = None
        """The points read the last status made says (None: none made)."""
        self._finished = False
        self._status_failed = False

    def step(self) -> float | None:
        self._looked_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        try:
            ended = self._ended()  # first: the wire run's last lines are written before its row turns terminal
            if ended or self.reader.changed():
                self.pending.extend(self.reader.read())
            while self.pending:
                if self.writer.admit_point(self.pending[0]).admitted:
                    self.admitted += 1
                self.pending.popleft()
        except ArrivalsClosed as closed:
            logger.info("Job %s: the follower ends: %s", self._job_id, closed)
            return None
        except ArrivalNotReady as error:
            return self.failed(error)
        except (CompletedUnreadable, ArrivalRefused) as error:
            logger.error("Job %s: the follower cannot go on: %s", self._job_id, error)
            self.error = error
            return None
        except (OSError, SQLAlchemyError) as error:
            return self.failed(error)
        if self._failing is not None:
            logger.info("Job %s: the follower reads %s again", self._job_id, self.reader.path)
            self._failures, self._failing = 0, None
        self._report(None)
        if not ended:
            return self.interval
        if self.reader.tail:
            logger.warning(
                "Job %s: ignored a torn last line of the followed run's %s (%d bytes) at its end",
                self._job_id,
                COMPLETED_FILENAME,
                len(self.reader.tail),
            )
        self.done.set()
        return None

    def failed(self, error: Exception) -> float:
        """A look that failed: the next is tried after a wait doubling up to ``FAILED_WAIT_SECONDS``; logged once per
        change of its reason."""
        self._failures += 1
        reason = f"{type(error).__name__}: {error}"
        if reason != self._failing:
            logger.warning("Job %s: the follower's look failed, tried again: %s", self._job_id, reason)
            self._failing = reason
        self._report(self._shown(error))
        return min(FAILED_WAIT_SECONDS, self.interval * 2.0**self._failures)

    def _shown(self, error: Exception) -> str:
        """The waiting reason ``arrival-status.json`` shows for ``error`` (the module's docstring; no secret)."""
        if isinstance(error, ArrivalNotReady) and self.pending:
            point = self.pending[0]
            line = f"line {point.n + 1} of the followed run's {COMPLETED_FILENAME}"
            return f"point {quoted(point.point_id)} ({line}) not admitted yet: {error}"
        if isinstance(error, OSError):
            return f"the follower's look failed: {type(error).__name__}: {error}"
        return f"the follower's look failed: {type(error).__name__}"

    def _report(self, waiting: str | None) -> None:
        """``arrival-status.json`` at once when the waiting reason changes, else once ``status_seconds`` have passed
        since the last one was made while it waits or has read new points, and again at a later look while its write
        fails (the module's docstring)."""
        if waiting != self._waiting:
            self._waiting = waiting
            self._error_at = None if waiting is None else self._looked_at
            self._unwritten = self._status()
        elif self._clock() >= self._status_due and (waiting is not None or self.reader.count != self._rows_written):
            self._unwritten = self._status()
        self._write()

    def finish(self) -> None:
        """The follower's last status (the module's docstring): its last look's, with no waiting reason, once it has
        ended; nothing when it wrote none. Done once it is written (idempotent); never raises."""
        if self._finished or not (self._written or self._waiting is not None or self._unwritten is not None):
            return
        if self._waiting is not None or self._unwritten is None:
            self._waiting = self._error_at = None
            self._unwritten = self._status()
        self._finished = self._write()

    def _status(self) -> dict[str, Any]:
        self._status_due = self._clock() + self.status_seconds
        self._rows_written = self.reader.count
        return {
            "checked_at": self._looked_at or datetime.now(UTC).replace(microsecond=0).isoformat(),
            "rows_seen": self.reader.count,
            "error": self._waiting,
            "error_at": self._error_at,
        }

    def _write(self) -> bool:
        """Writes the status still to write; whether none is left. A status that cannot be written is logged once."""
        if self._unwritten is None:
            return True
        try:
            write_json_file(self._status_path, self._unwritten, overwrite=True)
        except OSError as failure:
            if not self._status_failed:
                self._status_failed = True
                logger.warning("Job %s: %s could not be written: %s", self._job_id, ARRIVAL_STATUS_FILENAME, failure)
            return False
        self._unwritten = None
        self._written = True
        return True


class FollowerThread:
    """A ``Follower`` in a daemon thread of the job process (the module's docstring): its first look at once, then
    each after the wait the last returned; it ends when ``stopping()`` is true before a look (the run's monitor read
    the stop flag or the service stop), when a look returns None, or at ``close``. An error that escapes a look (a
    defect) is waited out like a failed look, its traceback logged once per change of its reason (not at each
    retry)."""

    def __init__(self, follower: Follower, *, stopping: Callable[[], bool]) -> None:
        self.follower = follower
        self._stopping = stopping
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._run, name="live-follower", daemon=True)

    def start(self) -> FollowerThread:
        self._thread.start()
        return self

    def close(self) -> None:
        """End the thread: no look starts after this; waits at most ``CLOSE_WAIT_SECONDS`` for one in progress, then
        writes the follower's last status again if the thread's own write of it failed (``Follower.finish``).
        Idempotent."""
        self._closed.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(CLOSE_WAIT_SECONDS)
            if self._thread.is_alive():
                logger.warning("The follower's thread is still running %g s after its close", CLOSE_WAIT_SECONDS)
                return
        if self._thread.ident is not None and not self._thread.is_alive():
            self.follower.finish()

    @property
    def done(self) -> bool:
        """The wire run has ended and every completed point is admitted: the run seals."""
        return self.follower.done.is_set()

    @property
    def error(self) -> Exception | None:
        return self.follower.error

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        delay: float | None = 0.0
        defect: str | None = None  # the last look's, logged with its traceback
        while delay is not None and not self._closed.wait(delay):
            if self._stopping():
                break
            try:
                delay = self.follower.step()
            except Exception as error:  # a defect: its traceback once per change of its reason, waited out
                if f"{type(error).__name__}: {error}" != defect:
                    defect = f"{type(error).__name__}: {error}"
                    logger.exception("The follower's look failed")
                delay = self.follower.failed(error)
            else:
                defect = None
        self.follower.finish()


def start_follower(
    engine: Engine,
    request: RunRequest,
    settings: LiveSettings,
    *,
    stopping: Callable[[], bool],
) -> FollowerThread | None:
    """The follower's thread of the live run ``request`` (``execute_run``), started; None for a run whose source is not
    a wire run (a DAQ, ``notify``)."""
    live = LiveDocument.model_validate_json(json.dumps(request.live))
    follows = live.follows
    if live.source != "run" or follows is None:
        return None
    follower = Follower(
        ArrivalWriter(engine, request.job_id, request.run_directory, live),
        CompletedReader(follows.directory),
        ended=lambda: wire_run_ended(engine, follows.job_id),
        interval=settings.follow_seconds,
        job_id=request.job_id,
        status_seconds=settings.publish_seconds,
    )
    return FollowerThread(follower, stopping=stopping).start()
