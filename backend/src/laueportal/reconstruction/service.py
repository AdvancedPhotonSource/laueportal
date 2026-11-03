"""Reconstruction services: wire run creation and retrieval, and the reconstructions grid.

A wire run's source is its ``scan_number`` (the create form's picker).
"""

from typing import Any, Unpack

from sqlalchemy import Engine, Select, select
from sqlalchemy.orm import Session, joinedload

from laueportal.core.paging import GridField, GridSpec, fetch_page
from laueportal.core.settings import Settings
from laueportal.files.inputs import ProgressCallback, WorkflowValidationError, format_output_path, resolve_inputs
from laueportal.jobs.live import LiveDocument, live_template
from laueportal.jobs.models import Job
from laueportal.jobs.progress import RUN_PROGRESS_FIELDS, active_or_changed_since, grid_progress_columns
from laueportal.jobs.records import new_job, publish_live_run, publish_run_inputs, publishing
from laueportal.jobs.rundir import request_document
from laueportal.jobs.schemas import RunGridParams
from laueportal.jobs.validation import check_creatable, check_paths_creatable
from laueportal.reconstruction.models import ReconstructionRun, WireReconstructionParameters
from laueportal.reconstruction.schemas import ReconstructionRunPage, ReconstructionRunSummary
from laueportal.reconstruction.wire.live_params import WireLiveParamsV1
from laueportal.reconstruction.wire.params import WireParamsV1
from laueportal.scans.models import Catalog

RECONSTRUCTION_RUN_GRID = GridSpec(
    fields={
        "id": GridField("number"),
        "method": GridField("text"),
        "scan_number": GridField("number"),
        "sample_name": GridField("text"),
        "n_points": GridField("number"),
        "percent_brightest": GridField("number"),
        "depth_start": GridField("number"),
        "depth_end": GridField("number"),
        "author": GridField("text"),
        "notes": GridField("text"),
        "created_at": GridField("date"),
        "start_time": GridField("date"),
        "finish_time": GridField("date"),
        **RUN_PROGRESS_FIELDS,
    },
    key="id",
    default_sort="-id",
    changed_since=active_or_changed_since,
)
"""The reconstructions grid: every field sorts and filters (stored columns, the status and the counters; the status
filters by its name and sorts by its code, the lifecycle order); newest first. The source is the
stored ``scan_number`` (blank: "Unlinked"), with its catalog's ``sample_name`` (the create form's pickers). Live: the active runs and those changed since
(``jobs.progress.active_or_changed_since``)."""


def create_reconstruction(
    request: WireParamsV1,
    *,
    engine: Engine,
    settings: Settings | None = None,
    progress_callback: ProgressCallback | None = None,
) -> ReconstructionRun:
    """Resolve files, create the run and its job atomically, then publish the manifest. Run creation's own
    refusals come first (``jobs.validation.check_creatable``, with ``settings`` when the caller has them), before
    anything is saved; once the rows are saved, any failure is a ``RunPublicationError`` naming the run's job, which
    is Failed (``jobs.records.publishing``)."""

    check_creatable(request, settings=settings)
    inputs = resolve_inputs(
        request.input_path,
        request.filename_prefixes,
        request.scan_point_values,
        append_suffix_wildcard=True,
        progress_callback=progress_callback,
    )

    with Session(engine, expire_on_commit=False) as session:
        with session.begin():
            job = new_job(
                computer_name=request.computer_name,
                backend=request.backend,
                priority=request.priority,
                submitted_at=request.submitted_at,
                n_inputs=len(inputs),
            )
            session.add(job)
            session.flush()

            run = ReconstructionRun(
                scan_number=request.scan_number,
                job=job,
                method="wire",
                input_path=request.input_path,
                output_path=None,
                author=request.author,
                notes=request.notes,
                algorithm_version=request.algorithm_version,
                created_at=request.submitted_at,
            )
            session.add(run)
            session.flush()
            run_directory = format_output_path(request.output_path_template, run.id)
            run.output_path = run_directory
            job.run_directory = run_directory
            run.wire_parameters = _wire_parameters(request, scan_points_len=request.scan_point_count)
        run_id = run.id
        job_id = job.job_id

    with publishing(engine, job_id=job_id, display_id=f"Reconstruction R{run_id}", now=request.submitted_at):
        # saved: from here on, any failure is a RunPublicationError for this run, which is Failed
        payload = {
            "kind": "wire_reconstruction",
            "run": {
                "reconstruction_id": run_id,
                "job_id": job_id,
                "scan_number": request.scan_number,
                "method": "wire",
            },
            "request": request_document(request),
            "output": {"directory": run_directory},
        }
        publish_run_inputs(
            engine,
            job_id=job_id,
            display_id=f"Reconstruction R{run_id}",
            run_directory=run_directory,
            inputs=inputs,
            request_payload=payload,
            now=request.submitted_at,
        )
        created = get_reconstruction(run_id, engine=engine)
        if created is None:  # deleted concurrently; nothing deletes runs today
            raise RuntimeError(f"Reconstruction R{run_id} vanished after it was created")
    return created


