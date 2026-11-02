"""The indexing results viewer's service: a run's detail (its job and stored parameters), its
artifact, the XML chooser, the dataset the products read, and the summary product.

- **Artifact:** the resolution order (``indexing.results.resolve_indexing_artifacts``), as ``ready``,
  ``xml_only`` or ``ambiguous``; without results, ``not_on_host`` when the recorded run directory is absent or
  unreadable, else ``writing`` while the run is queued or running and ``empty`` after. Each path the resolution
  reads is checked through ``files.probe`` first, keyed by itself (``resolve``: the recorded results file and an
  absolute Output XML opened, the run directory listed and its results file and XML opened), and one whose file
  system does not answer makes the artifact ``not_responding``; the resolution then runs in the request's thread
  (its validation of a results file is h5py's work, which never runs in the probe).
  An ``xml_only`` artifact also reports its conversion job (``indexing.conversion``).
- **No database connection across file I/O:** every route reads the run in a session of its own and
  closes it (``read_run``) before it touches a file or the queue (the select-xml route writes its choice in a
  second short session at the end).
- **Dataset:** the results file of a ``ready`` artifact, else the Output XML of an ``xml_only`` one, loaded
  through the app's ``science.cache.DatasetCache``. ``ambiguous`` is a 409 (choose the XML first), no results
  (``writing``, ``empty``, ``not_on_host``) a 404 with the notes, ``not_responding`` a 503.
- **Paths inside the outputs** that publication does not rewrite are mapped when read (``map_output_path``):
  ``remote.paths.portal_path`` with the run's plan (``run_path_map``, the run directory's ``bdp.json``; a local run's
  paths are portal paths), then confined to the file roots (``files.roots.resolve_inside_roots``). An unavailable value
  is reported and never opened. Today that is the geometry: a dataset loads with the geometry text a results file
  embeds, with a mapped geometry file (``geometry=``: an Output XML's run's recorded one, or the one the file names), or
  without geometry, never letting lauelab open the path the file names (``science.cache``). The detector images the
  steps name are mapped and confined the same way before they are opened (``indexing.detector``). The crystal is the one
  the output embeds: lauelab never opens the ``xtlFile`` (``/crystal@source``) it names.
- **Scope:** every product takes the same ``DataScope`` (``indexing.api.ScopeDep``, ``science.viewer``).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from xml.etree.ElementTree import ParseError

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from laueportal.core import etag
from laueportal.core.db import session_scope
from laueportal.core.problems import ProblemError, ProblemItem, problems_detail
from laueportal.core.settings import Settings
from laueportal.core.text import quoted
from laueportal.files.probe import NotResponding, bounded, default_probe, open_check
from laueportal.files.roots import (
    PathNotAllowed,
    RealRoots,
    is_file_here,
    recorded_real_path,
    resolve_inside_roots,
    shown,
)
from laueportal.indexing import results
from laueportal.indexing.models import IndexingRun
from laueportal.indexing.schemas import (
    ArtifactState,
    AxisChoice,
    ColorModeChoice,
    ConversionJob,
    ConversionStatus,
    GeometrySource,
    IndexingArtifact,
    IndexingResultsSummary,
    IndexingRunDetail,
    LaueGoParameters,
    OutputPath,
    OutputPathState,
    ResultsCounts,
    ResultsGeometry,
    XmlSource,
)
from laueportal.jobs.live import has_live_marker
from laueportal.jobs.rundir import RESULTS_FILENAME
from laueportal.jobs.state import JobStatus
from laueportal.remote.paths import portal_path, run_path_map
from laueportal.science import viewer as science_viewer
from laueportal.science.cache import DatasetCache, SourceKey, VisualizationDataset, file_identity, source_key
from laueportal.science.results import InvalidResultsFile, NamedGeometry, named_geometry

STATES = {
    results.STATUS_RESULTS: ArtifactState.READY,
    results.STATUS_XML_ONLY: ArtifactState.XML_ONLY,
    results.STATUS_AMBIGUOUS: ArtifactState.AMBIGUOUS,
    results.STATUS_MISSING: ArtifactState.EMPTY,
}
ACTIVE = (JobStatus.QUEUED, JobStatus.RUNNING)


class IndexingRunNotFound(ProblemError):
    def __init__(self, indexing_id: int) -> None:
        super().__init__(404, "Indexing run not found", f"No indexing run I{indexing_id}")


class ArtifactConflict(ProblemError):
    """The request does not fit the run's artifact state (a 409)."""

    def __init__(self, detail: str) -> None:
        super().__init__(409, "Artifact state conflict", detail)


