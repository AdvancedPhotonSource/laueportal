"""A live run's ``live.json``, and which runs are live.

``live.json`` says where a live run's inputs come from; its parameters stay in ``request.json`` (``wire-live/v1``,
``lauego-live/v1``), with exactly the batch schemas' keys. It is written once, when the run is created, beside
``request.json``: through a partial file, fsynced, then published by an exclusive link (``rundir.write_json_file``); it
is never rewritten. Every key is always there, null where it does not apply:

- ``format`` ``laue-portal-live``, ``version`` 1;
- ``source``: ``daq`` (DM's file catalog for a DAQ), ``notify`` (``laueportal live notify``) or ``run`` (a live
  LaueGo run following a live wire run's completed points);
- ``daq``: for ``daq`` only: DM's DAQ ``id``, its ``experiment``, the experiment's ``storage`` directory (the
  catalog's file paths are relative to it) and the DAQ's ``destination`` (where it writes; ``folder`` lies inside);
- ``folder``: the folder the files arrive in, and ``filename_template``: their name, one ``%d`` each file's number,
  or null: every file whose name ends ``.h5``, its number the last run of digits in its name
  (``jobs.arrivals.RunFiles``); a follower's are its wire run's;
- ``follows``: for ``run`` only: the wire run's ``job_id`` (its job row says when it ends) and run ``directory``
  (whose ``completed.jsonl`` the follower reads).

The paths are absolute and normalized; a check against the file system or DM (the folder exists, the DAQ runs) is the
creation's, not this model's.

**Which runs are live** (no column): a run whose ``queue_job_id`` is exactly ``live_<its own job id>``
(``live_queue_id``, given when it is queued: ``jobs.queue.enqueue_live_run``) and whose run directory holds
``live.json``. A Queued or Running row is live by its queue identity alone (``live_marker``, one SQL expression; no
migrated row is active, so none can match there); an ended row also needs the file (``is_live_run``: one stat).

**One live session at a time**: a live run is *open* while it is Queued or Running
(``open_live_runs``). While one is, the worker's dispatcher claims no local batch run or conversion
(``jobs.dispatch``), and a new live session is refused: ``check_one_live_session`` raises ``LiveSessionOpen`` naming
the open run, which the Live page's Start (``jobs.live_session``) calls once, before it creates the session's runs (a
follower of the session's own wire run is part of the same Start), and answers with a 409.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import ColumnElement, Select, String, cast, literal, select
from sqlalchemy.orm import Session

from laueportal.core.params import TEMPLATE_MAX_LENGTH
from laueportal.files.roots import is_file_here
from laueportal.jobs import state
from laueportal.jobs.models import Job
from laueportal.jobs.rundir import LIVE_FILENAME as LIVE_FILENAME
from laueportal.jobs.rundir import write_json_file

LIVE_FORMAT = "laue-portal-live"
LIVE_VERSION = 1
LIVE_QUEUE_PREFIX = "live_"

LiveSource = Literal["daq", "notify", "run"]


def _absolute(value: str) -> str:
    """An absolute path, normalized (``os.path.normpath``); a relative one, or one with a NUL, is refused."""
    if "\0" in value or not os.path.isabs(value):
        raise ValueError("must be an absolute path")
    return os.path.normpath(value)


def inside(path: str, folder: str) -> bool:
    """Whether ``path`` is ``folder`` or lies inside it, by their text: both absolute and normalized (``_absolute``),
    no file system read. A check that must hold against links compares the real paths (``os.path.realpath``), as
    ``jobs.arrivals``' does for a followed point."""
    return os.path.commonpath((path, folder)) == folder


class _LiveModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class LiveDaq(_LiveModel):
    """The DAQ a ``daq`` run's catalog queries name (the binding, ``jobs.daq.bind_daq``)."""

    id: str = Field(min_length=1)
    """DM's DAQ ID (text)."""
    experiment: str = Field(min_length=1)
    """DM's experiment name."""
    storage: str
    """The experiment's storage directory: the catalog's ``experimentFilePath`` is relative to it."""
    destination: str
    """Where the DAQ writes (its storage directory and ``destDirectory``): the run's folder lies inside it."""

    _paths = field_validator("storage", "destination")(_absolute)

    @model_validator(mode="after")
    def _destination_in_storage(self) -> Self:
        if not inside(self.destination, self.storage):
            raise ValueError("the DAQ's destination must lie inside its experiment's storage directory")
        return self


class LiveFollows(_LiveModel):
    """The live wire run a ``run`` (follower) run indexes the points of."""

    job_id: int = Field(gt=0)
    directory: str

    _directory = field_validator("directory")(_absolute)


