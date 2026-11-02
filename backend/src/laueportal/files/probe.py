"""Bounded file checks: production keeps run directories and inputs on ``/net`` mounts that can
be absent, unreadable or hung. A stat or a listing on a hung NFS mount blocks in the kernel, where no timeout
reaches it and no thread can be stopped, so a request never makes one in its own thread: it runs the check on a
small pool of the process's own (``FileProbe``, ``bounded``) and waits at most ``timeout`` for it.

- **Not responding.** A check that does not return in time is ``NotResponding`` (a 503 problem with
  ``Retry-After``, or the state ``not_responding`` where an answer reports one), and its mount is remembered as
  hung: while that check is still in the kernel, and for ``hung_seconds`` after it timed out, every check of a path
  on the mount is ``NotResponding`` at once, without a thread. So a hung mount costs a request at most one
  ``timeout``, and the checks after it answer at once; once its stuck checks return (the mount came back) and the
  memory has passed, the next check looks again.
- **The bound on threads stuck in the kernel:** the pool has ``threads`` daemon threads and never grows, so at
  most that many threads are ever stuck, whatever the requests. One mount has at most ``per_mount`` checks in
  flight (``CHECKS_PER_MOUNT``, 2; fewer than the threads): another check of it waits for one of them to end, or
  for the mount to be remembered, within its own ``timeout``. So a hung mount holds at most two threads, and the
  other mounts keep the rest. Once every thread is stuck (hung mounts enough to hold them all), a check answers
  ``NotResponding`` at once, and the checks still queued are dropped; a check dropped unrun never marks its mount.
  The threads are daemons: one stuck in the kernel never holds up the process's exit.
- **Requested checks** (``requested=True``: the checks a request makes for a path it gives, the file browser's, Find
  Matching Files', Find Indices', form validation's, the scan-log import's and run creation's) share a budget of
  ``request_threads``, ``threads`` less ``RECORDED_THREADS`` (2): at most that many are in flight at once, and once
  that many are stuck, another answers at once. So whatever paths requests give, the checks of recorded paths (a
  job's run directory, an artifact's files) keep two threads that only a recorded path's own hung mount can take.
- **The mount** of a path is the longest mount point of ``/proc/self/mountinfo`` that holds it, by its text (no
  link is followed: that would touch the mount), with the next component under an ``autofs`` mount (``/net/<host>``,
  whose own mount appears only once it is used). A path on the root file system is keyed by its first two
  components (``/home/user``): ``/`` is never remembered. Without ``/proc``, the path's first two components. A
  key is where a check blocks only when the path's text leads there, with no symbolic link or ``..`` on its way:
  so a path from a request is resolved one component at a time, each stat keyed by its own folder
  (``files.roots.real_path``), and a link's target under the target's key.
- **What it bounds:** the checks, each keyed by the path it looks at: a stat, a folder's listing, the opening of a
  file the request then reads (``open_check``, which on NFS asks the server: close-to-open consistency revalidates
  an open file's attributes, where a stat may answer from the attribute cache). What a request reads once its
  check answered (a catalog, a frame, a dataset, a JSON file, a large folder's listing) is read in its own thread,
  so a mount that hangs between the check and the read still holds that request: the checks after it then answer
  at once. h5py's work is never a check: its lock is process-wide, so a read stuck elsewhere would hold a check.
- **Per process** (``configure``, at startup): the API bounds its checks (``[files]`` ``probe_timeout_seconds``,
  ``hung_mount_seconds``, ``probe_threads``); the worker and the CLI run them in their own thread, unbounded (a slow
  but healthy mount must not fail a conversion), and a forked child keeps its parent's choice. A check made inside
  another check runs in that check's thread.

One probe per process (``default_probe``, made at its first use, and again in a forked child); tests install
their own.
"""

from __future__ import annotations

import itertools
import math
import os
import queue
import stat
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import CancelledError, Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from laueportal.core.mounts import MOUNTINFO, read_mountinfo
from laueportal.core.problems import ProblemError

if TYPE_CHECKING:
    from laueportal.core.settings import FilesSettings

T = TypeVar("T")

