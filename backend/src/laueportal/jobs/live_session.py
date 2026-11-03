"""The Live page's API: a live session's form, its validation, its Start, and the DAQ list's answer. The routes are
``jobs.api``'s, present while ``[live] enabled``.

**The form** (``POST /live-runs/validate``, ``POST /live-runs``): ``scan``, the Scan card (``source``: ``daq`` with
its ``daq_id``, or ``notify``; ``folder``; ``filename_template``, optional: blank or null, every file whose name ends
``.h5``; ``output_folder``; ``scan_number``; ``author``), and ``reconstruction`` and/or ``indexing``, each the live
schema's form fields (``wire-live/v1``, ``lauego-live/v1``) but those the Scan card sets, or null. The Scan card sets
each run's identity and paths: ``scan_number``, ``author``, ``input_path`` (the folder's real path, which ``live.json``
names too), ``filename_prefixes`` (the template, ``[]`` with none; ``live.json``'s null), ``output_path_template``
(``<output_folder>/rec_%d``, ``<output_folder>/index_%d``) and ``reconstruction_id``: a LaueGo run alone indexes the
folder's raw frames (``reconstruction_id`` null); beside a wire run it follows it, its ``input_path`` the wire run's
catalog and its ``reconstruction_id`` the wire run, set once that run exists (``jobs.creation.create_live_session``).
A program part that sends one of those keys is refused (``set_by_scan``), so the pair has one scan number.

**The validation** (:func:`validate`; reads only, and never asks DM): issues as a batch form's
(``jobs.validation``: ``{loc, severity, code, message}``), each ``loc`` starting with its part (``["scan",
"folder"]``, ``["reconstruction", "geometry_file"]``; a run's issue on a field the Scan card sets is reported once,
on the Scan card's field, and none when that field has an error of its own). The checks: the Scan card's fields; the
model's rules (``check_form`` with the live kind); the batch checks that apply to a live run (the files inside the
roots and existing, the output folder taking the run ID, the wire depths and skips); and the live create
checks: one scan number for the pair (the Scan card's), ``depth_range`` only for a follower, the folder exists (it
may be empty: the files arrive later; for a DAQ source a missing one is a warning, since the DAQ makes it when it
first writes, and it is still resolved inside the roots through its nearest existing folder), the output folder is
inside the roots (a missing one is no issue: Start makes it, its parents too; an existing one that holds anything
but earlier sessions' run folders, ``rec_<ID>`` and ``index_<ID>``, is a warning, ``not_empty``), the geometry (and the crystal) load (``science.calibration``; a file over 1 MiB is refused
unread, ``too_large``). A DAQ source's
DAQ is DM's to check, once per live create: Start makes the bind check (``jobs.daq.bind_daq``,
one DM call, once nothing else is wrong: a refusal is an error on ``daq_id``, DM's silence a 503). Validate makes no
call (the form validates as it is edited): with the DAQ's record in the DAQ list the api keeps (``jobs.daq.DaqList``,
``last_seen``), it checks the folder against the DAQ's destination as that list saw it (``jobs.daq.inside_destination``:
the bind check's refusal on ``daq_id``); without one, a warning on ``daq_id`` says that Start checks the DAQ.

**Start** (:func:`start`): the ``Idempotency-Key`` is claimed first (``jobs.idempotency``, route ``createLiveRuns``:
the same key with the same body answers the same runs; with another body, a 409), then the form is validated (a 422
with every issue, errors first; DM's silence a 503 ``urn:laueportal:problem:dm-unavailable``, the DAQ's folders not
answering a 503 ``urn:laueportal:problem:not-responding``; the key released). Then, under ONE lock (``lock``: the
api's ``app.state.live_start``; the api is one process), the one-session check
(``jobs.live.check_one_live_session``: while a live run is Queued or Running, a 409
``urn:laueportal:problem:live-session-open`` naming it, nothing created, the key released), the session's creations
and their queueing together (``jobs.creation.create_live_session``): two Starts close together never both pass the
check (it sees only queued runs), and a Both session's wire run is never claimed alone (the dispatcher would give it
the whole live share). The answer (201, ``CreatedLiveRuns``) is kept with the key.
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, cast

from pydantic import Field, TypeAdapter, ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from laueportal.core.db import SQLITE_INTEGER_MAX
from laueportal.core.problems import (
    MAX_PROBLEM_ITEMS,
    REQUIRED,
    ProblemError,
    ProblemItem,
    field_message,
    problems_detail,
)
from laueportal.core.settings import LiveSettings, Settings
from laueportal.core.text import quoted
from laueportal.files import probe
from laueportal.files.roots import PathNotAllowed
from laueportal.indexing.lauego.live_params import LaueGoLiveParamsV1
from laueportal.indexing.models import IndexingRun
from laueportal.jobs import programs
from laueportal.jobs.creation import LiveSessionNotCreated, LiveSessionPlan, create_live_session
from laueportal.jobs.daq import (
    MAX_DAQ_ID_LENGTH,
    DaqListAnswer,
    DaqRefused,
    DaqUnavailable,
    bind_daq,
    destination,
    inside_destination,
)
from laueportal.jobs.idempotency import Claim, IdempotencyStore
from laueportal.jobs.live import LiveDaq, LiveDocument, LiveSessionOpen, check_one_live_session, live_template
from laueportal.jobs.schemas import (
    CreatedLiveRuns,
    LiveDaqEntry,
    LiveDaqList,
    LiveRunsConflict,
    OpenLiveRun,
    RunReference,
    RunStatus,
)
from laueportal.jobs.state import STATUS_NAMES
from laueportal.jobs.validation import (
    SQLITE_INTEGER_MIN,
    Checked,
    Issues,
    check_file,
    check_folder,
    check_form,
    check_not_empty,
    check_output,
    finish,
    inside_roots,
    issue_count,
    padded,
)
from laueportal.reconstruction.models import ReconstructionRun
from laueportal.reconstruction.validation import check_depths, check_skips
from laueportal.reconstruction.wire.live_params import WireLiveParamsV1
from laueportal.remote.dm import DaqRecord

logger = logging.getLogger(__name__)

ROUTE = "createLiveRuns"
"""The create endpoint's operation ID (its idempotency keys' route)."""
LIVE_SESSION_OPEN = "urn:laueportal:problem:live-session-open"
"""The 409 of a Start while a live run is Queued or Running (``LiveRunsConflict.open_run`` names it)."""
DM_UNAVAILABLE = "urn:laueportal:problem:dm-unavailable"
"""The 503 of a DM that cannot be asked or does not answer (``jobs.daq.DaqUnavailable``): the DAQ list, Start's bind
check."""
DM_RETRY_SECONDS = 10
"""Start's ``Retry-After`` when DM did not answer its bind check."""

