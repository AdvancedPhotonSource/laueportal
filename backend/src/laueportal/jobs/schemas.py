"""Typed results of the job domain: what the queue controls report (served as is), the run status and run
reference the API shows for a job, the run monitor's rows, the run lists' parameters, and the job page (the job,
its failed inputs and its log tail).

They carry raw values (states, counts, phases), never prose: wording is the frontend's job. The exception is a
validation's issues (``ValidationReport``), whose ``message`` the create forms show next to the field.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from laueportal.core.db import SQLITE_INTEGER_MAX
from laueportal.core.paging import GridPage, LiveGridParams
from laueportal.core.problems import Problem, ProblemItem
from laueportal.core.timeutil import UTCTimestamp
from laueportal.jobs.state import JobStatus


class CancelState(StrEnum):
    """What ``jobs.controls.cancel_run`` did."""

    CANCELLED = "cancelled"
    """The run had not started: it left the queue and was finalized as Cancelled (all inputs not run)."""
    REQUESTED = "requested"
    """The run is executing: a stop was recorded; the executor drains work in flight and finalizes it. For a
    submitted remote run (queued or running), the remote poller delivers the stop and finalizes it."""
    ALREADY_TERMINAL = "already_terminal"
    """The run had already ended; nothing changed."""
    NOT_FOUND = "not_found"
    """No job has this ID."""
    STOPPING_NOW = "stopping_now"
    """Stop now (``POST /jobs/{job_id}/stop-now``, a live run whose stop was already requested): the worker kills its
    job process, and the run ends Failed (``interrupted``) with what it published kept."""
    NOT_LIVE = "not_live"
    """Stop now is for a live run only (``POST /jobs/{job_id}/stop-now``: a 409); nothing changed."""


class CancelResult(BaseModel):
    """What a stop did (``jobs.controls.cancel_run``; ``POST /api/v1/jobs/{job_id}/cancel``, which answers an
    unknown job with a 404 instead of ``not_found``)."""

    model_config = ConfigDict(frozen=True, use_attribute_docstrings=True)

    job_id: int
    state: CancelState
    n_pending: int = 0
    """Inputs not yet processed when the request was made (0 unless ``cancelled`` or ``requested``)."""


class MoveState(StrEnum):
    """What ``jobs.controls.move_run_to_front`` did."""

    MOVED = "moved"
    """The run is now ahead of every other queued run (``job.moved_to_front_at``; the newest move first)."""
    NOT_QUEUED = "not_queued"
    """The run is not waiting in the queue (running, ended, or not yet enqueued); ``phase`` says where it is."""
    REMOTE = "remote"
    """The run executes on a remote target (``aps``, ``crux``), whose scheduler orders it; nothing moved."""
    LIVE = "live"
    """The run is a live run (``jobs.live``), which takes a live slot before any batch item: nothing moved."""
    NOT_FOUND = "not_found"
    """No job has this ID."""


class MoveResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: int
    state: MoveState
    phase: str | None = None
    """The run's phase (``jobs.state.RunPhase``) when it was looked up; None when the job does not exist."""


class RunStatus(StrEnum):
    """A run's status in the API (lower-case strings); the ``job`` table stores
    ``jobs.state.JobStatus``'s integers."""

    QUEUED = "queued"
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @classmethod
    def of(cls, status: int) -> RunStatus:
        """The API name of a stored ``Job.status``."""
        return cls(JobStatus(status).name.lower())


class RunKind(StrEnum):
    """What a job runs: an indexing run (``I12``) or a reconstruction run (``R3``)."""

    INDEXING = "indexing"
    RECONSTRUCTION = "reconstruction"


class RunReference(BaseModel):
    """The run a job executes, for links and labels: kind, ID and label (``I12``, ``R3``)."""

    model_config = ConfigDict(frozen=True, use_attribute_docstrings=True)

    kind: RunKind
    id: int
    label: str
    """``I`` (indexing) or ``R`` (reconstruction) and the run's ID."""

    @classmethod
    def of(cls, *, indexing_id: int | None, reconstruction_id: int | None) -> RunReference | None:
        """The reference of a job's run, from its indexing or reconstruction run's ID (None when it has neither)."""
        if indexing_id is not None:
            return cls(kind=RunKind.INDEXING, id=indexing_id, label=f"I{indexing_id}")
        if reconstruction_id is not None:
            return cls(kind=RunKind.RECONSTRUCTION, id=reconstruction_id, label=f"R{reconstruction_id}")
        return None


# ---------------------------------------------------------------------------
# API: the run lists' parameters and the run monitor


class RunGridParams(LiveGridParams):
    """A run list's grid parameters, and the scan whose runs to list (the scan detail's run cards)."""

    scan_id: int | None = Field(
        default=None,
        ge=0,
        le=SQLITE_INTEGER_MAX,
        description="Only the runs of this scan (its scan number); none for an unknown scan.",
    )