PROBE_THREADS = 8
"""The probe's threads: the most that can ever be stuck in the kernel on hung mounts (``[files] probe_threads``)."""
PROBE_TIMEOUT_SECONDS = 5.0
"""How long a request waits for a check (a stat on a healthy NFS mount takes milliseconds;
``[files] probe_timeout_seconds``)."""
HUNG_SECONDS = 30.0
"""How long a mount whose check timed out stays ``NotResponding`` at least (also while that check is stuck), and the
503's ``Retry-After`` (``[files] hung_mount_seconds``)."""
CHECKS_PER_MOUNT = 2
"""The most checks one mount has in flight (queued or running), so a hung mount holds at most this many threads."""
RECORDED_THREADS = 2
"""The threads requested checks never take (``FileProbe.request_threads``): the recorded paths' own."""
MOUNTS_SECONDS = 10.0
"""How long a read of the mount table is used."""
PLACES_KEPT = 4096
"""The most paths' places (``FileProbe.place``) kept until the mount table is read again."""
PLACE_KEPT_CHARACTERS = 1024
"""The longest path whose place is kept (so the kept paths hold at most 4 M characters); a longer one's is looked up
again (``_place``: its cost does not grow with the path)."""
NOT_RESPONDING = "urn:laueportal:problem:not-responding"
"""``NotResponding``'s problem type."""
OPEN_CHECK_BYTES = 8
"""What ``open_check`` reads of a regular file."""


class NotResponding(ProblemError):
    """A file check that did not answer in time: the path's file system may be hung (a 503 with ``Retry-After``)."""

    def __init__(self, path: str | os.PathLike[str], seconds: float, *, retry_after: float = HUNG_SECONDS) -> None:
        from laueportal.files.roots import shown  # files.roots checks through the probe

        self.path = os.fspath(path)
        """The path that did not answer."""
        super().__init__(
            503,
            "Not responding",
            f"{shown(path)} did not answer within {seconds:g} s: its file system may be hung.",
            problem_type=NOT_RESPONDING,
            headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
        )


def open_check(path: str | os.PathLike[str]) -> os.stat_result:
    """Open ``path`` read-only and read the first bytes of a regular file, then close it: the check of a file the
    caller reads next. On NFS the open asks the server (close-to-open consistency), where a stat can answer from
    the attribute cache. The open does not block on a FIFO (``O_NONBLOCK``). Raises ``OSError`` as the read would."""

    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        status = os.fstat(descriptor)
        if stat.S_ISREG(status.st_mode):
            os.read(descriptor, OPEN_CHECK_BYTES)
        return status
    finally:
        os.close(descriptor)


def read_mounts(path: str = MOUNTINFO) -> tuple[tuple[str, bool], ...]:
    """``(mount point, autofs)`` of every mount in ``path`` (``/proc/self/mountinfo``, which never blocks)."""
    return tuple((mount.point, mount.fstype == "autofs") for mount in read_mountinfo(path))


class MountTable(dict[str, bool]):
    """``{mount point: autofs}`` (``mount_points``), and ``deepest``: the most components a mount point has."""

    def __init__(self, points: Iterable[tuple[str, bool]] | Mapping[str, bool] = ()) -> None:
        super().__init__(points)
        self.deepest = max((len(_ends(point, len(point))) for point in self), default=0)


def mount_points(mounts: tuple[tuple[str, bool], ...]) -> MountTable:
    """``read_mounts``' table as ``{mount point: autofs}`` (a point mounted twice: its last, top mount)."""
    return MountTable(mounts)


def mount_of(path: str, mounts: tuple[tuple[str, bool], ...] | Mapping[str, bool] | None) -> str:
    """The key ``path`` is remembered under (``mounts``: ``read_mounts`` or ``mount_points``; None: the first two
    components): its mount, by its text, the host under an autofs mount, and the first two components on the root
    file system."""
    text = os.path.normpath(os.path.abspath(path))
    if text.startswith("//"):
        text = os.sep + text.lstrip(os.sep)
    table = mounts if isinstance(mounts, MountTable) or mounts is None else MountTable(mounts)
    return _place(text, table)[0]


