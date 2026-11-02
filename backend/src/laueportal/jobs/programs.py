"""The programs a run is created with: ``lauego`` (LaueGo indexing, ``lauego/v1``) and ``wire`` (wire reconstruction,
``wire/v1``), their live schemas ``lauego-live`` and ``wire-live`` (``lauego-live/v1``, ``wire-live/v1``), and what the
portal fills in from config.

The parameter models (``indexing/lauego/params.py``, ``reconstruction/wire/params.py`` and their ``live_params.py``)
name the setting of each default and fixed value (``x-config``; a live field is its batch field's copy, so it names the
same setting); this module reads them from the settings, so the models never import the settings (they are imported
on the compute side). ``GET /api/v1/programs`` serves :func:`list_programs`: the batch programs, and the live ones
while ``[live] enabled`` (the Live page's).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any, Literal

from laueportal.core.params import ProgramParams
from laueportal.core.problems import ProblemError
from laueportal.core.settings import Settings
from laueportal.files.inputs import normalize_output_path_template
from laueportal.indexing.lauego.live_params import LaueGoLiveParamsV1
from laueportal.indexing.lauego.params import LaueGoParamsV1
from laueportal.jobs.schemas import Program, ProgramList
from laueportal.reconstruction.wire.live_params import WireLiveParamsV1
from laueportal.reconstruction.wire.params import WireParamsV1
from laueportal.remote.settings import RemoteSettingsError, available_backends, portal_settings

ProgramKind = Literal["lauego", "wire"]
"""A batch program (a create form's)."""
LiveProgramKind = Literal["lauego-live", "wire-live"]
"""A live program (the Live page's runs)."""
AnyProgramKind = Literal["lauego", "wire", "lauego-live", "wire-live"]
"""Any program ``GET /programs`` lists."""

PROGRAMS: dict[ProgramKind, type[ProgramParams]] = {"lauego": LaueGoParamsV1, "wire": WireParamsV1}
"""Each program's parameter model."""
LIVE_PROGRAMS: dict[ProgramKind, type[ProgramParams]] = {"lauego": LaueGoLiveParamsV1, "wire": WireLiveParamsV1}
"""Each program's live parameter model (``lauego-live/v1``, ``wire-live/v1``): a live run of it
(``jobs.creation.create_live_run``), listed as ``LIVE_KINDS``' kind."""
LIVE_KINDS: dict[ProgramKind, LiveProgramKind] = {"lauego": "lauego-live", "wire": "wire-live"}
"""Each program's live kind."""
MODELS: dict[AnyProgramKind, type[ProgramParams]] = {
    **PROGRAMS,
    **{LIVE_KINDS[kind]: model for kind, model in LIVE_PROGRAMS.items()},
}
"""Every listed program's parameter model, by kind."""
TITLES: dict[AnyProgramKind, str] = {
    "lauego": "LaueGo indexing",
    "wire": "Wire reconstruction",
    "lauego-live": "Live LaueGo indexing",
    "wire-live": "Live wire reconstruction",
}
OUTPUT_PATH_TEMPLATE = "output_path_template"
"""The one field whose default is made from its setting (``[output_templates]``), not copied: :func:`output_path_template`."""

_ITEM = re.compile(r"^(?P<name>\w+)\[(?P<index>\d+)\]$")


def config_value(settings: Settings, key: str) -> Any:
    """The setting ``key`` names (a dotted path; ``name[i]`` is an item of a tuple); a path as its text."""
    value: Any = settings
    for part in key.split("."):
        item = _ITEM.match(part)
        value = getattr(value, item["name"] if item else part)
        if item:
            value = value[int(item["index"])]
    return os.fspath(value) if isinstance(value, os.PathLike) else value


def output_path_template(
    kind: AnyProgramKind, settings: Settings, *, scan_number: int | None = None, reconstruction_id: int | None = None
) -> str:
    """The run directory template of a new run: ``[output_templates]`` under ``files.runs_root``,
    ``{run}`` as ``%d``; the segments with ``{scan}`` or ``{rec}`` are left out when the run has no scan or
    reconstruction. Exactly one ``%d`` (``WorkflowValidationError`` otherwise)."""

    template = config_value(settings, MODELS[kind].field_metadata(OUTPUT_PATH_TEMPLATE)["x-config"])
    segments = [
        segment.format(scan=scan_number, rec=reconstruction_id, run="%d")
        for segment in template.split("/")
        if not ("{scan}" in segment and scan_number is None) and not ("{rec}" in segment and reconstruction_id is None)
    ]
    return normalize_output_path_template(os.path.join(settings.files.runs_root, *segments))


def fixed_values(kind: AnyProgramKind, settings: Settings) -> dict[str, Any]:
    """The values a user never edits: every ``config`` field of the program, from its setting."""
    model = MODELS[kind]
    return {
        name: config_value(settings, model.field_metadata(name)["x-config"]) for name in model.fields_from("config")
    }


def form_defaults(kind: AnyProgramKind, settings: Settings) -> dict[str, Any]:
    """The configured default of every form field that has a setting (``[indexing.defaults]``,
    ``[reconstruction.defaults]``, ``[output_templates]``); a field without one starts from its source."""
    model = MODELS[kind]
    defaults: dict[str, Any] = {}
    for name in model.fields_from("form"):
        key = model.field_metadata(name).get("x-config")
        if key is not None:
            defaults[name] = (
                output_path_template(kind, settings) if name == OUTPUT_PATH_TEMPLATE else config_value(settings, key)
            )
    return defaults


def build_params(kind: AnyProgramKind, form: Mapping[str, Any], settings: Settings) -> ProgramParams:
    """The parameters of a new run from a client's form values: the fixed values come from config, the derived ones
    from the portal; a client's value for either is refused (``ProgramParams.from_form``)."""
    return MODELS[kind].from_form(form, fixed=fixed_values(kind, settings))


def execution_targets(settings: Settings) -> list[dict[str, str]]:
    """The execution targets this deployment offers, each ``{"value", "label"}``, in order
    (``remote.settings.available_backends``): one "APS" choice, ``local`` until the DM ``aps`` target is enabled,
    then ``aps``; "ALCF" (``crux``) when that target is enabled. A 503 problem when remote execution is enabled but
    its settings cannot be used (``laueportal remote poll`` says why; the reason may quote the file, so it is not
    sent)."""
    try:
        remote = portal_settings(settings.remote, runs_root=settings.files.runs_root)
    except RemoteSettingsError:
        raise ProblemError(
            503,
            "Remote settings unusable",
            "Remote execution is enabled, but its settings (remote.settings_file) cannot be used.",
        ) from None
    return [{"value": option.value, "label": option.label} for option in available_backends(remote)]


def parameters_schema(
    kind: AnyProgramKind, settings: Settings, *, targets: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    """The JSON Schema of the program's parameter model as ``GET /api/v1/programs`` serves it:

    - each form and config field's ``default`` is the configured value (an unset setting gives no default unless
      the field admits null);
    - ``required`` lists only the form fields a client must send (the config and derived values are the portal's,
      and ``build_params`` refuses them);
    - ``x-layout`` is the form layout (``form_layout``): ``[{"group", "fields"}]``, in order;
    - ``backend``'s ``x-options`` and ``default`` are the deployment's targets and the first of them (``targets``,
      :func:`execution_targets` when not given), where ``backend`` is a form field (a live run's is derived, ``local``).
    """
    model = MODELS[kind]
    schema = model.model_json_schema()
    properties: dict[str, dict[str, Any]] = schema["properties"]
    for name, value in {**form_defaults(kind, settings), **fixed_values(kind, settings)}.items():
        if value is not None or _nullable(properties[name]):
            properties[name]["default"] = list(value) if isinstance(value, tuple) else value
    form = set(model.fields_from("form"))
    schema["required"] = [name for name in schema.get("required", ()) if name in form]
    schema["x-layout"] = [{"group": group, "fields": list(fields)} for group, fields in model.form_layout]
    if "backend" in form:
        targets = execution_targets(settings) if targets is None else targets
        properties["backend"] |= {"x-options": targets, "default": targets[0]["value"]}
    return schema


def _nullable(schema: Mapping[str, Any]) -> bool:
    return any(option.get("type") == "null" for option in schema.get("anyOf", ()))


def listed_kinds(settings: Settings) -> list[AnyProgramKind]:
    """The programs ``GET /programs`` lists: the batch ones, then the live ones while ``[live] enabled``."""
    batch: list[AnyProgramKind] = ["lauego", "wire"]
    return [*batch, *(LIVE_KINDS[kind] for kind in PROGRAMS)] if settings.live.enabled else batch


def list_programs(settings: Settings) -> ProgramList:
    targets = execution_targets(settings)
    return ProgramList(
        programs=[
            Program(
                kind=kind,
                title=TITLES[kind],
                schema_id=MODELS[kind].model_fields["schema_id"].default,
                json_schema=parameters_schema(kind, settings, targets=targets),
            )
            for kind in listed_kinds(settings)
        ]
    )
