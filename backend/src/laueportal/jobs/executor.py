"""Execute a queued run and record its final status.

The executor loads the saved request, calls the compute function, and records
progress and output details. It also handles cancellation and process cleanup.
Output validation failures mark the run as failed even if all inputs completed.

A live run (queued as ``live_<job id>`` with its ``live.json``, ``jobs.live``) runs its
live compute function (``contract.get_live_compute_function``) with ``LiveExecutorHooks``: its inputs are the lines
of ``arrivals.jsonl`` its hooks read from their offset, each batch counted (``state.record_arrivals``) before the
compute sees it. The stop flag seals it (the lock taken once, the ledger read to its end) and the compute drains every
admitted input; it then ends Finished with at least one input and none failed, Cancelled with none, Failed when any
failed or was not processed ("Incomplete: ..."). A run-level error fails it at once, keeping what it published; a
service stop interrupts it as a batch run. It has no time limit: ``run.json``'s ``job_timeout_seconds`` is
null, and the job process arms no alarm for it (``jobs.process``). A live run whose source is a DAQ also runs
its catalog thread (``jobs.catalog.start_catalog``) beside the compute: closed before the seal takes the arrivals lock
(``LiveExecutorHooks.before_seal``), so it makes no DM call after it, and again at the run's end. A follower (a live
LaueGo run whose source is a live wire run) runs its follower's thread (``jobs.follower.start_follower``) the same way:
it admits the wire run's completed points and, reading the wire run's job row, is done once that run has ended and
every completed point is admitted; the hooks then seal the run as a Stop would, and
its thread's run-level error fails the run.
"""

from __future__ import annotations

import logging
import math
import os
import platform
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from laueportal.core.db import begin_write, make_engine
from laueportal.core.settings import LiveSettings, get_settings
from laueportal.core.timestamps import local_timestamp
from laueportal.core.timeutil import utcnow
from laueportal.files.inputs import normalize_optional_integer_range
from laueportal.jobs import state
from laueportal.jobs.arrivals import Arrival, ArrivalReader, seal
from laueportal.jobs.catalog import CatalogThread, start_catalog
from laueportal.jobs.contract import RunOutcome, RunRequest, get_compute_function, get_live_compute_function
from laueportal.jobs.follower import FollowerThread, start_follower
from laueportal.jobs.lifecycle import (
    SHUTDOWN_SIGNAL,
    STOP_REASON_SHUTDOWN,
    ProgressRecorder,
    RunMonitor,
    RunTimeout,
    terminate_descendants,
)
from laueportal.jobs.live import has_live_marker, read_live_document
from laueportal.jobs.models import Job
from laueportal.jobs.queue import RunPolicy
from laueportal.jobs.rundir import (
    ARRIVALS_FILENAME,
    FAILURE_REPORT_FILENAME,
    RUN_SUMMARY_FILENAME,
    FailureRecord,
    FailureReportWriter,
    ManifestEntry,
    input_identity,
    read_manifest,
    read_request,
    scan_frame_identity,
    verify_manifest,
    write_json_file,
)
from laueportal.jobs.state import JobStatus, RunPhase
from laueportal.util.srange import srange

logger = logging.getLogger(__name__)

RUN_SUMMARY_FORMAT = "laue-portal-run-summary"
RUN_SUMMARY_VERSION = 1


class RunSetupError(RuntimeError):
    """The run could not be started from its persisted state."""


@dataclass
class _Decision:
    status: JobStatus
    message: str
    phase: RunPhase | None = None


