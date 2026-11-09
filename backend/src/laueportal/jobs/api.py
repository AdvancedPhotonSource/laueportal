"""Jobs endpoints: the run monitor's grid, the job page (the job, its failed inputs, its log tail) and the controls
(Stop, Move to Front); the programs a run is created with; and the Live page's: the DAQ list, a live session's
validation and its Start, present while ``[live] enabled`` (``require_live``: a 404 otherwise; they stay in the
contract, which never depends on the configuration)."""

from __future__ import annotations

from functools import partial
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Path, Query, Request, Response

from laueportal.core.api import EngineDep, SessionDep, SessionsDep, SettingsDep, read_folders
from laueportal.core.db import SQLITE_INTEGER_MAX
from laueportal.core.paging import LiveGridParams
from laueportal.core.problems import Problem, ProblemError
from laueportal.jobs import live_session, live_status, prefill, programs, service
from laueportal.jobs.daq import DaqList
from laueportal.jobs.schemas import (
    IDEMPOTENCY_KEY,
    IDEMPOTENCY_KEY_LENGTH,
    LIVE_DAQS_RESPONSES,
    LIVE_RESPONSES,
    LIVE_RUNS_BODY,
    LIVE_RUNS_RESPONSES,
    CancelResult,
    CreatedLiveRuns,
    FailurePage,
    FailureParams,
    JobDetail,
    JobLog,
    JobPage,
    LiveDaqList,
    LiveSessionState,
    LogParams,
    ProgramList,
    ProgramPrefill,
    ValidationReport,
)

router = APIRouter(tags=["jobs"])

JobId = Annotated[int, Path(ge=0, le=SQLITE_INTEGER_MAX, description="The job ID.")]


@router.get(
    "/jobs",
    name="listJobs",
    summary="List jobs",
    response_model=JobPage,
    openapi_extra=service.JOB_GRID.openapi_extra(),
)
def list_jobs(params: Annotated[LiveGridParams, Query()], session: SessionDep, response: Response) -> JobPage:
    """The run monitor (the grid query protocol), live (`changed_since`: the active jobs
    and those changed since). Sort and filter: `job_id`, `scan_number`, `duration_seconds`, the counters
    `n_inputs`, `n_succeeded`, `n_failed`, `n_not_run`, `n_processed` (number), `author`, `phase`, `status`,
    `target`, `execution` (by the names shown: `APS`, `ALCF`), `dm_state` (text) and
    `start_time` (date); `status` sorts in the lifecycle order (queued, running, finished, failed, cancelled); `run`
    filters as text on its label (`I12`, `R3`) and does not sort; `message` and a `crux` run's `observation` (the job page's, for the Status cell) neither sort nor filter.
    Default sort `-job_id`."""
    response.headers["Cache-Control"] = "no-store"
    return service.list_jobs(session, params)


@router.get("/jobs/{job_id}", name="getJob", summary="Get a job", response_model=JobDetail)
def get_job(job_id: JobId, sessions: SessionsDep, settings: SettingsDep, response: Response) -> JobDetail:
    """The job page: the job row (status and phase, computer, submit, start and finish times, duration,
    messages, its run and the run's scan), its progress (counters, heartbeat age against the stale limit, a stop
    requested), a remote run's target, DM state and Globus link (a `crux` run's remote observation while it is queued
    or running: phase, detail, PBS ID, stale, times), and the run's configuration from its run directory
    (problems, output validation error and warnings, partial results, the submitted request, execution and versions,
    a migrated run's history, the artifacts and the viewer of each). Every run file read is listed in
    `configuration.files` with its state: a file or directory that is not on this host is `missing` (unreadable
    ones say why), and one whose file system does not answer within a few seconds (a hung mount)
    `not_responding`, never an error. Poll it while the job is queued or running. 404 for an unknown job. The job
    is read in a session closed before any file is."""
    response.headers["Cache-Control"] = "no-store"
    return service.get_job(sessions, job_id, stale_after_seconds=settings.execution.stale_heartbeat_seconds)


@router.get(
    "/jobs/{job_id}/failures", name="listJobFailures", summary="List a job's failed inputs", response_model=FailurePage
)
def list_job_failures(
    job_id: JobId, params: Annotated[FailureParams, Query()], sessions: SessionsDep, response: Response
) -> FailurePage:
    """A page of the job's failure report (`failures.jsonl`, the Failed Inputs; 50 records by default, at most 200),
    in run order: each failed or unattempted input's position, ID, path, category, error and message. A remote run's
    paths are mapped to the portal's view through the run's own plan (null where unavailable). A report that is not
    recorded, not on this host or not readable (larger than 256 MB, a record on the page malformed or longer than
    64 KiB) gives an empty page whose `report` says why: a 200, never a 404 or a 500. 404 only for an unknown job."""
    response.headers["Cache-Control"] = "no-store"
    return service.list_job_failures(sessions, job_id, params)


