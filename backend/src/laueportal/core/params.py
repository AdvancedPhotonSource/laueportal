"""The base of the program parameter models, ``lauego/v1`` and ``wire/v1``.

A model holds exactly the ``request.json`` ``request`` keys (``backend`` included) plus ``schema_id``;
``jobs.rundir.request_document`` writes them. Each field says where its value comes from (``x-source`` in its JSON
Schema):

- ``form``: the user sets it on the create form, which starts from a default in config (``x-config``, e.g.
  ``indexing.defaults.box_size``) or from the source the run is made from (prefill);
- ``config``: a setting users never edit (detector crop, units, beamline, threads, memory): the portal takes it
  from config (``x-config``; ``jobs.programs.fixed_values``), and :meth:`ProgramParams.from_form` refuses it from
  a client;
- ``derived``: set when the run is created (``schema_id``, ``computer_name``, ``priority``, ``submitted_at``,
  ``algorithm_version``, ``cosmic_filter``); a client cannot set it either.

The JSON Schema carries the form's metadata: ``title`` (the form label without its unit, which is ``x-unit``, for a
field the form shows), ``description`` (the API's documentation, for developers), ``x-hint`` (a form field's short hint
for users, the label's tooltip: what the value is, without code, lauelab or config names), ``x-unit``, the limits
(``minimum``, ``exclusiveMinimum``, ``maximum``), ``x-group`` (the form section), ``x-widget``, ``x-placeholder`` (at
most a terse value pattern: ``Si_%d.h5``, ``Automatic``), ``x-extensions`` (a file picker's filter) and ``x-options``
(an enumeration's choices in order, each ``{"value", "label"}``); each model's ``form_layout`` is the form's layout (its
sections in order, each with its fields in order). ``jobs.programs`` adds the configured defaults, the layout
(``x-layout``) and the deployment's execution targets for ``GET /api/v1/programs``.

The models check the per-field rules and normalize each value; the cross-field and context checks are
``indexing/validation.py``'s and ``reconstruction/validation.py``'s. Nothing
here imports the settings or the database (``jobs.programs`` reads the config); the compute side never imports this
module, since a compute node has no pydantic (import-linter rule 3b).

The live schemas, ``wire-live/v1`` and ``lauego-live/v1`` (``*/live_params.py``), share these
models' fields (``shared_field``) without the scan indices: ``jobs.programs.LIVE_PROGRAMS``
(the jobs core's live runs); ``GET /programs`` lists them while ``[live] enabled``, the Live page's API takes them
(``jobs.live_session``).
"""

from __future__ import annotations

import copy
import math
import os
import socket
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, ClassVar, Literal, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, field_validator
from pydantic_core import InitErrorDetails, PydanticCustomError

Source = Literal["form", "config", "derived"]
SOURCES: tuple[Source, ...] = ("form", "config", "derived")

Widget = Literal[
    "text",  # one line of text
    "textarea",  # several lines (notes)
    "integer",
    "number",
    "checkbox",
    "select",  # one of x-options
    "folder",  # a folder inside the roots (the path picker)
    "file",  # a file inside the roots (the path picker)
    "output-folder",  # the run's output folder: a path with one %d, the run ID
    "filename-templates",  # file-name templates with %d placeholders and wildcards (with "Find Matching Files")
    "indices",  # an srange selection such as 1-10,15 (with "Find Indices")
    "hkl",  # one of the three integers of the Central HKL widget
    "scan",  # the source scan (a picker)
    "reconstruction",  # the source reconstruction (a picker)
    "target",  # the execution target (the form's "Run on")
    "forward-cache",  # a LaueMatching forward cache: its name, chosen from those that fit or typed for a new one
    "forward-cache-new",  # whether that cache is new: set by the forward-cache widget, drawn by nothing
]


def _fspath(value: Any) -> Any:
    """A path object as its text (``os.fspath``); anything else as given."""
    return os.fspath(value) if isinstance(value, os.PathLike) else value


PathText = Annotated[str, BeforeValidator(_fspath)]
"""A path field: text, or a path object (as text)."""