SCAN = "scan"
PARTS: dict[str, programs.ProgramKind] = {"reconstruction": "wire", "indexing": "lauego"}
"""The program parts of the form and the program of each (Reconstruction: a live wire run; Indexing: a live LaueGo
run)."""
SCAN_SETS: dict[str, str | None] = {
    "scan_number": "scan_number",
    "author": "author",
    "input_path": "folder",
    "filename_prefixes": "filename_template",
    "output_path_template": "output_folder",
    "reconstruction_id": None,
}
"""The run fields the Scan card sets, and the Scan card's field a run's issue on each is reported on (None for
a follower's ``reconstruction_id``, which no form field sets)."""
RUN_FOLDERS: dict[programs.ProgramKind, str] = {"wire": "rec_%d", "lauego": "index_%d"}
"""Each run's folder in the session's output folder."""
RUN_FOLDER_NAMES = re.compile("|".join(re.escape(name).replace("%d", "[0-9]+") for name in RUN_FOLDERS.values()))
"""The names of the session's run folders (``rec_<ID>``, ``index_<ID>``): an output folder holding only these is not
"not empty" (``jobs.validation.check_not_empty``)."""
DAQ_MAKES_FOLDER = "Not found yet: the DAQ creates it"
"""The warning on a DAQ source's Folder that is not there yet (for notifications it is an error)."""