class _ApiModel(BaseModel):
    """API schemas: attribute docstrings become the OpenAPI descriptions (and Orval's comments)."""

    model_config = ConfigDict(use_attribute_docstrings=True)


class RemotePhase(StrEnum):
    """Where a ``crux`` run is, by its latest remote observation (four phases, and Login needed)."""

    UPLINK = "uplink"
    """Its inputs are on their way to ALCF."""
    QUEUED = "queued"
    """At ALCF, waiting (for the endpoint, its PBS submission or a PBS allocation: the detail says which)."""
    RUNNING = "running"
    """Its PBS job is processing."""
    DOWNLINK = "downlink"
    """Its results are on their way back, or being published."""
    LOGIN_REQUIRED = "login_required"
    """Its Globus flow waits for a sign-in (a session or consent requirement): someone signs in to Globus and resumes
    the run ("Login needed")."""


class RemoteObservation(_ApiModel):
    """A ``crux`` run's latest remote observation: the ``remoteObservation`` that the launcher's DM stage prints and the
    remote poller accepted from DM's record (``remote.observation``)."""

    phase: RemotePhase
    detail: str | None
    """What the phase is waiting for or doing, in the launcher's words (``Waiting for PBS allocation``,
    ``Processing``, ``Publishing results``)."""
    pbs_id: str | None
    """The run's PBS job ID (``123456.crux-pbs-01``) when the launcher knows it."""
    stale: bool
    """The phase may lag: the launcher could not see the run, or the observation is more than 90 s old."""
    observed_at: UTCTimestamp
    """When the launcher (or, for ``Publishing results``, the poller) observed it."""
    phase_since: UTCTimestamp | None
    """Since when the run has been in this phase; null when the launcher did not say."""
    flow_state: str | None
    """The Globus flow's step when observed (``InputTransfer``, ``RunPbsJob``, ``ResultTransfer``); null when the
    launcher did not say."""


class JobSummary(_ApiModel):
    """One row of the run monitor (``GET /api/v1/jobs``): a job and the run it executes."""

    job_id: int
    run: RunReference | None
    """The run the job executes (``I12``, ``R3``); null for a job without one. It filters as text on its label
    (e.g. ``equals`` ``I12``, ``startsWith`` ``R``) and does not sort."""
    author: str | None
    """The run's author."""
    status: RunStatus
    phase: str | None
    """The finer execution phase (``created``, ``published``, ``queued``, ``running``, ``finalizing``, then
    ``finished``, ``failed``, ``cancelled`` or ``interrupted``)."""
    n_inputs: int
    n_succeeded: int
    n_failed: int
    n_not_run: int
    n_processed: int
    """Inputs processed, with or without an error (``n_succeeded`` + ``n_failed``): the Run Progress."""
    start_time: UTCTimestamp | None
    """When the run started; null until it does."""
    duration_seconds: float | None
    """Seconds from the start to the finish, or to the page's ``as_of`` while the run has not finished, to the
    millisecond; null until the run starts."""
    message: str | None
    """The last line of the job's messages (its latest event, e.g. why it ended); it neither sorts nor filters."""
    scan_number: int | None
    """The run's scan (the Scan ID); null for an unlinked run."""
    target: str
    """Where the run executes: ``local`` (this portal's worker), or the remote ``aps`` or ``crux``, reached
    through DM (``jobs.backends``)."""
    execution: str
    """The target by its name, the run monitor's Execution (``core.params.BACKEND_NAMES``): ``APS``
    (``local``, ``aps``), ``ALCF`` (``crux``), else the target. It sorts and filters as text, the name shown."""
    dm_state: str | None
    """A remote run's DM state, as the submission and the remote poller last recorded it; null for a local
    run, and for a remote one before its submission."""
    observation: RemoteObservation | None
    """A ``crux`` run's latest remote observation as the job page shows it (``JobRemoteRun.observation``, ``stale``
    at the page's ``as_of``), for the Status cell while the run is queued or running; null when the job page shows
    none. It neither sorts nor filters."""
    live: bool
    """An open live run: its live queue identity while it is queued or running (``jobs.live.live_marker``, enough
    for those rows), so its Stop seals it; false once it has ended (the list reads no run directory) and
    for a batch run. It neither sorts nor filters."""
    held: bool
    """A queued local batch run held by an open live session: no batch run starts while a live run is queued or
    running; it starts once the session has ended. It neither sorts nor filters."""


class JobPage(GridPage[JobSummary]):
    """A page of the run monitor."""


# ---------------------------------------------------------------------------
# API: the job page

