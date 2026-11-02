"""Live wire reconstruction: one lauelab scan directory that grows as wire stacks arrive.

A live wire run (``wire-live/v1``) is registered here for run kind ``wire_reconstruction``
(``register_live_compute_function``); the executor runs it with its live hooks (``jobs.executor``).

- **The start.** ``prepare_scan([])`` makes an empty running scan in the run's ``reconstruction/`` with the request's
  settings (``compute.settings_from_request``, as a batch run's) and publishes ``scan.h5``. It reads the geometry
  file once, here, and every point uses that text for the whole session, even if the file changes or goes:
  ``PreparedScan.add`` and the point tasks carry it.
- **Each arrival** (``RunHooks.arrivals``, in arrival order: the catalog's points follow it) is added with
  ``PreparedScan.add(source, point_id=<its input ID>)``: a point ID and point file named after the file's stem, as a
  batch run's. An input it refuses (lauelab's expected point errors: unreadable, malformed) is a failed input in
  ``failures.jsonl``; lauelab's ``add`` leaves it out of the catalog, so it has no row there. So is, before ``add``, a
  file that is no longer the one admitted (its identity in ``arrivals.jsonl``).
- **The pool:** ``reconstruct_point`` in spawned worker processes, ``cores // num_threads`` of them (``live_pool``:
  the dispatcher's share, ``RunRequest.cores``), each point with ``num_threads`` OpenMP threads (at most
  the cores). Each worker publishes its own point file; its outcome is recorded with ``PreparedScan.record``, and a
  failed one is marked failed in the catalog and is a failed input (as a batch run's). Progress is reported per point.
- **Publication**: ``scan.h5`` is republished at most every ``live.publish_seconds``
  (``RunRequest.publish_seconds``) and only when a point was recorded (complete or failed) since the last. lauelab's
  ``record`` publishes by itself when its own five seconds have passed (``PreparedScan``: at most once every five
  seconds), and so does ``record_dispatch``, which is not used (a dispatched point stays ``pending`` in the catalog).
  So outcomes are held until a publication is due; then one is recorded (lauelab may publish here), then
  ``snapshot(force=True)`` publishes if it did not (an unchanged catalog is never rewritten), then the others are
  recorded, for the next publication (lauelab publishes no sooner than five seconds after this one). ``finish()`` and
  ``close()`` publish at once; recording the outcomes still held just before them (and, at a run-level error,
  ``record_dispatch``) may publish once more, when lauelab's own five seconds have passed: lauelab's, and unavoidable
  through its public API (``finish()`` takes a scan only once every dispatched point is recorded, and only a
  publication within five seconds keeps ``record`` from publishing), so at most one such snapshot per run end.
- **``completed.jsonl``:** after each publication (a snapshot, ``finish()``, ``close()``, and lauelab's own inside
  ``record`` or ``record_dispatch``, seen by ``scan.h5``'s identity changing during the call), one line per point the
  scan shows complete that has none yet (``rundir.CompletedPoint``: ``n``, ``point_id``, ``scan_point``, ``path``,
  ``n_depths``, ``completed_at``), taken from the scan's own outcomes: a point that a ``record`` whose publication
  failed left recorded gets its line at the next publication that shows it, and a point a catalog on disk shows
  complete has its line even when a later publication fails. An append that failed is truncated back
  (``rundir.append_completed``) and its lines are the next append's first, as they were (the same bytes at the same
  offsets, for a reader that took some before the truncation), then the points completed since. So a follower
  or a viewer that reads a line finds its point file and the catalog's complete row. Its lines are written
  before the compute returns or raises, so before the run's row turns terminal.
- **The seal** (``should_stop``: every arrival handed out): every admitted point is still dispatched and recorded;
  then ``finish()`` publishes the final catalog at once, the failed points marked (``cancelled=True`` when the
  catalog holds no point: nothing arrived, or nothing could be added), and the last lines follow it. The executor's
  live end rule decides the run's end.
- **A service stop** (``shutting_down``): no further point starts; those waiting are not run (``interrupted``) and stay
  ``unattempted`` (``finish(cancelled=True)``); the running ones finish.
- **A run-level error** (the arrivals' ledger, a worker's output failure or lost worker, the catalog's publication,
  ``completed.jsonl``) ends the run at once (Failed at once): the outcomes already in when it came are
  kept (every outcome of the wait that brought a worker's error), the pool's workers are ended at once and waited for
  (so no point file is published after the catalog says its point never ran), the outcomes that came in meanwhile are
  taken, the held outcomes are recorded, each
  point still running (or whose worker was lost) is recorded dispatched, and ``close()`` publishes the catalog failed
  (those points ``interrupted``, the waiting ones ``unattempted``); the published files stay, the points it shows
  complete get their lines, then the error is raised. A point whose worker raised an error of its own (its output
  failed) is a failed point, in the catalog and ``failures.jsonl``, and the run's error names it ("reconstruction worker
  failed on point ...", as lauelab's batch LaueGo scheduler names its input). Unlike lauelab's batch scheduler (``reconstruct_scan``, so a
  batch run), which after a worker's output failure lets the other running points finish and records them (it ends
  them at once only for a lost worker or an error of its own), a live run ends them at once at every run-level error
  and marks them ``interrupted``.

No Resume: ``export_state`` and ``restore`` are not used. Like ``compute``, it never imports the database, the
settings or a service (import-linter rule 3), nor pydantic (rule 3b).
"""