Source = Literal["daq", "notify"]

_TEXT = TypeAdapter(str)
_OPTIONAL_TEXT = TypeAdapter(str | None)
SCAN_FIELDS: dict[str, tuple[str, TypeAdapter[Any]]] = {
    "source": ("Source", TypeAdapter(Source)),
    "daq_id": ("DAQ", TypeAdapter(Optional[Annotated[str, Field(max_length=MAX_DAQ_ID_LENGTH)]])),  # noqa: UP045
    "folder": ("Folder", _TEXT),
    "filename_template": ("Filename", _OPTIONAL_TEXT),
    "output_folder": ("Output folder", _TEXT),
    "scan_number": (
        "Scan Number",
        TypeAdapter(Optional[Annotated[int, Field(ge=SQLITE_INTEGER_MIN, le=SQLITE_INTEGER_MAX)]]),  # noqa: UP045
    ),
    "author": ("Author", _OPTIONAL_TEXT),
}
"""The Scan card's fields: each one's label (for the messages that name it) and type."""
SCAN_REQUIRED = ("source", "folder", "output_folder", "author")
SCAN_OPTIONAL = ("filename_template",)
"""The Scan card's fields a blank value leaves out (null): no template."""


@dataclass
class _Scan:
    """The Scan card's values that passed their own rules (``None`` for one that did not, or was left blank)."""

    source: Source | None = None
    daq_id: str | None = None
    folder: str | None = None
    real_folder: Path | None = None
    """The folder resolved inside the roots, once it is known to be one."""
    filename_template: str | None = None
    output_folder: str | None = None
    scan_number: int | None = None
    author: str | None = None
    failed: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Planned:
    """A validation's outcome: its issues and, when no issue is an error, the session's runs (``plan``)."""

    issues: list[ProblemItem]
    plan: LiveSessionPlan | None


# ---------------------------------------------------------------------------
# The validation


def validate(form: Any, *, settings: Settings, engine: Engine, daqs: DaqListAnswer | None = None) -> list[ProblemItem]:
    """Every issue of a live session's form (the module's docstring; ``POST /live-runs/validate``), with no DM call: a
    DAQ source's DAQ is checked against ``daqs``, the DAQ list's kept answer (``jobs.daq.DaqList.last_seen``; ``None``
    before the list was asked)."""
    return plan_session(form, settings=settings, engine=engine, bind=False, daqs=daqs).issues


def plan_session(
    form: Any, *, settings: Settings, engine: Engine, bind: bool = True, daqs: DaqListAnswer | None = None
) -> Planned:
    """Validate a live session's form and build its runs (the module's docstring). ``bind`` (Start): make the DAQ
    bind check (one DM call) once nothing else is wrong, DM's silence raised as a 503 problem; else (Validate) check
    the DAQ against ``daqs`` (no call)."""

    issues = Issues()
    if not isinstance(form, Mapping):
        issues.error([], "dict_type", "Must be an object")
        return Planned(finish(issues), None)
    form = cast(Mapping[str, Any], form)
    for name in form:
        if name not in (SCAN, *PARTS):
            issues.error([name], "extra_forbidden", f"{quoted(name)} is not a part of a live session")
    scan = _check_scan(issues, settings, engine, form.get(SCAN))
    parts = {name: _part(issues, name, form.get(name)) for name in PARTS}
    enabled = [name for name, part in parts.items() if part is not None]
    if not enabled and not any(issue.loc and issue.loc[0] in PARTS for issue in issues):  # a part's own error says why
        issues.error([], "no_runs", "Enable reconstruction, indexing, or both")
    follower = parts["reconstruction"] is not None
    runs: dict[str, Checked] = {}
    for name in enabled:
        part = parts[name]
        assert part is not None
        kind = PARTS[name]
        if kind == "lauego" and not follower and _given(part.get("depth_range")):
            issues.error(
                ["indexing", "depth_range"],
                "depth_without_reconstruction",
                "Only for indexing that follows a reconstruction: depth indices select a reconstructed point's depths",
            )
        run_issues = Issues()
        runs[name] = _check_run(run_issues, settings, kind, {**part, **_scan_values(scan, kind)})
        _report(issues, name, run_issues, scan)
    if not bind and scan.source == "daq" and scan.daq_id is not None and scan.real_folder is not None:
        _check_seen_daq(issues, scan, daqs)
    plan = None
    if not issues.has_error() and runs and (bind or scan.source != "daq"):  # a DAQ's live.json needs the bind check
        daq = _bind(issues, settings.live, scan) if scan.source == "daq" else None
        if not issues.has_error():
            plan = _plan(issues, scan, daq, runs)
    return Planned(finish(issues), plan)