def param(
    default: Any = ...,
    *,
    title: str,
    source: Source,
    description: str,
    hint: str | None = None,
    unit: str | None = None,
    config: str | None = None,
    group: str | None = None,
    widget: Widget | None = None,
    placeholder: str | None = None,
    extensions: Sequence[str] | None = None,
    options: Mapping[str, str] | None = None,
    default_factory: Callable[[], Any] | None = None,
    gt: float | None = None,
    ge: float | None = None,
    le: float | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
) -> Any:
    """A parameter field with its form metadata (module docstring); ``default`` ``...`` means required.

    ``config`` names the setting its default (``form``) or its value (``config``) comes from: a dotted path into
    the settings, with ``[i]`` for an item of a tuple (``indexing.fixed.detector_crop[0]``). A form field has a
    ``hint`` (``x-hint``), the user's; the ``description`` is the developer's.
    """

    if source == "config" and config is None:
        raise ValueError(f"{title}: a config field names its setting")
    if source == "derived" and config is not None:
        raise ValueError(f"{title}: a derived field has no setting")
    if source == "form" and not hint:
        raise ValueError(f"{title}: a form field has a hint for its users")
    extra: dict[str, Any] = {"x-source": source}
    for key, value in (
        ("x-unit", unit),
        ("x-config", config),
        ("x-group", group),
        ("x-widget", widget),
        ("x-hint", hint),
        ("x-placeholder", placeholder),
        ("x-extensions", None if extensions is None else list(extensions)),
        (
            "x-options",
            None if options is None else [{"value": choice, "label": label} for choice, label in options.items()],
        ),
    ):
        if value is not None:
            extra[key] = value
    if source != "form":
        extra["readOnly"] = True
    limits: dict[str, Any] = {
        name: value
        for name, value in (("gt", gt), ("ge", ge), ("le", le), ("min_length", min_length), ("max_length", max_length))
        if value is not None
    }
    if default_factory is not None:
        return Field(
            default_factory=default_factory, title=title, description=description, json_schema_extra=extra, **limits
        )
    return Field(default, title=title, description=description, json_schema_extra=extra, **limits)


Backend = Literal["local", "aps", "crux"]
"""The execution targets (``jobs.backends.BACKENDS``)."""


# The keys every program shares, after its own, in their order.

AUTHOR_MAX_LENGTH = 200
"""The longest Author, in characters (the migrated runs' longest: 31)."""
NOTES_MAX_LENGTH = 10_000
"""The longest Notes, in characters (the migrated runs' longest: 49)."""
TEMPLATES_MAX_ITEMS = 100
"""The most file-name templates a run takes (``filename_prefixes``; the migrated runs' most: 1)."""
TEMPLATE_MAX_LENGTH = 255
"""The longest file-name template, in characters: a file name's maximum (the migrated runs' longest: 58)."""

FilenameTemplate = Annotated[str, Field(max_length=TEMPLATE_MAX_LENGTH)]
"""One of ``filename_prefixes``' templates (an item's ``maxLength``)."""


def author_param(config: str) -> Any:
    return param(
        None,
        title="Author",
        source="form",
        config=config,
        group="Identity",
        widget="text",
        description=f"Who made the run, or a tag (at most {AUTHOR_MAX_LENGTH} characters).",
        hint="Who made the run, or a tag.",
        max_length=AUTHOR_MAX_LENGTH,
    )


def notes_param(config: str) -> Any:
    return param(
        None,
        title="Notes",
        source="form",
        config=config,
        group="Notes",
        widget="textarea",
        description=f"Free text about the run (at most {NOTES_MAX_LENGTH:,} characters).",
        hint="Free text about the run.",
        max_length=NOTES_MAX_LENGTH,
    )


def algorithm_version_param() -> Any:
    return param(
        None,
        title="Algorithm Version",
        source="derived",
        description="Not set by the portal: null.",
    )


BACKEND_NAMES: Mapping[str, str] = {"local": "APS", "aps": "APS", "crux": "ALCF"}
"""Where each execution target computes: the one mapping that names a target, as the forms' Run on choices
(``backend_param``), the run monitor's Execution and the job page's Remote Run card (``jobs.service``) show it. A
target without a name here is shown as it is."""


