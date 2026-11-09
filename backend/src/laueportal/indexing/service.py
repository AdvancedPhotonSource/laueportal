"""Indexing services: LaueGo and LaueMatching run creation and retrieval, and the indexations grid. A run's
source is its ``scan_number`` and ``reconstruction_id`` (the create form's pickers)."""

from typing import Any, Unpack

from sqlalchemy import Engine, Select, case, func, select
from sqlalchemy.orm import Session, joinedload

from laueportal.core.paging import GridField, GridSpec, fetch_page
from laueportal.core.settings import Settings
from laueportal.files.inputs import (
    ProgressCallback,
    WorkflowValidationError,
    format_output_path,
    resolve_request_inputs,
)
from laueportal.indexing.laue_matching.data import with_instrument
from laueportal.indexing.laue_matching.params import LaueMatchingParamsV1
from laueportal.indexing.lauego.live_params import LaueGoLiveParamsV1
from laueportal.indexing.lauego.params import DEFAULT_OUTPUT_XML, LaueGoParamsV1
from laueportal.indexing.models import IndexingRun, LaueGoIndexingParameters, LaueMatchingIndexingParameters
from laueportal.indexing.schemas import IndexingRunPage, IndexingRunSummary
from laueportal.jobs.live import LiveDocument, live_template
from laueportal.jobs.models import Job
from laueportal.jobs.progress import RUN_PROGRESS_FIELDS, active_or_changed_since, grid_progress_columns
from laueportal.jobs.records import new_job, publish_live_run, publish_run_inputs, publishing
from laueportal.jobs.rundir import RESULTS_FILENAME, request_document
from laueportal.jobs.schemas import RunGridParams
from laueportal.jobs.validation import check_creatable, check_paths_creatable
from laueportal.reconstruction.models import ReconstructionRun
from laueportal.scans.models import Catalog

INDEXING_RUN_GRID = GridSpec(
    fields={
        "id": GridField("number"),
        "method": GridField("text"),
        "method_label": GridField("text"),
        "scan_number": GridField("number"),
        "sample_name": GridField("text"),
        "reconstruction_id": GridField("number"),
        "n_points": GridField("number"),
        "n_depths": GridField("number"),
        "box_size": GridField("number"),
        "crystal_file": GridField("text"),
        "author": GridField("text"),
        "notes": GridField("text"),
        "created_at": GridField("date"),
        **RUN_PROGRESS_FIELDS,
    },
    key="id",
    default_sort="-id",
    changed_since=active_or_changed_since,
)
"""The indexations grid: every field sorts and filters (stored columns,
the status, by name and by code as on every run list, and the counters); newest first. The source is
the stored ``scan_number`` and
``reconstruction_id`` (both blank: "Unlinked"), with the scan's catalog ``sample_name`` (the create form's pickers).
Live: the active runs and those changed since
(``jobs.progress.active_or_changed_since``)."""


def create_laue_matching(
    request: LaueMatchingParamsV1,
    *,
    engine: Engine,
    settings: Settings | None = None,
    progress_callback: ProgressCallback | None = None,
) -> IndexingRun:
    """Create a LaueMatching run as :func:`create_indexing` creates a LaueGo run. The crystal and detector values are
    read from the run's files first, and frozen in its ``request.json``."""

    if settings is None:
        raise ValueError("A LaueMatching run needs the settings (laue_matching.data_root)")
    check_creatable(request, settings=settings)
    return _create_indexing(
        with_instrument(request, settings), engine=engine, settings=settings, progress_callback=progress_callback
    )


def create_indexing(
    request: LaueGoParamsV1,
    *,
    engine: Engine,
    settings: Settings | None = None,
    progress_callback: ProgressCallback | None = None,
) -> IndexingRun:
    """Resolve inputs, create the run and its job atomically, then publish the manifest.

    Inputs can come from a directory of frame files or a reconstruction
    catalog. Catalog selections become point-file references in ``inputs.jsonl``.
    The request is saved in ``request.json``. The job records the file locations,
    manifest digest, and input count, without creating a database row per input.
    Run creation's own refusals come first (``jobs.validation.check_creatable``,
    with ``settings`` when the caller has them), before anything is saved; once
    the rows are saved, any failure is a ``RunPublicationError`` naming the run's
    job, which is Failed (``jobs.records.publishing``).
    """

    # Filtering is disabled for new runs, including reruns of historical settings.
    request = request.with_values(cosmic_filter=False)
    check_creatable(request, settings=settings)
    return _create_indexing(request, engine=engine, settings=settings, progress_callback=progress_callback)