def _given(value: Any) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


def _check_scan(issues: Issues, settings: Settings, engine: Engine, part: Any) -> _Scan:
    """The Scan card: each field's type, the required ones, the source's DAQ, the folder (an existing folder inside
    the roots), the template (one file name, one ``%d``), the output folder (inside the roots), the scan (one that
    exists; none, an unlinked session: a warning)."""

    scan = _Scan()
    if not isinstance(part, Mapping):
        issues.error(
            [SCAN], "missing" if part is None else "dict_type", REQUIRED if part is None else "Must be an object"
        )
        scan.failed |= set(SCAN_FIELDS)
        return scan
    part = cast(Mapping[str, Any], part)
    for name in part:
        if name not in SCAN_FIELDS:
            issues.error([SCAN, name], "extra_forbidden", f"{quoted(name)} is not a field of the Scan card")
    for name, (_, adapter) in SCAN_FIELDS.items():
        value = part.get(name)
        if name in SCAN_REQUIRED and not _given(value):
            issues.error([SCAN, name], "missing", REQUIRED)
            scan.failed.add(name)
            continue
        if name in SCAN_OPTIONAL and not _given(value):
            continue
        try:
            setattr(scan, name, adapter.validate_python(value))
        except ValidationError as error:
            first = error.errors()[0]
            issues.error([SCAN, name], str(first.get("type", "invalid")), field_message(first))
            scan.failed.add(name)
    if scan.source == "daq" and not _given(scan.daq_id) and "daq_id" not in scan.failed:
        issues.error([SCAN, "daq_id"], "missing", REQUIRED)
        scan.failed.add("daq_id")
    elif scan.source == "notify" and _given(scan.daq_id):
        issues.error([SCAN, "daq_id"], "not_for_source", "Only for a DAQ source")
        scan.failed.add("daq_id")
    if scan.folder is not None:
        missing = DAQ_MAKES_FOLDER if scan.source == "daq" else None  # the DAQ makes its folder when it first writes
        scan.real_folder = check_folder(issues, settings, [SCAN, "folder"], "Folder", scan.folder, missing=missing)
        if scan.real_folder is None:
            scan.failed.add("folder")
    template = scan.filename_template
    if template is not None:
        problem = _template_problem(template)
        if problem is not None:
            issues.error([SCAN, "filename_template"], problem[0], problem[1])
            scan.failed.add("filename_template")
    if scan.output_folder is not None:
        output = inside_roots(issues, settings, [SCAN, "output_folder"], "Output folder", scan.output_folder)
        if output is None:
            scan.failed.add("output_folder")
        else:
            check_not_empty(issues, [SCAN, "output_folder"], "Output folder", output, ignored=RUN_FOLDER_NAMES)
    if "scan_number" not in scan.failed:
        _check_scan_number(issues, engine, scan.scan_number)
    return scan


def _template_problem(template: str) -> tuple[str, str] | None:
    """Why ``template`` (given: a blank one is none) is not a live run's file name template (``live.json``'s rule,
    ``jobs.live``): white space at its ends, a path, not exactly one ``%d``."""
    if padded(template):
        return "path_not_allowed", "Has white space at its start or end: remove it"
    if "/" in template:
        return "not_a_file_name", "Must be a file name, not a path"
    if template.count("%d") != 1:
        return "placeholders", "Must have exactly one %d, each file's number"
    return None