class ResultsNotFound(ProblemError):
    def __init__(self, indexing_id: int, problems: tuple[str, ...]) -> None:
        reasons = "; ".join(problems) or "no results file and no XML document found"
        super().__init__(404, "Results not found", f"I{indexing_id} has no results to show: {reasons}")


RESULTS_UNREADABLE = "urn:laueportal:problem:results-unreadable"
"""``ResultsUnreadable``'s problem type, so that a client tells it from the other 409 (``ArtifactConflict``,
``about:blank``: choose an XML first) without reading the title."""


class ResultsUnreadable(ProblemError):
    """The artifact exists but lauelab cannot read it (a 409: a malformed document is the run's state, not the
    request's fault; with a chosen XML, choose again)."""

    def __init__(self, path: str, error: BaseException) -> None:
        super().__init__(
            409,
            "Results unreadable",
            f"{shown(path)} cannot be read: {quoted(f'{type(error).__name__}: {error}')}",
            problem_type=RESULTS_UNREADABLE,
        )


def load_run(session: Session, indexing_id: int) -> IndexingRun:
    run = results.load_indexing_run(session, indexing_id)
    if run is None:
        raise IndexingRunNotFound(indexing_id)
    return run


@dataclass(frozen=True)
class ViewedRun:
    """A run as the artifact routes read it (``read_run``), readable once its session has closed."""

    run: IndexingRun
    """With its parameters loaded."""
    active: bool
    """Its job is queued or running."""
    live_open: bool = False
    """It is a live run still open (queued or running: its queue identity, ``jobs.live.has_live_marker``)."""


def read_run(sessions: sessionmaker[Session], indexing_id: int) -> ViewedRun:
    """The run, read in a session of its own that is closed before the caller touches a file or the queue (a hung
    mount must not hold a database connection). 404 for an unknown run."""
    with session_scope(sessions) as session:
        run = load_run(session, indexing_id)  # with its parameters (joinedload): readable once the session closes
        active = JobStatus(run.job.status) in ACTIVE
        live_open = active and has_live_marker(run.job)
    return ViewedRun(run, active, live_open)


@dataclass(frozen=True)
class Resolved:
    """A run's artifact as found (``resolve``)."""

    resolution: results.ArtifactResolution | None
    """None when a path's file system did not answer."""
    on_host: bool
    """Whether the recorded run directory is there and can be listed (looked at only without results)."""
    where: str | None
    """The path that did not answer."""


def _listable(path: str) -> bool:
    try:
        with os.scandir(path):
            return True
    except (OSError, ValueError):  # absent, not a folder, unreadable, a stale mount; a NUL in the path
        return False


def _opened(path: str) -> None:
    """The check of a file the resolution reads next (``files.probe.open_check``); what it finds wrong, the
    resolution reports."""
    try:
        open_check(path)
    except (OSError, ValueError):
        pass


def _checked(run: IndexingRun, output_xml: str | None) -> bool:
    """Check each path the resolution reads, each through ``files.probe`` keyed by itself: the recorded results file
    and an absolute Output XML (opened), and the run directory (listed, and its results file, the recorded XML and
    the chosen XML opened). Whether the run directory can be listed (True without one); ``NotResponding`` from the
    first that does not answer."""

    absolute_xml = output_xml if output_xml and os.path.isabs(output_xml) else None
    for path in (run.results_path, absolute_xml):
        if path:
            bounded(path, lambda path=path: _opened(path))
    directory = run.output_path
    if not directory:
        return True

    def look() -> bool:
        if not _listable(directory):
            return False
        for name in (RESULTS_FILENAME, None if absolute_xml else output_xml, run.selected_xml):
            if name:
                _opened(os.path.join(directory, name))
        return True

    return bounded(directory, look)


