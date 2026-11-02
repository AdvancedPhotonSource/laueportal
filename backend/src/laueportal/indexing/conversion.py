"""Converting a run's Output XML to ``output.h5`` on the worker: the job kind
``convert_results``.

A conversion is a row of the ``conversion`` table (one per indexing run, replaced by a new conversion), not a run: it
has no ``job`` row, no run directory and no request, so it never appears in the Run Monitor or the job pages and no
run's status or phase changes. ``POST /indexing-runs/{id}/convert`` inserts or replaces the run's row ``queued``
(``enqueue_conversion``), or returns the one queued or running; the worker's dispatcher takes queued conversions
before any queued run, the oldest request first (``jobs.dispatch``), marks the row ``running`` and starts a job
process (``jobs.process convert <id>``), which converts (``run_conversion``) with a timeout of its own
(``CONVERSION_TIMEOUT_SECONDS``, not the runs' ``execution.job_timeout_seconds``) and records ``finished`` or
``failed`` with its message. ``GET …/artifact`` reports the row (``find_conversion``) while the artifact is an XML,
and the page polls that until the artifact is ``ready``.

The job converts one run (``indexing.results.convert_indexing_results``): it writes
``<run>/output.h5`` (validated, published by rename) and then sets ``results_path``. It records the geometry
file the XML names, mapped (``indexing.viewer.conversion_geometry``), and
refuses to let lauelab open a geometry path that is not available for the run. An outcome other than converted,
linked or already converted fails the conversion with its message.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from laueportal.core.db import begin_write, session_scope
from laueportal.core.settings import Settings
from laueportal.core.timeutil import utcnow
from laueportal.indexing import results
from laueportal.indexing.models import Conversion, IndexingRun
from laueportal.indexing.schemas import ConversionJob, ConversionStatus
from laueportal.indexing.viewer import conversion_geometry
from laueportal.jobs.live import live_session_open
from laueportal.jobs.order import CONVERSION_QUEUED
from laueportal.jobs.queue import CONVERT_JOB_TYPE as CONVERT_JOB_TYPE

logger = logging.getLogger(__name__)

CONVERSION_TIMEOUT_SECONDS = 3600
"""How long a conversion may run before its job process stops it (a large historical XML takes minutes)."""

SUCCEEDED = frozenset({results.OUTCOME_CONVERTED, results.OUTCOME_LINKED, results.OUTCOME_ALREADY_CONVERTED})
"""The outcomes after which the run has a validated ``output.h5`` recorded."""

ACTIVE = (ConversionStatus.QUEUED, ConversionStatus.RUNNING)


class ConversionFailed(RuntimeError):
    """A conversion ended with an outcome other than success."""


def conversion_queue_id(indexing_id: int) -> str:
    return f"{CONVERT_JOB_TYPE}_{int(indexing_id)}"


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def conversion_of(row: Conversion, session: Session) -> ConversionJob:
    """The conversion as the API gives it, read in ``session`` (required: a queued one says whether an open live
    session holds it, ``held``)."""
    held = row.status == CONVERSION_QUEUED and live_session_open(session)
    return ConversionJob(
        held=held,
        id=conversion_queue_id(row.indexing_id),
        kind=CONVERT_JOB_TYPE,
        indexing_id=row.indexing_id,
        status=ConversionStatus(row.status),
        message=row.message,
        enqueued_at=_utc(row.requested_at),
        started_at=_utc(row.started_at),
        ended_at=_utc(row.ended_at),
    )


def find_conversion(indexing_id: int, *, sessions: Engine | sessionmaker[Session]) -> ConversionJob | None:
    """The run's conversion, queued, running or ended; None when it has none."""

    with session_scope(sessions) as session:
        row = session.get(Conversion, indexing_id)
        return None if row is None else conversion_of(row, session)


def enqueue_conversion(indexing_id: int, *, sessions: Engine | sessionmaker[Session]) -> ConversionJob:
    """Queue the run's conversion (its row ``queued``), or return the one already queued or running. An ended one is
    replaced. One transaction under the write lock, so two requests queue one conversion."""

    with session_scope(sessions) as session:
        begin_write(session)
        row = session.get(Conversion, indexing_id)
        if row is not None and row.status in ACTIVE:
            return conversion_of(row, session)
        if row is None:
            row = Conversion(indexing_id=indexing_id)
            session.add(row)
        row.status = ConversionStatus.QUEUED.value
        row.requested_at = utcnow()
        row.started_at = row.ended_at = row.message = None
        row.process_host = row.process_pid = row.process_since_boot = None
        session.flush()
        conversion = conversion_of(row, session)
    logger.info("Queued the conversion of I%s", indexing_id)
    return conversion


def convert_run(indexing_id: int, *, engine: Engine, settings: Settings) -> results.ConversionOutcome:
    """Convert one run's Output XML (``results.convert_indexing_results`` for one run) with the geometry its XML names,
    mapped (``conversion_geometry``)."""

    def geometry_for(run: IndexingRun, xml_path: str) -> tuple[str | None, str | None]:
        return conversion_geometry(xml_path, run_directory=run.output_path, settings=settings)

    return results.convert_indexing_results([indexing_id], engine=engine, geometry_for=geometry_for).outcomes[0]


def run_conversion(indexing_id: int, *, engine: Engine, settings: Settings) -> results.ConversionOutcome:
    """The conversion in its job process (``jobs.process``): convert, then record the row ``finished`` or ``failed``
    with its message (``outcome: message``; an error's text when the conversion raised, which is raised again)."""

    status, message = ConversionStatus.FAILED, "The conversion stopped."
    try:
        outcome = convert_run(indexing_id, engine=engine, settings=settings)
        message = f"{outcome.outcome.replace('_', ' ')}: {outcome.message}"
        if outcome.outcome in SUCCEEDED:
            status = ConversionStatus.FINISHED
            logger.info("Converted I%s: %s", indexing_id, message)
        else:
            logger.warning("The conversion of I%s failed: %s", indexing_id, message)
        return outcome
    except BaseException as error:
        message = f"{type(error).__name__}: {error}" if str(error) else type(error).__name__
        raise
    finally:
        _record_end(engine, indexing_id, status, message)


def _record_end(engine: Engine, indexing_id: int, status: ConversionStatus, message: str) -> None:
    with Session(engine) as session, session.begin():
        begin_write(session)
        row = session.get(Conversion, indexing_id)
        if row is None or row.status != ConversionStatus.RUNNING:
            return
        row.status = status.value
        row.message = message
        row.ended_at = utcnow()
