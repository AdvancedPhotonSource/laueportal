"""The validation of a new run's parameters, shared by ``indexing.validation`` (``lauego``) and
``reconstruction.validation`` (``wire``), which add each program's cross-field and context checks.

A client sends one parameter object, the create form's values; the answer is a list of issues, each
``{loc, severity, code, message}`` (``core.problems.ProblemItem``): ``loc[0]`` names the field (``["box_size"]``), and
an item's index may follow (a template, ``["filename_prefixes", 1]``; a rule of the whole list, such as wire's one
``%d``, names only the field); an empty ``loc`` is a rule of the whole form. ``severity`` is ``error`` (the run cannot
be created) or ``warning`` (it can), ``code`` is stable and ``message`` is for people, in plain words that do not repeat
the field's label (``core.problems.field_message``; the form's summary adds it): a required form field left blank
(empty, white space, null, or a list of blanks) is ``missing``, "Required". Nothing is written: a validation reads the
database and the file system only.

- **The model's rules** (``check_form``): ``jobs.programs.build_params`` builds the parameter model from the form;
  each problem it reports (pydantic's ``type`` as the code; ``set_by_portal`` for a config or derived value,
  ``extra_forbidden`` for an unknown key) becomes an error. A field with a problem is *failed*: for the checks that
  follow, the form is built again with a stand-in for each failed field (its default), so the other fields are still
  checked, every field at once; a check that reads a failed field is skipped.
- **The context checks** (here, used by both programs): every path is written without white space at its start or end,
  absolute, inside the roots (``files.roots.resolve_inside_roots``) and exists; the output folder is inside the roots
  and takes the run ID (no warning when it does not exist yet: run creation makes it), and the folder it is made in is
  not a file (run creation would fail, ``Not a directory``); on a remote target, the output folder stays under
  ``<files.runs_root>/analysis/`` and the Output XML is a relative name (``remote.plan.build_plan``'s rules); the target
  is one the deployment offers, and an ALCF run's inputs are inside the experiment folder (what submission requires);
  Author is required; a run without a source is a warning ("unlinked"); at most ``execution.max_run_inputs`` inputs; and
  input resolution finds the files the run would read (the run's creation resolves them the same way). Each path's
  resolution and first stat run through ``files.probe``: one whose file system does not answer is the error
  ``not_responding``; the input folder's listing follows in the request's thread.
- **Only what the parameters carry** is checked: a clone's source indexing run is not among them (``lauego/v1``
  has no indexing ID), so prefill answers 404 for an unknown one.

The answer is bounded like a problem's errors (``core.problems.bounded``), at most ``MAX_ISSUES`` issues.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast, get_args

from pydantic import ValidationError

from laueportal.core.db import SQLITE_INTEGER_MAX
from laueportal.core.params import ProgramParams
from laueportal.core.problems import REQUIRED, ProblemError, ProblemItem, bounded, field_message
from laueportal.core.settings import ExecutionSettings, Settings
from laueportal.core.text import quoted
from laueportal.files import probe
from laueportal.files.inputs import WorkflowValidationError, format_output_path, requested_input_count
from laueportal.files.roots import PathNotAllowed, real_path, resolve_inside_roots
from laueportal.indexing.laue_matching.params import LaueMatchingParamsV1
from laueportal.indexing.lauego.live_params import LaueGoLiveParamsV1
from laueportal.indexing.lauego.params import LaueGoParamsV1
from laueportal.jobs import programs
from laueportal.jobs.backends import is_remote
from laueportal.reconstruction.wire.live_params import WireLiveParamsV1
from laueportal.reconstruction.wire.params import WireParamsV1

Severity = Literal["error", "warning"]
Params = LaueGoParamsV1 | LaueMatchingParamsV1 | WireParamsV1
LiveParams = LaueGoLiveParamsV1 | WireLiveParamsV1
"""A program's parameters (``jobs.programs.PROGRAMS``)."""