def resolve(run: IndexingRun) -> Resolved:
    """The resolution order for ``run`` and whether its run directory is on this host, once every path it reads
    answered (``_checked``): a file system that does not answer gives no resolution. The resolution's own reads (its
    stats, the XML glob, a results file's validation in h5py) follow in the caller's thread."""
    output_xml = run.lauego_parameters.output_xml if run.lauego_parameters is not None else None
    try:
        listable = _checked(run, output_xml)
    except NotResponding as error:
        return Resolved(None, True, error.path)
    resolution = results.resolve_indexing_artifacts(run)
    on_host = resolution.status != results.STATUS_MISSING or not run.output_path or listable
    return Resolved(resolution, on_host, None)


def _resolution(resolved: Resolved) -> results.ArtifactResolution:
    """The resolution, or ``NotResponding`` (a 503) for a run directory that did not answer."""
    if resolved.resolution is None:
        raise default_probe().not_responding(resolved.where or "")
    return resolved.resolution


def run_detail(session: Session, indexing_id: int) -> IndexingRunDetail:
    """The run, its job and its stored parameters (the viewer's header and Parameters tab); 404 for an unknown run."""
    run = load_run(session, indexing_id)
    stored = run.lauego_parameters
    return IndexingRunDetail(
        id=run.id,
        method=run.method,
        scan_number=run.scan_number,
        reconstruction_id=run.reconstruction_id,
        job_id=run.job_id,
        input_path=run.input_path,
        output_path=run.output_path,
        author=run.author,
        notes=run.notes,
        created_at=run.created_at,
        parameters=None if stored is None else LaueGoParameters.model_validate(stored, from_attributes=True),
    )


# ---------------------------------------------------------------------------
# Artifact and the XML chooser


def artifact_state(viewed: ViewedRun, resolved: Resolved) -> ArtifactState:
    """The artifact's state: the resolution's; without results, not on this host, writing or empty."""
    resolution = resolved.resolution
    if resolution is None:
        return ArtifactState.NOT_RESPONDING
    if resolution.status != results.STATUS_MISSING:
        return STATES[resolution.status]
    if not resolved.on_host:
        return ArtifactState.NOT_ON_HOST
    return ArtifactState.WRITING if viewed.active else ArtifactState.EMPTY


def results_version(resolved: Resolved) -> str | None:
    """A ``ready`` artifact's version: a digest of its results file's identity (``science.cache.file_identity``: real
    path, size, modification and change times), looked at through ``files.probe`` after the resolution's own check of
    it; another version is another file (a live run's preview ``output.h5`` replaced). None for another state, or a file
    that no longer answers or stats."""
    resolution = resolved.resolution
    if resolution is None or resolution.status != results.STATUS_RESULTS or not resolution.results_path:
        return None
    path = resolution.results_path
    try:
        identity = bounded(path, lambda: file_identity(path))
    except (NotResponding, OSError, ValueError):
        return None
    text = json.dumps(asdict(identity), sort_keys=True)
    return hashlib.blake2b(text.encode(), digest_size=8).hexdigest()


def artifact_tag(artifact: IndexingArtifact) -> str:
    """The artifact's entity tag: its content (the results file's version included) and the code. The artifact is
    resolved for each request (a valid results file's summary kept by its identity, ``science.results``), so the
    tag saves the answer, and the page's reload, not the resolution."""
    return etag.entity_tag("indexing-artifact", artifact.model_dump(mode="json"))