class ExecutorHooks:
    """The hooks handed to a compute function."""

    def __init__(self, job_id: int, monitor: RunMonitor, recorder: ProgressRecorder, failures: FailureReportWriter):
        self._job_id = job_id
        self._monitor = monitor
        self._recorder = recorder
        self._failures = failures
        self.messages: list[str] = []
        self.outcome: RunOutcome | None = None

    def should_stop(self) -> bool:
        return self._monitor.stop_requested

    def shutting_down(self) -> bool:
        return self._monitor.stop_reason == STOP_REASON_SHUTDOWN

    def arrivals(self) -> list[ManifestEntry]:
        return []  # a batch run's inputs are its manifest

    def report_progress(self, *, succeeded: int, failed: int) -> None:
        self._recorder.report(succeeded=succeeded, failed=failed)

    def record_failure(self, record: FailureRecord) -> None:
        self._failures.append(record)

    def report_outcome(self, outcome: RunOutcome) -> None:
        self.outcome = outcome

    def log(self, message: str) -> None:
        self.messages.append(message)
        logger.info("Run %s: %s", self._job_id, message)


class LiveExecutorHooks(ExecutorHooks):
    """A live run's hooks (the module's docstring): ``arrivals`` reads ``arrivals.jsonl`` from its offset (whole lines
    only), counts what it read on the run's row and hands it out; once the stop flag is seen (a follower: also once
    its follower's thread is done, its wire run ended and every completed point admitted) it seals the run first,
    and ``should_stop`` is True from the call that handed out the last arrivals. A service stop stops it at once, as
    a batch run (no more arrivals; the counted ones not processed are not run). A follower's thread's run-level error
    is raised by ``arrivals``.

    However fast the live compute calls ``arrivals``, the file on the runs root is looked at no more often than every
    ``interval`` seconds (``live.arrivals_seconds``): a stat, and a read only when it changed; a call in
    between returns nothing. The seal reads it to the end at once. Each call also writes the progress counts the
    compute's last reports left pending (``ProgressRecorder.flush_pending``), so the row shows a burst's last inputs
    without waiting for a later one."""

    def __init__(
        self,
        job_id: int,
        monitor: RunMonitor,
        recorder: ProgressRecorder,
        failures: FailureReportWriter,
        *,
        engine: Engine,
        request: RunRequest,
        interval: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        super().__init__(job_id, monitor, recorder, failures)
        self._engine = engine
        self._request = request
        self._reader = ArrivalReader(request.run_directory)
        self._interval = interval
        self._clock = clock
        self._next_look = -math.inf
        self._delayed = False
        self.n_inputs = 0
        """The inputs counted so far (the row's ``n_inputs``)."""
        self.scan_points: list[int] = []
        """Each counted arrival's scan point, in arrival order."""
        self.sealed = False
        self.before_seal: Callable[[], None] | None = None
        """Called before the seal takes the arrivals lock: the catalog thread's last read and close (``jobs.catalog``),
        so that it makes no DM call after the seal, or the follower's thread's (``jobs.follower``), so that it
        looks no more."""
        self.follower: FollowerThread | None = None
        """A follower's thread: its end (done) seals the run, its run-level error fails it."""

    def should_stop(self) -> bool:
        return self.sealed or self._monitor.stop_reason == STOP_REASON_SHUTDOWN

    def arrivals(self) -> list[ManifestEntry]:
        self._recorder.flush_pending()
        if self.should_stop():
            return []
        sealing = self._monitor.stop_requested
        if self.follower is not None:
            if self.follower.error is not None:
                raise self.follower.error  # the follower cannot know its input: a run-level error
            if not sealing and self.follower.done:
                sealing = True
                self.log("its wire run has ended and every point it completed is admitted")
        if sealing:  # a writer that read the run open has appended once the lock is ours; later ones are refused
            if self.before_seal is not None:
                self.before_seal()
            seal(self._request.run_directory)
        elif not self._look():
            return []
        arrivals, offset = self._reader.read()
        entries = self._entries(arrivals)
        if entries:
            try:
                self._record(len(entries))
            except OperationalError as error:  # the database write-locked past its busy timeout: the next call
                if not self._delayed:
                    logger.warning("Run %s: its arrivals could not be counted yet (%s)", self._job_id, error.orig)
                self._delayed = True
                return []
            self._delayed = False
        self._reader.advance(offset, len(arrivals))
        self.n_inputs += len(entries)
        self.scan_points.extend(arrival.scan_point for arrival in arrivals)
        if sealing:
            if self._reader.tail:
                logger.warning("Run %s: ignored a torn last line of %s at its seal", self._job_id, ARRIVALS_FILENAME)
                self.log(f"ignored a torn last line of {ARRIVALS_FILENAME} ({len(self._reader.tail)} bytes)")
            self.sealed = True
            self.log(f"sealed after {self._reader.count} arrivals ({self.n_inputs} inputs)")
        return entries

    def _look(self) -> bool:
        """Whether to read ``arrivals.jsonl`` now: no look within ``interval`` of the last, and a look reads only when
        the file changed (a stat)."""
        now = self._clock()
        if now < self._next_look:
            return False
        self._next_look = now + self._interval
        return self._reader.changed()

    def _record(self, count: int) -> None:
        with Session(self._engine) as session, session.begin():
            begin_write(session)
            job = session.get(Job, self._job_id)
            if job is None:
                raise state.ExecutionStateError(f"job {self._job_id} vanished")
            state.record_arrivals(job, count)

    def _entries(self, arrivals: list[Arrival]) -> list[ManifestEntry]:
        """The inputs ``arrivals`` give: a file is one; a followed point is one per selected depth
        (the request's ``depth_range``, else every depth). Each carries its file's identity when it was admitted, which
        the live compute checks before it processes the input."""
        entries: list[ManifestEntry] = []
        for arrival in arrivals:
            index = self.n_inputs + len(entries)
            if arrival.point_id is None:
                entries.append(
                    ManifestEntry(
                        index,
                        input_identity(arrival.path),
                        arrival.path,
                        0,
                        arrival.scan_point,
                        None,
                        identity=arrival.identity,
                    )
                )
                continue
            selected = self._selected_depths()
            depths = range(arrival.n_depths or 0) if selected is None else selected
            for offset, depth_index in enumerate(depths):
                entries.append(
                    ManifestEntry(
                        index + offset,
                        scan_frame_identity(arrival.point_id, depth_index),
                        arrival.path,
                        0,
                        arrival.scan_point,
                        depth_index,
                        point_id=arrival.point_id,
                        depth_index=depth_index,
                        identity=arrival.identity,
                    )
                )
        return entries

    def _selected_depths(self) -> tuple[int, ...] | None:
        text = self._request.parameters.get("depth_range")
        return normalize_optional_integer_range(text, "depth range")[1]


def _display_id(job: Job) -> str:
    if job.indexing_run is not None:
        return f"Indexing I{job.indexing_run.id}"
    if job.reconstruction_run is not None:
        return f"Reconstruction R{job.reconstruction_run.id}"
    return f"Job {job.job_id}"


def _not_startable(job: Job) -> str | None:
    """Why ``job`` is not a run its dispatcher claimed for this process (Running, phase ``running``), or None."""

    if job.status != JobStatus.RUNNING or job.phase != RunPhase.RUNNING:
        return f"job {job.job_id} is {job.phase!r}/{state.STATUS_NAMES.get(JobStatus(job.status), job.status)}"
    return None


def _start(
    engine: Engine, job_id: int, policy: RunPolicy, *, cores: int | None = None
) -> tuple[RunRequest | None, str | None]:
    """Read a claimed run's persisted request; returns (request, None), or (None, reason) when nothing should run.

    The claim is the dispatcher's (``jobs.dispatch.claim_run``: Queued → Running in one ``begin_write``
    transaction); the job process it started runs the run it claimed. The slow reads (the manifest and the
    request, on NFS) run outside any lock, from a plain read of the run; a setup error is then recorded on the run,
    in a ``begin_write`` transaction and only while it is still Running (committed before ``RunSetupError`` is
    raised); a run that ended meanwhile (a reconciler) is left alone and nothing runs. ``cores``: a live run's
    (``RunRequest.cores``; the dispatcher's)."""

    with Session(engine) as session:
        job = session.get(Job, job_id)
        if job is None:
            raise RunSetupError(f"job {job_id} does not exist")
        skipped = _not_startable(job)
        if skipped is not None:
            return None, skipped
        display_id = _display_id(job)
        manifest_path, request_path, run_directory = job.manifest_path, job.request_path, job.run_directory
        digest, n_inputs = job.manifest_digest or "", job.n_inputs
        live_marked = has_live_marker(job)

    live: dict[str, Any] | None = None

    failure: str | None = None
    document: dict[str, Any] = {}
    if not manifest_path or not request_path or not run_directory:
        failure = "the job has no published manifest"
    else:
        try:
            verify_manifest(manifest_path, expected_digest=digest, expected_count=n_inputs)
            document = read_request(request_path)
            if live_marked:  # a live run's live.json is part of its setup
                live = read_live_document(run_directory).model_dump(mode="json")
        except Exception as error:
            failure = str(error)

    if failure is not None:
        with Session(engine) as session, session.begin():
            begin_write(session)
            job = session.get(Job, job_id)
            if job is None or _not_startable(job) is not None:
                return None, f"job {job_id} ended while its files were read"
            state.record_failure(job, f"Run setup failed: {failure}")
        raise RunSetupError(f"job {job_id}: {failure}")
    assert manifest_path is not None and run_directory is not None
    request = RunRequest(
        job_id=job_id,
        kind=str(document.get("kind")),
        display_id=display_id,
        run_directory=run_directory,
        manifest_path=manifest_path,
        document=document,
        n_inputs=n_inputs,
        workers=policy.workers,
        max_in_flight=policy.max_in_flight,
        reconstruction_workers=policy.reconstruction_workers,
        live=live,
        cores=cores if live is not None else None,
    )
    return request, None


def _decide(
    request: RunRequest,
    outcome: RunOutcome | None,
    error: BaseException | None,
    stop_reason: str | None,
    policy: RunPolicy,
    processed: tuple[int, int],
) -> _Decision:
    succeeded, failed = processed
    not_run = request.n_inputs - succeeded - failed
    counts = f"{succeeded} succeeded, {failed} failed, {not_run} not run of {request.n_inputs}"
    if isinstance(error, RunTimeout):
        return _Decision(
            JobStatus.FAILED,
            f"Run exceeded the configured limit of {policy.job_timeout_seconds} s; {counts}",
        )
    if error is not None:
        return _Decision(JobStatus.FAILED, f"Run failed: {type(error).__name__}: {error}; {counts}")
    assert outcome is not None
    if outcome.validation_error:
        return _Decision(
            JobStatus.FAILED, f"Authoritative output failed validation: {outcome.validation_error}; {counts}"
        )
    if outcome.stopped and stop_reason == STOP_REASON_SHUTDOWN:
        return _Decision(
            JobStatus.FAILED, f"Stopped by service shutdown; completed results kept; {counts}", RunPhase.INTERRUPTED
        )
    if outcome.stopped:
        return _Decision(JobStatus.CANCELLED, f"Cancelled by request; completed results kept; {counts}")
    if succeeded == 0 and request.n_inputs > 0:
        return _Decision(JobStatus.FAILED, f"All inputs failed; {counts}")
    if failed > 0 or succeeded < request.n_inputs:
        return _Decision(JobStatus.FAILED, f"Incomplete: {counts}")
    summary = outcome.summary or "all inputs processed"
    return _Decision(JobStatus.FINISHED, f"Finished: {counts}; {summary}")


LIVE_NOT_SEALED = "the live compute returned before the run was stopped"
LIVE_NOTHING_ARRIVED = "Stopped before any input arrived"


def _decide_live(
    n_inputs: int,
    outcome: RunOutcome | None,
    error: BaseException | None,
    stop_reason: str | None,
    sealed: bool,
    processed: tuple[int, int],
) -> _Decision:
    """A live run's end: a run-level error or a failed output validation Failed, a service stop
    Failed/interrupted, as a batch run's; sealed and drained, Finished with at least one input and none failed,
    Cancelled with none, Failed when any failed or was not processed."""
    succeeded, failed = processed
    counts = f"{succeeded} succeeded, {failed} failed, {n_inputs - succeeded - failed} not run of {n_inputs}"
    if error is not None:
        return _Decision(JobStatus.FAILED, f"Run failed: {type(error).__name__}: {error}; {counts}")
    assert outcome is not None
    if outcome.validation_error:
        return _Decision(
            JobStatus.FAILED, f"Authoritative output failed validation: {outcome.validation_error}; {counts}"
        )
    if stop_reason == STOP_REASON_SHUTDOWN:
        return _Decision(
            JobStatus.FAILED, f"Stopped by service shutdown; completed results kept; {counts}", RunPhase.INTERRUPTED
        )
    if not sealed:
        return _Decision(JobStatus.FAILED, f"Run failed: {LIVE_NOT_SEALED}; {counts}")
    if n_inputs == 0:
        return _Decision(JobStatus.CANCELLED, LIVE_NOTHING_ARRIVED)
    if failed > 0 or succeeded < n_inputs:
        return _Decision(JobStatus.FAILED, f"Incomplete: {counts}")
    summary = outcome.summary or "all inputs processed"
    return _Decision(JobStatus.FINISHED, f"Finished: {counts}; {summary}")


def _write_run_summary(
    request: RunRequest,
    decision: _Decision,
    outcome: RunOutcome | None,
    *,
    started_at: datetime,
    finished_at: datetime,
    processed: tuple[int, int],
    failure_report: str | None,
    policy: RunPolicy,
    hook_messages: list[str],
    n_inputs: int,
    live: bool = False,
) -> str:
    succeeded, failed = processed
    document = {
        "format": RUN_SUMMARY_FORMAT,
        "version": RUN_SUMMARY_VERSION,
        "job_id": request.job_id,
        "display_id": request.display_id,
        "kind": request.kind,
        "status": state.STATUS_NAMES[decision.status],
        "phase": str(decision.phase or state._PHASE_FOR_STATUS[decision.status]),
        "message": decision.message,
        "started_at": local_timestamp(started_at),  # naive local time to the second
        "finished_at": local_timestamp(finished_at),
        "counters": {
            "n_inputs": n_inputs,
            "n_succeeded": succeeded,
            "n_failed": failed,
            "n_not_run": n_inputs - succeeded - failed,
            "n_indexed": outcome.n_indexed if outcome else None,
        },
        "artifacts": dict(outcome.artifacts) if outcome else {},
        "warnings": list(outcome.warnings) if outcome else [],
        "validation_error": outcome.validation_error if outcome else None,
        "provenance": dict(outcome.provenance) if outcome else {},
        "failure_report": os.path.basename(failure_report) if failure_report else None,
        "policy": {
            "job_timeout_seconds": None if live else policy.job_timeout_seconds,  # a live run has none
            "workers": policy.workers,
            "max_in_flight": policy.max_in_flight,
            "reconstruction_workers": policy.reconstruction_workers,
        },
        "host": platform.node(),
        "log": hook_messages,
    }
    path = os.path.join(request.run_directory, RUN_SUMMARY_FILENAME)
    write_json_file(path, document, overwrite=True)
    return path


def execute_run(
    job_id: int,
    *,
    engine: Engine | None = None,
    policy: RunPolicy | None = None,
    shutdown: threading.Event | None = None,
    live_settings: LiveSettings | None = None,
    cores: int | None = None,
) -> dict[str, Any] | None:
    """Run one persisted job to a terminal state. Returns a summary, or None when nothing ran.

    The run is one the dispatcher claimed (Running; ``jobs.dispatch.claim_run``), run in the job process it started
    (``jobs.process``), which passes its engine, its policy, its service-stop event (``shutdown``, set by its own
    handler of the stop signal, maybe before the run began: the run then starts already stopping, and drains), its
    ``[live]`` settings and its cores (a live run's: the dispatcher's ``--cores``, ``RunRequest.cores``; its
    ``live.publish_seconds`` goes to its live compute as ``RunRequest.publish_seconds``). Without
    them, the engine, the policy and a live run's settings come from the process's settings (``get_settings()``, read
    here at call time), and the engine is disposed when the run ends.
    """

    if engine is None:
        settings = get_settings()
        own_engine = make_engine(settings)
        try:
            return execute_run(
                job_id,
                engine=own_engine,
                policy=policy or RunPolicy.from_settings(settings.execution),
                shutdown=shutdown,
                live_settings=live_settings or settings.live,
                cores=cores,
            )
        finally:
            own_engine.dispose()
    if policy is None:
        policy = RunPolicy.from_settings(get_settings().execution)
    request, skipped = _start(engine, job_id, policy, cores=cores)
    if request is None:
        logger.info("Skipping run %s: %s", job_id, skipped)
        return None
    if request.live is not None:
        live_settings = live_settings or get_settings().live
        request = replace(request, publish_seconds=live_settings.publish_seconds)

    started_at = utcnow()
    started_clock = time.monotonic()
    monitor = RunMonitor(engine, job_id, policy, shutdown=shutdown)
    recorder = ProgressRecorder(engine, job_id, policy.progress_seconds)
    failures = FailureReportWriter(os.path.join(request.run_directory, FAILURE_REPORT_FILENAME), overwrite=True)
    live = None
    catalog: CatalogThread | None = None
    follower: FollowerThread | None = None
    if request.live is not None:
        assert live_settings is not None
        interval = live_settings.arrivals_seconds
        live = LiveExecutorHooks(job_id, monitor, recorder, failures, engine=engine, request=request, interval=interval)
    hooks = live if live is not None else ExecutorHooks(job_id, monitor, recorder, failures)
    previous_handler = None
    in_main_thread = threading.current_thread() is threading.main_thread()
    if in_main_thread:
        previous_handler = signal.signal(SHUTDOWN_SIGNAL, lambda signum, frame: monitor.request_shutdown())
    monitor.start()

    outcome: RunOutcome | None = None
    error: BaseException | None = None
    try:
        if live is not None:
            assert live_settings is not None
            catalog = start_catalog(engine, request, live_settings, stopping=lambda: monitor.stop_requested)
            if catalog is not None:  # a DAQ run's arrivals (jobs.catalog): its last read, then its end, before the seal
                live.before_seal = catalog.before_seal
            follower = start_follower(engine, request, live_settings, stopping=lambda: monitor.stop_requested)
            if follower is not None:  # a follower's arrivals (jobs.follower): its end seals the run
                live.follower = follower
                live.before_seal = follower.close
            outcome = get_live_compute_function(request.kind)(request, live)
        else:
            compute = get_compute_function(request.kind)
            outcome = compute(request, read_manifest(request.manifest_path), hooks)
        if not isinstance(outcome, RunOutcome):
            raise RunSetupError(f"compute function for {request.kind!r} returned {type(outcome).__name__}")
        n_inputs = request.n_inputs if live is None else live.n_inputs
        if outcome.n_processed > n_inputs or outcome.n_succeeded < 0 or outcome.n_failed < 0:
            raise RunSetupError(f"compute function reported {outcome.n_processed} processed inputs of {n_inputs}")
        recorder.report(succeeded=outcome.n_succeeded, failed=outcome.n_failed)
    except BaseException as exc:  # timeouts and interrupts must still finalize the run
        error = exc
        if not isinstance(outcome, RunOutcome):
            outcome = hooks.outcome
        logger.exception("Run %s failed", job_id)
    finally:
        if catalog is not None:
            catalog.close()
        if follower is not None:
            follower.close()
        monitor.close()
        if in_main_thread and previous_handler is not None:
            signal.signal(SHUTDOWN_SIGNAL, previous_handler)
        swept = terminate_descendants()
        if swept:
            hooks.log(f"terminated {swept} leftover compute process(es)")
        try:
            failure_report = failures.close()
        except Exception as close_error:
            failure_report = None
            if error is None:
                error = close_error
        processed = (recorder.succeeded, recorder.failed)
        n_inputs = request.n_inputs if live is None else live.n_inputs
        if live is None:
            decision = _decide(request, outcome, error, monitor.stop_reason, policy, processed)
        else:
            decision = _decide_live(n_inputs, outcome, error, monitor.stop_reason, live.sealed, processed)
        finished_at = utcnow()
        try:
            _write_run_summary(
                request,
                decision,
                outcome,
                started_at=started_at,
                finished_at=finished_at,
                processed=processed,
                failure_report=failure_report,
                policy=policy,
                hook_messages=hooks.messages,
                n_inputs=n_inputs,
                live=live is not None,
            )
        except Exception as summary_error:
            if decision.status == JobStatus.FINISHED:
                decision = _Decision(JobStatus.FAILED, f"Run summary could not be written: {summary_error}")
            else:
                hooks.log(f"run summary could not be written: {summary_error}")
        scan_points = None if live is None else live.scan_points
        _finalize(engine, job_id, decision, processed, failure_report, finished_at, outcome, scan_points)
        logger.info("Run %s finished in %.1f s: %s", job_id, time.monotonic() - started_clock, decision.message)
    if error is not None:
        raise error
    return {
        "job_id": job_id,
        "status": state.STATUS_NAMES[decision.status],
        "message": decision.message,
        "n_succeeded": processed[0],
        "n_failed": processed[1],
    }


def _finalize(
    engine: Engine,
    job_id: int,
    decision: _Decision,
    processed: tuple[int, int],
    failure_report: str | None,
    finished_at: datetime,
    outcome: RunOutcome | None,
    scan_points: list[int] | None = None,
) -> None:
    """Record the run's end; a live run's (``scan_points``: its arrivals' numbers) also gets them as its run's scan
    points, the run grid's Scan Indices."""
    with Session(engine) as session, session.begin():
        job = session.get(Job, job_id)
        if job is None or state.is_terminal(job):
            return
        if scan_points is not None:
            _record_scan_points(job, scan_points)
        if outcome is not None and job.indexing_run is not None:
            if outcome.n_indexed is not None:
                job.indexing_run.n_frames_indexed = int(outcome.n_indexed)
            # Artifacts hold only published, validated paths, so the pointer is set after publication.
            if outcome.artifacts.get("results"):
                job.indexing_run.results_path = outcome.artifacts["results"]
        if job.phase == RunPhase.RUNNING:
            state.mark_finalizing(job, now=finished_at)
        state.record_progress(job, succeeded=processed[0], failed=processed[1], now=finished_at)
        state.finalize(
            job,
            status=decision.status,
            message=decision.message,
            failure_report_path=failure_report,
            phase=decision.phase,
            now=finished_at,
        )


def _record_scan_points(job: Job, scan_points: list[int]) -> None:
    parameters = (
        job.indexing_run.lauego_parameters
        if job.indexing_run is not None
        else job.reconstruction_run.wire_parameters
        if job.reconstruction_run is not None
        else None
    )
    if parameters is None:
        return
    numbers = sorted(set(scan_points))
    parameters.scan_points = str(srange(",".join(map(str, numbers)))) if numbers else ""
    parameters.scan_points_len = len(numbers)