SQLITE_INTEGER_MIN = -SQLITE_INTEGER_MAX - 1
"""The least integer SQLite's INTEGER holds."""
MAX_ISSUES = 50
"""The most issues a validation answers; the last one then says how many more there were (``more_issues``)."""
MORE_ISSUES = "more_issues"

STAND_INS: dict[str, Any] = {
    "scan_number": None,
    "reconstruction_id": None,
    "input_path": "/",
    "output_path_template": "/%d",
    "filename_prefixes": ["*"],
    "scan_points": "1",
    "depth_range": None,
    "geometry_file": "/",
    "crystal_file": "/",
    "mask_file": None,
    "output_xml": None,
    "depth": None,
    "forward_cache": "cache",
}
"""A valid value of each required form field whose configured default may be missing or refused (a blank scan
selection, a source), put in place of a failed field so that the other fields can still be checked."""
LIVE_STAND_INS: dict[str, Any] = {**STAND_INS, "filename_prefixes": ["%d"]}
"""``STAND_INS`` for a live schema, whose one template has exactly one ``%d``."""

TRANSFERRED = "crux"
"""The target whose inputs are transferred (Globus, to ALCF's Crux) from inside the experiment folder."""
TRANSFERRED_INPUTS = ("input_path", "geometry_file", "crystal_file", "mask_file")
"""The fields naming what a transferred run reads (``remote.plan.build_plan``: the manifest's inputs and these
files)."""
REMOTE_OUTPUT_FOLDER = "analysis"
"""A remote run's output folder stays under ``<files.runs_root>/analysis/`` (``bdp.yaml``'s experiment directory,
``remote.plan.build_plan``)."""


class Issues(list[ProblemItem]):
    """The issues found so far, in the order found."""

    def add(self, severity: Severity, loc: Sequence[str | int], code: str, message: str) -> None:
        self.append(ProblemItem(loc=list(loc), message=message, code=code, severity=severity))

    def error(self, loc: Sequence[str | int], code: str, message: str) -> None:
        self.add("error", loc, code, message)

    def warning(self, loc: Sequence[str | int], code: str, message: str) -> None:
        self.add("warning", loc, code, message)

    def has_error(self, *names: str) -> bool:
        """Whether a field among ``names`` (all fields when none is given) has an error."""
        return any(issue.severity == "error" and (not names or (issue.loc and issue.loc[0] in names)) for issue in self)


@dataclass(frozen=True)
class Validated:
    """A validation's outcome: the issues, and the run's parameters when the model accepted the form."""

    params: ProgramParams | None
    """The parameters the run would be created with; ``None`` when a field has a problem of its own."""
    issues: list[ProblemItem]

    @property
    def ok(self) -> bool:
        """Whether the run can be created: no issue is an error (warnings do not stop it)."""
        return self.params is not None and not any(issue.severity == "error" for issue in self.issues)


@dataclass(frozen=True)
class Checked:
    """The form after the model's rules (``check_form``)."""

    params: Params | LiveParams | None
    """The run's parameters, when every field passed its own rules."""
    values: Params | LiveParams | None
    """``params``, or the form with a stand-in for each failed field, for the other checks (``None`` if even that
    could not be built)."""
    failed: frozenset[str]
    """The fields (and unknown keys) that have a problem of their own."""

    def usable(self, *names: str) -> bool:
        """Whether a check reading ``names`` can run: the values exist and none of ``names`` failed."""
        return self.values is not None and not self.failed.intersection(names)


def title(params: ProgramParams, name: str) -> str:
    """The field's form label (its JSON Schema ``title``)."""
    return type(params).model_fields[name].title or name


def _blank(value: Any) -> bool:
    """Left blank: null, blank text, or a list of none but blank items (an empty Filename)."""
    if isinstance(value, (list, tuple)):
        return all(_blank(item) for item in cast(Sequence[Any], value))
    return value is None or (isinstance(value, str) and not value.strip())