def backend_param() -> Any:
    return param(
        "local",
        title="Run on",
        source="form",
        group="Execution",
        widget="target",
        options=BACKEND_NAMES,
        description=(
            "The execution target: `local` (this portal's worker), `aps` (PBS on the APS cluster, through DM) or "
            "`crux` (ALCF's Crux, through Globus). The form offers these choices "
            "(`remote.settings.available_backends`): one APS choice, `local` until the `aps` target is enabled, "
            "and ALCF when the `crux` target is."
        ),
        hint="Where the run is computed.",
    )


def computer_name_param() -> Any:
    return param(
        title="Computer",
        source="derived",
        default_factory=socket.gethostname,
        description="The host that created the run (its real name).",
    )


def priority_param() -> Any:
    return param(0, title="Priority", source="derived", description="Always 0: a queue priority, unused.")


def submitted_at_param() -> Any:
    return param(
        title="Submitted",
        source="derived",
        default_factory=lambda: datetime.now(UTC),
        description="When the run was created: aware UTC.",
    )


def schema_id_param(schema_id: str) -> Any:
    return param(schema_id, title="Schema", source="derived", description=f"The parameter schema, `{schema_id}`.")


class ProgramParams(BaseModel):
    """The parameters of one run of a program: ``request.json``'s ``request`` (module docstring)."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    form_layout: ClassVar[tuple[tuple[str, tuple[str, ...]], ...]] = ()
    """The form layout: each section (a field's ``x-group``) in order, with its form fields in order."""

    @classmethod
    def field_metadata(cls, name: str) -> Mapping[str, Any]:
        """The ``x-`` metadata of field ``name`` (``x-source``, ``x-config``, ...)."""
        extra = cls.model_fields[name].json_schema_extra
        return extra if isinstance(extra, dict) else {}

    @classmethod
    def fields_from(cls, *sources: Source) -> tuple[str, ...]:
        """The fields whose value comes from one of ``sources``, in the model's order."""
        return tuple(name for name in cls.model_fields if cls.field_metadata(name)["x-source"] in sources)

    @classmethod
    def from_form(cls, form: Mapping[str, Any], *, fixed: Mapping[str, Any]) -> Self:
        """The parameters of a new run: the form's values, plus the fixed settings (``fixed``: every ``config``
        field, from config) and the derived values (the model's defaults: this host, now, priority 0).

        A form value for a field the form does not set (``config`` or ``derived``) is refused (one error per
        field, ``set_by_portal``) before anything else is checked; so is any other unknown key (``extra_forbidden``).
        """

        expected = set(cls.fields_from("config"))
        if set(fixed) != expected:
            raise ValueError(f"fixed values must be exactly {sorted(expected)}; got {sorted(fixed)}")
        portal = set(cls.fields_from("config", "derived"))
        refused = [name for name in form if name in portal]
        if refused:
            errors = [
                InitErrorDetails(
                    type=PydanticCustomError(
                        "set_by_portal",
                        "{field} is set by the portal ({source}), not by the form",
                        {"field": name, "source": cls.field_metadata(name)["x-source"]},
                    ),
                    loc=(name,),
                    input=form[name],
                )
                for name in refused
            ]
            raise ValidationError.from_exception_data(cls.__name__, errors)
        return cls.model_validate({**form, **fixed})

    def with_values(self, **changes: Any) -> Self:
        """A copy with ``changes``, checked and normalized like the original (``model_copy`` checks nothing)."""
        return self.model_validate({**self.model_dump(), **changes})

    @field_validator("computer_name", check_fields=False)
    @classmethod
    def _computer_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Computer name is required")
        return value

    @field_validator("author", "notes", check_fields=False)
    @classmethod
    def _blank_text(cls, value: str | None) -> str | None:
        return blank_to_none(value)


# The live schemas, ``wire-live/v1`` and ``lauego-live/v1``. Each has exactly its batch schema's fields, in their
# order, so a live run's ``request.json`` holds exactly the batch schema's keys; each field is the batch schema's
# (``shared_field``) but for the scan indices, the target and the live rules below.


def shared_field(model: type[ProgramParams], name: str) -> Any:
    """A copy of ``model``'s field ``name`` (its default, limits and form metadata) for a model that shares it.

    A copy, since pydantic adopts the field it is given. Annotate the sharing field with the field's own type
    (``str`` for a ``PathText``, whose validator the copy carries), so the two JSON Schemas are the same.
    """
    return copy.deepcopy(model.model_fields[name])


def live_scan_points_param() -> Any:
    return param(
        "",
        title="Scan Indices",
        source="derived",
        description=(
            "Always empty for a live run: its inputs are the files that arrive while it runs (`arrivals.jsonl`), and "
            "the run row's scan points become their numbers when it ends. Kept so that `request.json` holds "
            "exactly the batch schema's keys."
        ),
    )


def live_backend_param() -> Any:
    return param(
        "local",
        title="Run on",
        source="derived",
        description="Always `local`: a live run is computed on the portal host.",
    )


LIVE_NOT_ON_FORM = ("scan_points", "backend")
"""The batch schemas' form fields a live schema makes derived: the scan indices and the target."""


def live_form_layout(model: type[ProgramParams]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """The batch schema ``model``'s form layout without the fields a live run does not take from the form
    (``LIVE_NOT_ON_FORM``) and the sections left empty (its Execution, the target's)."""
    sections = (
        (group, tuple(name for name in fields if name not in LIVE_NOT_ON_FORM)) for group, fields in model.form_layout
    )
    return tuple((group, fields) for group, fields in sections if fields)


LIVE_TEMPLATE_RULE = "A live run takes at most one file name template, with exactly one %d, the number of each file"


def live_template_items(value: Any) -> Any:
    """A live run's ``filename_prefixes`` before its one-item limit: blank templates dropped, as the batch schemas
    drop them (``files.inputs.normalize_filename_templates``); none left is a run with no template."""
    if not isinstance(value, list | tuple):
        return value
    return [item for item in value if not (isinstance(item, str) and not item.strip())]


def live_templates(templates: Sequence[str]) -> tuple[str, ...]:
    """A live run's ``filename_prefixes``, stripped as the batch schema's: at most one template, with exactly one
    ``%d`` (the number of each arriving file comes from it). None (``()``): the run takes every file whose name ends
    ``.h5``, its number the last run of digits in its name (``jobs.arrivals.RunFiles``)."""
    stripped = tuple(text for template in templates if (text := str(template).strip()))
    if len(stripped) > 1 or any(template.count("%d") != 1 for template in stripped):
        raise ValueError(LIVE_TEMPLATE_RULE)
    return stripped


def required_path(value: str, label: str) -> str:
    """A required path, normalized (``os.path.normpath``); empty is refused."""
    normalized = os.path.normpath(value)
    if not normalized or normalized == os.curdir:
        raise ValueError(f"{label} is required")
    return normalized


def optional_text(value: str | None) -> str | None:
    """Stripped text; empty is ``None`` (for ``mask_file`` and ``output_xml``)."""
    value = value.strip() if value is not None else None
    return value or None


def blank_to_none(value: str | None) -> str | None:
    """Blank text (empty or only white space) is ``None``; any other text is kept as given (a blank Author or
    Notes is null)."""
    return None if value is None or not value.strip() else value


DEPTH_RULE = "Depth must be a number in µm, or empty for automatic"


def depth_text(value: Any) -> str | None:
    """The ``depth`` override (µm) as text: blank is ``None`` (automatic); a finite number is kept as typed, without
    surrounding blanks (a JSON number as its text); anything else, including NaN and infinity, is refused, rather than
    creating a run whose every frame fails."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
    else:
        raise ValueError(DEPTH_RULE)
    try:
        number = float(text)
    except ValueError:
        raise ValueError(DEPTH_RULE) from None
    if not math.isfinite(number):
        raise ValueError(DEPTH_RULE)
    return text