class LiveDocument(_LiveModel):
    """``live.json`` (module docstring)."""

    format: Literal["laue-portal-live"] = LIVE_FORMAT
    version: Literal[1] = LIVE_VERSION
    source: LiveSource
    daq: LiveDaq | None = None
    folder: str
    filename_template: str | None = Field(default=None, min_length=1, max_length=TEMPLATE_MAX_LENGTH)
    follows: LiveFollows | None = None

    _folder = field_validator("folder")(_absolute)

    @field_validator("filename_template")
    @classmethod
    def _template(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip() or "/" in value or "\0" in value or value.count("%d") != 1:
            raise ValueError("must be one file name with exactly one %d")
        return value

    @model_validator(mode="after")
    def _source_details(self) -> Self:
        if (self.daq is not None) != (self.source == "daq"):
            raise ValueError("a DAQ is named exactly when the source is a DAQ")
        if (self.follows is not None) != (self.source == "run"):
            raise ValueError("a followed run is named exactly when the source is a run")
        if self.daq is not None and not inside(self.folder, self.daq.destination):
            raise ValueError("the folder must lie inside the DAQ's destination")
        return self


def live_template(templates: Sequence[str]) -> str | None:
    """``live.json``'s ``filename_template`` from a live run's ``filename_prefixes`` (at most one): None with none."""
    return templates[0] if templates else None


def live_path(run_directory: str | os.PathLike[str]) -> str:
    return os.path.join(run_directory, LIVE_FILENAME)


def write_live_document(run_directory: str | os.PathLike[str], document: LiveDocument) -> str:
    """Write ``live.json`` into ``run_directory`` once (``FileExistsError`` when it is there); returns its sha256."""
    return write_json_file(live_path(run_directory), document.model_dump(mode="json"))


def read_live_document(run_directory: str | os.PathLike[str]) -> LiveDocument:
    """``run_directory``'s ``live.json``, checked: a ``ValidationError`` for another format or version, or a key
    missing."""
    with open(live_path(run_directory), encoding="utf-8") as handle:
        text = handle.read()
    document = LiveDocument.model_validate_json(text)
    missing = [name for name in LiveDocument.model_fields if name not in document.model_fields_set]
    if missing:  # every key is written (``write_live_document``), so a default stands for nothing here
        raise ValidationError.from_exception_data(
            LiveDocument.__name__, [{"type": "missing", "loc": (name,), "input": text} for name in missing]
        )
    return document


# ---------------------------------------------------------------------------
# Which runs are live


def live_queue_id(job_id: int) -> str:
    """A live run's queue identity (``job.queue_job_id``): ``live_<its job id>``, its mark as a live run."""
    return f"{LIVE_QUEUE_PREFIX}{int(job_id)}"


def live_marker() -> ColumnElement[bool]:
    """The SQL test of a live run's queue identity: ``queue_job_id = 'live_' || job_id``. Enough alone for Queued and
    Running rows (the module's docstring); a query over ended rows checks ``live.json`` too (``is_live_run``)."""
    return Job.queue_job_id == literal(LIVE_QUEUE_PREFIX) + cast(Job.job_id, String)


def has_live_marker(job: Job) -> bool:
    """Whether ``job``'s queue identity is its live one (``live_marker`` on a loaded row)."""
    return job.queue_job_id == live_queue_id(job.job_id)


def is_live_job(session: Session, job_id: int) -> bool:
    """Whether the Queued or Running job ``job_id`` is a live run: its queue identity (one indexed lookup)."""
    return bool(session.scalar(select(live_marker()).where(Job.job_id == job_id)))


def is_live_run(job: Job) -> bool:
    """Whether ``job`` is a live run: its live queue identity, and, once it has ended, ``live.json`` in its run
    directory (one stat, unbounded: a request reaches it through ``files.probe``; a folder that is gone reads as a
    batch run's)."""
    if not has_live_marker(job):
        return False
    if not state.is_terminal(job):
        return True
    return bool(job.run_directory) and is_file_here(live_path(job.run_directory or ""))


# ---------------------------------------------------------------------------
# One live session at a time


def open_live_runs() -> Select[int, int]:
    """The open live runs (Queued or Running: ``live_marker`` is enough for them), ``(job_id, status)``, oldest
    first."""
    open_statuses = (int(state.JobStatus.QUEUED), int(state.JobStatus.RUNNING))
    return select(Job.job_id, Job.status).where(Job.status.in_(open_statuses), live_marker()).order_by(Job.job_id)


def live_session_open(session: Session) -> bool:
    """Whether a live run is open (Queued or Running): then no batch run or conversion is claimed, and a queued one is
    held (``JobSummary.held``). One indexed query."""
    return session.scalar(open_live_runs().limit(1)) is not None


class LiveSessionOpen(Exception):
    """A new live session refused: the live run ``job_id`` is still Queued or Running (one session at a time)."""

    def __init__(self, job_id: int, status: int) -> None:
        self.job_id = job_id
        self.status = state.JobStatus(status)
        super().__init__(f"Live run job {job_id} is {state.STATUS_NAMES[self.status]}: one live session at a time")


def check_one_live_session(session: Session) -> None:
    """Refuse a new live session while a live run is Queued or Running: ``LiveSessionOpen`` names the oldest such
    run (the module's docstring; the Live page's Start calls it once, before it creates the session's runs).

    It sees only queued live runs (``live_marker``, set when ``jobs.creation.create_live_run`` queues one), never a
    run being created, and it is a read, not a guard: the Live page's Start (``jobs.live_session.start``) holds one
    lock across this check, the session's creations and their queueing, so two Starts close together cannot both
    pass; and it queues a Both session's two runs together once both exist (``jobs.creation.create_live_session``),
    so the wire run is never claimed alone with the whole live share. That lock is one api
    process's: the queueing checks again in its write transaction (``jobs.queue.enqueue_live_runs``), so a second
    session started elsewhere is never queued (its runs end Failed)."""
    row = session.execute(open_live_runs().limit(1)).first()
    if row is not None:
        raise LiveSessionOpen(row.job_id, row.status)