def _required(model: type[ProgramParams], name: str) -> bool:
    """Whether ``name`` is a form field the form must fill: required by the model, and not nullable."""
    field = model.model_fields[name]
    nullable = field.annotation is None or type(None) in get_args(field.annotation)
    return field.is_required() and not nullable and model.field_metadata(name)["x-source"] == "form"


def _issue(model: type[ProgramParams], error: Mapping[str, Any]) -> ProblemItem:
    loc = [part if isinstance(part, int) else str(part) for part in error.get("loc", ())]
    name = loc[0] if loc else None
    field = model.model_fields.get(name) if isinstance(name, str) else None
    label = (field.title or name) if field is not None else None
    code = str(error.get("type", "invalid"))
    blank = "input" in error and _blank(error["input"])
    if field is not None and len(loc) == 1 and blank and _required(model, str(name)):
        code = "missing"  # left blank, whatever the type's own complaint ("" is not an integer)
    if code == "set_by_portal":
        message = f"{label} is set by the portal ({(error.get('ctx') or {}).get('source')}), not by the form"
    elif code == "extra_forbidden":
        message = f"{quoted(name)} is not a parameter of {model.model_fields['schema_id'].default}"
    else:  # plain words without the label, which the form's summary adds (``core.problems.field_message``)
        message = field_message({**error, "type": code})
    return ProblemItem(loc=loc, message=message, code=code, severity="error")


def _beyond_sqlite(params: ProgramParams, names: Iterable[str]) -> list[dict[str, Any]]:
    """The problems (in pydantic's form) of the integers among ``names`` that SQLite's INTEGER cannot hold
    (``core.db.SQLITE_INTEGER_MAX``): a source lookup with one would fail, and so would the run's insert."""

    problems: list[dict[str, Any]] = []
    for name in names:
        value = getattr(params, name)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if value > SQLITE_INTEGER_MAX:
            problems.append(
                {
                    "type": "less_than_equal",
                    "loc": (name,),
                    "msg": f"Input should be at most {SQLITE_INTEGER_MAX}",
                    "ctx": {"le": SQLITE_INTEGER_MAX},
                    "input": value,
                }
            )
        elif value < SQLITE_INTEGER_MIN:
            problems.append(
                {
                    "type": "greater_than_equal",
                    "loc": (name,),
                    "msg": f"Input should be at least {SQLITE_INTEGER_MIN}",
                    "ctx": {"ge": SQLITE_INTEGER_MIN},
                    "input": value,
                }
            )
    return problems


def check_form(kind: programs.AnyProgramKind, form: Mapping[str, Any], settings: Settings, issues: Issues) -> Checked:
    """The model's rules: build the parameters from ``form`` (``jobs.programs.build_params``; a live kind's too,
    the Live page's) and report each problem as an error, and an integer SQLite cannot store (``_beyond_sqlite``)
    too. The fields that fail get stand-ins (their default: ``STAND_INS``, the model's, or the configured one), so the
    values for the other checks can still be built."""

    model = programs.MODELS[kind]
    stand_ins = STAND_INS if kind in programs.PROGRAMS else LIVE_STAND_INS
    form_fields = set(model.fields_from("form"))
    defaults = programs.form_defaults(kind, settings)
    values = dict(form)
    failed: set[str] = set()
    reported: set[tuple[Any, ...]] = set()
    ordered = [name for name in model.model_fields if name in form_fields]
    for _ in range(4):  # the portal's values (refused first), the fields' own rules, the integers, with stand-ins
        try:
            built = cast(Params | LiveParams, programs.build_params(kind, values, settings))
        except ValidationError as error:
            problems = error.errors()
        else:
            problems = _beyond_sqlite(built, ordered)
            if not problems:
                return Checked(None if failed else built, built, frozenset(failed))
        for problem in problems:  # each new one; a refused stand-in is not the form's problem
            loc = tuple(problem.get("loc", ()))
            key = (loc, problem.get("type"), str(problem.get("msg")))
            if (not loc or str(loc[0]) not in failed) and key not in reported:
                reported.add(key)
                issues.append(_issue(model, problem))
        names = {str(problem["loc"][0]) for problem in problems if problem.get("loc")}
        if names & failed or not names:  # a stand-in was refused, or a rule of the whole form: no values to check
            break
        failed |= names
        values = {name: value for name, value in values.items() if name not in names}  # unknown keys stay out
        for name in names & form_fields:
            field = model.model_fields[name]
            if name in stand_ins:
                values[name] = stand_ins[name]
            elif not field.is_required():
                values[name] = field.default
            elif name in defaults:
                values[name] = defaults[name]
    if not issues.has_error():  # never "not ok" without a reason
        issues.error([], "invalid", f"The parameters cannot be checked as {model.model_fields['schema_id'].default}")
    return Checked(None, None, frozenset(failed))


