"""The Live page's session: ``GET /api/v1/live/session``, the latest live session.

A session is the runs one Start created (``jobs.live_session``): a live wire run and its follower, or one live run
alone. The latest is found by the newest live run's job row (``jobs.live.live_marker``: its queue identity, which a
session's runs get only once they are all queued, so a refused session is never one): a live LaueGo run whose
reconstruction is a live run is a follower, and its wire run comes first. An ended row is a live run only with
``live.json`` in its run directory (``jobs.live.is_live_run``'s rule: a migrated row's queue identity
may match), looked at once the rows are read: a newest row without it is no session, a wire run without it is
left out of its follower's. A run's counters and stop are its job
row's; its ``arrival-status.json`` is read for the page's arrival status, and the
source run's ``live.json`` and rows give the session's Scan card (the page shows it while the session is open
and fills the next one from it once it has ended). ``stopped`` says that a user stopped the session: its source
run's stop flag (``job.cancel_requested_at``, set only by a user's Stop or Stop now, ``jobs.controls``, and kept once
the run has ended). The answer keeps a stopped session that has ended, since the page fills the next session's form
from it; the page shows no session then (``LiveSession.stopped``) unless a run Failed of its own (a run ended by its
Stop now, ``LiveSessionRun.stopped_now``, does not count, unless a file of it failed), while one that ended without a
Stop stays shown until the next Start. The rows are read in a session closed before any
file is (a hung mount holds no database connection), and each file through ``files.probe`` (``jobs.summary``'s
JSON reader): one that is missing, unreadable or does not answer is a state, never an error.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from laueportal.core.db import session_scope
from laueportal.jobs import state
from laueportal.jobs.live import LiveDocument, has_live_marker, live_marker, live_path
from laueportal.jobs.models import Job
from laueportal.jobs.rundir import ARRIVAL_STATUS_FILENAME, ARRIVALS_FILENAME, LIVE_FILENAME
from laueportal.jobs.schemas import (
    FileState,
    JobLive,
    LiveArrivalStatus,
    LiveSession,
    LiveSessionRun,
    LiveSessionScan,
    LiveSessionState,
    RunReference,
    RunStatus,
)
from laueportal.jobs.summary import FileRead, count_arrivals, read_json_file

_OPEN = (RunStatus.QUEUED, RunStatus.RUNNING)


@dataclass(frozen=True)
class _Row:
    """What the session's view reads of one run's rows, before any file."""

    run: LiveSessionRun
    stopped: bool
    """A user's Stop or Stop now reached the run (its stop flag), whether or not it has ended."""
    run_directory: str | None
    scan_number: int | None
    author: str | None
    output_path: str | None
    """The run's folder (``<output_folder>/rec_<id>`` or ``index_<id>``, the Scan card's output folder in it)."""


def _row(job: Job) -> _Row:
    indexing, reconstruction = job.indexing_run, job.reconstruction_run
    owner = indexing if indexing is not None else reconstruction
    return _Row(
        run=LiveSessionRun(
            job_id=job.job_id,
            run=RunReference.of(
                indexing_id=None if indexing is None else indexing.id,
                reconstruction_id=None if reconstruction is None else reconstruction.id,
            ),
            status=RunStatus.of(job.status),
            n_inputs=job.n_inputs,
            n_succeeded=job.n_succeeded,
            n_failed=job.n_failed,
            stop_requested=job.cancel_requested_at is not None and not state.is_terminal(job),
            stopped_now=job.phase == state.RunPhase.INTERRUPTED and state.stop_now_requested(job.messages),
            arrival=None,
        ),
        stopped=job.cancel_requested_at is not None,
        run_directory=job.run_directory,
        scan_number=None if owner is None else owner.scan_number,
        author=None if owner is None else owner.author,
        output_path=None if owner is None else owner.output_path,
    )


def _session_rows(session: Session) -> list[_Row]:
    """The latest session's runs, the wire run first; ``[]`` before the first live run."""
    newest = session.scalars(select(Job).where(live_marker()).order_by(Job.job_id.desc()).limit(1)).first()
    if newest is None:
        return []
    indexing = newest.indexing_run
    if indexing is not None and indexing.reconstruction_id is not None:
        followed = indexing.reconstruction
        if followed is not None and followed.job is not None and has_live_marker(followed.job):
            return [_row(followed.job), _row(newest)]
    return [_row(newest)]


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _arrival(run_directory: str | None) -> LiveArrivalStatus | None:
    """The run's ``arrival-status.json``: None when it has none (not written, or no run directory)."""
    path = os.path.join(run_directory, ARRIVAL_STATUS_FILENAME) if run_directory else None
    document, read = read_json_file(path, ARRIVAL_STATUS_FILENAME)
    if read.state in (FileState.MISSING, FileState.NOT_RECORDED):
        return None
    values = document or {}
    rows_seen = values.get("rows_seen")
    return LiveArrivalStatus(
        state=read.state,
        checked_at=_text(values.get("checked_at")),
        rows_seen=rows_seen if isinstance(rows_seen, int) and not isinstance(rows_seen, bool) else None,
        error=_text(values.get("error")),
        error_at=_text(values.get("error_at")),
    )