FAILURE_PAGE_SIZE = 50
"""Failed inputs per page of the job page's failure report."""
MAX_FAILURE_PAGE_SIZE = 200
"""The most failure records one request returns (a record may hold up to 64 KiB, ``jobs.summary``)."""
LOG_TAIL_LINES = 50
"""Log lines the job page shows."""
MAX_LOG_TAIL_LINES = 1000
"""The most log lines one request returns."""


class FileState(StrEnum):
    """Whether one of a run's files (or its run directory) could be read on the host serving the API: production's
    paths need not exist there."""

    AVAILABLE = "available"
    """It exists and was read."""
    NOT_RECORDED = "not_recorded"
    """The job records no path for it: no run directory yet (a job not yet published), no request (a run migrated
    from v1's per-input records), no failure report (nothing failed, or the run has not ended)."""
    MISSING = "missing"
    """Nothing is at the path on this host: not written (yet), or on storage this host does not mount (production's
    ``/net/...`` paths on a development machine)."""
    UNREADABLE = "unreadable"
    """Something is at the path but was not read: not a regular file (or a directory), a symbolic link, permission
    denied, too large, not the JSON expected, or JSON the response cannot serve (a lone surrogate, nesting deeper
    than 100 levels); ``detail`` says which."""
    NOT_RESPONDING = "not_responding"
    """Its file system did not answer in time (``files.probe``: a hung mount); nothing was read."""


class RunDirectory(_ApiModel):
    """The run's directory, as the job records it, and whether it is on this host."""

    path: str | None
    """Null until the run is published (and for a job without a run)."""
    state: FileState
    """``available`` when it is a directory on this host."""
    detail: str | None
    """Why it is ``unreadable`` (e.g. not a directory, permission denied); null otherwise."""


class RunFile(_ApiModel):
    """One file the endpoint read, and what came of it."""

    name: str
    """``request.json``, ``run.json``, ``history.json``, ``failures.jsonl`` or ``pbs-output.log``."""
    path: str | None
    """Where it was read (or looked for); null when the job records no path for it (``not_recorded``)."""
    state: FileState
    detail: str | None
    """Why it is ``unreadable``; null otherwise."""


class JobProgress(_ApiModel):
    """The Run Progress card: the job's stored counters and the run's liveness."""

    n_inputs: int
    n_processed: int
    """Inputs processed, with or without an error (``n_succeeded`` + ``n_failed``)."""
    n_succeeded: int
    n_failed: int
    n_not_run: int
    n_pending: int
    """Inputs neither processed nor recorded as not run: ``n_inputs`` - ``n_processed`` - ``n_not_run``."""
    heartbeat_age_seconds: float | None
    """Seconds from the run's last heartbeat to ``as_of``; null until it has one. A remote run's heartbeat is the
    remote poller's."""
    stale_after_seconds: int | None
    """The stale limit a running local run's heartbeat age is compared with (``execution.stale_heartbeat_seconds``);
    null for a remote run, to which the heartbeat check does not apply."""
    stale: bool
    """A running local run whose heartbeat is missing or older than the limit: the run process is probably gone,
    and the worker's next startup records the run as interrupted. Always false for other runs."""
    stop_requested: bool
    """A stop was requested and the run has not ended yet: it finishes the work in flight and keeps completed
    results."""
    stop_requested_at: UTCTimestamp | None
    """When a stop was requested; null if none was."""


class JobRemoteRun(_ApiModel):
    """Where a remote run executes and DM's view of it, and for a ``crux`` run where it is (its remote observation). Its
    progress counters update when the results return; the heartbeat check does not apply."""

    target: str
    """The execution target (``aps``, ``crux``; ``jobs.backends``)."""
    execution: str
    """The target by its name, as the run monitor's Execution shows it (``JobSummary.execution``: ``APS``,
    ``ALCF``)."""
    dm_state: str | None
    """DM's state for the run, as the submission and the remote poller last recorded it; null before submission."""
    globus_url: str | None
    """The run's Globus flow page, as DM reported it (``http`` or ``https`` only); null until DM reports one."""
    observation: RemoteObservation | None
    """A ``crux`` run's latest remote observation while the run is queued or running; null otherwise (an ``aps``
    run, none received yet, or the run ended)."""


class RequestManifest(_ApiModel):
    """``request.json``'s ``manifest``: the frozen input list."""

    path: str | None
    n_inputs: int | None
    sha256: str | None


class SubmittedRequest(_ApiModel):
    """The submitted request (``request.json``), raw values in submission order."""

    parameters: dict[str, Any]
    """The ``request`` section, without the keys the job row already shows (``submitted_at``, ``computer_name``) or
    that are not shown (``priority``)."""
    output: dict[str, Any]
    manifest: RequestManifest | None