def _check_scan_number(issues: Issues, engine: Engine, scan_number: int | None) -> None:
    from laueportal.scans.models import Metadata

    if scan_number is None:
        issues.warning([SCAN, "scan_number"], "unlinked", "No scan: this creates an unlinked live session")
        return
    with Session(engine) as session:
        if session.get(Metadata, scan_number) is None:
            issues.error([SCAN, "scan_number"], "source_not_found", f"Scan {scan_number} was not found")


def _part(issues: Issues, name: str, value: Any) -> Mapping[str, Any] | None:
    """A program part: its form without the fields the Scan card sets (each one sent refused, ``set_by_scan``), or
    ``None`` when it is off (null, absent) or not an object."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        issues.error([name], "dict_type", "Must be an object or null")
        return None
    part = cast(Mapping[str, Any], value)
    for key in part:
        if key in SCAN_SETS:
            issues.error([name, key], "set_by_scan", "Set by the Scan card")
    return {key: item for key, item in part.items() if key not in SCAN_SETS}


def _scan_values(scan: _Scan, kind: programs.ProgramKind) -> dict[str, Any]:
    """The run fields the Scan card sets. A follower's ``input_path`` and ``reconstruction_id`` are its wire
    run's, set once that run exists; it is validated on the folder (``live.json``'s folder is its wire run's)."""
    folder = os.fspath(scan.real_folder) if scan.real_folder is not None else scan.folder
    output = scan.output_folder or ""
    values: dict[str, Any] = {
        "scan_number": scan.scan_number,
        "author": scan.author,
        "input_path": folder or "",
        "filename_prefixes": [] if scan.filename_template is None else [scan.filename_template],
        "output_path_template": os.path.join(output, RUN_FOLDERS[kind]) if output.strip() else "",
    }
    if kind == "lauego":
        values["reconstruction_id"] = None
    return values


def _check_run(issues: Issues, settings: Settings, kind: programs.ProgramKind, form: Mapping[str, Any]) -> Checked:
    """One run's checks: the live model's rules, its files (inside the roots, existing, and loading as lauelab reads
    them), its output folder, and a wire run's depths and skips."""

    checked = check_form(programs.LIVE_KINDS[kind], form, settings, issues)
    values = checked.values
    if values is None:
        return checked
    files = ("geometry_file",) if kind == "wire" else ("geometry_file", "crystal_file", "mask_file")
    sizes = {
        name: status.st_size for name in files if (status := check_file(issues, settings, checked, name)) is not None
    }
    _check_loads(issues, checked, kind, sizes)
    check_output(issues, settings, checked)
    if isinstance(values, WireLiveParamsV1):
        check_depths(issues, checked, values)
        check_skips(issues, checked, values)
    return checked


def _check_loads(issues: Issues, checked: Checked, kind: programs.ProgramKind, sizes: Mapping[str, int]) -> None:
    """A live create check: the geometry loads as the run reads it (a wire run's with its wire section and detector 0),
    and an indexing run's crystal; checked once the file passed ``check_file`` (``sizes``: each such file's size, from
    its stat), and never parsed when it is larger than ``science.calibration.MAX_CALIBRATION_BYTES`` (``too_large``):
    by that stat, or by the read itself (``science.calibration``: one descriptor, at most the bound + 1 byte read)."""
    from laueportal.science.calibration import (  # lauelab, only here
        CalibrationProblem,
        crystal_problem,
        geometry_problem,
        size_problem,
    )

    values = checked.values
    assert values is not None
    loads: list[tuple[str, Callable[[str], CalibrationProblem | None]]] = [
        ("geometry_file", lambda path: geometry_problem(path, wire=kind == "wire"))
    ]
    if kind == "lauego":
        loads.append(("crystal_file", crystal_problem))
    for name, problem_of in loads:
        if not checked.usable(name) or issues.has_error(name) or name not in sizes:
            continue
        too_large = size_problem(sizes[name])
        if too_large is not None:
            issues.error([name], "too_large", too_large)
            continue
        problem = problem_of(getattr(values, name))
        if problem is not None:
            issues.error([name], problem.code, problem.message)