def _ends(text: str, count: int) -> list[int]:
    """Where each of the first ``count`` components of an absolute ``text`` ends (``text[:end]`` is its prefix of
    that many components), empty components skipped."""
    ends: list[int] = []
    start = 1
    while len(ends) < count and start < len(text):
        end = text.find(os.sep, start)
        if end < 0:
            end = len(text)
        if end > start:
            ends.append(end)
        start = end + 1
    return ends


def _place(text: str, points: MountTable | None) -> tuple[str, bool]:
    """``mount_of`` of a normalized absolute ``text``, and whether it lies on the root file system (no mount point
    but ``/`` holds it; False without a mount table). Only its prefixes no deeper than the deepest mount point are
    looked up, so the cost does not grow with the path's depth (a walk places each component of a path)."""
    ends = _ends(text, 2 if points is None else max(2, points.deepest + 1))
    first_two = text[: ends[min(len(ends), 2) - 1]] if ends else os.sep
    if points is None:
        return first_two, False
    for depth in range(min(len(ends), points.deepest), 0, -1):  # the longest mount point that holds it, by its text
        prefix = text[: ends[depth - 1]]
        autofs = points.get(prefix)
        if autofs is not None:
            if autofs and len(ends) > depth:
                return text[: ends[depth]], False
            return prefix, False
    return first_two, True


@dataclass(eq=False)
class _Check:
    future: Future[Any]
    check: Callable[[], Any]
    mount: str
    requested: bool
    stuck: bool = False
    """Its caller stopped waiting while it ran: it holds a thread in the kernel."""


_on_probe_thread = threading.local()
_names = itertools.count()