class RunExecution(_ApiModel):
    """Execution and versions (``run.json``): what the run actually ran with."""

    provenance: dict[str, Any]
    """Versions and settings the compute function reported (a remote run's include its ``path_map``), as written."""
    policy: dict[str, Any]
    """The execution policy (a local run: the portal's limits; a remote run: its plan's ``execution``)."""
    host: str | None
    started_at: str | None
    """The execution window's start as ``run.json`` records it (a local run's naive local time, a remote
    run's UTC with ``+00:00``)."""
    finished_at: str | None
    n_indexed: int | None
    """Frames indexed (LaueGo runs)."""


class MessageClass(_ApiModel):
    message: str
    count: int
    """Inputs that recorded this message."""


class RunHistory(_ApiModel):
    """What a run migrated from v1's per-input records kept (its ``history.json``)."""

    original_status_name: str | None
    first_input_at: str | None
    """When the per-input work started, as recorded."""
    last_input_at: str | None
    message_classes: list[MessageClass]
    """The most frequent messages (at most 10), most frequent first."""


class ArtifactViewer(StrEnum):
    """The page that views an artifact."""

    INDEXING_RESULTS = "indexing_results"
    """The indexation's results viewer (``/indexing/$indexingId``)."""
    RECONSTRUCTION = "reconstruction"
    """The reconstruction viewer (``/reconstructions/$reconstructionId``)."""


class RunArtifact(_ApiModel):
    """An output the run published (``run.json``'s ``artifacts``, or an indexation's recorded results)."""

    name: str
    """The artifact's name (``results``, ``xml``, ``reconstruction``): artifacts are served by it."""
    path: str
    """Where the run recorded it (shown, never requested by path)."""
    viewer: ArtifactViewer | None
    """The page that views it (for a link); null when none does (the Output XML)."""


class RunConfiguration(_ApiModel):
    """The Run Configuration card: what the run's files say (``jobs.summary``). Each file read is in ``files`` with
    its state, so a file that is not on this host is reported, never an error."""

    kind: str | None
    """What ran (``lauego_indexing``, ``wire_reconstruction``): ``run.json``'s, else ``request.json``'s, else the
    run's."""
    run_directory: RunDirectory
    files: list[RunFile]
    """The files read: ``request.json`` and ``run.json``, and for a run without ``run.json`` whose failure report
    lies outside its run directory (in the migration export of a run migrated from v1's per-input records), the
    ``history.json`` next to that report."""
    problems: list[str]
    """Notes on files that exist but were not read (each names the file and why)."""
    validation_error: str | None
    """Why the run's output failed validation (``run.json``); null when it did not."""
    warnings: list[str]
    """``run.json``'s warnings."""
    partial_results: bool
    """The run ended without finishing (failed or cancelled) but published results (a ``results`` or
    ``reconstruction`` artifact) that hold its completed inputs."""
    request: SubmittedRequest | None
    """Null without a readable ``request.json``."""
    execution: RunExecution | None
    """Null without a readable ``run.json`` (also while the run has not ended: it is written last)."""
    history: RunHistory | None
    """Null except for a run migrated from v1's per-input records."""
    artifacts: list[RunArtifact]
    """By name."""


class LiveArrivalStatus(_ApiModel):
    """A live run's `arrival-status.json`: how its source of arrivals is doing (a DAQ's catalog thread, a follower
    waiting for a point, or a `notify` run's last file refused for having no number). Its keys as written,
    unchecked."""

    state: FileState
    """`available` (read), `unreadable` or `not_responding`; the keys are null unless it was read."""
    checked_at: str | None
    """The last look's time (UTC, to the second)."""
    rows_seen: int | None
    """The catalog rows seen so far (a follower's: the lines of its wire run's `completed.jsonl` read)."""
    error: str | None
    """The last look's error (the rows not used, the files not admitted yet, a follower's wait); null: none."""
    error_at: str | None
    """When that error was first seen."""


class JobLive(_ApiModel):
    """A live run's arrivals on the job page: where its inputs come from (`live.json`), how many arrived
    (`arrivals.jsonl`) and its source's status (`arrival-status.json`). Each
    file is read as recorded: one missing, unreadable or not responding is a state."""

    source: Literal["daq", "notify", "run"] | None
    """`daq` (DM's file catalog for a DAQ), `notify` (`laueportal live notify`) or `run` (a live wire run's completed
    points); null unless `live.json` was read (`source_state`)."""
    source_state: FileState
    """How reading `live.json` went."""
    daq_id: str | None
    """`daq`: DM's DAQ ID."""
    folder: str | None
    """The folder the files arrive in (a follower's: its wire run's)."""
    filename_template: str | None
    """Their name, one `%d` each file's number; null when the run has none (every file whose name ends `.h5`, its
    number the last run of digits in its name) or `live.json` was not read."""
    follows: RunReference | None
    """`run`: the live wire run it follows."""
    follows_job_id: int | None
    """`run`: that run's job."""
    arrivals: int | None
    """The inputs admitted so far: the whole lines of `arrivals.jsonl` (a follower's: the points); null unless it was
    read (`arrivals_state`)."""
    arrivals_state: FileState
    arrival: LiveArrivalStatus | None
    """Its `arrival-status.json` (a DAQ's catalog thread, or a follower: its last check, the points read and, while a
    point holds it, the waiting reason in `error`; a `notify` run's: the last file it refused for having no number);
    null while it has none (a follower before its first, a `notify` run before such a file)."""