def _create_indexing(
    request: LaueGoParamsV1 | LaueMatchingParamsV1,
    *,
    engine: Engine,
    settings: Settings | None,
    progress_callback: ProgressCallback | None,
) -> IndexingRun:
    """Resolve the inputs, save the run and its job, then publish the run's files (``create_indexing``'s docstring)."""

    matching = isinstance(request, LaueMatchingParamsV1)
    method = "laue_matching" if matching else "lauego"
    inputs = resolve_request_inputs(
        request.input_path,
        request.filename_prefixes,
        request.scan_point_values,
        depth_values=request.depth_values,
        progress_callback=progress_callback,
    )

    with Session(engine, expire_on_commit=False) as session:
        with session.begin():
            parent = None
            if request.reconstruction_id is not None:
                parent = session.get(ReconstructionRun, request.reconstruction_id)
                if parent is None:
                    raise WorkflowValidationError(f"Reconstruction R{request.reconstruction_id} does not exist")
                if (
                    request.scan_number is not None
                    and parent.scan_number is not None
                    and request.scan_number != parent.scan_number
                ):
                    raise WorkflowValidationError(
                        f"Indexing scan {request.scan_number} does not match reconstruction scan {parent.scan_number}"
                    )

            if request.scan_number is None and parent is not None and parent.scan_number is not None:
                request = request.with_values(scan_number=parent.scan_number)  # the reconstruction's scan
            scan_number = request.scan_number

            job = new_job(
                computer_name=request.computer_name,
                backend=request.backend,
                priority=request.priority,
                submitted_at=request.submitted_at,
                n_inputs=len(inputs),
            )
            session.add(job)
            session.flush()

            run = IndexingRun(
                scan_number=scan_number,
                reconstruction=parent,
                job=job,
                method=method,
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
            if isinstance(request, LaueMatchingParamsV1):
                run.laue_matching_parameters = _laue_matching_parameters(request)
            else:
                run.lauego_parameters = _lauego_parameters(request, scan_points_len=request.scan_point_count)
        run_id = run.id
        job_id = job.job_id

    with publishing(engine, job_id=job_id, display_id=f"Indexing I{run_id}", now=request.submitted_at):
        # saved: from here on, any failure is a RunPublicationError for this run, which is Failed
        payload = {
            "kind": f"{method}_indexing",
            "run": {
                "indexing_id": run_id,
                "job_id": job_id,
                "scan_number": scan_number,
                "reconstruction_id": request.reconstruction_id,
                "method": method,
            },
            "request": request_document(request),
            "output": {"directory": run_directory, "results": RESULTS_FILENAME},
        }
        if isinstance(request, LaueGoParamsV1):
            payload["output"]["xml"] = request.output_xml or DEFAULT_OUTPUT_XML
        publish_run_inputs(
            engine,
            job_id=job_id,
            display_id=f"Indexing I{run_id}",
            run_directory=run_directory,
            inputs=inputs,
            request_payload=payload,
            now=request.submitted_at,
        )
        created = get_indexing(run_id, engine=engine)
        if created is None:  # deleted concurrently; nothing deletes runs today
            raise RuntimeError(f"Indexing I{run_id} vanished after it was created")
    return created


def create_live_indexing(
    request: LaueGoLiveParamsV1, live: LiveDocument, *, engine: Engine, settings: Settings | None = None
) -> IndexingRun:
    """Create a live LaueGo indexing run (``lauego-live/v1``) and its job, then publish its
    files (``jobs.records.publish_live_run``: ``request.json`` with ``lauego/v1``'s keys, an empty ``inputs.jsonl``,
    ``live.json``, the arrivals' lock and ledger), 0 inputs. On raw frames its folder and template are ``live``'s; a
    follower (``live.source`` ``run``) indexes the points of the wire run ``live.follows`` names, which is its
    ``reconstruction_id``. The refusals and the failures are ``create_indexing``'s; queue it with
    ``jobs.queue.enqueue_live_run``."""

    request = request.with_values(cosmic_filter=False)  # as every new run (create_indexing)
    check_paths_creatable(request, settings=settings)
    if live.source == "run":
        if live.follows is None or request.reconstruction_id is None:
            raise WorkflowValidationError("A live indexing run that follows a reconstruction names it")
    elif live.folder != request.input_path or live.filename_template != live_template(request.filename_prefixes):
        raise WorkflowValidationError("A live indexing run takes the files of its live.json's folder and file name")

    with Session(engine, expire_on_commit=False) as session:
        with session.begin():
            parent = None
            if request.reconstruction_id is not None:
                parent = session.get(ReconstructionRun, request.reconstruction_id)
                if parent is None:
                    raise WorkflowValidationError(f"Reconstruction R{request.reconstruction_id} does not exist")
                if live.follows is not None and live.follows.job_id != parent.job_id:
                    raise WorkflowValidationError(
                        f"live.json follows job {live.follows.job_id}, not Reconstruction R{parent.id}'s"
                    )
            if request.scan_number is None and parent is not None and parent.scan_number is not None:
                request = request.with_values(scan_number=parent.scan_number)
            scan_number = request.scan_number
            job = new_job(
                computer_name=request.computer_name,
                backend=request.backend,
                priority=request.priority,
                submitted_at=request.submitted_at,
                n_inputs=0,
            )
            session.add(job)
            session.flush()
            run = IndexingRun(
                scan_number=scan_number,
                reconstruction=parent,
                job=job,
                method="lauego",
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
            run.lauego_parameters = _lauego_parameters(request, scan_points_len=0)
        run_id = run.id
        job_id = job.job_id

    with publishing(engine, job_id=job_id, display_id=f"Indexing I{run_id}", now=request.submitted_at):
        payload = {
            "kind": "lauego_indexing",
            "run": {
                "indexing_id": run_id,
                "job_id": job_id,
                "scan_number": scan_number,
                "reconstruction_id": request.reconstruction_id,
                "method": "lauego",
            },
            "request": request_document(request),
            "output": {
                "directory": run_directory,
                "results": RESULTS_FILENAME,
                "xml": request.output_xml or DEFAULT_OUTPUT_XML,
            },
        }
        publish_live_run(
            engine,
            job_id=job_id,
            display_id=f"Indexing I{run_id}",
            run_directory=run_directory,
            request_payload=payload,
            live=live,
            now=request.submitted_at,
        )
        created = get_indexing(run_id, engine=engine)
        if created is None:  # nothing deletes runs
            raise RuntimeError(f"Indexing I{run_id} vanished after it was created")
    return created


def _lauego_parameters(
    request: LaueGoParamsV1 | LaueGoLiveParamsV1, *, scan_points_len: int | None
) -> LaueGoIndexingParameters:
    """The run's parameter row (a live run's scan points are empty until it ends: ``jobs.executor``)."""
    return LaueGoIndexingParameters(
        filename_prefixes=list(request.filename_prefixes),
        threshold=request.threshold,
        threshold_ratio=request.threshold_ratio,
        max_rfactor=request.max_rfactor,
        box_size=request.box_size,
        max_number=request.max_number,
        min_separation=request.min_separation,
        peak_shape=request.peak_shape,
        scan_points=request.scan_points,
        scan_points_len=scan_points_len,
        depth_range=request.depth_range,
        depth_range_len=request.depth_count,
        detector_crop_x1=request.detector_crop_x1,
        detector_crop_x2=request.detector_crop_x2,
        detector_crop_y1=request.detector_crop_y1,
        detector_crop_y2=request.detector_crop_y2,
        min_size=request.min_size,
        max_peaks=request.max_peaks,
        smooth=request.smooth,
        mask_file=request.mask_file,
        index_kev_max_calc=request.index_kev_max_calc,
        index_kev_max_test=request.index_kev_max_test,
        index_angle_tolerance=request.index_angle_tolerance,
        index_h=request.index_h,
        index_k=request.index_k,
        index_l=request.index_l,
        index_cone=request.index_cone,
        energy_unit=request.energy_unit,
        exposure_unit=request.exposure_unit,
        cosmic_filter=request.cosmic_filter,
        reciprocal_lattice_unit=request.reciprocal_lattice_unit,
        lattice_parameters_unit=request.lattice_parameters_unit,
        output_xml=request.output_xml,
        geometry_file=request.geometry_file,
        crystal_file=request.crystal_file,
        depth=request.depth,
        beamline=request.beamline,
    )


def _laue_matching_parameters(request: LaueMatchingParamsV1) -> LaueMatchingIndexingParameters:
    return LaueMatchingIndexingParameters(
        filename_prefixes=list(request.filename_prefixes),
        scan_points=request.scan_points,
        scan_points_len=request.scan_point_count,
        depth_range=request.depth_range,
        depth_range_len=request.depth_count,
        geometry_file=request.geometry_file,
        crystal_file=request.crystal_file,
        mask_file=request.mask_file,
        background_file=request.background_file,
        forward_cache=request.forward_cache,
        create_forward_cache=request.create_forward_cache,
        threshold_method=request.threshold_method,
        threshold_percentile=request.threshold_percentile,
        threshold_value=request.threshold_value,
        min_area=request.min_area,
        watershed_image=request.watershed_image,
        elo=request.elo,
        ehi=request.ehi,
        max_nr_laue_spots=request.max_nr_laue_spots,
        min_nr_spots=request.min_nr_spots,
        min_good_spots=request.min_good_spots,
        min_intensity=request.min_intensity,
        max_angle=request.max_angle,
        robust_filter=request.robust_filter,
    )


def get_indexing(indexing_id: int, *, engine: Engine) -> IndexingRun | None:
    """Load an indexing run with its parameters, parent, and job."""

    statement = (
        select(IndexingRun)
        .where(IndexingRun.id == indexing_id)
        .options(
            joinedload(IndexingRun.lauego_parameters),
            joinedload(IndexingRun.laue_matching_parameters),
            joinedload(IndexingRun.reconstruction),
            joinedload(IndexingRun.job),
        )
    )
    with Session(engine) as session:
        return session.scalars(statement).one_or_none()


# --- the indexations grid ------------------------------------------------------------------------------


def indexing_run_list_query(scan_id: int | None = None) -> Select[Unpack[tuple[Any, ...]]]:
    """The indexations grid's select: one row per indexing run, with its scan's sample, its parameters' columns and its
    job's status and counters; only the runs of scan ``scan_id`` when given.

    A run without its parameters row is listed too (those columns blank)."""

    query = (
        select(
            IndexingRun.id.label("id"),
            IndexingRun.method.label("method"),
            case((IndexingRun.method == "laue_matching", "LaueMatching"), else_="LaueGo").label("method_label"),
            IndexingRun.scan_number.label("scan_number"),
            Catalog.sample_name.label("sample_name"),
            IndexingRun.reconstruction_id.label("reconstruction_id"),
            func.coalesce(
                LaueGoIndexingParameters.scan_points_len, LaueMatchingIndexingParameters.scan_points_len
            ).label("n_points"),
            func.coalesce(
                LaueGoIndexingParameters.depth_range_len, LaueMatchingIndexingParameters.depth_range_len
            ).label("n_depths"),
            LaueGoIndexingParameters.box_size.label("box_size"),
            func.coalesce(LaueGoIndexingParameters.crystal_file, LaueMatchingIndexingParameters.crystal_file).label(
                "crystal_file"
            ),
            IndexingRun.author.label("author"),
            IndexingRun.notes.label("notes"),
            IndexingRun.created_at.label("created_at"),
            *grid_progress_columns(),
        )
        .select_from(IndexingRun)
        .join(Job, Job.job_id == IndexingRun.job_id)
        .outerjoin(LaueGoIndexingParameters, LaueGoIndexingParameters.indexing_id == IndexingRun.id)
        .outerjoin(LaueMatchingIndexingParameters, LaueMatchingIndexingParameters.indexing_id == IndexingRun.id)
        .outerjoin(Catalog, Catalog.scan_number == IndexingRun.scan_number)  # one catalog per scan at most
    )
    if scan_id is not None:
        query = query.where(IndexingRun.scan_number == scan_id)
    return query


def list_indexing_runs(session: Session, params: RunGridParams) -> IndexingRunPage:
    """One page of the indexations grid (or of a scan's indexations): two queries."""

    page = fetch_page(session, indexing_run_list_query(params.scan_id), INDEXING_RUN_GRID, params)
    rows = [IndexingRunSummary.model_validate(dict(row)) for row in page.rows]
    return IndexingRunPage(rows=rows, total=page.total, as_of=page.as_of)