def create_live_reconstruction(
    request: WireLiveParamsV1, live: LiveDocument, *, engine: Engine, settings: Settings | None = None
) -> ReconstructionRun:
    """Create a live wire reconstruction (``wire-live/v1``) and its job, then publish its files
    (``jobs.records.publish_live_run``: ``request.json`` with ``wire/v1``'s keys, an empty ``inputs.jsonl``,
    ``live.json``, the arrivals' lock and ledger), 0 inputs. Its folder and template must be ``live``'s (the files
    it takes are the ones that arrive there). The refusals and the failures are ``create_reconstruction``'s; queue
    it with ``jobs.queue.enqueue_live_run``."""

    check_paths_creatable(request, settings=settings)
    if (
        live.source == "run"
        or live.folder != request.input_path
        or live.filename_template != live_template(request.filename_prefixes)
    ):
        raise WorkflowValidationError("A live reconstruction takes the files of its live.json's folder and file name")

    with Session(engine, expire_on_commit=False) as session:
        with session.begin():
            job = new_job(
                computer_name=request.computer_name,
                backend=request.backend,
                priority=request.priority,
                submitted_at=request.submitted_at,
                n_inputs=0,
            )
            session.add(job)
            session.flush()
            run = ReconstructionRun(
                scan_number=request.scan_number,
                job=job,
                method="wire",
                input_path=request.input_path,
                output_path=None,
                author=request.author,
                notes=request.notes,
                algorithm_version=request.algorithm_version,
                created_at=request.submitted_at,
            )
            session.add(run)
            session.flush()
            run_directory = format_output_path(request.output_path_template, run.id)
            run.output_path = run_directory
            job.run_directory = run_directory
            run.wire_parameters = _wire_parameters(request, scan_points_len=0)
        run_id = run.id
        job_id = job.job_id

    with publishing(engine, job_id=job_id, display_id=f"Reconstruction R{run_id}", now=request.submitted_at):
        payload = {
            "kind": "wire_reconstruction",
            "run": {
                "reconstruction_id": run_id,
                "job_id": job_id,
                "scan_number": request.scan_number,
                "method": "wire",
            },
            "request": request_document(request),
            "output": {"directory": run_directory},
        }
        publish_live_run(
            engine,
            job_id=job_id,
            display_id=f"Reconstruction R{run_id}",
            run_directory=run_directory,
            request_payload=payload,
            live=live,
            now=request.submitted_at,
        )
        created = get_reconstruction(run_id, engine=engine)
        if created is None:  # nothing deletes runs
            raise RuntimeError(f"Reconstruction R{run_id} vanished after it was created")
    return created


def _wire_parameters(
    request: WireParamsV1 | WireLiveParamsV1, *, scan_points_len: int | None
) -> WireReconstructionParameters:
    """The run's parameter row (a live run's scan points are empty until it ends: ``jobs.executor``)."""
    return WireReconstructionParameters(
        filename_prefixes=list(request.filename_prefixes),
        geometry_file=request.geometry_file,
        percent_brightest=request.percent_brightest,
        wire_edges=request.wire_edges,
        depth_start=request.depth_start,
        depth_end=request.depth_end,
        depth_resolution=request.depth_resolution,
        frame_skip=request.frame_skip,
        wire_skip=request.wire_skip,
        num_threads=request.num_threads,
        memory_limit_mb=request.memory_limit_mb,
        scan_points=request.scan_points,
        scan_points_len=scan_points_len,
        verbose=request.verbose,
    )


def get_reconstruction(reconstruction_id: int, *, engine: Engine) -> ReconstructionRun | None:
    """Load a reconstruction with its parameters and job."""

    statement = (
        select(ReconstructionRun)
        .where(ReconstructionRun.id == reconstruction_id)
        .options(
            joinedload(ReconstructionRun.wire_parameters),
            joinedload(ReconstructionRun.job),
        )
    )
    with Session(engine) as session:
        return session.scalars(statement).one_or_none()


# ---------------------------------------------------------------------------
# The reconstructions grid


def reconstruction_run_list_query(scan_id: int | None = None) -> Select[Unpack[tuple[Any, ...]]]:
    """The reconstructions grid's select: one row per run, with its scan's sample, its wire parameters' columns and its
    job's times, status and counters; only the runs of scan ``scan_id`` when given."""

    query = (
        select(
            ReconstructionRun.id.label("id"),
            ReconstructionRun.method.label("method"),
            ReconstructionRun.scan_number.label("scan_number"),
            Catalog.sample_name.label("sample_name"),
            WireReconstructionParameters.scan_points_len.label("n_points"),
            WireReconstructionParameters.percent_brightest.label("percent_brightest"),
            WireReconstructionParameters.depth_start.label("depth_start"),
            WireReconstructionParameters.depth_end.label("depth_end"),
            ReconstructionRun.author.label("author"),
            ReconstructionRun.notes.label("notes"),
            ReconstructionRun.created_at.label("created_at"),
            Job.start_time.label("start_time"),
            Job.finish_time.label("finish_time"),
            *grid_progress_columns(),
        )
        .select_from(ReconstructionRun)
        .join(Job, Job.job_id == ReconstructionRun.job_id)
        .outerjoin(WireReconstructionParameters, WireReconstructionParameters.reconstruction_id == ReconstructionRun.id)
        .outerjoin(Catalog, Catalog.scan_number == ReconstructionRun.scan_number)  # one catalog per scan at most
    )
    if scan_id is not None:
        query = query.where(ReconstructionRun.scan_number == scan_id)
    return query


def list_reconstruction_runs(session: Session, params: RunGridParams) -> ReconstructionRunPage:
    """One page of the reconstructions grid (or of a scan's reconstructions): two queries."""

    page = fetch_page(session, reconstruction_run_list_query(params.scan_id), RECONSTRUCTION_RUN_GRID, params)
    rows = [ReconstructionRunSummary.model_validate(dict(row)) for row in page.rows]
    return ReconstructionRunPage(rows=rows, total=page.total, as_of=page.as_of)