class JobDetail(_ApiModel):
    """The job page (``GET /api/v1/jobs/{job_id}``): the job row, its run's progress, a remote run's
    state and the run's configuration. Durations and ages are measured to ``as_of``."""

    job_id: int
    run: RunReference | None
    """The run the job executes (``I12``, ``R3``); null for a job without one."""
    scan_number: int | None
    """The run's scan (the Scan ID); null for an unlinked run."""
    status: RunStatus
    phase: str | None
    """The finer execution phase (``created``, ``published``, ``queued``, ``running``, ``finalizing``, then
    ``finished``, ``failed``, ``cancelled`` or ``interrupted``)."""
    computer_name: str
    """The host the job was submitted from, as recorded at creation."""
    submit_time: UTCTimestamp | None
    start_time: UTCTimestamp | None
    finish_time: UTCTimestamp | None
    duration_seconds: float | None
    """Seconds from the start to the finish, or to ``as_of`` while the run has not finished, to the millisecond;
    null until the run starts."""
    messages: str | None
    """The job's messages, one line per event, oldest first."""
    progress: JobProgress
    remote: JobRemoteRun | None
    """Null for a local run."""
    held: bool
    """A queued local batch run held by an open live session (``JobSummary.held``)."""
    live: JobLive | None
    """A live run's arrivals; null for a run that is not live (`jobs.live.is_live_run`)."""
    configuration: RunConfiguration
    as_of: UTCTimestamp
    """The server's time (UTC) when the job was read."""


class FailureParams(BaseModel):
    """A page of a job's failure report: ``params: Annotated[FailureParams, Query()]``."""

    model_config = ConfigDict(extra="forbid")

    offset: int = Field(default=0, ge=0, le=SQLITE_INTEGER_MAX, description="Index of the first record to return.")
    limit: int = Field(
        default=FAILURE_PAGE_SIZE,
        ge=1,
        le=MAX_FAILURE_PAGE_SIZE,
        description=f"Records to return (at most {MAX_FAILURE_PAGE_SIZE}).",
    )


class FailureRow(_ApiModel):
    """One failed or unattempted input (a ``failures.jsonl`` record)."""

    index: int | None
    """The input's 0-based position in run order; null for a run migrated from v1's per-input
    records."""
    input_id: str | None
    """The input (its file stem, or ``<point>_<depth>`` for a reconstructed frame)."""
    source: str | None
    """The input's path, as the portal sees it: a remote run's is mapped through the run's own plan; null when it is
    unavailable (no portal path maps it, or none was recorded)."""
    category: str
    """``input``, ``numerical``, ``memory``, ``cancelled``, ``interrupted``, ``not_run`` or ``error``."""
    error_type: str | None
    """The error's class name; null for an input not attempted."""
    message: str
    recorded_at: str | None
    """When it was recorded, as written."""


class FailurePage(_ApiModel):
    """A page of a job's failed inputs (``GET /api/v1/jobs/{job_id}/failures``). A report that cannot be read
    (``report.state`` not ``available``) gives an empty page, never an error."""

    rows: list[FailureRow]
    total: int
    """The records the report holds by the job's counters: its failed inputs, and once the run has ended its
    inputs not run. It may differ from the file (a migrated run's report, a run still running)."""
    has_more: bool
    """Whether the report holds records after this page."""
    report: RunFile
    """``failures.jsonl``: ``not_recorded`` until the run ends with a record (none is published when nothing failed),
    ``missing`` when it is not on this host."""


class LogParams(BaseModel):
    """How much of a job's log: ``params: Annotated[LogParams, Query()]``."""

    model_config = ConfigDict(extra="forbid")

    tail: int = Field(
        default=LOG_TAIL_LINES,
        ge=1,
        le=MAX_LOG_TAIL_LINES,
        description=f"Lines to return from the end of each log (at most {MAX_LOG_TAIL_LINES}).",
    )


class LogTail(_ApiModel):
    """The last lines of one log."""

    lines: list[str]
    """At most ``tail`` lines, oldest first."""
    truncated: bool
    """Whether the log holds earlier lines."""
    n_lines: int | None
    """How many lines the log holds, when that is known without reading it all (``run.json``'s log); null
    otherwise."""
    file: RunFile
    """Where it was read, and whether it could be."""