def _report(issues: Issues, part: str, found: Sequence[ProblemItem], scan: _Scan) -> None:
    """A run's issues as the session's: on its part (``[part, *loc]``), or, for a field the Scan card sets, on the
    Scan card's field, once (none when that field has an error of its own: its own check says why)."""
    for issue in found:
        name = issue.loc[0] if issue.loc else None
        if not isinstance(name, str) or name not in SCAN_SETS:
            loc = [part, *issue.loc] if issue.loc else []
            issues.append(issue.model_copy(update={"loc": loc}))
            continue
        target = SCAN_SETS[name]
        loc = [SCAN, target] if target is not None else [part, name]
        if target in scan.failed:
            continue
        if any(
            list(known.loc) == loc and (known.severity, known.code) == (issue.severity, issue.code) for known in issues
        ):
            continue
        issues.append(issue.model_copy(update={"loc": loc}))


def _bind(issues: Issues, settings: LiveSettings, scan: _Scan) -> LiveDaq | None:
    """Start's DAQ bind check (``jobs.daq.bind_daq``, one DM call): ``live.json``'s ``daq``, or ``None`` and an error
    on ``daq_id``. DM's silence is a 503 problem, and a mount that does not answer the probe's (``NotResponding``)."""
    assert scan.daq_id is not None and scan.real_folder is not None
    try:
        return bind_daq(settings, scan.daq_id, os.fspath(scan.real_folder))
    except DaqRefused as refused:
        issues.error([SCAN, "daq_id"], "daq_refused", str(refused))
    except DaqUnavailable as silent:
        raise ProblemError(
            503,
            "DM unavailable",
            str(silent),
            problem_type=DM_UNAVAILABLE,
            headers={"Retry-After": str(DM_RETRY_SECONDS)},
        ) from None
    return None


def _check_seen_daq(issues: Issues, scan: _Scan, daqs: DaqListAnswer | None) -> None:
    """Validate's DAQ check, with no DM call (the module's docstring): the folder inside the DAQ's destination as the
    DAQ list last saw it (the bind check's refusal, ``daq_refused``); without the list's record of the DAQ, a warning
    that Start checks it."""
    assert scan.daq_id is not None and scan.real_folder is not None
    loc = [SCAN, "daq_id"]
    record = next((daq for daq in daqs.daqs if daq.id == scan.daq_id), None) if daqs is not None else None
    if record is None:
        issues.warning(loc, "checked_at_start", "Checked with DM at Start")
        return
    try:
        inside_destination(record, os.fspath(scan.real_folder))
    except DaqRefused as refused:
        issues.error(loc, "daq_refused", str(refused))
    except PathNotAllowed as refused:
        issues.error(loc, "daq_refused", f"The DAQ's destination: {refused.problem.detail}")
    except probe.NotResponding:
        issues.error(loc, "not_responding", "The DAQ's folders are not responding: their file system may be hung")


def _plan(issues: Issues, scan: _Scan, daq: LiveDaq | None, runs: Mapping[str, Checked]) -> LiveSessionPlan | None:
    """The session's runs: each run's parameters and ``live.json`` (a follower's is made once its wire run exists)."""
    assert scan.source is not None and scan.real_folder is not None
    wire_checked, lauego_checked = runs.get("reconstruction"), runs.get("indexing")
    wire = cast(WireLiveParamsV1 | None, wire_checked.params if wire_checked else None)
    lauego = cast(LaueGoLiveParamsV1 | None, lauego_checked.params if lauego_checked else None)
    first = wire or lauego
    if first is None:  # no error, so every run's parameters were built
        return None
    try:
        live = LiveDocument(
            source=scan.source,
            daq=daq,
            folder=os.fspath(scan.real_folder),
            filename_template=live_template(first.filename_prefixes),
        )
    except ValidationError as error:  # the Scan card's own checks hold live.json's rules; never a 500
        issues.error([SCAN], "invalid", field_message(error.errors()[0]))
        return None
    return LiveSessionPlan(
        wire=(wire, live) if wire is not None else None,
        indexing=(lauego, None if wire is not None else live) if lauego is not None else None,
    )