def artifact_of(
    viewed: ViewedRun,
    resolved: Resolved,
    *,
    find_conversion: Callable[[int], ConversionJob | None] | None = None,
    version: str | None = None,
) -> IndexingArtifact:
    """The API's view of ``resolved``. ``find_conversion`` (``indexing.conversion.find_conversion``: the run's
    ``conversion`` row) is asked only for an ``xml_only`` artifact; ``version`` is ``results_version``'s."""

    run = viewed.run
    state = artifact_state(viewed, resolved)
    resolution = resolved.resolution or results.ArtifactResolution(run.id, run.output_path, results.STATUS_MISSING)
    problems = [shown(problem) for problem in resolution.problems]
    conversion = None
    if state is ArtifactState.XML_ONLY and find_conversion is not None:
        conversion = find_conversion(run.id)
    return IndexingArtifact(
        indexing_id=run.id,
        state=state,
        results_path=shown(resolution.results_path) if resolution.results_path else None,
        xml_path=shown(resolution.xml_path) if resolution.xml_path else None,
        xml_source=XmlSource(resolution.xml_source) if resolution.xml_source else None,
        xml_candidates=[shown(Path(candidate).name) for candidate in resolution.xml_candidates],
        conversion_job_id=conversion.id if conversion is not None else None,
        conversion=conversion,
        problems=problems,
        results_version=version if state is ArtifactState.READY else None,
        live_open=viewed.live_open,
    )


def get_artifact(
    sessions: sessionmaker[Session],
    indexing_id: int,
    *,
    find_conversion: Callable[[int], ConversionJob | None] | None = None,
) -> IndexingArtifact:
    viewed = read_run(sessions, indexing_id)
    resolved = resolve(viewed.run)
    return artifact_of(viewed, resolved, find_conversion=find_conversion, version=results_version(resolved))


def select_xml(
    sessions: sessionmaker[Session],
    indexing_id: int,
    name: str,
    *,
    find_conversion: Callable[[int], ConversionJob | None],
) -> IndexingArtifact:
    """Record ``name`` as the run's Output XML (``indexing_run.selected_xml``) while its artifact is ambiguous, or
    while the XML in use is the chosen one (to change the choice); returns the artifact it then resolves to
    (``xml_only``). 409 when a results file, the recorded XML or the only XML decides, or while the run's
    conversion (``find_conversion``) is queued or running: it converts the choice it finds when it starts, and
    once it has converted, ``output.h5`` decides; a 503 when the run directory does not answer
    (``NotResponding``). 422 for a name
    that is not one of the candidates. The choice is written in a short session of its own at the end."""

    viewed = read_run(sessions, indexing_id)
    run = viewed.run
    resolution = _resolution(resolve(run))
    choosable = resolution.status == results.STATUS_AMBIGUOUS or (
        resolution.status == results.STATUS_XML_ONLY and resolution.xml_source == results.XML_SOURCE_CHOSEN
    )
    if not choosable:
        decided = f" ({resolution.xml_source} XML)" if resolution.xml_source else ""
        raise ArtifactConflict(
            f"I{indexing_id}'s artifact is {STATES[resolution.status].value}{decided}: an XML is chosen only among "
            "several (ambiguous, or to change an earlier choice)."
        )
    names = {Path(candidate).name for candidate in resolution.xml_candidates}
    if name not in names:
        raise ProblemError(
            422,
            "Validation failed",
            problems_detail(1),
            errors=[
                ProblemItem(
                    loc=["body", "name"],
                    message=f"{quoted(name)} is not one of I{indexing_id}'s XML files.",
                    code="unknown_xml",
                )
            ],
        )
    conversion = find_conversion(indexing_id)
    if conversion is not None and conversion.status in (ConversionStatus.QUEUED, ConversionStatus.RUNNING):
        raise ArtifactConflict(
            f"I{indexing_id}'s conversion to output.h5 is {conversion.status.value}: the XML cannot change until it "
            "ends. Once it has converted, output.h5 holds the results and the choice is fixed; if it fails, the XML "
            "can be chosen again."
        )
    with session_scope(sessions) as session:
        session.execute(update(IndexingRun).where(IndexingRun.id == indexing_id).values(selected_xml=name))
    run.selected_xml = name  # the run read before (detached): what it now resolves to
    return artifact_of(viewed, resolve(run))


def check_convertible(sessions: sessionmaker[Session], indexing_id: int) -> None:
    """409 unless the run's artifact is ``xml_only`` and the run has a run directory to receive ``output.h5``; 503
    when the run directory does not answer."""

    run = read_run(sessions, indexing_id).run
    resolution = _resolution(resolve(run))
    if resolution.status != results.STATUS_XML_ONLY or not run.output_path:
        raise ArtifactConflict(
            f"I{indexing_id}'s artifact is {STATES[resolution.status].value}: only an Output XML with a run "
            "directory to receive output.h5 (xml_only) is converted."
        )