from __future__ import annotations

import multiprocessing
import os
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from datetime import UTC, datetime

from lauelab.indexing import InputError, ReconstructionError
from lauelab.reconstruct import (
    InvalidScanFile,
    PointOutcome,
    PointTask,
    PreparedScan,
    ScanResult,
    prepare_scan,
    reconstruct_point,
    validate_scan_file,
)

# lauelab d5de751 has no public name for the errors that fail one point's input (not in lauelab.reconstruct's
# __all__): this is the tuple its own prepare_scan catches for each input. Check it again on an upgrade.
from lauelab.reconstruct.scan import EXPECTED_POINT_ERRORS

from laueportal.core.provenance import library_provenance
from laueportal.jobs.contract import RunHooks, RunOutcome, RunRequest, register_live_compute_function
from laueportal.jobs.processes import end_pool_workers
from laueportal.jobs.rundir import (
    CATEGORY_INTERRUPTED,
    COMPLETED_FILENAME,
    RECONSTRUCTION_DIRECTORY,
    CompletedPoint,
    FailureRecord,
    ManifestEntry,
    append_completed,
    unchanged_since_admission,
)
from laueportal.reconstruction.wire.compute import (
    WIRE_KIND,
    WireSettings,
    reconstructor_options,
    settings_from_request,
)

CHANGED_AFTER_ADMISSION = "Input changed after it was admitted"
ENGINE = "lauelab PreparedScan (live: add per arrival, reconstruct_point in local worker processes, one scan directory)"
POLL_SECONDS = 0.2
"""How long the loop waits for a worker's outcome (or sleeps, with none running) between its looks at the arrivals
(the hooks bound those to ``live.arrivals_seconds``) and its publication's clock."""


