from __future__ import annotations

import logging
import subprocess
import sys
import tomllib
import types
import typing
from pathlib import Path, PurePath

import pytest
from lauelab.indexing import PeakParams
from pydantic import BaseModel, ValidationError

from laueportal.core.settings import (
    RETIRED_SECTIONS,
    ConfigError,
    ExecutionSettings,
    IndexingDefaults,
    IndexingFixed,
    ReconstructionDefaults,
    environment_overrides,
    get_settings,
    load_settings,
)
from laueportal.indexing.lauego.params import LaueGoParamsV1
from laueportal.reconstruction.wire.params import WireParamsV1
from tests.settings_support import make_settings

REPO_ROOT = Path(__file__).resolve().parents[3]
SECRET = "s3cret-value-never-printed"

MINIMAL_TOML = """
[database]
path = "{tmp}/portal.db"

[files]
runs_root = "{tmp}/runs"
"""


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "settings.toml"
    path.write_text(text.format(tmp=tmp_path))
    return path


def test_loads_toml_and_applies_nested_environment_overrides(tmp_path: Path, clean_env: pytest.MonkeyPatch) -> None:
    config = write_config(
        tmp_path,
        MINIMAL_TOML
        + """
[app]
port = 2110
log_level = "warning"

[files.roots]
samples = "/data/samples"

[indexing.defaults]
box_size = 18
""",
    )
    clean_env.setenv("LAUEPORTAL_APP__PORT", "2105")
    clean_env.setenv("LAUEPORTAL_INDEXING__DEFAULTS__BOX_SIZE", "7")
    clean_env.setenv("LAUEPORTAL_FILES__ROOTS__EXTRA", "/data/extra")

    settings = load_settings(config)

    assert settings.database.path == tmp_path / "portal.db"
    assert settings.app.port == 2105  # the environment wins over the file
    assert settings.app.log_level == "warning"  # untouched keys of the same section survive
    assert settings.indexing.defaults.box_size == 7
    assert settings.files.roots == {"samples": Path("/data/samples"), "extra": Path("/data/extra")}
    assert settings.execution.workers == 1  # defaults fill what the file leaves out