class FileProbe:
    """File checks on ``threads`` daemon threads of its own, each waited for at most ``timeout`` seconds, with the
    mounts that did not answer remembered (the module's docstring). ``timeout=None``: unbounded, every check runs
    in its caller's thread."""

    def __init__(
        self,
        *,
        threads: int = PROBE_THREADS,
        timeout: float | None = PROBE_TIMEOUT_SECONDS,
        hung_seconds: float = HUNG_SECONDS,
        per_mount: int = CHECKS_PER_MOUNT,
        mounts: Callable[[], tuple[tuple[str, bool], ...]] = read_mounts,
    ) -> None:
        if threads < 1 or per_mount < 1 or (timeout is not None and timeout <= 0) or hung_seconds < 0:
            raise ValueError("threads, per_mount and timeout must be positive, hung_seconds not negative")
        self.threads = threads
        self.timeout = timeout
        self.hung_seconds = hung_seconds
        self.per_mount = max(1, min(per_mount, threads - 1))
        """Fewer than the threads, so a hung mount always leaves one to the others."""
        self.request_threads = max(1, threads - RECORDED_THREADS)
        """The most requested checks in flight at once (``RECORDED_THREADS`` fewer than the threads, at least one)."""
        self.name = f"file-probe-{next(_names)}"
        """Its threads are ``<name>-<index>``."""
        self.pid = os.getpid()
        self._read_mounts = mounts
        self._mounts: MountTable | None = None
        self._mounts_at = -float("inf")
        self._places: dict[str, tuple[str, bool]] = {}
        """``place``'s answers on the current mount table, for paths up to ``PLACE_KEPT_CHARACTERS``."""
        self._tasks: queue.SimpleQueue[_Check] = queue.SimpleQueue()
        self._changed = threading.Condition(threading.Lock())
        """Guards what follows; notified when a check ends or a mount is remembered."""
        self._started = False
        self._in_flight: dict[str, int] = {}
        """Per mount: its checks queued or running."""
        self._stuck: dict[str, int] = {}
        """Per mount: its checks that timed out and are still running."""
        self._stuck_total = 0
        self._requested = 0
        """Requested checks queued or running."""
        self._requested_stuck = 0
        self._hung_until: dict[str, float] = {}

    def _mount_table(self) -> MountTable | None:
        now = time.monotonic()
        with self._changed:
            if now - self._mounts_at > MOUNTS_SECONDS:
                try:
                    self._mounts = mount_points(self._read_mounts())
                except (OSError, ValueError, IndexError):
                    self._mounts = None
                self._mounts_at = now
                self._places = {}
            return self._mounts

    def mount(self, path: str | os.PathLike[str]) -> str:
        """The key ``path`` is remembered under (``mount_of``)."""
        return mount_of(os.fspath(path), self._mount_table())

    def place(self, text: str) -> tuple[str, bool]:
        """``mount`` of a normalized absolute ``text`` (no ``.``, ``..`` or empty component), and whether it lies on
        the root file system: a stat there blocks only when ``/`` does, which no key remembers (``files.roots``'
        walk makes such stats in the check it is in). Its cost depends on the mount table, not on ``text``'s depth."""
        mounts = self._mount_table()
        places = self._places
        place = places.get(text)
        if place is None:
            place = _place(text, mounts)
            if len(text) <= PLACE_KEPT_CHARACTERS and len(places) < PLACES_KEPT:
                places[text] = place
        return place

    def keys_apart(self, folder: str | os.PathLike[str]) -> Callable[[str], bool]:
        """Whether a child of ``folder``, by its name, has another key than ``folder`` (``mount``): a mount point, a
        host of an autofs folder, or a folder's within the first two components on the root file system. A stat of
        such a child is a check of its own."""
        text = os.path.normpath(os.path.abspath(folder))
        mounts = self._mount_table()
        if self.mount(os.path.join(text, "\0")) != self.mount(text):  # no file name holds a NUL
            return lambda _name: True
        if mounts is None:
            return lambda _name: False
        return lambda name: os.path.join(text, name) in mounts

    def hung(self, path: str | os.PathLike[str]) -> bool:
        """Whether ``path``'s mount is remembered as not responding."""
        mount = self.mount(path)
        with self._changed:
            return self._is_hung(mount)

    def not_responding(self, path: str | os.PathLike[str]) -> NotResponding:
        """The answer for a check of ``path`` that did not answer (``Retry-After``: the memory of a hung mount)."""
        return NotResponding(path, self.timeout or 0, retry_after=self.hung_seconds)

    def _is_hung(self, mount: str) -> bool:
        if mount == os.sep:
            return False
        return self._stuck.get(mount, 0) > 0 or time.monotonic() < self._hung_until.get(mount, -float("inf"))

    def _turn(self, path: str | os.PathLike[str], mount: str, deadline: float, requested: bool) -> None:
        """Wait (holding ``_changed``) until ``mount`` may have another check in flight, and a requested check
        another of the requests' threads, and count it; or raise ``NotResponding`` at once when the mount is
        remembered, every thread is stuck or (a requested check) every requests' thread, and at ``deadline``."""
        while True:
            if (
                self._is_hung(mount)
                or self._stuck_total >= self.threads
                or (requested and self._requested_stuck >= self.request_threads)
            ):
                raise self.not_responding(path)
            if self._in_flight.get(mount, 0) < self.per_mount and (
                not requested or self._requested < self.request_threads
            ):
                self._in_flight[mount] = self._in_flight.get(mount, 0) + 1
                self._requested += requested
                return
            left = deadline - time.monotonic()
            if left <= 0:  # its mount's checks still hold their turns: it never ran, so it marks nothing
                raise self.not_responding(path)
            self._changed.wait(left)

    def run(self, path: str | os.PathLike[str], check: Callable[[], T], *, requested: bool = False) -> T:
        """``check()`` (a stat, a listing or an open of ``path``, which it keys: no symbolic link or ``..`` may lead
        it elsewhere) on the probe's threads: its value or its exception, or ``NotResponding`` when ``path``'s mount
        is remembered as hung, every thread is stuck (``requested``, a check of a path a request gave: every one of
        ``request_threads``), or ``check`` does not return within ``timeout``."""
        if self.timeout is None or getattr(_on_probe_thread, "active", False):
            return check()  # unbounded, or a check inside a check: that one is bounded
        mount = self.mount(path)
        deadline = time.monotonic() + self.timeout
        task = _Check(Future(), check, mount, requested)
        with self._changed:
            self._turn(path, mount, deadline, requested)
            if not self._started:
                for index in range(self.threads):
                    threading.Thread(target=self._work, name=f"{self.name}-{index}", daemon=True).start()
                self._started = True
        task.future.add_done_callback(lambda _future: self._ended(task))
        self._tasks.put(task)
        try:
            return task.future.result(max(0.0, deadline - time.monotonic()))
        except FutureTimeout:
            pass
        except CancelledError:  # dropped once every thread got stuck
            raise self.not_responding(path) from None
        if task.future.cancel():  # it never got a thread
            raise self.not_responding(path) from None
        with self._changed:
            answered = task.future.done()
            if not answered:
                task.stuck = True
                self._stuck[mount] = self._stuck.get(mount, 0) + 1
                self._stuck_total += 1
                self._requested_stuck += requested
                self._hung_until[mount] = time.monotonic() + self.hung_seconds
                self._changed.notify_all()
            every_thread_stuck = self._stuck_total >= self.threads
        if answered:  # it answered just now
            return task.future.result()
        if every_thread_stuck:
            self._drop_queued()
        raise self.not_responding(path)

    def _ended(self, task: _Check) -> None:
        """A check has ended: returned (maybe from the kernel, after its caller stopped waiting) or been dropped."""
        with self._changed:
            left = self._in_flight.get(task.mount, 1) - 1
            if left > 0:
                self._in_flight[task.mount] = left
            else:
                self._in_flight.pop(task.mount, None)
            self._requested -= task.requested
            if task.stuck:
                self._stuck_total -= 1
                self._requested_stuck -= task.requested
                stuck = self._stuck.get(task.mount, 1) - 1
                if stuck > 0:
                    self._stuck[task.mount] = stuck
                else:
                    self._stuck.pop(task.mount, None)
            self._changed.notify_all()

    def _drop_queued(self) -> None:
        """Every thread is stuck: the queued checks will not run in time, so their callers answer now."""
        while True:
            try:
                task = self._tasks.get_nowait()
            except queue.Empty:
                return
            task.future.cancel()

    def _work(self) -> None:
        _on_probe_thread.active = True
        while True:
            task = self._tasks.get()
            if not task.future.set_running_or_notify_cancel():
                continue
            try:
                value = task.check()
            except BaseException as error:  # the caller raises it
                task.future.set_exception(error)
            else:
                task.future.set_result(value)
            del task