def finish(issues: Iterable[ProblemItem]) -> list[ProblemItem]:
    """The issues as answered: each bounded (``core.problems.bounded``), at most ``MAX_ISSUES``."""

    found = list(issues)
    if len(found) > MAX_ISSUES:
        kept, rest = found[: MAX_ISSUES - 1], found[MAX_ISSUES - 1 :]
        severity: Severity = "error" if any(issue.severity == "error" for issue in rest) else "warning"
        more = ProblemItem(loc=[], code=MORE_ISSUES, severity=severity, message=f"{len(rest)} more issues")
        found = [*kept, more]
    return [bounded(issue) for issue in found]


def issue_count(issues: Sequence[ProblemItem]) -> int:
    """How many issues a validation found: those ``finish`` answered, the ``more_issues`` item counted as the
    issues it stands for."""

    more = next((issue for issue in issues if issue.code == MORE_ISSUES), None)
    if more is None:
        return len(issues)
    return len(issues) - 1 + int(more.message.split(" ", 1)[0])


# --- the context checks both programs make ---------------------------------------------------------------------


PATH_WIDGETS = frozenset({"folder", "file", "output-folder"})
"""The widgets of the path fields (``x-widget``)."""


def path_fields(params: ProgramParams) -> tuple[str, ...]:
    """The fields that hold a path (``PATH_WIDGETS``), in the model's order."""
    model = type(params)
    return tuple(name for name in model.model_fields if model.field_metadata(name).get("x-widget") in PATH_WIDGETS)


def padded(path: str) -> bool:
    """Whether ``path`` has white space at its start or end. ``files.roots.resolve_inside_roots`` strips it before it
    checks, while the parameters keep it, so the run would read (or be made at) another path than the one checked."""
    return path != path.strip()


def inside_roots(issues: Issues, settings: Settings, loc: Sequence[str | int], label: str, path: str) -> Path | None:
    """The real path of ``path``, or ``None`` and an error when it is blank (``missing``), has white space at its
    start or end, or is not absolute, or is not inside the roots."""

    if not path.strip():
        issues.error(loc, "missing", REQUIRED)
        return None
    if padded(path):
        issues.error(loc, "path_not_allowed", f"{label} has white space at its start or end: remove it")
        return None
    try:
        return resolve_inside_roots(path, settings)[1]
    except PathNotAllowed as refused:
        issues.error(loc, "path_not_allowed", f"{label}: {refused.problem.detail}")
    except probe.NotResponding:
        _not_responding(issues, loc, label)
    return None


def _not_responding(issues: Issues, loc: Sequence[str | int], label: str) -> None:
    issues.error(loc, "not_responding", f"{label} is not responding: its file system may be hung")


def _real(issues: Issues, loc: Sequence[str | int], label: str, path: str | os.PathLike[str]) -> Path | None:
    """A configured folder's ``Path(path).resolve()`` through ``files.probe`` (``files.roots.real_path``: one mount
    at a time, requested checks), or ``None`` and an error when its file system does not answer (or it needs too
    many links). A path from the form goes through ``_inside``."""

    try:
        return real_path(path, requested=True)
    except PathNotAllowed as refused:
        issues.error(loc, "path_not_allowed", f"{label}: {refused.problem.detail}")
        return None
    except probe.NotResponding:
        _not_responding(issues, loc, label)
        return None