class JobLog(_ApiModel):
    """A job's logs (``GET /api/v1/jobs/{job_id}/log``)."""

    job_id: int
    run_log: LogTail
    """The run's log: ``run.json``'s ``log``, the messages the compute function logged
    (the local executor's, or the remote runner's). ``run.json`` is written when the run ends."""
    pbs_output: LogTail | None
    """A remote run's PBS output (``pbs-output.log``: the batch job's standard output and error, published with
    its results, or still in the return inbox when publication refused them); read from the end of the file. Null
    for a local run."""


class Program(_ApiModel):
    """A program a run is created with, and its parameter model."""

    kind: Literal["lauego", "laue_matching", "wire", "lauego-live", "wire-live"]
    """The program: `lauego` (LaueGo indexing), `laue_matching` (LaueMatching indexing) or `wire` (wire reconstruction), the create forms'; `lauego-live` or
    `wire-live`, their live schemas (the Live page's runs), listed only while live processing is enabled."""
    title: str
    schema_id: str
    """The parameter schema's version (`lauego/v1`, `laue-matching/v1`, `wire/v1`, `lauego-live/v1`, `wire-live/v1`), which
    every `request.json` it writes holds."""
    json_schema: dict[str, Any]
    """The parameter model's JSON Schema: one property per `request.json` key, in `request.json`'s order,
    each with its `title` (the form label), `description`, limits (`minimum`, `exclusiveMinimum`, `maximum`),
    `default` (the configured one), and the form's hints: `x-source` (`form`: the user's; `config`: a fixed setting,
    never on the form; `derived`: set by the portal), `x-config` (the setting it comes from), `x-unit`, `x-group` (the
    form section), `x-widget`, `x-placeholder` and `x-options` (the choices in order, each `{value, label}`;
    for `backend` where it is a form field, the targets this deployment offers, the first being the `default`; a live
    run's is derived, `local`). The portal's own values are
    `readOnly`, and `required` lists only the form fields a client must send. `x-layout` is the form layout: its
    sections in order, each `{group, fields}` with the form fields in order."""


class ProgramList(_ApiModel):
    """The programs (`GET /api/v1/programs`): the batch programs, then the live ones while live processing is
    enabled (so their presence says the Live page is)."""

    programs: list[Program]


VALIDATE_BODY = (
    "One run's parameters: the create form's values, one per form field of `{schema}` (`GET /api/v1/programs`; "
    "the portal sets the config and derived ones, and refuses them here as on creation)."
)
"""The request body of a validate endpoint."""
CREATE_BODY = (
    "One run's parameters, as the validate endpoint takes them (`{schema}`): one run per request; a list or any other "
    "multi-run payload is refused, and nothing is created."
)
"""The request body of a create endpoint."""
IDEMPOTENCY_KEY = (
    "The create form's key (a UUID made when the form opens; at most 255 characters): the same key with the same body "
    "answers the run created the first time, for 24 hours; with another body, a 409."
)
"""The ``Idempotency-Key`` header of a create endpoint (``jobs.idempotency``)."""
IDEMPOTENCY_KEY_LENGTH = 255


class ValidationReport(_ApiModel):
    """What a validate endpoint answers for one run's parameters: a 200 whatever
    it finds; the create endpoint refuses the same parameters with a 422 when an issue is an error."""

    issues: list[ProblemItem]
    """Each problem found, in the order found: `loc` names the field (`["box_size"]`; a file-name template
    `["filename_prefixes", 1]`; `[]` for the whole), `severity` is `error` (the run cannot be created) or `warning`
    (it can), `code` is stable (pydantic's type for a field's own rule, e.g. `greater_than`, `missing`; the portal's
    otherwise, e.g. `not_found`, `path_not_allowed`, `source_path_differs`) and `message` is for people. Empty when
    the parameters are fine. At most 50 (the last then says how many more there were, `more_issues`)."""


class ProgramPrefill(_ApiModel):
    """A create form's values from one source context (`GET /api/v1/programs/{kind}/prefill`): the
    URL's prefill, and the clone of a run."""

    kind: Literal["lauego", "laue_matching", "wire"]
    schema_id: str
    """The parameter schema the values are for (`lauego/v1`, `wire/v1`)."""
    clone_of: RunReference | None
    """The run whose parameters were copied (a clone: an indexing run on the LaueGo form, a reconstruction on the
    wire form); null when the values come from a scan or a reconstruction as the source, or from the defaults."""
    values: dict[str, Any]
    """One value per form field of the program (its JSON Schema's `x-source` `form`, `GET /api/v1/programs`), as
    the create and validate endpoints take them: the configured defaults, the source's values over them, null where
    there is neither. Not validated: a copied value the model refuses is the validate endpoint's to report."""