# ---------------------------------------------------------------------------
# Paths inside the outputs


@dataclass(frozen=True)
class MappedPath:
    shown: OutputPath
    """How the API reports it (display strings)."""
    file: str | None
    """The path that passed the roots check (``resolve_inside_roots``'s, links resolved), to open and to record;
    None unless it is available."""


def mapped_output_path(
    value: str, *, run_directory: str | None, base: str | os.PathLike[str], settings: Settings | None
) -> MappedPath:
    """``value``, a path an output wrote, as the portal reads it.

    The run's plan maps it (``portal_path``; a local run, without ``bdp.json``, keeps it as written, and a relative
    name of a local run is relative to ``base``, the output's folder, as lauelab reads it); the result must lie in
    the file roots (``settings``; None: no roots, as ``laueportal results convert --db`` reads no configuration),
    and is available only when it is a file here, checked through ``files.probe`` (``not_responding`` when its file
    system does not answer)."""

    mappings = run_path_map(run_directory) if run_directory else None
    return mapped_path(value, mappings=mappings, base=base, settings=settings)


def mapped_path(
    value: str,
    *,
    mappings: Mapping[str, str] | None,
    base: str | os.PathLike[str],
    settings: Settings | None,
    roots: RealRoots | None = None,
) -> MappedPath:
    """``mapped_output_path`` with the run's map already read (``remote.paths.run_path_map``: None for a local
    run), for a reader that maps many values of one run (the wire viewer's point sources; ``roots``: the file
    roots resolved once for them all, ``files.roots.real_roots``). A recorded path: ``resolve_inside_roots``'
    ``recorded``."""

    candidate = value if mappings is not None or os.path.isabs(value) else os.path.join(base, value)
    try:
        mapped = portal_path(candidate, mappings, resolve=recorded_real_path)  # a remote run's: through files.probe
    except NotResponding:
        return MappedPath(OutputPath(value=shown(value), path=None, state=OutputPathState.NOT_RESPONDING), None)
    if mapped is None:
        return MappedPath(OutputPath(value=shown(value), path=None, state=OutputPathState.UNAVAILABLE), None)

    try:
        if settings is None:
            raise PathNotAllowed("No file roots are configured.")
        _root, resolved = resolve_inside_roots(mapped, settings, recorded=True, roots=roots)  # through files.probe
        available = bounded(resolved, lambda: is_file_here(resolved))
    except PathNotAllowed:
        return MappedPath(OutputPath(value=shown(value), path=shown(mapped), state=OutputPathState.OUTSIDE_ROOTS), None)
    except NotResponding:
        state = OutputPathState.NOT_RESPONDING
        return MappedPath(OutputPath(value=shown(value), path=shown(mapped), state=state), None)
    state = OutputPathState.AVAILABLE if available else OutputPathState.MISSING
    return MappedPath(
        OutputPath(value=shown(value), path=shown(mapped), state=state), str(resolved) if available else None
    )


def map_output_path(
    value: str, *, run_directory: str | None, base: str | os.PathLike[str], settings: Settings | None
) -> OutputPath:
    """How the API reports ``value`` (``mapped_output_path``)."""

    return mapped_output_path(value, run_directory=run_directory, base=base, settings=settings).shown


@dataclass(frozen=True)
class GeometryChoice:
    """The geometry a dataset loads with (``science.cache.load_dataset``)."""

    file: str | None
    """The mapped geometry file to give lauelab, as it passed the roots check; None: the embedded text, or no
    geometry."""
    source: GeometrySource
    """What the dataset loads with, when lauelab can read it."""
    named: OutputPath | None
    recorded: OutputPath | None
    values: tuple[str, ...]
    """Every geometry path the file names, as written."""