def _inside(issues: Issues, settings: Settings, loc: Sequence[str | int], label: str, path: str) -> Path | None:
    """A path from the form resolved inside the roots (``resolve_inside_roots``), or ``None``: with an error when
    its file system does not answer, and without one when the roots refuse it (its own field's check says why)."""

    try:
        return resolve_inside_roots(path, settings)[1]
    except PathNotAllowed:
        return None
    except probe.NotResponding:
        _not_responding(issues, loc, label)
        return None


def _status(
    issues: Issues, loc: Sequence[str | int], label: str, path: Path, *, raise_missing: bool = False
) -> os.stat_result | None:
    """``path``'s status, through ``files.probe``, or ``None`` and an error: ``not_found``, ``unreadable`` when the
    server cannot reach it (a folder on its way that the server may not open: ``Path.is_file`` and ``is_dir`` raise
    then, since Python 3.12; ``files.browser`` reads it the same way), or ``not_responding``. ``raise_missing``: a
    path that is not there raises ``FileNotFoundError`` for the caller to report."""

    try:
        return probe.bounded(path, path.stat, requested=True)
    except probe.NotResponding:
        _not_responding(issues, loc, label)
    except FileNotFoundError:
        if raise_missing:
            raise
        issues.error(loc, "not_found", f"{label} not found")
    except NotADirectoryError:
        issues.error(loc, "not_found", f"{label} not found")
    except OSError as error:
        issues.error(loc, "unreadable", f"{label} is not accessible ({error.strerror or type(error).__name__})")
    return None


def check_file(issues: Issues, settings: Settings, checked: Checked, name: str) -> os.stat_result | None:
    """A file the run reads (geometry, crystal, mask): inside the roots and an existing file. Returns
    its status (the stat just made, through the probe) when it is one, else ``None``."""

    if not checked.usable(name) or checked.values is None:
        return None
    value = getattr(checked.values, name)
    if value is None:
        return None
    label = title(checked.values, name)
    path = inside_roots(issues, settings, [name], label, value)
    if path is None:
        return None
    status = _status(issues, [name], label, path)
    if status is None:
        return None
    if stat.S_ISDIR(status.st_mode):
        issues.error([name], "not_a_file", f"{label} is a folder, not a file")
    elif not stat.S_ISREG(status.st_mode):
        issues.error([name], "not_found", f"{label} not found")
    else:
        return status
    return None


def check_folder(
    issues: Issues,
    settings: Settings,
    loc: Sequence[str | int],
    label: str,
    path: str,
    *,
    missing: str | None = None,
) -> Path | None:
    """A folder a run takes its files from as they arrive (a live run's): inside the roots and an existing folder, which
    may still be empty. Its real path, or ``None`` after an error. ``missing``: a folder that is not there yet is this
    warning (``not_found``) instead of an error, and its real path is still returned, resolved through its nearest
    existing folder (``files.roots.resolve_inside_roots``, so no link on its way leads it out of the roots): a DAQ
    makes its folder when it first writes (``jobs.live_session``)."""

    real = inside_roots(issues, settings, loc, label, path)
    if real is None:
        return None
    try:
        status = _status(issues, loc, label, real, raise_missing=missing is not None)
    except FileNotFoundError:
        assert missing is not None
        issues.warning(loc, "not_found", missing)
        return real
    if status is None:
        return None
    if not stat.S_ISDIR(status.st_mode):
        issues.error(loc, "not_a_folder", f"{label} must be a folder")
        return None
    return real


