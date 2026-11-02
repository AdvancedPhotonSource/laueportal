"""Jobs as the API serves them: the run monitor's grid list, the job page, and the queue
controls' answers.

The run monitor lists every job with the run it executes (an indexing or a reconstruction run, each joined by its unique
``job_id``), that run's scan, and where the run executes (the target and DM's state; a ``crux`` run's remote observation
while it is active, ``job.remote_observation``, as the job page shows it, which the Status cell shows; it neither sorts
nor filters). Two fields are computed outside the grid's sort and filter: the run reference (``run``) filters as text on
its label (``I12``) but does not sort, because a text sort would put ``I10`` before ``I9`` (the default order, by job
ID, already orders runs by creation); and the last line of the messages (``message``) is taken in Python from the page's
rows, so it neither sorts nor filters (SQLite has no cheap way to find a text's last line). Every other field sorts and
filters, the computed ones too (author, scan and target are coalesced columns, the duration date arithmetic on the row);
the status filters by its name and sorts by its code, in the lifecycle order (``jobs.progress.RUN_PROGRESS_FIELDS``).

The job page is three reads, each answering an unknown job with ``JobNotFound``
(404): ``get_job`` (the job row, its progress, a remote run's state and the run configuration, ``jobs.summary``),
``list_job_failures`` (a page of ``failures.jsonl``) and ``get_job_log`` (the logs' tails). A run file that is not
on this host, or not readable, is reported with its state in the response (``schemas.FileState``), never as an
error: production's run directories (``/net/...``) need not exist on the host serving the API.

The controls are ``jobs.controls``' on the job table: ``cancel_job`` answers an unknown job with
``JobNotFound`` (404) and otherwise with the ``CancelResult``; ``move_job_to_front`` returns when the run moved and
raises a 404 or a 409 for what did not (not queued, a remote run).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Unpack
from urllib.parse import urlsplit

from sqlalchemy import ColumnElement, Engine, Select, String, and_, case, cast, exists, func, literal, not_, select
from sqlalchemy.orm import Session, aliased, sessionmaker

from laueportal.core.db import session_scope
from laueportal.core.paging import GridField, GridSpec, LiveGridParams, fetch_page
from laueportal.core.params import BACKEND_NAMES
from laueportal.core.problems import ProblemError
from laueportal.core.timeutil import UTCDateTime, utcnow
from laueportal.indexing.models import IndexingRun
from laueportal.jobs import controls, live_status, summary
from laueportal.jobs.backends import LOCAL, is_remote
from laueportal.jobs.live import LIVE_QUEUE_PREFIX, has_live_marker, live_marker, live_session_open
from laueportal.jobs.models import Job
from laueportal.jobs.order import local_jobs
from laueportal.jobs.progress import (
    ACTIVE_STATUSES,
    RUN_PROGRESS_FIELDS,
    RunProgress,
    active_or_changed_since,
    grid_progress_columns,
    heartbeat_age_seconds,
    is_stale,
)
from laueportal.jobs.rundir import FailureRecord
from laueportal.jobs.schemas import (
    ArtifactViewer,
    CancelResult,
    CancelState,
    FailurePage,
    FailureParams,
    FailureRow,
    JobDetail,
    JobLog,
    JobPage,
    JobProgress,
    JobRemoteRun,
    JobSummary,
    LogParams,
    LogTail,
    MoveState,
    RemoteObservation,
    RemotePhase,
    RunArtifact,
    RunConfiguration,
    RunDirectory,
    RunExecution,
    RunFile,
    RunHistory,
    RunReference,
    RunStatus,
    SubmittedRequest,
)
from laueportal.jobs.state import JobStatus, RunPhase
from laueportal.reconstruction.models import ReconstructionRun
from laueportal.remote import observation

JOB_GRID = GridSpec(
    fields={
        "job_id": GridField("number"),
        "run": GridField("text", sortable=False),
        "author": GridField("text"),
        "phase": GridField("text"),
        **RUN_PROGRESS_FIELDS,
        "start_time": GridField("date"),
        "duration_seconds": GridField("number"),
        "scan_number": GridField("number"),
        "target": GridField("text"),
        "execution": GridField("text"),
        "dm_state": GridField("text"),
    },
    key="job_id",
    default_sort="-job_id",
    changed_since=active_or_changed_since,
)
"""The run monitor: newest job first; ``run`` filters only and ``message`` is not a grid field (the
module docstring says why). Live: the active jobs and those changed since (``jobs.progress.active_or_changed_since``)."""


class JobNotFound(ProblemError):
    def __init__(self, job_id: int) -> None:
        super().__init__(404, "Job not found", f"No job {job_id}")


# ---------------------------------------------------------------------------
# The run monitor's grid


def _run_label() -> ColumnElement[Any]:
    """``RunReference.label`` in SQL: ``I<id>`` for an indexing run, ``R<id>`` for a reconstruction, else null."""

    return case(
        (IndexingRun.id.is_not(None), literal("I") + cast(IndexingRun.id, String)),
        (ReconstructionRun.id.is_not(None), literal("R") + cast(ReconstructionRun.id, String)),
    )


def _duration_seconds(now: datetime) -> ColumnElement[Any]:
    """Seconds from the start to the finish (or ``now`` until the run finishes), to the millisecond; null before the
    start. SQLite's ``julianday`` reads ``UTCDateTime``'s text."""

    end = func.coalesce(Job.finish_time, literal(now, UTCDateTime()))
    return func.round((func.julianday(end) - func.julianday(Job.start_time)) * 86400.0, 3)


def _execution() -> ColumnElement[Any]:
    """``JobSummary.execution`` in SQL: the job's target by its name (``core.params.BACKEND_NAMES``: ``APS``,
    ``ALCF``), else the target itself, so that the grid filters and sorts the name it shows (as ``run_status_name``
    does)."""

    target = func.coalesce(Job.backend, LOCAL)
    return case(BACKEND_NAMES, value=target, else_=target)


def execution_name(target: str) -> str:
    """``_execution`` for one target: the name the run monitor and the job page show."""

    return BACKEND_NAMES.get(target, target)


def _open_live_run() -> ColumnElement[Any]:
    """``JobSummary.live`` in SQL: the live queue identity (``jobs.live.live_marker``) of a queued or running job."""

    return case((and_(live_marker(), Job.status.in_(ACTIVE_STATUSES)), True), else_=False)


def _held() -> ColumnElement[Any]:
    """``JobSummary.held`` in SQL: a Queued local batch run (phase ``queued``, no live queue identity) while a live run
    is open (another row with a live queue identity, Queued or Running: ``jobs.live.live_session_open``'s rule), in
    the page's own query (one uncorrelated ``EXISTS``, evaluated once)."""

    other = aliased(Job)
    other_live = other.queue_job_id == literal(LIVE_QUEUE_PREFIX) + cast(other.job_id, String)
    live_open = exists().where(other.status.in_(ACTIVE_STATUSES), other_live)
    queued_batch = and_(
        Job.status == int(JobStatus.QUEUED), Job.phase == RunPhase.QUEUED, local_jobs(), not_(live_marker())
    )
    return case((and_(queued_batch, live_open), True), else_=False)


def job_list_query(now: datetime) -> Select[Unpack[tuple[Any, ...]]]:
    """The run monitor's select: one row per job, its run (by label and by ID), the run's scan and author, the job's
    state, counters and times, its execution target and the target's name, DM state and stored remote observation,
    and whether it is an open live run. A running run's duration is measured to ``now``."""

    scan_number = func.coalesce(ReconstructionRun.scan_number, IndexingRun.scan_number)
    return (
        select(
            Job.job_id.label("job_id"),
            _run_label().label("run"),
            IndexingRun.id.label("indexing_id"),
            ReconstructionRun.id.label("reconstruction_id"),
            func.coalesce(ReconstructionRun.author, IndexingRun.author).label("author"),
            Job.phase.label("phase"),
            *grid_progress_columns(),
            Job.start_time.label("start_time"),
            _duration_seconds(now).label("duration_seconds"),
            Job.messages.label("messages"),
            scan_number.label("scan_number"),
            func.coalesce(Job.backend, LOCAL).label("target"),
            _execution().label("execution"),
            Job.external_state.label("dm_state"),
            Job.remote_observation.label("stored_observation"),
            _open_live_run().label("live"),
            _held().label("held"),
        )
        .select_from(Job)
        .outerjoin(ReconstructionRun, ReconstructionRun.job_id == Job.job_id)
        .outerjoin(IndexingRun, IndexingRun.job_id == Job.job_id)
    )


def last_line(messages: str | None) -> str | None:
    """The last line of a job's messages (``jobs.state`` appends one line per event), trailing blanks ignored."""

    lines = (messages or "").rstrip().splitlines()
    return lines[-1] if lines else None


def _job_summary(row: Mapping[Any, Any], now: datetime) -> JobSummary:
    values = dict(row)
    values["run"] = RunReference.of(
        indexing_id=values.pop("indexing_id"), reconstruction_id=values.pop("reconstruction_id")
    )
    values["message"] = last_line(values.pop("messages"))
    stored = values.pop("stored_observation")
    values["observation"] = observation_view(values["target"], values["status_code"], stored, now)
    return JobSummary.model_validate(values)


def list_jobs(session: Session, params: LiveGridParams) -> JobPage:
    """One page of the run monitor: two queries. The page's ``as_of`` is the time running durations end at, and the
    time its rows' observations are read at (``observation_view``)."""

    now = utcnow()
    page = fetch_page(session, job_list_query(now), JOB_GRID, params, as_of=now)
    return JobPage(rows=[_job_summary(row, now) for row in page.rows], total=page.total, as_of=page.as_of)


# ---------------------------------------------------------------------------
# The job page

ARTIFACT_VIEWERS = {"results": ArtifactViewer.INDEXING_RESULTS, "reconstruction": ArtifactViewer.RECONSTRUCTION}
"""The artifacts a page views: an indexation's results and a reconstruction's scan catalog. The Output XML
(``xml``) has none."""


def _job(session: Session, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise JobNotFound(job_id)
    return job


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def remote_observation(job: Job, now: datetime) -> RemoteObservation | None:
    """A ``crux`` job's stored remote observation (``job.remote_observation``) as the job page shows it
    (``observation_view``)."""

    return observation_view(job.backend, job.status, job.remote_observation, now)


def observation_view(backend: str | None, status: int, value: Any, now: datetime) -> RemoteObservation | None:
    """A stored remote observation (``value``) of a job of ``backend`` and ``status`` as the job page and the run
    monitor's row show it (one builder, which both share), while a ``crux`` job is queued or
    running; None otherwise, and when it has no known phase or no observation time, and
    when SQLite's JSON functions cannot read it (``observation.unreadable``). Stale when the launcher said so or it is more than ``STALE_AFTER_SECONDS`` old at ``now``."""

    if backend != observation.OBSERVED_TARGET or status not in ACTIVE_STATUSES or not isinstance(value, dict):
        return None
    if observation.unreadable(value) is not None:
        return None
    phase, observed_at = value.get("phase"), observation.aware_time(value.get("observed_at"))
    if not isinstance(phase, str) or phase not in observation.PHASES or observed_at is None:
        return None
    pbs = value.get("pbs")
    return RemoteObservation(
        phase=RemotePhase(phase),
        detail=_text(value.get("detail")),
        pbs_id=_text(pbs.get("job_id")) if isinstance(pbs, dict) else None,
        stale=bool(value.get("stale")) or (now - observed_at).total_seconds() > observation.STALE_AFTER_SECONDS,
        observed_at=observed_at,
        phase_since=observation.aware_time(value.get("phase_since")),
        flow_state=_text(value.get("flow_state")),
    )


def _web_link(url: str | None) -> str | None:
    """``url`` when it is an absolute ``http`` or ``https`` URL, a link the page may show; None otherwise."""

    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    return url if parts.scheme in ("http", "https") and parts.netloc else None


def _job_duration(job: Job, now: datetime) -> float | None:
    """Seconds from the start to the finish, or to ``now`` until the run finishes (the run monitor's rule)."""

    if job.start_time is None:
        return None
    return round(((job.finish_time or now) - job.start_time).total_seconds(), 3)


def _run_file(read: summary.FileRead) -> RunFile:
    return RunFile(name=read.name, path=read.path, state=read.state, detail=read.detail)


def _job_progress(progress: RunProgress, *, remote: bool, stale_after_seconds: int, now: datetime) -> JobProgress:
    return JobProgress(
        n_inputs=progress.n_inputs,
        n_processed=progress.n_processed,
        n_succeeded=progress.n_succeeded,
        n_failed=progress.n_failed,
        n_not_run=progress.n_not_run,
        n_pending=progress.n_pending,
        heartbeat_age_seconds=heartbeat_age_seconds(progress, now=now),
        stale_after_seconds=None if remote else stale_after_seconds,
        stale=is_stale(progress, stale_after_seconds=stale_after_seconds, now=now),
        stop_requested=progress.cancel_requested_at is not None and not progress.is_terminal,
        stop_requested_at=progress.cancel_requested_at,
    )


def _configuration(details: summary.RunDetails) -> RunConfiguration:
    request = summary.submitted_request(details)
    execution = summary.execution_info(details)
    history = summary.history_info(details)
    directory = details.directory
    return RunConfiguration(
        kind=details.kind,
        run_directory=RunDirectory(path=directory.path, state=directory.state, detail=directory.detail),
        files=[_run_file(read) for read in details.files],
        problems=list(details.problems),
        validation_error=summary.validation_error(details),
        warnings=summary.run_warnings(details),
        partial_results=summary.has_partial_results(details),
        request=None if request is None else SubmittedRequest.model_validate(request, from_attributes=True),
        execution=None if execution is None else RunExecution.model_validate(execution, from_attributes=True),
        history=None if history is None else RunHistory.model_validate(history, from_attributes=True),
        artifacts=[
            RunArtifact(name=name, path=path, viewer=ARTIFACT_VIEWERS.get(name))
            for name, path in sorted(details.artifacts.items())
        ],
    )


def get_job(sessions: sessionmaker[Session], job_id: int, *, stale_after_seconds: int) -> JobDetail:
    """The job page's data (``JobDetail``), measured to now; ``JobNotFound`` (404) for an unknown job.
    ``stale_after_seconds``: ``execution.stale_heartbeat_seconds``. The rows are read in a session closed before
    the run's files are (a hung mount holds no database connection)."""

    with session_scope(sessions) as session:
        job = _job(session, job_id)
        indexing, reconstruction = job.indexing_run, job.reconstruction_run  # loaded while the session is open
        record = summary.run_record(job)
        live = live_status.live_row(job)
        queued_batch = job.status == JobStatus.QUEUED and job.phase == RunPhase.QUEUED and not is_remote(job.backend)
        held = queued_batch and not has_live_marker(job) and live_session_open(session)
    details = summary.read_run_details(record)
    now = utcnow()
    scan_number = reconstruction.scan_number if reconstruction is not None else None
    if scan_number is None and indexing is not None:
        scan_number = indexing.scan_number
    remote = None
    if job.backend is not None and is_remote(job.backend):
        remote = JobRemoteRun(
            target=job.backend,
            execution=execution_name(job.backend),
            dm_state=job.external_state,
            globus_url=_web_link(job.external_url),
            observation=remote_observation(job, now),
        )
    return JobDetail(
        job_id=job.job_id,
        run=RunReference.of(
            indexing_id=None if indexing is None else indexing.id,
            reconstruction_id=None if reconstruction is None else reconstruction.id,
        ),
        scan_number=scan_number,
        status=RunStatus.of(job.status),
        phase=job.phase,
        computer_name=job.computer_name,
        submit_time=job.submit_time,
        start_time=job.start_time,
        finish_time=job.finish_time,
        duration_seconds=_job_duration(job, now),
        messages=job.messages,
        progress=_job_progress(
            details.progress, remote=remote is not None, stale_after_seconds=stale_after_seconds, now=now
        ),
        remote=remote,
        held=held,
        live=live_status.job_live(live),
        configuration=_configuration(details),
        as_of=now,
    )


def _failure_row(record: FailureRecord) -> FailureRow:
    return FailureRow(
        index=record.index,
        input_id=record.input_id,
        source=record.source,
        category=record.category,
        error_type=record.error_type,
        message=record.message,
        recorded_at=record.recorded_at,
    )


def list_job_failures(sessions: sessionmaker[Session], job_id: int, params: FailureParams) -> FailurePage:
    """A page of the job's failure report (``jobs.summary.failure_report_page``: a remote run's sources mapped
    through its plan); a report that cannot be read is an empty page saying why. ``JobNotFound`` (404) for an
    unknown job. The job is read in a session closed before the report is."""

    with session_scope(sessions) as session:
        job = _job(session, job_id)
    progress = RunProgress.from_job(job)
    page = summary.failure_report_page(progress, job.run_directory, offset=params.offset, limit=params.limit)
    return FailurePage(
        rows=[_failure_row(record) for record in page.records],
        total=summary.failure_count(progress),
        has_more=page.has_more,
        report=_run_file(page.file),
    )


def _log_tail(tail: summary.LogTail) -> LogTail:
    return LogTail(lines=tail.lines, truncated=tail.truncated, n_lines=tail.n_lines, file=_run_file(tail.file))


def get_job_log(sessions: sessionmaker[Session], job_id: int, params: LogParams) -> JobLog:
    """The tails of the job's logs: the run's log (``run.json``'s) and a remote run's PBS output.
    ``JobNotFound`` (404) for an unknown job. The job is read in a session closed before the logs are."""

    with session_scope(sessions) as session:
        job = _job(session, job_id)
    document, read = summary.read_run_summary(job.run_directory)
    pbs_output = None
    if job.backend is not None and is_remote(job.backend):
        pbs_output = _log_tail(summary.pbs_output(job.run_directory, job.submission_id, limit=params.tail))
    return JobLog(
        job_id=job.job_id,
        run_log=_log_tail(summary.run_log(document, read, limit=params.tail)),
        pbs_output=pbs_output,
    )


# ---------------------------------------------------------------------------
# The controls


def cancel_job(job_id: int, *, engine: Engine) -> CancelResult:
    """Stop a job (``jobs.controls.cancel_run``): its ``CancelResult``, or ``JobNotFound`` (404).

    A queued run is ``cancelled`` at once; a running one, or a submitted remote one, is ``requested``; an ended one
    is ``already_terminal``."""

    result = controls.cancel_run(job_id, engine=engine)
    if result.state == CancelState.NOT_FOUND:
        raise JobNotFound(job_id)
    return result


def stop_job_now(job_id: int, *, engine: Engine) -> CancelResult:
    """Stop a live run now (``jobs.controls.stop_run_now``): its ``CancelResult``, ``JobNotFound`` (404), or a 409
    ``ProblemError`` for a batch run (nothing changed)."""

    result = controls.stop_run_now(job_id, engine=engine)
    if result.state == CancelState.NOT_FOUND:
        raise JobNotFound(job_id)
    if result.state == CancelState.NOT_LIVE:
        raise ProblemError(409, "Not a live run", f"Job {job_id} is not a live run: Stop now is for live runs")
    return result


def move_job_to_front(job_id: int, *, engine: Engine) -> None:
    """Move a queued job ahead of the other queued jobs (``jobs.controls.move_run_to_front``).

    Raises ``JobNotFound`` (404), or a 409 ``ProblemError`` when the job is not waiting in the queue, runs remotely
    (its scheduler orders it) or is a live run. Nothing moved then."""

    result = controls.move_run_to_front(job_id, engine=engine)
    if result.state == MoveState.MOVED:
        return
    if result.state == MoveState.NOT_FOUND:
        raise JobNotFound(job_id)
    if result.state == MoveState.REMOTE:
        raise ProblemError(
            409,
            "Job runs remotely",
            f"Job {job_id} runs on a remote target, whose scheduler orders it: it cannot be moved to the front",
        )
    if result.state == MoveState.LIVE:
        raise ProblemError(
            409,
            "Live run",
            f"Job {job_id} is a live run, which starts before any batch run: it cannot be moved to the front",
        )
    where = f" (phase {result.phase})" if result.phase else ""
    raise ProblemError(
        409, "Job not queued", f"Job {job_id} is not waiting in the queue{where}: only a queued job moves"
    )