@router.get("/jobs/{job_id}/log", name="getJobLog", summary="Get the tail of a job's log", response_model=JobLog)
def get_job_log(
    job_id: JobId, params: Annotated[LogParams, Query()], sessions: SessionsDep, response: Response
) -> JobLog:
    """The last `tail` lines (50 by default, at most 1000) of the job's logs: the run's log
    (`run.json`'s `log`, written when the run ends) and, for a remote run, its PBS output (`pbs-output.log`, read
    from the end of the file). A log that is not on this host is reported in its `file`, never an error. 404 for an
    unknown job."""
    response.headers["Cache-Control"] = "no-store"
    return service.get_job_log(sessions, job_id, params)


@router.post("/jobs/{job_id}/cancel", name="cancelJob", summary="Stop a job", response_model=CancelResult)
def cancel_job(job_id: JobId, engine: EngineDep) -> CancelResult:
    """Stop a run. `state` is `cancelled` for a run that had not started (it is Cancelled at once), `requested` for
    a running run or a submitted remote one (the executor, or the remote poller, stops it and finalizes it), and
    `already_terminal` for a run that had ended. `n_pending`: the inputs not processed when asked. 404 for an
    unknown job."""
    return service.cancel_job(job_id, engine=engine)


@router.post(
    "/jobs/{job_id}/stop-now",
    name="stopJobNow",
    summary="Stop a live job now",
    response_model=CancelResult,
    responses={409: {"model": Problem, "description": "The job is not a live run: nothing changed."}},
)
def stop_job_now(job_id: JobId, engine: EngineDep) -> CancelResult:
    """Stop a live run now (the Live page's Stop now while its session is Stopping): a queued one is `cancelled` and a
    running one not stopped yet gets its stop (`requested`: it drains), as `POST /jobs/{job_id}/cancel` does; a
    running one whose stop was requested is `stopping_now`: the worker kills its job process and it ends Failed
    (interrupted), what it published kept. `already_terminal` for a run that had ended; 404 for an unknown job; 409
    for a batch run."""
    return service.stop_job_now(job_id, engine=engine)


@router.post(
    "/jobs/{job_id}/move-to-front",
    name="moveJobToFront",
    summary="Move a queued job to the front of the queue",
    status_code=204,
    response_class=Response,
    response_model=None,
)
def move_job_to_front(job_id: JobId, engine: EngineDep) -> Response:
    """Move a queued job ahead of the other queued jobs (the worker takes the jobs moved to the front first, the
    newest move first); running work is never interrupted. 204 when it moved; 409 when the job is not waiting in the
    queue, runs remotely (its scheduler orders it) or is a live run (it starts before any batch run); 404 for an
    unknown job. Nothing moved unless 204."""
    service.move_job_to_front(job_id, engine=engine)
    return Response(status_code=204)


@router.get(
    "/programs", name="listPrograms", summary="List the programs and their parameters", response_model=ProgramList
)
def list_programs(settings: SettingsDep) -> ProgramList:
    """The programs a run is created with, `lauego` (LaueGo indexing) and `wire` (wire reconstruction), and, while
    live processing is enabled, their live schemas `lauego-live` and `wire-live` (the Live page's runs), each with its
    `schema_id` and its parameter model's JSON Schema: one property per `request.json` key with its title, unit,
    limits, configured default and form hints (`x-source`: the form, config or the portal), the form layout
    (`x-layout`) and the execution targets this deployment offers (`backend`'s `x-options`; a live run's `backend` is
    the portal's, `local`). The create forms and the Live page read their labels, defaults and layout here. A 503
    problem when remote execution is enabled but its settings cannot be used."""
    return programs.list_programs(settings)


SOURCE_QUERY = "One {what}'s ID (blank is none)."


