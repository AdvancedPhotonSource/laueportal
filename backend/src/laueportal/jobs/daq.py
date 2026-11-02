"""The DAQ side of a live run whose source is DM's file catalog: the DM client of the ``[live]`` settings, the DAQ list
the Live page shows, and the bind check at a live run's creation. The catalog thread that reads a bound DAQ's files is
``jobs.catalog``.

**One call each, nothing persistent**. Every DM operation is one subprocess of ``remote.dm.DMClient`` (its
script, its timeout ``live.dm_timeout_seconds``, over ``live.dm_ssh_host`` when set), with DM's setup file
``live.dm_setup_file``; without one, no call is made (``DaqUnavailable``). In the api a call runs in the api's
process group, as the submission's does.

**The DAQ list**: ``DaqList`` keeps one answer that every browser shares, and makes at most one DM
call per ``live.daq_list_seconds`` (30), only when asked: nothing refreshes it in the background, so no Live page
open means no call. A request that comes while the call runs waits for it and gets its answer (``get``; an
``async`` route awaits ``get_async``, holding no thread). A failed call is the answer too, kept as long, and each
failure in a row doubles that, up to ``remote.dm.FAILED_WAIT_SECONDS`` (60 s): a failing DM is asked less
often. The route is ``GET /live/daqs`` (``jobs.api``, the app's one ``DaqList``, ``app.state.daq_list``).

**The bind check** (``bind_daq``, once per live create: the Live page's Start, ``jobs.live_session``): DM's record of
the DAQ (one call; a DAQ DM does not know is refused) must say it is running, and the run's folder must lie inside the
DAQ's destination (its storage directory and its ``destDirectory``), itself inside the storage directory
(``inside_destination``); the answer is ``live.json``'s ``daq``. The Live page's Validate makes no call: it applies
``inside_destination`` to the DAQ's record in the DAQ list's kept answer (``DaqList.last_seen``), when there is one.
Start and Stop never touch the DAQ.

**Either source removable**: this module, ``jobs.catalog`` and the DM operations write nothing but through
``jobs.arrivals``' ``admit``, so the catalog can go without touching ``notify`` or the compute.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

import anyio.to_thread

from laueportal.core.settings import LiveSettings
from laueportal.core.text import quoted
from laueportal.core.timeutil import utcnow
from laueportal.files.roots import real_path
from laueportal.jobs.live import LiveDaq
from laueportal.remote.dm import (
    FAILED_WAIT_SECONDS,
    CallAborted,
    DaqNotFound,
    DaqRecord,
    DMClient,
    DMRecordError,
    describe_failure,
)
from laueportal.remote.settings import DMSettings, RemoteSettingsError
from laueportal.science.once import Pending

logger = logging.getLogger(__name__)

DM_ERRORS = (
    subprocess.CalledProcessError,
    subprocess.TimeoutExpired,
    OSError,
    ValueError,
    DMRecordError,
    CallAborted,
    RemoteSettingsError,
)
"""What one DM operation raises when it fails (the call, its answer, its settings)."""

NOT_CONFIGURED = "DM is not configured for live runs ([live] dm_setup_file)"
MAX_DAQ_ID_LENGTH = 200
"""The longest DAQ ID a request may name (the Live page's Scan card; DM's IDs are short): a longer one never reaches
DM."""


class DaqUnavailable(Exception):
    """DM could not be asked (not configured) or did not answer; ``str()`` is the one-line reason."""


class DaqRefused(Exception):
    """The bind check refused the DAQ or the folder; ``str()`` is the one-line reason."""


def describe(error: BaseException) -> str:
    """A failed DM operation in one line (``remote.dm.describe_failure``: never its command, which holds the API
    script), said to be DM's."""
    return f"DM call failed: {describe_failure(error)}"


def live_client(settings: LiveSettings, *, own_session: bool = False) -> DMClient:
    """The DM client of the ``[live]`` settings (``DaqUnavailable`` without ``dm_setup_file``). ``own_session``: the
    catalog thread's (each call a session of its own, killed whole at its timeout and at ``DMClient.abort``)."""
    if settings.dm_setup_file is None:
        raise DaqUnavailable(NOT_CONFIGURED)
    dm = DMSettings(
        setup_file=os.fspath(settings.dm_setup_file),
        ssh_host=settings.dm_ssh_host,
        timeout_seconds=settings.dm_timeout_seconds,
    )
    return DMClient(dm, own_session=own_session)