def live_pool(cores: int | None, threads: int | None, fallback_workers: int) -> tuple[int, int]:
    """``(workers, threads per worker)``: ``threads`` (the request's ``num_threads``; 1 when unset) at most ``cores``,
    and as many workers as fit in ``cores`` (at least 1). Without cores (a call outside the dispatcher's job
    process), ``fallback_workers`` (the execution policy's ``reconstruction_workers``)."""

    threads = threads or 1
    if cores is None:
        return max(1, fallback_workers), threads
    threads = min(threads, cores)
    return max(1, cores // threads), threads


@dataclass
class _Point:
    entry: ManifestEntry
    task: PointTask


class _LiveScan:
    """The scan, its pool and its publication (the module's docstring)."""

    def __init__(
        self,
        scan: PreparedScan,
        request: RunRequest,
        hooks: RunHooks,
        *,
        workers: int,
        threads: int,
        publish_seconds: float,
    ) -> None:
        self.scan = scan
        self.hooks = hooks
        self.run_directory = request.run_directory
        self.workers = workers
        self.threads = threads
        self.publish_seconds = publish_seconds
        self.points: dict[int, _Point] = {}  # by catalog index
        self.waiting: deque[_Point] = deque()
        self.running: dict[Future[PointOutcome], _Point] = {}
        self.held: list[PointOutcome] = []  # outcomes not recorded yet
        self.unpublished = False  # an outcome recorded since the last publication
        self.published_at = time.monotonic()  # prepare_scan published the empty catalog
        self.lined: set[int] = set()  # the catalog indices with a line in completed.jsonl
        self.unlined: list[tuple[int, CompletedPoint]] = []  # a failed append's lines (by catalog index), written first
        self.succeeded = 0
        self.failed = 0
        self.pool: ProcessPoolExecutor | None = None
        self.finishing = False

    def run(self) -> tuple[ScanResult, bool]:
        """Admit, dispatch and record until the seal (or the service stop) and every running point is in; finish the
        scan. Returns its result and whether the service stopped it."""
        self.pool = ProcessPoolExecutor(max_workers=self.workers, mp_context=multiprocessing.get_context("spawn"))
        stopping = interrupted = False
        while True:
            if not stopping:
                for entry in self.hooks.arrivals():
                    self._add(entry)
                stopping = self.hooks.should_stop()
            if not interrupted and self.hooks.shutting_down():
                interrupted = True
                self._withdraw()
            if not interrupted:
                self._dispatch()
            if stopping and not self.running and not self.waiting:
                break
            self._collect()
            self._publish_if_due()
        self.pool.shutdown(wait=True)
        self._record_held()
        self.finishing = True
        result = self.scan.finish(cancelled=interrupted or not self.scan.outcomes)
        self._line()
        return result, interrupted

    def abandon(self) -> None:
        """A run-level error's end (the module's docstring). After a failed ``finish()`` the scan has ended and its
        last publication stays as it is. When ``close()`` cannot publish, the catalog on disk is the last one published
        (a snapshot, or lauelab's own inside a ``record`` or ``record_dispatch`` here), and its points already have
        their lines."""
        if self.pool is not None:
            left = end_pool_workers(self.pool)  # first: none publishes a point file once the catalog is closed
            if left:
                self.hooks.log(f"after a run-level error, {left} worker process(es) did not end, even after SIGKILL")
        if self.finishing:
            return
        for future in [future for future in self.running if future.done() and not future.cancelled()]:
            try:
                self._take(future)  # an outcome that came in before the workers were ended: kept, as the held ones
            except Exception as error:
                self.hooks.log(f"after a run-level error, an outcome could not be taken: {_described(error)}")
        while self.held:
            try:
                self._record(self.held.pop(0))
            except Exception as error:
                self.hooks.log(f"after a run-level error, an outcome could not be recorded: {_described(error)}")
        for point in self.running.values():  # dispatched, no outcome: close() marks it interrupted
            try:
                self._lauelab(lambda task=point.task: self.scan.record_dispatch(task))
            except Exception as error:
                self.hooks.log(f"after a run-level error, a running point could not be marked: {_described(error)}")
        try:
            self.scan.close()
        except Exception as error:
            self.hooks.log(f"after a run-level error, the catalog could not be closed: {_described(error)}")
            return
        try:
            self._line()
        except Exception as error:
            self.hooks.log(f"after a run-level error, {COMPLETED_FILENAME} could not be written: {_described(error)}")

    def _add(self, entry: ManifestEntry) -> None:
        try:
            if not unchanged_since_admission(entry.source, entry.identity):
                raise InputError(CHANGED_AFTER_ADMISSION)
            task = self.scan.add(entry.source, point_id=entry.input_id)
        except EXPECTED_POINT_ERRORS as error:
            self.hooks.record_failure(FailureRecord.from_error(entry, ReconstructionError(str(error))))
            self.failed += 1
            self._progress()
            return
        point = _Point(entry, task)
        assert task.index is not None  # a scan's tasks have their catalog index
        self.points[task.index] = point
        self.waiting.append(point)

    def _withdraw(self) -> None:
        """The service stops: the points not started are not run."""
        while self.waiting:
            point = self.waiting.popleft()
            self.hooks.record_failure(
                FailureRecord.not_run(
                    point.entry, category=CATEGORY_INTERRUPTED, message="service stopped before this point"
                )
            )

    def _dispatch(self) -> None:
        assert self.pool is not None
        while self.waiting and len(self.running) < self.workers:
            point = self.waiting.popleft()
            self.running[self.pool.submit(reconstruct_point, point.task, num_threads=self.threads)] = point

    def _collect(self) -> None:
        """Take the outcomes of the points that ended, in dispatch order (``_take``); the first run-level error among
        them is raised once every other outcome of the same wait is held."""
        if not self.running:
            time.sleep(POLL_SECONDS)
            return
        done, _ = wait(self.running, timeout=POLL_SECONDS, return_when=FIRST_COMPLETED)
        error: Exception | None = None
        for future in [future for future in self.running if future in done]:
            taken = self._take(future)  # every outcome is taken; the first error is kept
            error = error or taken
        if error is not None:
            raise error

    def _take(self, future: Future[PointOutcome]) -> Exception | None:
        """Hold the outcome of a point that ended (complete, or failed: a failed input). A worker's error is a
        run-level error, returned: a lost worker (``BrokenProcessPool``) is no point's, and its point stays running
        (dispatched, no outcome, as lauelab's own scheduler leaves it); any other (its point's output failed) is its
        point's own, named in the error, and the point is held failed with it."""
        point = self.running[future]
        try:
            outcome = future.result()
        except BrokenProcessPool as error:
            return error
        except Exception as error:
            task = point.task
            self._took(future, PointOutcome(task.index, task.point_id, "failed", _described(error)))
            run_error = ReconstructionError(
                f"reconstruction worker failed on point {point.entry.input_id}: {_described(error)}"
            )
            run_error.__cause__ = error
            return run_error
        self._took(future, outcome)
        return None

    def _took(self, future: Future[PointOutcome], outcome: PointOutcome) -> None:
        point = self.running.pop(future)
        self.held.append(outcome)
        if outcome.status == "failed":
            self.hooks.record_failure(
                FailureRecord.from_error(
                    point.entry,
                    ReconstructionError(outcome.error or "reconstruction failed"),
                    context={"seconds": outcome.seconds},
                )
            )
            self.failed += 1
        else:
            self.succeeded += 1
        self._progress()

    def _publish_if_due(self) -> None:
        """Publish ``scan.h5`` once (the module's docstring) when a point was recorded or is held since the last
        publication and ``publish_seconds`` have passed since it."""
        if not (self.held or self.unpublished) or time.monotonic() - self.published_at < self.publish_seconds:
            return
        if self.held:
            self._record(self.held.pop(0))  # lauelab publishes here when its own five seconds have passed ...
        self.scan.snapshot(force=True)  # ... else this does
        self.published_at = time.monotonic()
        self.unpublished = False
        self._line()
        self._record_held()  # in the next publication

    def _record(self, outcome: PointOutcome) -> None:
        self.unpublished = True  # first: a record whose publication fails has recorded it (lauelab sets it first)
        self._lauelab(lambda: self.scan.record(outcome))

    def _lauelab(self, call: Callable[[], None]) -> None:
        """Run a lauelab call that publishes ``scan.h5`` by itself when lauelab's own five seconds have passed
        (``record``, ``record_dispatch``) and line what it published, at once: a point the catalog on disk shows
        complete has its line even if a later publication fails. lauelab publishes by a rename, so a publication
        changes the file's identity (an identity that cannot be read: no lines yet, the next publication's)."""
        before = _identity(self.scan.path)
        call()
        after = _identity(self.scan.path)
        if before is not None and after is not None and after != before:
            self._line()

    def _record_held(self) -> None:
        while self.held:
            self._record(self.held.pop(0))

    def _line(self) -> None:
        """``completed.jsonl``'s lines, after a publication, for the points the scan shows complete that have none
        yet, in catalog order: from the scan's own outcomes, so a point recorded by a ``record`` whose publication
        failed is lined by the next publication. The lines of an append that failed (truncated back, raised) come
        first, exactly as they were (their ``n``, order and ``completed_at``): a reader may have taken some of them
        before the truncation, and must find the same bytes at the same offsets, even when a point earlier in the
        catalog completed since."""
        now = datetime.now(UTC).isoformat(timespec="seconds")
        lines = list(self.unlined)
        retried = {index for index, _ in lines}
        for outcome in self.scan.outcomes:
            if outcome.status != "complete" or outcome.index in self.lined or outcome.index in retried:
                continue
            assert outcome.index is not None and outcome.output is not None
            point = self.points[outcome.index]
            lines.append(
                (
                    outcome.index,
                    CompletedPoint(
                        n=len(self.lined) + len(lines),
                        point_id=outcome.point_id,
                        scan_point=point.entry.scan_point,
                        path=outcome.output,
                        n_depths=point.task.n_depths,
                        completed_at=now,
                    ),
                )
            )
        try:
            append_completed(self.run_directory, [line for _, line in lines])
        except BaseException:
            self.unlined = lines
            raise
        self.lined.update(index for index, _ in lines)
        self.unlined = []

    def _progress(self) -> None:
        self.hooks.report_progress(succeeded=self.succeeded, failed=self.failed)


def _described(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _identity(path: os.PathLike[str]) -> tuple[int, int] | None:
    """A file's identity (inode, modification time); None when it cannot be read."""
    try:
        status = os.stat(path)
    except OSError:
        return None
    return status.st_ino, status.st_mtime_ns


def _provenance(settings: WireSettings, request: RunRequest, workers: int, threads: int) -> dict[str, object]:
    return {
        **library_provenance(ENGINE),
        "geometry_file": settings.geometry_file,
        "detector_index": settings.detector_index,
        "depth_range_um": list(settings.depth_range),
        "resolution_um": settings.resolution,
        "wire_edge": settings.wire_edge,
        "percent_brightest": settings.percent_brightest,
        "frame_skip": settings.frame_skip,
        "wire_skip": settings.wire_skip,
        "cores": request.cores,
        "workers": workers,
        "threads_per_worker": threads,
        "memory_limit_mb": settings.memory_limit_mb,
        "publish_seconds": request.publish_seconds,
        "output_layout": f"one point file per point with a scan catalog ({RECONSTRUCTION_DIRECTORY}/), in arrival order",
    }


def compute_live_wire_reconstruction(request: RunRequest, hooks: RunHooks) -> RunOutcome:
    """A live wire run's compute (the module's docstring)."""

    if request.publish_seconds is None:
        raise RuntimeError("a live run's request has no publish_seconds (its job process gives it)")
    settings = settings_from_request(request.parameters)
    workers, threads = live_pool(request.cores, settings.threads_per_worker, request.reconstruction_workers)
    outcome = RunOutcome(n_succeeded=0, n_failed=0, provenance=_provenance(settings, request, workers, threads))
    # The library refuses a nonempty destination, so an earlier run's output is never overwritten; the geometry is
    # read here, once.
    scan = prepare_scan(
        [],
        os.path.join(request.run_directory, RECONSTRUCTION_DIRECTORY),
        geometry=settings.geometry_file,
        detector=settings.detector_index,
        **reconstructor_options(settings),
    )
    outcome.artifacts["reconstruction"] = os.fspath(scan.path)
    hooks.report_outcome(outcome)
    live = _LiveScan(scan, request, hooks, workers=workers, threads=threads, publish_seconds=request.publish_seconds)
    try:
        result, interrupted = live.run()
    except BaseException:
        live.abandon()
        raise
    finally:
        outcome.n_succeeded, outcome.n_failed = live.succeeded, live.failed
    outcome.stopped = True  # sealed (or stopped by the service): no further arrival is admitted
    if interrupted:
        hooks.log("the service stopped the run: the points not started were not run")
    try:
        summary = validate_scan_file(result.path)
    except (InvalidScanFile, OSError) as error:
        outcome.validation_error = f"{result.path}: {error}"
        return outcome
    outcome.provenance["reconstruction_summary"] = {
        "run_status": summary.run_status,
        "n_points": summary.n_points,
        "n_complete": summary.n_complete,
        "n_failed": summary.n_failed,
        "n_unattempted": summary.n_unattempted,
    }
    outcome.summary = (
        f"{summary.n_complete} of {summary.n_points} point(s) reconstructed into {RECONSTRUCTION_DIRECTORY}/"
    )
    return outcome


register_live_compute_function(WIRE_KIND, compute_live_wire_reconstruction)