def choose_geometry(
    path: str, *, run_directory: str | None, settings: Settings | None, recorded: str | None = None
) -> GeometryChoice:
    """For the results file or Output XML ``path``: the geometry text a results file embeds; else ``recorded`` (for
    an Output XML, the run's recorded ``geometry_file``) when it is available here; else
    the first geometry path the file names that is available here; else none. Available: mapped, confined to the
    roots and a file (``mapped_output_path``)."""

    try:
        named = named_geometry(path)
    except (OSError, ValueError, ParseError):  # loading or converting it reports what is wrong with the file
        named = NamedGeometry(embedded=False, paths=())
    base = os.path.dirname(os.path.abspath(path))

    def mapped(value: str) -> MappedPath:
        return mapped_output_path(value, run_directory=run_directory, base=base, settings=settings)

    run_geometry = mapped(recorded) if recorded else None
    names = [mapped(value) for value in named.paths]
    usable = next((item for item in names if item.file is not None), None)
    shown_path = usable or (names[0] if names else None)
    if named.embedded:
        file, source = None, GeometrySource.EMBEDDED
    elif run_geometry is not None and run_geometry.file is not None:
        file, source = run_geometry.file, GeometrySource.RECORDED
    elif usable is not None:
        file, source = usable.file, GeometrySource.FILE
    else:
        file, source = None, GeometrySource.NONE
    return GeometryChoice(
        file=file,
        source=source,
        named=shown_path.shown if shown_path else None,
        recorded=run_geometry.shown if run_geometry else None,
        values=named.paths,
    )


def conversion_geometry(
    xml_path: str, *, run_directory: str | None, settings: Settings | None
) -> tuple[str | None, str | None]:
    """``(geometry, refusal)`` for converting ``xml_path``: the mapped geometry file to record, and why the
    conversion must not run. lauelab's ``convert_xml`` resolves the XML's ``geoFile`` itself when given no
    geometry, so it runs without one only when none of the paths it would try is a file here."""

    choice = choose_geometry(xml_path, run_directory=run_directory, settings=settings)
    if choice.file is not None:
        return choice.file, None
    xml_directory = Path(xml_path).resolve().parent
    for value in choice.values:
        candidate = Path(value).expanduser()  # what lauelab tries (xml._resolve_geometry)
        tried = (candidate,) if candidate.is_absolute() else (candidate, xml_directory / candidate)
        if any(is_file_here(path) for path in tried):
            mapped = map_output_path(value, run_directory=run_directory, base=xml_directory, settings=settings)
            return None, (
                f"the XML names the geometry file {shown(value)}, which is {mapped.state.value.replace('_', ' ')} "
                "for this run, and lauelab would open it as written; nothing was converted"
            )
    return None, None


# ---------------------------------------------------------------------------
# The dataset and the summary


@dataclass(frozen=True)
class ViewerSource:
    """What a run's viewer reads, resolved without loading it (``viewer_source``): cheap enough to answer a
    revalidation (304) before any dataset is loaded."""

    indexing_id: int
    source: str
    """``results`` or ``xml``."""
    path: str
    choice: GeometryChoice
    key: SourceKey
    """The file's identity (real path, size, modification and change times) and the geometry file's it loads
    with."""
    run_directory: str | None = None
    """The run's output directory (its plan maps the paths the outputs name, ``map_output_path``)."""
    data_folder: str | None = None
    """The run's input folder (a relative ``inputImage`` is resolved in it)."""

    @property
    def identity(self) -> tuple[object, ...]:
        """What the dataset and the summary's geometry depend on, for an entity tag (``core.etag``). Whether
        lauelab can read the geometry file (the summary's ``source: none`` otherwise) is fixed by its identity."""
        geometry = ResultsGeometry(source=self.choice.source, named=self.choice.named, recorded=self.choice.recorded)
        return (self.indexing_id, self.source, self.path, asdict(self.key), geometry.model_dump(mode="json"))


@dataclass(frozen=True)
class ViewerDataset:
    indexing_id: int
    source: str
    """``results`` or ``xml``."""
    path: str
    dataset: VisualizationDataset
    geometry: ResultsGeometry