def check_not_empty(
    issues: Issues, loc: Sequence[str | int], label: str, folder: Path, *, ignored: re.Pattern[str] | None = None
) -> None:
    """An existing folder that holds an entry (a file, a folder, a hidden one) is a warning (``not_empty``); but a
    folder whose name ``ignored`` matches whole: a live session's output folder may hold earlier sessions' run folders
    (``rec_<ID>``, ``index_<ID>``, unique by ID), so only something else warns that runs could mix. The entries are
    read through ``files.probe``, up to the first that warns. A missing folder is no issue (run creation makes it, its
    parents too); one that is a file or lies under one is ``check_output``'s error, and one the server cannot read is
    left to the creation."""

    def holds_an_entry() -> bool:
        with os.scandir(folder) as entries:
            for entry in entries:
                if ignored is None or not (ignored.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False)):
                    return True
        return False

    try:
        holds = probe.bounded(folder, holds_an_entry, requested=True)
    except probe.NotResponding:
        _not_responding(issues, loc, label)
        return
    except OSError:
        return
    if holds:
        issues.warning(loc, "not_empty", "Not empty")


InputKind = Literal["folder", "catalog"]


def check_input_folder(
    issues: Issues, settings: Settings, checked: Checked, *, is_catalog: Callable[[Path], bool] | None = None
) -> InputKind | None:
    """The run's input (``input_path``): inside the roots, and a folder holding at least one file or, when
    ``is_catalog`` is given (indexing), a readable reconstruction catalog (``scan.h5``). Returns which, or ``None``
    after an error."""

    if not checked.usable("input_path") or checked.values is None:
        return None
    label = title(checked.values, "input_path")
    path = inside_roots(issues, settings, ["input_path"], label, checked.values.input_path)
    if path is None:
        return None
    status = _status(issues, ["input_path"], label, path)
    if status is None:
        return None
    if stat.S_ISREG(status.st_mode):
        if is_catalog is not None and is_catalog(path):
            return "catalog"
        wanted = "a folder of files or a reconstruction's scan.h5" if is_catalog is not None else "a folder"
        issues.error(["input_path"], "not_a_folder", f"{label} must be {wanted}")
        return None
    if not stat.S_ISDIR(status.st_mode):
        issues.error(["input_path"], "not_found", f"{label} not found")
        return None
    try:
        with os.scandir(path) as entries:
            has_file = any(entry.is_file() for entry in entries)
    except OSError as error:
        issues.error(["input_path"], "unreadable", f"{label} cannot be read ({error.strerror or 'error'})")
        return None
    if not has_file:
        issues.error(["input_path"], "no_files", f"{label} contains no files")
        return None
    return "folder"


def remote_target(issues: Issues, checked: Checked) -> bool:
    """Whether the run goes to a remote target this deployment offers (``check_backend`` comes first), so the
    remote rules apply."""
    values = checked.values
    return (
        values is not None
        and checked.usable("backend")
        and is_remote(values.backend)
        and not issues.has_error("backend")
    )


def check_output(issues: Issues, settings: Settings, checked: Checked) -> None:
    """The output folder (``output_path_template``, with its ``%d``): inside the roots, and formatted with the run ID
    as the run's creation formats it; on a remote target, under ``<files.runs_root>/analysis/`` once formatted and
    resolved (an error, as ``remote.plan.build_plan`` refuses any other at submission; also for a formatted folder
    the roots refuse, which only a link can do once the template passed). Whether it exists is not checked: run creation
    makes it; but the folder it is made in (its path before the run ID's placeholder) must not be a file, nor lie
    under one (``not_a_folder``)."""

    values = checked.values
    if not checked.usable("output_path_template") or values is None:
        return
    label = title(values, "output_path_template")
    template = values.output_path_template
    real = inside_roots(issues, settings, ["output_path_template"], label, template)
    if real is None:
        return
    try:
        run_directory = format_output_path(template, 1)  # as the run's creation does with its ID
    except WorkflowValidationError:
        issues.error(
            ["output_path_template"],
            "output_template",
            f"{label} cannot take the run ID: write a literal % as %%, and use no other % placeholder than its %d",
        )
        return
    if remote_target(issues, checked):
        analysis = _real(issues, ["output_path_template"], label, Path(settings.files.runs_root) / REMOTE_OUTPUT_FOLDER)
        if analysis is None:
            return
        try:
            run: Path | None = resolve_inside_roots(run_directory, settings)[1]
        except PathNotAllowed:  # the folder formatted with the ID leaves the roots (a link): so it leaves analysis
            run = None
        except probe.NotResponding:
            _not_responding(issues, ["output_path_template"], label)
            return
        if run is None or not run.is_relative_to(analysis) or run == analysis:
            issues.error(
                ["output_path_template"],
                "remote_output_folder",
                f"A remote run's {label} must be inside {analysis} (the experiment's analysis folder)",
            )
            return
    _check_output_parent(issues, label, _before_placeholder(real))