# ---------------------------------------------------------------------------
# Start


def start(form: Any, *, key: str, settings: Settings, engine: Engine, lock: threading.Lock) -> CreatedLiveRuns:
    """Start a live session (the module's docstring; ``POST /live-runs``): its runs created and queued together
    under ``lock``, or a problem (422, 409, 503) with nothing created."""

    store = IdempotencyStore(engine, route=ROUTE, other_body=_other_body)
    claim = store.claim(key, form)
    if claim.replay is not None:
        return CreatedLiveRuns.model_validate(claim.replay)
    try:  # the validation only reads: whatever it raises, nothing was created
        plan = _planned(form, settings=settings, engine=engine)
    except BaseException:
        _release(store, claim)
        raise
    with lock:  # ONE lock across the check, the creations and the queueing (the module's docstring)
        try:
            _refuse_an_open_session(engine)
        except BaseException:
            _release(store, claim)
            raise
        try:
            runs = create_live_session(plan, settings=settings, engine=engine)
        except LiveSessionNotCreated as refused:  # run creation's own refusal, before it saved anything
            _release(store, claim)
            message = str(refused)
            raise ProblemError(
                422, "Run not created", message, errors=[{"loc": [], "code": "not_creatable", "message": message}]
            ) from refused
        # Any other failure keeps the key claimed: a run may be saved, and a retry must not make a second session.
    answer = CreatedLiveRuns(runs=runs)
    try:
        store.keep(claim, answer.model_dump(mode="json"))
    except SQLAlchemyError:
        labels = ", ".join(run.run.label for run in runs)
        logger.warning("The answer for live session %s could not be kept with its Idempotency-Key", labels)
    return answer


def _planned(form: Any, *, settings: Settings, engine: Engine) -> LiveSessionPlan:
    """The session's runs, or a 422 with the issues, errors first (as a batch create's)."""
    planned = plan_session(form, settings=settings, engine=engine)
    if planned.plan is not None:
        return planned.plan
    count = issue_count(planned.issues)
    issues = sorted(planned.issues, key=lambda issue: issue.severity != "error")[:MAX_PROBLEM_ITEMS]
    detail = problems_detail(count)
    if count > len(issues):
        detail = f"{detail} (the first {len(issues)} of {count} are listed)"
    raise ProblemError(422, "Validation failed", detail, errors=issues)


def _release(store: IdempotencyStore, claim: Claim) -> None:
    try:
        store.release(claim)
    except SQLAlchemyError:
        logger.warning("An Idempotency-Key could not be released; it stays claimed for 24 h", exc_info=True)


def _refuse_an_open_session(engine: Engine) -> None:
    """A 409 naming the open live run while one is Queued or Running (``check_one_live_session``)."""
    with Session(engine) as session:
        try:
            check_one_live_session(session)
        except LiveSessionOpen as open_session:
            indexing_id = session.scalar(select(IndexingRun.id).where(IndexingRun.job_id == open_session.job_id))
            reconstruction_id = session.scalar(
                select(ReconstructionRun.id).where(ReconstructionRun.job_id == open_session.job_id)
            )
            run = RunReference.of(indexing_id=indexing_id, reconstruction_id=reconstruction_id)
            open_run = OpenLiveRun(job_id=open_session.job_id, run=run, status=RunStatus.of(open_session.status))
            raise LiveSessionRefused(open_run, STATUS_NAMES[open_session.status]) from None