def viewer_source(sessions: sessionmaker[Session], indexing_id: int, *, settings: Settings) -> ViewerSource:
    """The file every product reads: the run's results file, else its Output XML, with the geometry it loads with
    and their identities. Reads the run in a session of its own, closed before anything else, so a viewer request
    holds no database connection while it then waits for a dataset load or a simulation's turn
    (docs/contributing/backend.md, "Long work"); then stats the files and reads the geometry paths the file names;
    loads nothing."""

    run = read_run(sessions, indexing_id).run
    resolved = resolve(run)
    resolution = _resolution(resolved)
    if resolution.status == results.STATUS_AMBIGUOUS:
        raise ArtifactConflict(
            f"I{indexing_id}'s run directory holds {len(resolution.xml_candidates)} XML files and none is recorded: "
            "choose the one with the results (select-xml)."
        )
    recorded = None
    if resolution.status == results.STATUS_RESULTS and resolution.results_path:
        source, path = "results", resolution.results_path
    elif resolution.status == results.STATUS_XML_ONLY and resolution.xml_path:
        source, path = "xml", resolution.xml_path
        recorded = run.lauego_parameters.geometry_file if run.lauego_parameters is not None else None
    elif not resolved.on_host:
        raise ResultsNotFound(indexing_id, (f"its run directory {shown(run.output_path or '')} is not on this host",))
    else:
        raise ResultsNotFound(indexing_id, resolution.problems)
    try:
        choice = choose_geometry(path, run_directory=run.output_path, settings=settings, recorded=recorded)
        key = source_key(path, choice.file)
    except (OSError, ValueError, ParseError, InvalidResultsFile) as error:
        raise ResultsUnreadable(path, error) from error
    return ViewerSource(
        indexing_id=indexing_id,
        source=source,
        path=path,
        choice=choice,
        key=key,
        run_directory=run.output_path,
        data_folder=run.input_path,
    )


async def load_viewer_dataset(source: ViewerSource, *, cache: DatasetCache) -> ViewerDataset:
    """The dataset of ``source``, through ``cache``, for an ``async`` route: a load another request
    makes is waited for in the event loop, holding no thread, and a load of its own runs in a worker thread
    (``DatasetCache.load``, by the identity ``viewer_source`` read)."""

    try:
        dataset = await cache.load(source.key)
    except (OSError, ValueError, ParseError, InvalidResultsFile) as error:
        raise ResultsUnreadable(source.path, error) from error
    return _viewer_dataset(source, dataset)


def _viewer_dataset(source: ViewerSource, dataset: VisualizationDataset) -> ViewerDataset:
    choice = source.choice
    geometry_source = GeometrySource.NONE if dataset.geometry is None else choice.source
    return ViewerDataset(
        indexing_id=source.indexing_id,
        source=source.source,
        path=source.path,
        dataset=dataset,
        geometry=ResultsGeometry(source=geometry_source, named=choice.named, recorded=choice.recorded),
    )


def summary(loaded: ViewerDataset, scope: science_viewer.DataScope) -> IndexingResultsSummary:
    """The summary of a loaded dataset (``load_viewer_dataset``) within ``scope``."""

    dataset = loaded.dataset
    steps = science_viewer.scope_steps(dataset, scope)
    counts = science_viewer.dataset_counts(dataset)
    crystal = dataset.crystal
    return IndexingResultsSummary(
        indexing_id=loaded.indexing_id,
        source="results" if loaded.source == "results" else "xml",
        path=shown(loaded.path),
        counts=ResultsCounts(frames=counts.frames, patterns=counts.patterns, peaks=counts.peaks),
        axes=[AxisChoice(value=value, label=label) for value, label in science_viewer.available_axes(dataset)],
        color_modes=[
            ColorModeChoice(
                value=mode.value,
                label=mode.label,
                scalar=mode.scalar,
                available=mode.unavailable_reason is None,
                reason=mode.unavailable_reason,
            )
            for mode in science_viewer.color_modes(dataset)
        ],
        steps=steps.tolist(),
        default_step=science_viewer.default_step(dataset, steps),
        structure=(crystal.name or None) if crystal is not None else None,
        space_group=int(crystal.space_group) if crystal is not None and crystal.space_group is not None else None,
        geometry=loaded.geometry,
    )