@router.get(
    "/programs/{kind}/prefill",
    name="prefillProgram",
    summary="Fill a create form from one source",
    response_model=ProgramPrefill,
)
def prefill_program(
    kind: Annotated[programs.ProgramKind, Path(description="The program: `lauego`, `laue_matching` or `wire`.")],
    settings: SettingsDep,
    engine: EngineDep,
    response: Response,
    scan: Annotated[str | None, Query(description=SOURCE_QUERY.format(what="scan"))] = None,
    reconstruction: Annotated[str | None, Query(description=SOURCE_QUERY.format(what="reconstruction"))] = None,
    indexing: Annotated[str | None, Query(description=SOURCE_QUERY.format(what="LaueGo indexing run"))] = None,
) -> ProgramPrefill:
    """A create form's values from one source context: the configured defaults, with the source's over them. A scan
    gives its catalog's folder and file-name templates; a reconstruction on the LaueGo form its `scan.h5` as the input
    (its run directory without one), its templates, scan points and geometry file, and its scan; a clone (an indexing
    run on the LaueGo form, a reconstruction on the wire form) the run's saved parameters, input, scan and
    reconstruction, with the author and notes reset and no config value copied, an empty frame or wire skip given 1, a
    threshold ratio of 0 or less and a `nan` depth made null, and its target when still offered. The output folder is
    the configured template for the scan and reconstruction; the energy cutoffs stay the configured ones unless cloned.
    A 404 for an unknown scan or run; a 422 when a parameter is not an ID (`int_parsing`), when the sources disagree
    (`source_mismatch`) or for an indexing source on the wire form. The values are not validated (the validate
    endpoint does)."""
    response.headers["Cache-Control"] = "no-store"
    return prefill.prefill(
        kind, scan=scan, reconstruction=reconstruction, indexing=indexing, settings=settings, engine=engine
    )


# ---------------------------------------------------------------------------
# The Live page (jobs.live_session)


async def require_live(settings: SettingsDep) -> None:
    """A live route's gate: a 404 problem while live processing is off (``[live] enabled``), as if it were absent."""
    if not settings.live.enabled:
        raise ProblemError(404, "Not Found", "Live processing is not enabled on this portal")


LIVE = [Depends(require_live)]


def get_daq_list(request: Request) -> DaqList:
    """The api's DAQ list (``app.state.daq_list``): one answer shared by every request."""
    return request.app.state.daq_list


@router.get(
    "/live/daqs",
    name="listLiveDaqs",
    summary="List the DM DAQs a live session can take its files from",
    response_model=LiveDaqList,
    dependencies=LIVE,
    responses=LIVE_DAQS_RESPONSES,
)
async def list_live_daqs(
    response: Response, daqs: Annotated[DaqList, Depends(get_daq_list)], settings: SettingsDep
) -> LiveDaqList:
    """The Live page's DAQ list: DM's DAQs, each with where it writes (`destination`, the folder a DAQ source fills in),
    but those whose experiment name or data directory contains one of `[live] daq_name_exclude` (`logs`, `current`),
    ignoring case: left out of this list only (Validate and Start take any DAQ, the bind check at Start being DM's).
    One answer shared by every browser: at most one DM call per `[live] daq_list_seconds` (30 s), made only when asked
    (no Live page open, no call), a request that comes while it runs waiting for its answer (holding no thread);
    `kept_seconds`, the time the answer is still the one served (ask again after it). A failed call is a 503 problem
    (`urn:laueportal:problem:dm-unavailable`, its reason; DM not configured too), kept as long as an answer (twice as
    long for each failure in a row, up to 60 s), with `Retry-After` the time it is still kept. 404 while live processing
    is off."""
    response.headers["Cache-Control"] = "no-store"
    answer = await daqs.get_async()
    return live_session.daq_list(answer, time_left=daqs.time_left(), exclude=settings.live.daq_name_exclude)


@router.get(
    "/live/session",
    name="getLiveSession",
    summary="Get the latest live session",
    response_model=LiveSessionState,
    dependencies=LIVE,
    responses=LIVE_RESPONSES,
)
def get_live_session(sessions: SessionsDep, response: Response) -> LiveSessionState:
    """The Live page's session: the latest live session (the runs one Start created: the wire run
    first, then its follower; or the one run), or `session` null before the first. Each run's status, counters
    (`n_inputs` arrived, `n_succeeded`, `n_failed`) and stop request from its job row, and its `arrival-status.json`
    (null while it has none: a follower before its first, a `notify` run until it refused a file without a number);
    `open` while a run of it is Queued or Running; `stopped` once a user stopped it (its first run's stop flag, kept
    after it has ended: the Live page then shows no session once it has ended with no run Failed, a run ended by its
    Stop now with no failed file, `stopped_now`, not counting, and fills its form from it); `scan`, the session's
    Scan card from its source run's `live.json` and rows (null when that file cannot be read). Poll it while
    the session is open. The rows are read in a session closed before any file is; a file that is missing,
    unreadable or not answering is a state, never an error. 404 while live processing is off."""
    response.headers["Cache-Control"] = "no-store"
    return live_status.latest_session(sessions)


