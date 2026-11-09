"""Create and submit one run (``POST /api/v1/indexing-runs``, ``POST /api/v1/reconstruction-runs``).

One parameter object per request (no batch rows, pooled inputs or partial batch results), under an
``Idempotency-Key`` (``jobs.idempotency``). :func:`submit`:

1. claims the key (in the database, ``idempotency_key``): a kept answer is answered again as it was (nothing is
   checked or created again);
2. validates the form with the program's service (``indexing.validation.validate_indexing``,
   ``reconstruction.validation.validate_reconstruction``): an error is a 422 problem with every issue (errors
   first), and nothing is created; warnings do not stop the API (the form has the user confirm them);
3. creates the run with ``settings=`` (``indexing.service.create_indexing``, ``reconstruction.service.
   create_reconstruction``: run creation's own refusals, ``jobs.validation.check_creatable``, are a 422 too, with
   nothing created), which publishes its inputs (``jobs.records.publish_run_inputs``): once its rows are saved, any
   failure is a ``RunPublicationError`` for that run, which is Failed (``jobs.records.publishing``);
4. queues it on its target (``jobs.queue.enqueue_run``: a ``local`` run's phase ``queued`` committed, the row
   its queue entry, which the worker's dispatcher claims; ``aps`` and ``crux`` through DM, with
   ``remote.settings.portal_settings``);
5. answers the run and its job (``CreatedRun``, 201) and keeps the answer with the key.

A run that was created answers 201 whatever happened next: a publication or enqueue failure is reported for that
run (``CreatedRun.failure``). A run that never reached a queue is marked Failed with the reason
(``jobs.state.record_enqueue_failure``), so no run is left waiting that nothing will run; one whose DM reply was
lost stays queued for the remote poller (``remote.submission``).

The key is released only by a request known to have created nothing: whatever the validation raised (it only
reads) and run creation's own refusals (made before it saves anything). Any other failure of run creation keeps the
key claimed, so a retry is the key's 409, never a second run (``jobs.idempotency``).

A live run is created by :func:`create_live_run` from its validated live parameters and its
``live.json``, and queued as ``live_<job id>`` (``jobs.queue.enqueue_live_run``). A live session, the one or two runs
of the Live page's Start (``jobs.live_session``, which holds its key and its lock), is created by
:func:`create_live_session`: every run first, then all queued together (``jobs.queue.enqueue_live_runs``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from laueportal.core.params import ProgramParams
from laueportal.core.problems import MAX_PROBLEM_ITEMS, ProblemError, problems_detail
from laueportal.core.settings import Settings
from laueportal.files.inputs import WorkflowValidationError
from laueportal.indexing.lauego.live_params import LaueGoLiveParamsV1
from laueportal.indexing.models import IndexingRun
from laueportal.indexing.service import create_indexing, create_laue_matching, create_live_indexing
from laueportal.indexing.validation import validate_indexing, validate_laue_matching
from laueportal.jobs import state
from laueportal.jobs.backends import LOCAL, is_remote
from laueportal.jobs.idempotency import Claim, IdempotencyStore
from laueportal.jobs.live import LiveDocument, LiveFollows
from laueportal.jobs.models import Job
from laueportal.jobs.programs import ProgramKind
from laueportal.jobs.queue import enqueue_live_run, enqueue_live_runs, enqueue_run
from laueportal.jobs.records import RunPublicationError
from laueportal.jobs.rundir import reconstruction_catalog_path
from laueportal.jobs.schemas import CreatedRun, RunKind, RunReference, RunStatus
from laueportal.jobs.state import RunPhase
from laueportal.jobs.validation import Validated, issue_count
from laueportal.reconstruction.models import ReconstructionRun
from laueportal.reconstruction.service import create_live_reconstruction, create_reconstruction
from laueportal.reconstruction.validation import validate_reconstruction
from laueportal.reconstruction.wire.live_params import WireLiveParamsV1
from laueportal.remote.settings import portal_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Program:
    route: str
    """The create endpoint's operation ID (the idempotency keys' route)."""
    kind: RunKind
    label: str
    model: type[IndexingRun] | type[ReconstructionRun]
    validate: Callable[..., Validated]
    create: Callable[..., IndexingRun | ReconstructionRun]


PROGRAMS: dict[ProgramKind, _Program] = {
    "lauego": _Program("createIndexingRun", RunKind.INDEXING, "I", IndexingRun, validate_indexing, create_indexing),
    "laue_matching": _Program(
        "createLaueMatchingRun", RunKind.INDEXING, "I", IndexingRun, validate_laue_matching, create_laue_matching
    ),
    "wire": _Program(
        "createReconstructionRun",
        RunKind.RECONSTRUCTION,
        "R",
        ReconstructionRun,
        validate_reconstruction,
        create_reconstruction,
    ),
}


def submit(
    kind: ProgramKind,
    form: Mapping[str, Any],
    *,
    key: str,
    settings: Settings,
    engine: Engine,
) -> CreatedRun:
    """Create and enqueue one ``kind`` run from ``form`` (the create form's values) under the ``Idempotency-Key``
    ``key``; see the module (``engine``'s database keeps the keys)."""

    program = PROGRAMS[kind]
    store = IdempotencyStore(engine, route=program.route)
    claim = store.claim(key, form)
    if claim.replay is not None:
        return CreatedRun.model_validate(claim.replay)
    try:  # the validation only reads: whatever it raises, nothing was created
        params = _validated(program, form, settings=settings, engine=engine)
    except BaseException:
        _release(store, claim)
        raise
    try:
        run_id, job_id, failure = _create(program, params, settings=settings, engine=engine)
    except WorkflowValidationError as error:  # run creation's own refusals, made before it saves anything
        _release(store, claim)
        message = str(error)
        raise ProblemError(
            422, "Run not created", message, errors=[{"loc": [], "code": "not_creatable", "message": message}]
        ) from error
    # Any other failure of run creation keeps the key claimed: its run may be saved, and a retry must not make a
    # second one (the key's 409).
    queue_id = None
    if failure is None:
        queue_id, failure = _enqueue(program, run_id, job_id, settings=settings, engine=engine)
    answer = _answer(program, run_id, job_id, queue_id=queue_id, failure=failure, engine=engine)
    try:
        store.keep(claim, answer.model_dump(mode="json"))
    except SQLAlchemyError:
        logger.warning(
            "The answer for %s%s could not be kept with its Idempotency-Key", program.label, run_id, exc_info=True
        )
    return answer


def _release(store: IdempotencyStore, claim: Claim) -> None:
    try:
        store.release(claim)
    except SQLAlchemyError:
        logger.warning("An Idempotency-Key could not be released; it stays claimed for 24 h", exc_info=True)


def _validated(program: _Program, form: Mapping[str, Any], *, settings: Settings, engine: Engine) -> ProgramParams:
    """The run's parameters, or a 422 with the issues, errors first: the first ``MAX_PROBLEM_ITEMS``, and a
    ``detail`` that counts every issue the validation found (``issue_count``), not only those it answered."""

    validated = program.validate(form, settings=settings, engine=engine)
    if not validated.ok or validated.params is None:
        count = issue_count(validated.issues)
        issues = sorted(validated.issues, key=lambda issue: issue.severity != "error")[:MAX_PROBLEM_ITEMS]
        detail = problems_detail(count)
        if count > len(issues):
            detail = f"{detail} (the first {len(issues)} of {count} are listed)"
        raise ProblemError(422, "Validation failed", detail, errors=issues)
    return validated.params


def _create(
    program: _Program, params: ProgramParams, *, settings: Settings, engine: Engine
) -> tuple[int, int, str | None]:
    """Create the run: its ID, its job's, and why its publication failed (``None`` when it did not). Any failure
    but a ``RunPublicationError`` is raised as it is (a ``WorkflowValidationError`` is run creation's own refusal,
    made before it saves anything)."""

    try:
        run = program.create(params, engine=engine, settings=settings)
    except RunPublicationError as error:  # saved, then not published: the run is Failed with the reason
        logger.warning("%s", error)
        with Session(engine) as session:
            run_id = session.scalar(select(program.model.id).where(program.model.job_id == error.job_id))
        if run_id is None:  # its rows are saved together, so never: the key stays claimed
            raise RuntimeError(f"Job {error.job_id} has no {program.kind.value} run") from error
        return run_id, error.job_id, str(error)
    return run.id, run.job_id, None


def _reason(error: BaseException) -> str:
    if isinstance(error, ProblemError):
        return error.problem.detail or error.problem.title
    return f"{error}" or type(error).__name__


def _enqueue(
    program: _Program,
    run_id: int,
    job_id: int,
    *,
    settings: Settings,
    engine: Engine,
) -> tuple[str | None, str | None]:
    """Hand the published run to its target: its queue ID (``run_<id>``, or DM's job ID), or why it could not be
    queued."""

    with Session(engine) as session:
        backend = session.scalar(select(Job.backend).where(Job.job_id == job_id))
    try:
        remote = portal_settings(settings.remote, runs_root=settings.files.runs_root) if is_remote(backend) else None
        queue_id = enqueue_run(job_id, engine=engine, remote=remote)
    except Exception as error:
        logger.exception("%s%s could not be queued on %s", program.label, run_id, backend)
        _fail_unqueued(job_id, error, engine=engine)
        return None, f"{program.label}{run_id} could not be queued: {_reason(error)}"
    return queue_id, None


def _fail_unqueued(job_id: int, error: BaseException | str, *, engine: Engine) -> None:
    """Mark a run that never reached a queue Failed (``jobs.state.record_enqueue_failure``): nothing would run it.
    One that did (a DM submission whose reply was lost) is left as
    it is."""

    with Session(engine) as session:
        phase = session.scalar(select(Job.phase).where(Job.job_id == job_id))
    if phase == RunPhase.PUBLISHED:
        state.record_enqueue_failure(job_id, error if isinstance(error, str) else _reason(error), engine=engine)


def _answer(
    program: _Program, run_id: int, job_id: int, *, queue_id: str | None, failure: str | None, engine: Engine
) -> CreatedRun:
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if job is None:  # nothing deletes jobs
            raise RuntimeError(f"Job {job_id} of {program.label}{run_id} vanished")
        return CreatedRun(
            run=RunReference(kind=program.kind, id=run_id, label=f"{program.label}{run_id}"),
            job_id=job_id,
            status=RunStatus.of(job.status),
            target=job.backend or LOCAL,
            queue_id=queue_id,
            failure=failure,
        )


def create_live_run(
    params: WireLiveParamsV1 | LaueGoLiveParamsV1, live: LiveDocument, *, settings: Settings, engine: Engine
) -> CreatedRun:
    """Create one live run from its validated live parameters (a wire run, or a LaueGo run on raw frames or following
    a wire run) and its ``live.json``, publish its files and queue it as ``live_<job id>`` (the module's docstring).

    Run creation's own refusals raise ``WorkflowValidationError`` with nothing created. Once the run exists, the
    answer is its ``CreatedRun``, whatever happened next: a publication or queueing failure is its ``failure``, the
    run Failed (as ``submit``'s).

    It queues the run it created at once: for one run alone (the tests', a script's). The Live page's Start creates
    its session with :func:`create_live_session` instead, under two rules:
    - **One lock across a Start.** ``jobs.live.check_one_live_session`` sees only live runs already queued (its
      ``live_marker`` is set by the queueing), so a run being created is invisible to it: Start holds one lock (the
      api is one process; ``jobs.live_session``) across the check, the creations and the queueing, or two Starts close
      together both pass the check.
    - **A Both session's two runs queued together.** The dispatcher gives a live run claimed alone the whole live
      share (``dispatch.live_cores``: "both runs" is another live run open at the claim), so a wire run queued before
      its follower exists could be claimed alone and keep the whole share: ``create_live_session`` creates both, then
      queues both in one transaction."""

    program, run_id, job_id, failure = _create_live(params, live, settings=settings, engine=engine)
    queue_id = None
    if failure is None:
        try:
            queue_id = enqueue_live_run(job_id, engine=engine)
        except Exception as error:
            logger.exception("%s%s could not be queued", program.label, run_id)
            _fail_unqueued(job_id, error, engine=engine)
            failure = f"{program.label}{run_id} could not be queued: {_reason(error)}"
    return _answer(program, run_id, job_id, queue_id=queue_id, failure=failure, engine=engine)


def _create_live(
    params: WireLiveParamsV1 | LaueGoLiveParamsV1, live: LiveDocument, *, settings: Settings, engine: Engine
) -> tuple[_Program, int, int, str | None]:
    """Create and publish one live run, not queued: its program, its run's ID, its job's, and why its publication
    failed (``None`` when it did not; the run is then Failed). Run creation's own refusals raise
    ``WorkflowValidationError`` with nothing created."""

    program = PROGRAMS["wire" if isinstance(params, WireLiveParamsV1) else "lauego"]
    try:
        if isinstance(params, WireLiveParamsV1):
            run = create_live_reconstruction(params, live, engine=engine, settings=settings)
        else:
            run = create_live_indexing(params, live, engine=engine, settings=settings)
        return program, run.id, run.job_id, None
    except RunPublicationError as error:
        logger.warning("%s", error)
        with Session(engine) as session:
            found = session.scalar(select(program.model.id).where(program.model.job_id == error.job_id))
        if found is None:  # its rows are saved together, so never
            raise RuntimeError(f"Job {error.job_id} has no {program.kind.value} run") from error
        return program, found, error.job_id, str(error)


@dataclass(frozen=True)
class LiveSessionPlan:
    """A live session's runs as the Live page's validation built them (``jobs.live_session``): the wire run and its
    ``live.json`` (Reconstruction, Both), and the LaueGo run (Indexing, Both) with its ``live.json`` on raw frames;
    in a Both session (``follower``) its ``input_path`` and ``reconstruction_id`` are the wire run's, set once that
    run exists, and its ``live.json`` follows it."""

    wire: tuple[WireLiveParamsV1, LiveDocument] | None
    indexing: tuple[LaueGoLiveParamsV1, LiveDocument | None] | None

    def __post_init__(self) -> None:
        if self.wire is None and self.indexing is None:
            raise ValueError("A live session has at least one run")
        if self.indexing is not None and (self.indexing[1] is None) != (self.wire is not None):
            raise ValueError("A LaueGo run has its own live.json exactly when it has no wire run to follow")


class LiveSessionNotCreated(Exception):
    """A live session's first run was refused by run creation (``WorkflowValidationError``): nothing was created."""


def create_live_session(plan: LiveSessionPlan, *, settings: Settings, engine: Engine) -> list[CreatedRun]:
    """Create a live session's runs (the wire run first, then its follower; or the one run), then queue them together
    (``jobs.queue.enqueue_live_runs``): the second rule of :func:`create_live_run`'s docstring. The caller holds
    Start's lock across its one-session check and this call (the first rule).

    All or nothing: when a run cannot be created or published, none is queued, and each run created before it that
    was not is Failed with the reason (``jobs.state.record_enqueue_failure``; also before an unexpected error is
    raised); a refusal of the first run's creation is ``LiveSessionNotCreated``, with nothing created. Once a run exists the answer lists every run created, each a
    ``CreatedRun`` with its ``failure``."""

    created: list[tuple[_Program, int, int, str | None]] = []
    try:
        if plan.wire is not None:
            created.append(_create_live(*plan.wire, settings=settings, engine=engine))
        if plan.indexing is not None and not any(failure for *_, failure in created):
            params, live = plan.indexing
            if live is None:  # a follower of the session's wire run: its catalog and its id
                _, wire_id, wire_job, _ = created[0]
                with Session(engine) as session:
                    directory = session.scalar(select(Job.run_directory).where(Job.job_id == wire_job))
                wire_live = plan.wire[1] if plan.wire is not None else None
                assert directory and wire_live is not None  # a follower's plan has a wire run (LiveSessionPlan)
                params = params.with_values(
                    input_path=reconstruction_catalog_path(directory), reconstruction_id=wire_id
                )
                live = LiveDocument(
                    source="run",
                    folder=wire_live.folder,
                    filename_template=wire_live.filename_template,
                    follows=LiveFollows(job_id=wire_job, directory=directory),
                )
            created.append(_create_live(params, live, settings=settings, engine=engine))
    except WorkflowValidationError as error:
        if not created:
            raise LiveSessionNotCreated(str(error)) from error
        reason = f"the session's other run was not created: {error}"
        return _unqueued(created, reason, engine=engine)
    except BaseException:  # unexpected: the caller keeps its key claimed; leave no run of the session waiting
        if created:
            try:
                _unqueued(created, "the session's creation failed", engine=engine)
            except Exception:
                logger.exception("The live session's runs %s could not be marked Failed", created)
        raise
    failed = next((failure for *_, failure in created if failure), None)
    if failed is not None:
        return _unqueued(created, f"the session's other run failed: {failed}", engine=engine)
    job_ids = [job_id for _, _, job_id, _ in created]
    try:
        queue_ids: list[str | None] = list(enqueue_live_runs(job_ids, engine=engine))
    except Exception as error:
        logger.exception("Live session %s could not be queued", job_ids)
        return _unqueued(created, f"could not be queued: {_reason(error)}", engine=engine)
    return [
        _answer(program, run_id, job_id, queue_id=queue_id, failure=None, engine=engine)
        for (program, run_id, job_id, _), queue_id in zip(created, queue_ids, strict=True)
    ]


def _unqueued(created: list[tuple[_Program, int, int, str | None]], reason: str, *, engine: Engine) -> list[CreatedRun]:
    """The answers of a session none of whose runs was queued: each run not Failed already is (its ``failure``,
    ``reason``)."""
    answers = []
    for program, run_id, job_id, failure in created:
        if failure is None:
            failure = f"{program.label}{run_id} was not queued: {reason}"
            _fail_unqueued(job_id, failure, engine=engine)
        answers.append(_answer(program, run_id, job_id, queue_id=None, failure=failure, engine=engine))
    return answers