def _before_placeholder(template: Path) -> Path:
    """An output template's folders before the first that holds a ``%`` (the run ID's): where every run's output
    folder is made (the template itself when it has none)."""
    parts = template.parts
    fixed = next((index for index, part in enumerate(parts) if "%" in part), len(parts))
    return Path(*parts[:fixed])


def _check_output_parent(issues: Issues, label: str, folder: Path) -> None:
    """The folder a run's output folder is made in: an existing file there, or on its way, would fail the run's
    creation (``Not a directory``), so it is an error (``not_a_folder``). A missing one is made then; one the server
    cannot reach is left to the creation, which says why."""

    loc = ["output_path_template"]
    try:
        status = probe.bounded(folder, folder.stat, requested=True)
    except probe.NotResponding:
        _not_responding(issues, loc, label)
        return
    except NotADirectoryError:  # a file on the way: the kernel says so before it looks for what follows it
        issues.error(loc, "not_a_folder", "Lies under a file, not a folder")
        return
    except OSError:
        return
    if not stat.S_ISDIR(status.st_mode):
        issues.error(loc, "not_a_folder", f"{quoted(folder.name)} is a file, not a folder")


def check_backend(issues: Issues, settings: Settings, checked: Checked) -> None:
    """The execution target is one this deployment offers (``jobs.programs.execution_targets``, the form's "Run on"
    choices): any other is refused (an error), as submission refuses it."""

    values = checked.values
    if not checked.usable("backend") or values is None:
        return
    label = title(values, "backend")
    try:
        offered = [target["value"] for target in programs.execution_targets(settings)]
    except ProblemError as unusable:
        issues.error(["backend"], "backend_unavailable", f"{label}: {unusable.problem.detail}")
        return
    if values.backend not in offered:
        issues.error(
            ["backend"],
            "backend_not_offered",
            f"{label}: {quoted(values.backend)} is not offered by this deployment (it offers {', '.join(offered)})",
        )


def check_transferred_inputs(issues: Issues, settings: Settings, checked: Checked) -> None:
    """On ``crux`` (``TRANSFERRED``), the files the run reads (the input folder's, and the geometry, crystal and mask
    files) are inside the experiment folder (``files.runs_root``, which ``bdp.yaml``'s experiment directory must be):
    ``remote.plan.build_plan`` sends only those to ALCF and refuses any other at submission (an
    error on each field)."""

    values = checked.values
    if not remote_target(issues, checked) or values is None or values.backend != TRANSFERRED:
        return
    experiment = _real(issues, ["backend"], title(values, "backend"), settings.files.runs_root)
    if experiment is None:
        return
    for name in TRANSFERRED_INPUTS:
        value = getattr(values, name, None)
        if not checked.usable(name) or not isinstance(value, str) or padded(value) or not os.path.isabs(value):
            continue  # nothing to read, or the path's own error says why
        path = _inside(issues, settings, [name], title(values, name), value)
        if path is None:
            continue
        if not path.is_relative_to(experiment) or (path == experiment and name != "input_path"):
            issues.error(
                [name],
                "remote_input",
                f"An ALCF run reads only files inside {experiment} (the experiment folder): "
                f"{title(values, name)} is outside it",
            )