class CreatedRun(_ApiModel):
    """What a create endpoint answers (201): the new run and its job, and whether the run was queued.
    The same `Idempotency-Key` with the same body answers the same again."""

    run: RunReference
    job_id: int
    status: RunStatus
    """The job's status after the submission: `queued` once it is on its target's queue, `failed` when its
    publication or its queueing failed (`failure` says why)."""
    target: str
    """Where the run executes: `local`, or the remote `aps` or `crux`, reached through DM (`jobs.backends`)."""
    queue_id: str | None
    """The run's queue ID (`run_<job_id>`) for a local run, DM's processing job ID for a remote one; null when the
    run was not queued."""
    failure: str | None
    """Why the created run could not be published or queued (the job is then Failed, or, when DM's reply was lost,
    left for the remote poller to reconcile); null when it was queued."""


class KeyConflict(Problem):
    """A create endpoint's 409: its `Idempotency-Key` is in use. With another body after the
    key's run was created (its answer lost, then the form edited), `created` is the answer kept with the key, so the
    form shows that run, and the type is `urn:laueportal:problem:key-created`. Without a created run (the key's
    request still running, or dead), `created` is absent and the type `about:blank`."""

    created: CreatedRun | None = None
    """The kept answer of the run the key created (a 201's `CreatedRun`), when it was created."""


KEY_CONFLICT: dict[int | str, dict[str, Any]] = {
    409: {
        "model": KeyConflict,
        "description": "The Idempotency-Key is in use: with another body, or by a request still running or that did "
        "not finish. Another body after the key's run was created is of type `urn:laueportal:problem:key-created`, "
        "with `created`, the run's kept answer.",
    }
}
"""The create endpoints' documented 409 (``responses=``)."""


# ---------------------------------------------------------------------------
# API: the Live page's: its DAQ list, and the validation and creation of a live session


class LiveDaqEntry(_ApiModel):
    """A DAQ as DM's DAQ list describes it: DM's text, unchecked."""

    id: str
    """DM's DAQ ID: a DAQ source's `daq_id`."""
    experiment: str
    status: str | None
    """DM's status (`Running`, ...); a live run binds only a running DAQ."""
    data_directory: str | None
    """Where the DAQ reads the detector's files (the Live page's "From")."""
    storage_directory: str | None
    """The experiment's storage directory."""
    dest_directory: str | None
    """Where the DAQ writes, relative to `storage_directory` (null: the storage directory itself)."""
    destination: str | None
    """Where the DAQ writes: `storage_directory` joined with `dest_directory` (the Live page's "To"), the folder a DAQ
    source fills in; null when DM gives no absolute storage directory or a destination that leaves it. As text: the bind
    check at Start resolves it."""


class LiveDaqList(_ApiModel):
    """The DM DAQ list (`GET /api/v1/live/daqs`): one answer shared by every browser, at most one DM call per
    `[live] daq_list_seconds`."""

    daqs: list[LiveDaqEntry]
    failed: list[str]
    """Why each DAQ record DM gave could not be read (it is left out)."""
    checked_at: UTCTimestamp
    """When DM was asked for this answer."""
    kept_seconds: float
    """How long this answer is still the one served (seconds): asking sooner gets it again, with no DM call, so a
    page asks again only after it."""


LIVE_RUNS_BODY = (
    "A live session: `scan`, the Scan card, `{source, daq_id, folder, filename_template, "
    "output_folder, scan_number, author}` (`source` `daq` with the DAQ's `daq_id`, or `notify`; the folder the files "
    "arrive in, their name with one `%d` (blank or null: every file whose name ends `.h5`, its number the last run of "
    "digits in its name), the folder the runs' folders go in); `reconstruction`, the live wire run's "
    "form fields of `wire-live/v1` but those the Scan card sets, or null; `indexing`, the live LaueGo run's of "
    "`lauego-live/v1` likewise, or null (`GET /api/v1/programs`). With both, the LaueGo run follows the wire run. The "
    "Scan card sets each run's `scan_number`, `author`, `input_path`, `filename_prefixes`, `output_path_template` "
    "(`<output_folder>/rec_%d`, `<output_folder>/index_%d`) and the LaueGo run's `reconstruction_id`: a "
    "program part that sends one is refused."
)
"""The request body of the live validate and create endpoints."""


class CreatedLiveRuns(_ApiModel):
    """What `POST /api/v1/live-runs` answers (201): the live session's runs and their jobs, the wire
    run first, then its follower (or the one run). The same `Idempotency-Key` with the same body answers the same
    again."""

    runs: list[CreatedRun]
    """Each run created, with its job: `queued` once the session's runs are queued together, `failed` when a run
    could not be created, published or queued (`failure` says why: then none of the session's runs is queued)."""