def _live_json(row: _Row) -> tuple[dict[str, Any] | None, FileRead]:
    """The run's ``live.json`` (through the probe) and how reading it went."""
    return read_json_file(live_path(row.run_directory) if row.run_directory else None, LIVE_FILENAME)


def _scan(source: _Row, document: dict[str, Any] | None) -> LiveSessionScan | None:
    """The session's Scan card from its source run (the wire run of a pair): its ``live.json`` and its rows."""
    if document is None:
        return None
    try:
        live = LiveDocument.model_validate(document)
    except ValidationError:
        return None
    if live.source == "run":
        return None
    return LiveSessionScan(
        source=live.source,
        daq_id=None if live.daq is None else live.daq.id,
        folder=live.folder,
        filename_template=live.filename_template,
        output_folder=os.path.dirname(source.output_path) if source.output_path else None,
        scan_number=source.scan_number,
        author=source.author,
    )


def latest_session(sessions: sessionmaker[Session]) -> LiveSessionState:
    """The latest live session (the module's docstring), or ``session`` None before the first live run."""
    with session_scope(sessions) as session:
        rows = _session_rows(session)
    documents: dict[int, tuple[dict[str, Any] | None, FileRead]] = {}

    def live_json(row: _Row) -> tuple[dict[str, Any] | None, FileRead]:
        if row.run.job_id not in documents:
            documents[row.run.job_id] = _live_json(row)
        return documents[row.run.job_id]

    def is_live(row: _Row) -> bool:
        # ``is_live_run`` on the rows read: open, or ended with ``live.json`` there. Only a missing file (or no run
        # directory) says no: one that does not read, or a mount that does not answer, keeps the session shown.
        if row.run.status in _OPEN:
            return True
        return live_json(row)[1].state not in (FileState.MISSING, FileState.NOT_RECORDED)

    if not rows or not is_live(rows[-1]):
        return LiveSessionState(session=None)
    if not is_live(rows[0]):
        rows = rows[-1:]
    runs = [row.run.model_copy(update={"arrival": _arrival(row.run_directory)}) for row in rows]
    return LiveSessionState(
        session=LiveSession(
            open=any(run.status in _OPEN for run in runs),
            stopped=rows[0].stopped,
            runs=runs,
            scan=_scan(rows[0], live_json(rows[0])[0]),
        )
    )


# ---------------------------------------------------------------------------
# The job page's live block


@dataclass(frozen=True)
class LiveRow:
    """What the job page's live block reads of a job's rows (``live_row``), before any file."""

    run_directory: str | None
    open: bool
    """Queued or Running."""
    follows: RunReference | None
    """A LaueGo run's reconstruction (``R<id>``): the live wire run a follower follows."""


def live_row(job: Job) -> LiveRow | None:
    """The job's row as the live block needs it, read while its session is open: None unless the job's queue
    identity is its live one (``jobs.live.has_live_marker``)."""
    if not has_live_marker(job):
        return None
    indexing = job.indexing_run
    follows = None
    if indexing is not None and indexing.reconstruction_id is not None:
        follows = RunReference.of(indexing_id=None, reconstruction_id=indexing.reconstruction_id)
    return LiveRow(job.run_directory, not state.is_terminal(job), follows)


def job_live(row: LiveRow | None) -> JobLive | None:
    """The job page's live block: the source from ``live.json``, the arrivals (the whole lines of
    ``arrivals.jsonl``) and the arrival status (``arrival-status.json``), each file read through the probe (a state,
    never an error). None for a run that is not live: no live queue identity, or an ended row without ``live.json``
    (``jobs.live.is_live_run``'s rule: only a missing file says no, as ``latest_session`` reads it)."""
    if row is None:
        return None
    directory = row.run_directory
    document, read = read_json_file(live_path(directory) if directory else None, LIVE_FILENAME)
    if not row.open and read.state in (FileState.MISSING, FileState.NOT_RECORDED):
        return None
    live = None
    if document is not None:
        try:
            live = LiveDocument.model_validate(document)
        except ValidationError:
            read = FileRead(LIVE_FILENAME, read.path, FileState.UNREADABLE, "not a live.json")
    arrivals, counted = count_arrivals(
        os.path.join(directory, ARRIVALS_FILENAME) if directory else None, ARRIVALS_FILENAME
    )
    return JobLive(
        source=None if live is None else live.source,
        source_state=read.state,
        daq_id=None if live is None or live.daq is None else live.daq.id,
        folder=None if live is None else live.folder,
        filename_template=None if live is None else live.filename_template,
        follows=row.follows if live is not None and live.source == "run" else None,
        follows_job_id=None if live is None or live.follows is None else live.follows.job_id,
        arrivals=arrivals,
        arrivals_state=counted.state,
        arrival=_arrival(directory),
    )