def check_author(issues: Issues, checked: Checked) -> None:
    """An author is required (an error when blank); the model's type stays optional (blank is null)."""

    if checked.usable("author") and checked.values is not None and checked.values.author is None:
        issues.error(["author"], "missing", REQUIRED)


def check_source_path(issues: Issues, label: str, source: str | None, input_path: str) -> None:
    """A warning when the input differs from the source's path."""

    if source and os.path.normpath(source) != os.path.normpath(input_path):
        issues.warning(["input_path"], "source_path_differs", f"{label} has a different source path ({source})")


def max_run_inputs(settings: Settings | None) -> int:
    """The most inputs a new run may read (``execution.max_run_inputs``; its default without settings)."""
    if settings is None:
        return cast(int, ExecutionSettings.model_fields["max_run_inputs"].default)
    return settings.execution.max_run_inputs


def too_many_inputs(params: Params, limit: int) -> str | None:
    """Why the run requests more than ``limit`` inputs, counted from its index ranges without expanding them
    (``files.inputs.requested_input_count``); ``None`` when it does not."""

    depths = params.depth_count if isinstance(params, (LaueGoParamsV1, LaueMatchingParamsV1)) else None
    count = requested_input_count(params.filename_prefixes, params.scan_point_count, depths)
    if count <= limit:
        return None
    sizes = title(params, "scan_points") + (f" × {title(params, 'depth_range')}" if depths is not None else "")
    return f"The run would read {count:,} inputs ({sizes}, for each file name); a run reads at most {limit:,}"


def check_input_count(issues: Issues, settings: Settings, params: Params) -> bool:
    """At most ``execution.max_run_inputs`` inputs (an error on Scan Indices, and Depth Indices when given),
    checked before input resolution expands the ranges. Returns whether the count is within it."""

    problem = too_many_inputs(params, max_run_inputs(settings))
    if problem is None:
        return True
    issues.error(["scan_points"], "too_many_inputs", problem)
    if isinstance(params, (LaueGoParamsV1, LaueMatchingParamsV1)) and params.depth_range is not None:
        issues.error(["depth_range"], "too_many_inputs", problem)
    return False


def check_creatable(params: Params, *, settings: Settings | None = None) -> None:
    """Run creation's own refusals (``WorkflowValidationError``), made for every caller, including those that do
    not validate first (the seed, the runbook): a path with white space at its start or end (``padded``), an output
    folder that is not absolute or (with ``settings``) not inside the roots, and more inputs than
    ``execution.max_run_inputs`` (its default without ``settings``)."""

    check_paths_creatable(params, settings=settings)
    problem = too_many_inputs(params, max_run_inputs(settings))
    if problem is not None:
        raise WorkflowValidationError(problem)


def check_paths_creatable(params: Params | LiveParams, *, settings: Settings | None = None) -> None:
    """``check_creatable``'s path refusals, which a live run's creation makes too (it reads no inputs at creation): a
    padded path, an output folder that is not absolute or (with ``settings``) not inside the roots."""

    for name in path_fields(params):
        value = getattr(params, name)
        if isinstance(value, str) and padded(value):
            raise WorkflowValidationError(f"{title(params, name)} has white space at its start or end")
    output = title(params, "output_path_template")
    template = params.output_path_template
    if not os.path.isabs(template):
        raise WorkflowValidationError(f"{output} must be an absolute path")
    if settings is not None:
        try:
            resolve_inside_roots(template, settings)
        except PathNotAllowed as refused:
            raise WorkflowValidationError(f"{output}: {refused.problem.detail}") from None


def check_inputs(issues: Issues, resolve: Callable[[], Sequence[object]]) -> None:
    """Input resolution, as the run's creation makes it: it must find the files the run reads (an error)."""

    try:
        found = resolve()
    except WorkflowValidationError as error:
        issues.error(["input_path"], "no_inputs", str(error))
        return
    if not found:
        issues.error(["input_path"], "no_inputs", "No input files were found")