# ---------------------------------------------------------------------------
# The DAQ list (the Live page's)


@dataclass(frozen=True)
class DaqListAnswer:
    """The DAQ list as every browser gets it: the DAQs DM gave, the reason of each record it gave that cannot be read,
    the call's error (None when it answered), and when the call was made."""

    daqs: tuple[DaqRecord, ...]
    failed: tuple[str, ...]
    error: str | None
    checked_at: datetime


class DaqList:
    """The DM DAQ list, one answer shared by every caller (the module's docstring): at most one DM call per
    ``settings.daq_list_seconds`` (while calls fail, twice as long for each failure in a row, up to
    ``FAILED_WAIT_SECONDS``), counted from the start of the last, and only from ``get`` or ``get_async``. A caller
    that comes while a call runs waits for it. ``client``: a DM client to use (the tests'); else one is made from the
    settings for each call."""

    def __init__(
        self,
        settings: LiveSettings,
        *,
        client: DMClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._client = client
        self._clock = clock
        self._lock = threading.Lock()
        self._calls: dict[str, Pending] = {}
        self._answer: DaqListAnswer | None = None
        self._asked_at = 0.0
        self._kept_for = settings.daq_list_seconds
        """How long the kept answer stands: ``daq_list_seconds``, doubled for each failed call in a row up to
        ``FAILED_WAIT_SECONDS``."""

    def _claim(self) -> tuple[DaqListAnswer | None, Pending | None, bool]:
        """Under the lock: the kept answer while it is fresh; else the call running now (to wait for); else a new
        call, this caller's to make (``True``)."""
        with self._lock:
            now = self._clock()
            if self._answer is not None and now - self._asked_at < self._kept_for:
                return self._answer, None, False
            pending = self._calls.get("daqs")
            if pending is not None:
                return None, pending, False
            pending = Pending()
            self._calls["daqs"] = pending
            self._asked_at = now
            return None, pending, True

    def _settle(self, pending: Pending) -> DaqListAnswer:
        return pending.settle(self._call, keep=self._keep, lock=self._lock, table=self._calls, keys=("daqs",))

    def get(self) -> DaqListAnswer:
        """The shared answer: the kept one, the one being made, or a new call's (the module's docstring)."""
        answer, pending, mine = self._claim()
        if answer is not None:
            return answer
        assert pending is not None
        return self._settle(pending) if mine else pending.wait()

    async def get_async(self) -> DaqListAnswer:
        """``get`` for an ``async`` route: a new call runs in a worker thread, and a caller waiting for a call holds no
        thread (``Pending.wait_async``)."""
        answer, pending, mine = self._claim()
        if answer is not None:
            return answer
        assert pending is not None
        if mine:
            return await anyio.to_thread.run_sync(self._settle, pending)
        return await pending.wait_async()

    def time_left(self) -> float:
        """Seconds before a request may get a new call, not the kept answer (0: now): ``daq_list_seconds`` from the
        last call's start, or, while calls fail, the longer time a failure is kept (``GET /live/daqs``' 503
        ``Retry-After``)."""
        with self._lock:
            return max(0.0, self._asked_at + self._kept_for - self._clock())

    def last_seen(self) -> DaqListAnswer | None:
        """The kept answer, whatever its age, or ``None`` before the first call ended; never a call (the Live page's
        Validate checks a DAQ against it, ``jobs.live_session``)."""
        with self._lock:
            return self._answer

    def _keep(self, answer: DaqListAnswer) -> None:
        with self._lock:
            base = self._settings.daq_list_seconds
            failing = answer.error is not None and self._answer is not None and self._answer.error is not None
            self._kept_for = min(2 * self._kept_for, max(FAILED_WAIT_SECONDS, base)) if failing else base
            self._answer = answer

    def _call(self) -> DaqListAnswer:
        """One DM call (none without the settings); a failure is the answer's ``error``, logged once per change."""
        checked_at = utcnow()
        try:
            client = self._client or live_client(self._settings)
            records = client.daqs()
        except (DaqUnavailable, *DM_ERRORS) as error:
            reason = str(error) if isinstance(error, DaqUnavailable) else describe(error)
            previous = self._answer.error if self._answer is not None else None
            if reason != previous:
                logger.warning("The DM DAQ list failed: %s", reason)
            return DaqListAnswer((), (), reason, checked_at)
        daqs = tuple(record for record in records if isinstance(record, DaqRecord))
        failed = tuple(str(record) for record in records if isinstance(record, DMRecordError))
        return DaqListAnswer(daqs, failed, None, checked_at)


# ---------------------------------------------------------------------------
# The bind check (once per live create)


def _destination(record: DaqRecord) -> tuple[str, str]:
    """The DAQ's storage directory and destination as DM gives them: the
    storage absolute, the destination relative to it without ``..`` (none: the storage itself)."""
    storage = record.storage_directory or ""
    if not os.path.isabs(storage):
        raise DaqRefused(f"DAQ {record.id} has no absolute storage directory")
    suffix = PurePosixPath(record.dest_directory or ".")
    if suffix.is_absolute() or ".." in suffix.parts:
        raise DaqRefused(f"DAQ {record.id} has an invalid destination directory")
    return storage, os.path.join(storage, os.fspath(suffix))


def destination(record: DaqRecord) -> str | None:
    """Where DAQ ``record`` writes, as DM gives it (its storage directory joined with its ``destDirectory``,
    normalized, unresolved: the Live page's DAQ list); ``None`` when the bind check would refuse it (no absolute
    storage directory, a destination that leaves it)."""
    try:
        return os.path.normpath(_destination(record)[1])
    except DaqRefused:
        return None


def bind_daq(
    settings: LiveSettings,
    daq_id: str,
    folder: str,
    *,
    client: DMClient | None = None,
    resolve: Callable[[str], str] | None = None,
) -> LiveDaq:
    """The bind check at a live run's creation (the module's docstring): DM's record of DAQ ``daq_id`` (one call) says
    it is running, and ``folder`` (the run's folder as its creation records it: absolute, resolved by
    ``files.roots.resolve_inside_roots``) lies inside its destination, which lies inside its storage directory, both
    resolved on this host (``files.roots.real_path``, through the probe; ``resolve`` the tests' stand-in). Returns
    ``live.json``'s ``daq``. ``DaqRefused`` with the reason, or ``DaqUnavailable`` when DM cannot be asked or does
    not answer; a mount that does not answer is the probe's ``NotResponding`` (a 503 problem)."""
    if not os.path.isabs(folder):
        raise DaqRefused("the folder must be an absolute path")
    try:
        record = (client or live_client(settings)).daq(daq_id)
    except DaqNotFound:
        raise DaqRefused(f"DM has no DAQ {quoted(daq_id)}: select a running DAQ") from None
    except DMRecordError as error:
        raise DaqRefused(f"DM has no readable record of DAQ {quoted(daq_id)}: {error}") from None
    except DM_ERRORS as error:
        raise DaqUnavailable(describe(error)) from None
    if (record.status or "").lower() != "running":
        raise DaqRefused(f"DAQ {record.id} is not running ({record.status or 'no status'}): select a running DAQ")
    return inside_destination(record, folder, resolve=resolve)


def inside_destination(record: DaqRecord, folder: str, *, resolve: Callable[[str], str] | None = None) -> LiveDaq:
    """The bind check's rule on the folder, with DM's ``record`` of the DAQ (no call; the Live page's Validate takes
    it from the DAQ list it keeps): ``folder`` (absolute, resolved) lies inside the DAQ's destination, which lies
    inside its storage directory, both resolved on this host (``files.roots.real_path``, through the probe;
    ``resolve`` the tests' stand-in). Returns ``live.json``'s ``daq``; ``DaqRefused`` with the reason, the probe's
    ``NotResponding`` for a mount that does not answer."""
    storage, destination = _destination(record)
    resolved = resolve or (lambda path: os.fspath(real_path(path)))
    storage, destination = resolved(storage), resolved(destination)
    if os.path.commonpath((destination, storage)) != storage:
        raise DaqRefused(f"DAQ {record.id}'s destination {destination} lies outside its storage directory {storage}")
    folder = os.path.normpath(folder)
    if os.path.commonpath((folder, destination)) != destination:
        raise DaqRefused(f"The folder must lie inside DAQ {record.id}'s destination {destination}")
    return LiveDaq(id=record.id, experiment=record.experiment, storage=storage, destination=destination)