def test_get_settings_reads_laueportal_config_once(tmp_path: Path, clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("LAUEPORTAL_CONFIG", str(write_config(tmp_path, MINIMAL_TOML)))
    first = get_settings()
    assert first.files.runs_root == tmp_path / "runs"
    assert get_settings() is first


def test_get_settings_needs_laueportal_config(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="LAUEPORTAL_CONFIG is not set"):
        get_settings()


def test_settings_constructor_ignores_files_and_environment(tmp_path: Path, clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("LAUEPORTAL_APP__PORT", "2105")
    clean_env.setenv("LAUEPORTAL_CONFIG", str(write_config(tmp_path, MINIMAL_TOML)))
    settings = make_settings(tmp_path)
    assert settings.app.port == 2100
    assert settings.database.path == tmp_path / "laueportal.db"


def test_tilde_is_expanded_in_paths(tmp_path: Path, clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("HOME", str(tmp_path))
    config = write_config(
        tmp_path,
        """
[app]
static_dir = "~/dist"
[database]
path = "~/db/portal.db"
[files]
runs_root = "~/runs"
[files.roots]
samples = "~/samples"
[indexing.defaults]
geometry_file = "~/geo.xml"
""",
    )
    settings = load_settings(config)
    assert settings.app.static_dir == tmp_path / "dist"
    assert settings.database.path == tmp_path / "db/portal.db"
    assert settings.files.runs_root == tmp_path / "runs"
    assert settings.files.uploads_dir == tmp_path / "runs/.uploads"
    assert settings.files.roots["samples"] == tmp_path / "samples"
    assert settings.indexing.defaults.geometry_file == tmp_path / "geo.xml"


def test_empty_string_means_not_set(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        indexing={"defaults": {"threshold": "", "mask_file": "", "max_number": ""}},
        execution={"max_in_flight": ""},
    )
    defaults = settings.indexing.defaults
    assert (defaults.threshold, defaults.mask_file, defaults.max_number) == (None, None, None)
    assert settings.execution.max_in_flight is None


@pytest.mark.parametrize(
    ("toml", "location"),
    [
        ('[app]\nprot = "{secret}"', "app.prot"),
        ('[databse]\npath = "{secret}"', "databse"),
        ('[indexing.defaults]\nbox_sise = "{secret}"', "indexing.defaults.box_sise"),
        ('[beamline.motor_groups]\nsample = "{secret}"', "beamline.motor_groups.sample"),
    ],
)
def test_unknown_or_invalid_keys_in_the_file_fail_without_showing_values(
    tmp_path: Path, clean_env: pytest.MonkeyPatch, toml: str, location: str
) -> None:
    config = write_config(tmp_path, MINIMAL_TOML + "\n" + toml.replace("{secret}", SECRET))
    with pytest.raises(ConfigError) as error:
        load_settings(config)
    assert location in str(error.value)
    assert SECRET not in str(error.value)
    assert error.value.__cause__ is None and error.value.__suppress_context__  # no chained ValidationError


def test_unknown_nested_override_fails_without_showing_its_value(tmp_path: Path, clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("LAUEPORTAL_APP__PROT", SECRET)
    with pytest.raises(ConfigError) as error:
        load_settings(write_config(tmp_path, MINIMAL_TOML))
    assert "app.prot" in str(error.value)
    assert SECRET not in str(error.value)


def test_single_underscore_override_is_rejected(tmp_path: Path, clean_env: pytest.MonkeyPatch) -> None:
    # pydantic-settings would silently ignore it, and the runs would go to the file's runs_root.
    clean_env.setenv("LAUEPORTAL_FILES_RUNS_ROOT", SECRET)
    with pytest.raises(ConfigError, match="LAUEPORTAL_FILES_RUNS_ROOT") as error:
        load_settings(write_config(tmp_path, MINIMAL_TOML))
    assert SECRET not in str(error.value)


def test_environment_overrides_lists_names_only(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("LAUEPORTAL_APP__PORT", "2105")
    clean_env.setenv("LAUEPORTAL_CONFIG", "/somewhere.toml")
    assert environment_overrides() == ["LAUEPORTAL_APP__PORT"]


def test_required_sections_and_bad_files(tmp_path: Path, clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match=r"database: Field required"):
        load_settings(write_config(tmp_path, '[files]\nruns_root = "/runs"\n'))
    with pytest.raises(ConfigError, match="does not exist"):
        load_settings(tmp_path / "missing.toml")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_settings(write_config(tmp_path, "[database\n"))


@pytest.mark.parametrize(
    ("section", "values", "message"),
    [
        ("execution", {"heartbeat_seconds": 30, "stale_heartbeat_seconds": 20}, "stale_heartbeat_seconds"),
        ("execution", {"workers": 4, "max_in_flight": 2}, "max_in_flight"),
        ("execution", {"workers": 0}, "greater than 0"),
        ("execution", {"creation_timeout_seconds": 59}, "greater than or equal to 60"),
        ("output_templates", {"indexing": "scan_{scan}/index_{rn}"}, "unknown placeholders"),
        ("output_templates", {"reconstruction": "scan_{scan}/data"}, "must contain {run}"),
        ("files", {"roots": {"samples": "relative/path"}}, "absolute"),
        ("files", {"hdf_extensions": ["h5"]}, "extensions"),
        ("database", {"journal_mode": "memory"}, "journal_mode"),
        ("database", {"legacy_timezone": "Mars/Olympus"}, "unknown time zone"),
        ("database", {"protected": ["/ws/db/kept.db", "kept.db"]}, "protected paths must be absolute"),
        ("reconstruction", {"defaults": {"depth_start": 10, "depth_end": 0}}, "depth_start"),
        ("reconstruction", {"defaults": {"wire_edges": "middle"}}, "wire_edges"),
        ("indexing", {"defaults": {"max_rfactor": 2}}, "max_rfactor"),
        ("beamline", {"simulation_kev": [30, 6]}, "simulation_kev"),
        ("live", {"cpu_share": 1.5}, "less than or equal to 1"),
        ("live", {"wire_share": 1}, "less than 1"),
        ("live", {"catalog_seconds": 0}, "greater than 0"),
        ("live", {"catalog_seconds": 10, "catalog_idle_seconds": 5}, "catalog_idle_seconds"),
        ("live", {"poll_seconds": 5}, "Extra inputs"),
    ],
)
def test_values_are_validated(tmp_path: Path, section: str, values: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make_settings(tmp_path, **{section: values})


def test_a_retired_redis_section_and_its_variables_are_ignored_with_one_warning(
    tmp_path: Path, clean_env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A config's ``[redis]`` section and ``LAUEPORTAL_REDIS__*`` variables (the dev env's password) configure nothing;
    the settings load with one warning naming them, never a value. Without them, no warning."""
    config = write_config(tmp_path, MINIMAL_TOML + f'\n[redis]\nport = 2102\npassword = "{SECRET}"\nqueue = "q"\n')
    clean_env.setenv("LAUEPORTAL_REDIS__PASSWORD", SECRET)
    caplog.set_level(logging.WARNING, logger="laueportal.core.settings")

    settings = load_settings(config)

    assert RETIRED_SECTIONS == ("redis",) and "redis" not in settings.redacted()
    assert environment_overrides() == []
    [warning] = caplog.records
    assert warning.levelno == logging.WARNING
    assert warning.getMessage() == (
        f"Ignored [redis] in {config}, LAUEPORTAL_REDIS__PASSWORD: Redis and RQ are no longer used; remove them"
    )
    assert SECRET not in caplog.text
    caplog.clear()
    clean_env.delenv("LAUEPORTAL_REDIS__PASSWORD")
    load_settings(write_config(tmp_path, MINIMAL_TOML))
    assert caplog.records == []


def test_dev_template_is_valid_and_complete(tmp_path: Path, clean_env: pytest.MonkeyPatch) -> None:
    """``make setup`` renders it (``scripts/render-dev-config.py``): every path in the workspace or the checkout."""
    template = REPO_ROOT / "deploy/config/dev.toml.template"
    workspace, checkout, config = tmp_path / "work space", tmp_path / 'check"out', tmp_path / "dev.toml"
    render = subprocess.run(
        [sys.executable, REPO_ROOT / "scripts/render-dev-config.py", workspace, checkout, config],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (render.returncode, render.stderr) == (0, "")
    assert config.stat().st_mode & 0o777 == 0o600
    settings = load_settings(config)
    assert settings.app.port == 2100
    assert not set(RETIRED_SECTIONS) & set(tomllib.loads(template.read_text()))  # it would warn
    assert settings.database.path == workspace / "db" / "dev.db"
    assert settings.app.static_dir == checkout / "frontend" / "dist"
    assert set(settings.files.roots) == {"samples", "runs"}
    assert settings.database.protected == []

    def paths(value: object) -> list[PurePath]:
        if isinstance(value, PurePath):
            return [value]
        values = value.values() if isinstance(value, dict) else value if isinstance(value, list | tuple) else []
        return [path for item in values for path in paths(item)]

    found = paths(settings.model_dump())
    assert len(found) >= 8 and all(path.is_relative_to(workspace) or path.is_relative_to(checkout) for path in found)
    assert settings.beamline.motor_groups["energy"] == ["34ide:monoE:keV"]
    assert (settings.remote.enabled, settings.remote.settings_file) == (False, None)  # off in development


def test_a_config_that_still_sets_cosmic_filter_loads(tmp_path: Path, clean_env: pytest.MonkeyPatch) -> None:
    """``[indexing.fixed] cosmic_filter`` is retired: it had no effect (every run stores false). An earlier config
    template set it, so a config file that still sets it loads, and the key is ignored."""
    for value in ("false", "true"):
        config = write_config(tmp_path, MINIMAL_TOML + f"\n[indexing.fixed]\ncosmic_filter = {value}\n")
        assert "cosmic_filter" not in load_settings(config).indexing.fixed.model_dump()


def test_remote_execution_is_off_unless_enabled_with_its_settings_file(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    """Remote execution's settings are the separate bdp.yaml, which the portal only names."""
    assert (make_settings(tmp_path).remote.enabled, make_settings(tmp_path).remote.settings_file) == (False, None)
    with pytest.raises(ValidationError, match="settings_file is required"):
        make_settings(tmp_path, remote={"enabled": True})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        make_settings(tmp_path, remote={"enabled": False, "targets": {}})
    clean_env.setenv("HOME", str(tmp_path))
    enabled = make_settings(tmp_path, remote={"enabled": True, "settings_file": "~/config/bdp.yaml"})
    assert enabled.remote.settings_file == tmp_path / "config/bdp.yaml"
    clean_env.setenv("LAUEPORTAL_REMOTE__ENABLED", "true")
    clean_env.setenv("LAUEPORTAL_REMOTE__SETTINGS_FILE", str(tmp_path / "bdp.yaml"))
    loaded = load_settings(write_config(tmp_path, MINIMAL_TOML))
    assert (loaded.remote.enabled, loaded.remote.settings_file) == (True, tmp_path / "bdp.yaml")


def test_the_live_settings_defaults(tmp_path: Path) -> None:
    """The live rules' shares and the arrivals' conservative rates (the catalog's request size and row cap per call)."""
    live = make_settings(tmp_path).live
    assert (live.batch_stop_seconds, live.cpu_share, live.wire_share) == (60, 0.75, 0.67)
    rates = (live.catalog_seconds, live.catalog_idle_seconds, live.catalog_rescan_seconds)
    assert rates == (5, 30, 300) and (live.catalog_page_size, live.catalog_max_rows) == (1000, 5000)
    assert (live.follow_seconds, live.publish_seconds, live.arrivals_seconds, live.dm_timeout_seconds) == (5, 10, 1, 15)
    assert (live.dm_setup_file, live.dm_ssh_host, live.daq_list_seconds) == (None, None, 30)
    assert make_settings(tmp_path, live={"dm_ssh_host": " ", "dm_setup_file": ""}).live.dm_ssh_host is None
    with pytest.raises(ValidationError, match="ssh would read it as an option"):
        make_settings(tmp_path, live={"dm_ssh_host": "-oProxyCommand=x"})


def test_settings_are_immutable(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with pytest.raises(ValidationError):
        settings.app.port = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Form defaults against the parameter models, their pinned values and lauelab (their path to lauelab through
# request.json: tests/jobs/test_programs.py)


def _value_types(annotation: object) -> tuple[set[object], bool]:
    """(the value types an annotation admits, whether it admits None). Paths count as text."""
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return _value_types(typing.get_args(annotation)[0])
    if origin in (typing.Union, types.UnionType):
        found: set[object] = set()
        nullable = False
        for argument in typing.get_args(annotation):
            kinds, none = _value_types(argument)
            found |= kinds
            nullable |= none
        return found, nullable
    if annotation is type(None):
        return set(), True
    if origin is typing.Literal:
        return {type(value) for value in typing.get_args(annotation)}, False
    if isinstance(annotation, type) and issubclass(annotation, PurePath):
        return {str}, False
    return {origin or annotation}, False


def _accepts(request_types: set[object], value_type: object) -> bool:
    return value_type in request_types or (value_type is int and float in request_types)


REQUIRED_ON_THE_FORM = {"geometry_file", "crystal_file"}
"""Request fields with no usable default: the settings may leave them unset; the user (or the source) supplies them."""


def _check_types(section: type[BaseModel], request: type, renames: dict[str, list[str]]) -> list[str]:
    hints = typing.get_type_hints(request)
    problems = []
    for name, field in section.model_fields.items():
        settings_types, settings_nullable = _value_types(field.annotation)
        for target in renames.get(name, [name]):
            assert target in hints, f"{section.__name__}.{name}: {request.__name__} has no field {target}"
            request_types, request_nullable = _value_types(hints[target])
            if name in renames:  # a tuple split into scalar fields
                settings_types = set().union(*(_value_types(arg)[0] for arg in typing.get_args(field.annotation)))
            for value_type in settings_types:
                if not _accepts(request_types, value_type):
                    problems.append(f"{name}: {value_type} does not fit {target}: {hints[target]}")
            if settings_nullable and not request_nullable and target not in REQUIRED_ON_THE_FORM:
                problems.append(f"{name} may be unset but {target} is required")
    return problems


def test_form_defaults_have_the_request_field_types() -> None:
    crop = {"detector_crop": ["detector_crop_x1", "detector_crop_x2", "detector_crop_y1", "detector_crop_y2"]}
    assert _check_types(IndexingDefaults, LaueGoParamsV1, {"index_hkl": ["index_h", "index_k", "index_l"]}) == []
    assert _check_types(IndexingFixed, LaueGoParamsV1, crop) == []
    assert _check_types(ReconstructionDefaults, WireParamsV1, {}) == []
    compute_knobs = {
        name: ExecutionSettings.model_fields[name] for name in ("num_threads", "memory_limit_mb", "verbose")
    }
    hints = typing.get_type_hints(WireParamsV1)
    for name, field in compute_knobs.items():
        assert _value_types(field.annotation)[0] <= _value_types(hints[name])[0], name


def test_threshold_ratio_is_a_float_like_lauelabs() -> None:
    """lauelab's PeakParams.threshold_ratio is a float, and so is the form's: never truncated to an integer."""
    assert _value_types(IndexingDefaults.model_fields["threshold_ratio"].annotation) == ({float}, True)
    assert _value_types(typing.get_type_hints(LaueGoParamsV1)["threshold_ratio"]) == ({float}, True)
    assert _value_types(typing.get_type_hints(PeakParams)["threshold_ratio"]) == ({float}, True)
    assert IndexingDefaults(threshold_ratio=4.5).threshold_ratio == 4.5
    assert IndexingDefaults(threshold_ratio=4).threshold_ratio == 4.0  # a TOML integer
    # >= 0 when set: a migrated run's -1 meant "automatic", which a setting refuses
    with pytest.raises(ValidationError, match="threshold_ratio"):
        IndexingDefaults(threshold_ratio=-1)


def test_the_form_defaults_value_by_value() -> None:
    """The indexing and wire reconstruction forms' defaults, value by value."""
    indexing = IndexingDefaults()
    assert indexing.model_dump(exclude={"geometry_file", "crystal_file", "author", "notes"}) == {
        "box_size": 18,
        "max_rfactor": 0.5,
        "threshold": None,
        "threshold_ratio": None,
        "min_size": 3,
        "min_separation": 20,
        "max_number": 300,
        "peak_shape": "Lorentzian",
        "mask_file": None,
        "smooth": False,
        "index_kev_max_calc": 17.2,
        "index_kev_max_test": 35.0,
        "index_angle_tolerance": 0.1,
        "index_hkl": (0, 0, 1),
        "index_cone": 72.0,
        "max_peaks": 200,
        "output_xml": "output.xml",
        "scan_points": "",
        "depth_range": "",
        "depth": None,
    }
    assert IndexingFixed().model_dump() == {
        "detector_crop": (0, 2047, 0, 2047),
        "energy_unit": "keV",
        "exposure_unit": "sec",
        "reciprocal_lattice_unit": "1/nm",
        "lattice_parameters_unit": "nm",
        "beamline": "34ID-E",
    }
    assert ReconstructionDefaults().model_dump(exclude={"geometry_file", "author", "notes"}) == {
        "percent_brightest": 100,
        "wire_edges": "leading",
        "depth_start": -50,
        "depth_end": 150,
        "depth_resolution": 1,
        "frame_skip": 1,
        "wire_skip": 1,
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", None), (float("nan"), None), ("nan", None), (-49.0, "-49.0"), (0, "0"), (" 12.5 ", "12.5")],
)
def test_depth_default_is_text_or_unset(value: object, expected: str | None) -> None:
    assert IndexingDefaults(depth=value).depth == expected


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"depth": "deep"}, "depth must be a number"),
        ({"depth": "inf"}, "depth must be a finite number"),  # the parameter model refuses it
        ({"peak_shape": "Voigt"}, "peak_shape"),
        ({"peak_shape": "gaussian"}, "peak_shape"),
    ],
)
def test_form_defaults_are_checked_like_the_request(values: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        IndexingDefaults(**values)  # type: ignore[arg-type]