@router.post(
    "/live-runs/validate",
    name="validateLiveRuns",
    summary="Validate a live session",
    response_model=ValidationReport,
    dependencies=LIVE,
    responses=LIVE_RESPONSES,
)
async def validate_live_runs(
    form: Annotated[dict[str, Any], Body(description=LIVE_RUNS_BODY)],
    settings: SettingsDep,
    engine: EngineDep,
    request: Request,
) -> ValidationReport | Response:
    """Check a live session's form: the Scan card (the source and its DAQ, the folder an existing
    folder inside the roots, for a DAQ source one not there yet a warning (the DAQ makes it), the template blank or
    one file name with one `%d`, the output folder inside the roots, a warning when it exists and is not empty, the
    scan, the author), each run's parameters as its live schema's (`wire-live/v1`, `lauego-live/v1`) and the batch form's
    checks that apply, and the session's own: one scan number for the pair (the Scan card's), depth indices only for
    indexing that follows the reconstruction, the geometry (and the crystal) loading. Never a DM call (DM
    checks the DAQ once, at Start): a DAQ source's folder is checked against where the DAQ writes as the
    DAQ list (`listLiveDaqs`) last saw it (outside: an error on `["scan", "daq_id"]`), or, before the list was
    asked or without the DAQ in it, a warning there that Start checks the DAQ. A file system that does not answer is
    an issue `not_responding` on its field. Always a 200 with the issues, each `loc` starting with its part (`scan`,
    `reconstruction`, `indexing`; `[]` for the whole); `[]` when there are none. Reads only. A folder read's turn,
    as a create form's validation: while 16 wait, a 503 with `Retry-After`. 404 while live processing is off."""
    validate = partial(
        live_session.validate, form, settings=settings, engine=engine, daqs=get_daq_list(request).last_seen()
    )
    issues = await read_folders(request, validate)
    return issues if isinstance(issues, Response) else ValidationReport(issues=issues)


@router.post(
    "/live-runs",
    name="createLiveRuns",
    summary="Start a live session",
    status_code=201,
    response_model=CreatedLiveRuns,
    dependencies=LIVE,
    responses=LIVE_RUNS_RESPONSES,
)
async def create_live_runs(
    form: Annotated[dict[str, Any], Body(description=LIVE_RUNS_BODY)],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=IDEMPOTENCY_KEY_LENGTH, description=IDEMPOTENCY_KEY),
    ],
    settings: SettingsDep,
    engine: EngineDep,
    request: Request,
) -> CreatedLiveRuns | Response:
    """Start a live session: validated as `validateLiveRuns` validates it (a 422 problem with
    every issue when one is an error), then, while no live run is Queued or Running (else a 409
    `urn:laueportal:problem:live-session-open` naming the oldest, `open_run`), its runs are created (a live wire run,
    a live LaueGo run on the folder's raw frames, or both, the LaueGo run following the wire run) and queued
    together, all under one lock: two Starts never make two sessions, and the dispatcher never takes a wire run
    before its follower is queued. A 201 with the runs and their jobs once they exist (`failure` on a run that could
    not be created, published or queued: then none was queued). The same `Idempotency-Key` with the same body
    answers the same 201 again (24 hours); with another body, or while the first request runs, a 409 (another body
    after the session was started: `urn:laueportal:problem:key-created`, with `created`). A 503 with `Retry-After`,
    nothing created and the key released, when DM does not answer the DAQ bind check
    (`urn:laueportal:problem:dm-unavailable`) or the DAQ's folders do not (`urn:laueportal:problem:not-responding`),
    and before the key is claimed while 16 folder reads wait. A folder or file of the form whose file system does not
    answer the validation is an issue `not_responding` on its field, so a 422 that names it (as a batch create's
    validation); one that stops answering between the validation and the run's creation is a 503 `not-responding`
    that keeps the key claimed, as a batch create's. Stop a session with `cancelJob` on its first run (a follower
    drains once its wire run has ended). 404 while live processing is off."""
    return await read_folders(
        request,
        partial(
            live_session.start,
            form,
            key=idempotency_key,
            settings=settings,
            engine=engine,
            lock=request.app.state.live_start,
        ),
    )