class LiveSessionRefused(ProblemError):
    """A Start while a live session is open: a 409 ``LiveRunsConflict`` naming its oldest open run."""

    def __init__(self, open_run: OpenLiveRun, status: str) -> None:
        name = f"{open_run.run.label} (job {open_run.job_id})" if open_run.run else f"Job {open_run.job_id}"
        super().__init__(
            409,
            "Live session open",
            f"{name} is {status}: one live session at a time. Stop it before starting another.",
            problem_type=LIVE_SESSION_OPEN,
        )
        self.problem = LiveRunsConflict(**self.problem.model_dump(), open_run=open_run)


class LiveKeyConflict(ProblemError):
    """The 409 of a Start's ``Idempotency-Key`` used with another body (``jobs.idempotency``'s rules): with the key's
    session created, ``created`` is its kept answer and the type ``key-created``."""

    def __init__(self, detail: str, *, created: CreatedLiveRuns | None = None) -> None:
        from laueportal.jobs.idempotency import KEY_CREATED

        problem_type = "about:blank" if created is None else KEY_CREATED
        super().__init__(409, "Idempotency-Key conflict", detail, problem_type=problem_type)
        self.problem = LiveRunsConflict(**self.problem.model_dump(), created=created)


def _other_body(answer: Mapping[str, Any] | None) -> ProblemError:
    if answer is None:
        return LiveKeyConflict("This Idempotency-Key was used with another body; a new submission needs a new key.")
    created = CreatedLiveRuns.model_validate(answer)
    labels = ", ".join(f"{run.run.label} (job {run.job_id})" for run in created.runs)
    return LiveKeyConflict(
        f"This Idempotency-Key already started live session {labels} with other values: open it, or start a new "
        "submission (a new key) to start another.",
        created=created,
    )


# ---------------------------------------------------------------------------
# The DAQ list


def shown_names(record: DaqRecord) -> tuple[str, str]:
    """What the Live page's option shows of DAQ ``record`` (``daqLabel``, ``frontend/src/features/live/live-form.ts``):
    its experiment, and its data directory's last two names (``e1/raw`` for ``/detector/current/e1/raw``)."""
    names = [name for name in (record.data_directory or "").split("/") if name]
    return record.experiment, "/".join(names[-2:])


def excluded(record: DaqRecord, exclude: Sequence[str]) -> bool:
    """Whether ``GET /live/daqs`` leaves the DAQ ``record`` out: what its option shows (``shown_names``) contains one
    of ``exclude`` (``[live] daq_name_exclude``, no blank entry), ignoring case; a name in a folder its option does
    not show never counts. The kept answer (``DaqList``), Validate's check and Start's bind check still take it."""
    shown = [text.casefold() for text in shown_names(record)]
    return any(word.casefold() in text for word in exclude for text in shown)


def daq_list(answer: DaqListAnswer, *, time_left: float, exclude: Sequence[str] = ()) -> LiveDaqList:
    """The DAQ list's shared answer (``jobs.daq.DaqList``) as ``GET /live/daqs`` sends it, but the DAQs ``exclude``
    leaves out (``excluded``), with the time it is still
    kept (``time_left``: the page asks again only after it); a failed call's is a 503
    ``urn:laueportal:problem:dm-unavailable`` with its reason, ``Retry-After`` the time before DM may be asked again
    (``time_left``, ``DaqList.time_left``: a failure is kept as long as an answer, twice as long for each failure in a
    row; asking sooner gets the same answer, without a call), at least a second."""
    if answer.error is not None:
        raise ProblemError(
            503,
            "DM unavailable",
            answer.error,
            problem_type=DM_UNAVAILABLE,
            headers={"Retry-After": str(max(1, math.ceil(time_left)))},
        )
    return LiveDaqList(
        daqs=[
            LiveDaqEntry(
                id=record.id,
                experiment=record.experiment,
                status=record.status,
                data_directory=record.data_directory,
                storage_directory=record.storage_directory,
                dest_directory=record.dest_directory,
                destination=destination(record),
            )
            for record in answer.daqs
            if not excluded(record, exclude)
        ],
        failed=list(answer.failed),
        checked_at=answer.checked_at,
        kept_seconds=round(time_left, 1),
    )