@dataclass(frozen=True)
class _Configured:
    timeout: float | None
    hung_seconds: float
    threads: int


_configured = _Configured(PROBE_TIMEOUT_SECONDS, HUNG_SECONDS, PROBE_THREADS)
_default: FileProbe | None = None
_default_lock = threading.Lock()


def configure(files: FilesSettings | None = None, *, bound: bool = True) -> None:
    """The process's probe, once at startup: the API's bounded by ``files`` (``probe_timeout_seconds``,
    ``hung_mount_seconds``, ``probe_threads``; None: the defaults), the worker's and the CLI's unbounded
    (``bound=False``; a job process, ``jobs.process``, configures its own the same way)."""
    global _configured, _default
    configured = _Configured(
        (files.probe_timeout_seconds if files is not None else PROBE_TIMEOUT_SECONDS) if bound else None,
        files.hung_mount_seconds if files is not None else HUNG_SECONDS,
        files.probe_threads if files is not None else PROBE_THREADS,
    )
    with _default_lock:
        if configured != _configured:
            _configured, _default = configured, None


def default_probe() -> FileProbe:
    """The process's probe (``configure``), made at its first use (and again in a forked child, which has none of
    its threads)."""
    global _default
    with _default_lock:
        if _default is None or _default.pid != os.getpid():
            settings = _configured
            _default = FileProbe(threads=settings.threads, timeout=settings.timeout, hung_seconds=settings.hung_seconds)
        return _default


def bounded(path: str | os.PathLike[str], check: Callable[[], T], *, requested: bool = False) -> T:
    """``check()`` on the process's probe (``FileProbe.run``; ``requested``: a check of a path a request gave)."""
    return default_probe().run(path, check, requested=requested)