class OpenLiveRun(_ApiModel):
    """The open live run that refuses a new live session (one at a time): the oldest Queued or Running
    one."""

    job_id: int
    run: RunReference | None
    status: RunStatus


class LiveRunsConflict(Problem):
    """`POST /api/v1/live-runs`' 409. A live session is open (type `urn:laueportal:problem:live-session-open`):
    `open_run` names its oldest open run, and nothing was created. Or its `Idempotency-Key` is in use (as a create
    endpoint's): with another body after the key's session was created, `created` is its kept
    answer and the type `urn:laueportal:problem:key-created`; else `about:blank`."""

    created: CreatedLiveRuns | None = None
    """The kept answer of the session the key created, when it was created."""
    open_run: OpenLiveRun | None = None
    """The open live run, when a live session is open."""


class LiveSessionRun(_ApiModel):
    """One run of the live session, as its job row and its run directory say: the Live page's header strip."""

    job_id: int
    run: RunReference | None
    status: RunStatus
    n_inputs: int
    """The inputs arrived (a follower's: frames)."""
    n_succeeded: int
    """The inputs processed without error."""
    n_failed: int
    stop_requested: bool
    """A stop (Stop seals the session) is requested and the run has not ended yet."""
    stopped_now: bool
    """It ended by its Stop now: Failed, phase `interrupted`, its job process killed once its job's messages held
    "Stop now requested" (`jobs.dispatch.end_stopped_now`). A failure of its own (a failed file, a run-level error, a
    worker stop) is not."""
    arrival: LiveArrivalStatus | None
    """Its `arrival-status.json`; null while it has none (a follower before its first; a `notify` run until it
    refused a file without a number, its one line then)."""


class LiveSessionScan(_ApiModel):
    """The session's Scan card, as its source run recorded it (its `live.json` and rows): the Live page shows it
    while the session is open and fills the next session's Scan card from it once it has ended."""

    source: Literal["daq", "notify"]
    daq_id: str | None
    folder: str
    filename_template: str | None
    """Null: none (every file whose name ends `.h5`, its number the last run of digits in its name)."""
    output_folder: str | None
    """The folder the session's run folders went in (`<output_folder>/rec_%d`, `index_%d`)."""
    scan_number: int | None
    author: str | None


class LiveSession(_ApiModel):
    """The latest live session: the runs one Start created (the wire run first, then its follower; or the one
    run)."""

    open: bool
    """A run of it is Queued or Running (one live session at a time: Start is refused meanwhile)."""
    stopped: bool
    """A user stopped it: its first run's stop was requested (the Live page's Stop or Stop now, or Stop Run on that
    run's job page or in the Run Monitor), whether or not it has ended. The Live page shows no session once a stopped
    one has ended with no run Failed of its own (a run ended by its Stop now with no failed file, `stopped_now`, does
    not count; it still fills the next session's form from it); one that ended without a Stop, or with a run Failed
    otherwise, stays shown."""
    runs: list[LiveSessionRun]
    scan: LiveSessionScan | None
    """Null when its source run's `live.json` could not be read."""


class LiveSessionState(_ApiModel):
    """`GET /api/v1/live/session`: the latest live session, or null before the first."""

    session: LiveSession | None


LIVE_RUNS_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": Problem, "description": "Live processing is not enabled (`[live] enabled`)."},
    409: {
        "model": LiveRunsConflict,
        "description": "A live session is open (`urn:laueportal:problem:live-session-open`, with `open_run`), or the "
        "Idempotency-Key is in use (with another body after its session was created: "
        "`urn:laueportal:problem:key-created`, with `created`).",
    },
    503: {
        "model": Problem,
        "description": "DM did not answer the DAQ bind check (`urn:laueportal:problem:dm-unavailable`), the DAQ's "
        "folders did not answer it (`urn:laueportal:problem:not-responding`), or too many folder reads wait; with "
        "`Retry-After`. Nothing was created: send it again with the same key. A path that stops answering between "
        "the validation and the runs' creation is a 503 `not-responding` too, but the key stays claimed (the same key "
        "is a 409): send it with a new key; a run created before it is left Failed. (A folder or file of the form "
        "whose file system does not answer the validation is its field's issue `not_responding`: a 422.)",
    },
}
"""``POST /live-runs``' documented errors (``responses=``)."""
LIVE_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": Problem, "description": "Live processing is not enabled (`[live] enabled`)."},
}
"""Every live route's 404 while live processing is off."""
LIVE_DAQS_RESPONSES: dict[int | str, dict[str, Any]] = {
    **LIVE_RESPONSES,
    503: {
        "model": Problem,
        "description": "DM cannot be asked or did not answer (`urn:laueportal:problem:dm-unavailable`, its reason), "
        "with `Retry-After` the time this answer is kept (asking sooner gets it again, with no DM call).",
    },
}
"""``GET /live/daqs``' documented errors."""
