"""The remote settings (``remote.settings``): the ``bdp.yaml`` keys, typed, strict, and the portal's view of them."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from laueportal.remote.settings import (
    RemoteSettings,
    RemoteSettingsError,
    available_backends,
    load_settings,
    parse_settings,
    portal_settings,
    require_backend,
)
from tests.remote_support import Layout, bdp_settings

TEMPLATE = Path(__file__).resolve().parents[3] / "deploy" / "remote" / "bdp.yaml.template"


def _template() -> dict[str, Any]:
    return yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))


def _write(path: Path, values: Any) -> Path:
    path.write_text(yaml.safe_dump(values), encoding="utf-8")
    return path


def test_the_template_loads_and_keeps_every_value_as_written():
    """``exclude_unset`` gives back the file's own mapping: the plan copies sections without adding a key or converting
    a value (``bdp.json`` keeps the file's values)."""
    values = _template()
    settings = load_settings(TEMPLATE)
    assert settings.model_dump(exclude_unset=True) == values
    assert settings.target_document("crux") == values["targets"]["crux"]
    assert settings.target_document("aps") == values["targets"]["aps"]
    assert settings.targets.crux is not None and settings.targets.crux.pbs.walltime == "01:00:00"


def test_a_section_without_optional_keys_stays_without_them():
    values = bdp_settings(Layout(Path("/base")))
    target = values["targets"]["aps"]
    del target["module_commands"], target["execution"]["max_in_flight"]
    settings = parse_settings(values)
    document = settings.target_document("aps")
    assert "module_commands" not in document and "max_in_flight" not in document["execution"]
    assert document == target


@pytest.mark.parametrize(
    "where",
    [(), ("dm",), ("storage", "aps"), ("targets", "crux"), ("targets", "aps", "pbs"), ("targets", "crux", "execution")],
)
def test_unknown_keys_are_refused_by_name_without_values(where):
    values = _template()
    section = values
    for key in where:
        section = section[key]
    section["unexpected_key"] = "a-value-that-must-not-be-echoed"
    with pytest.raises(RemoteSettingsError) as error:
        parse_settings(values)
    message = str(error.value)
    assert ".".join((*where, "unexpected_key")) in message
    assert "a-value-that-must-not-be-echoed" not in message


def _set(values: dict[str, Any], path: tuple[str, ...], value: Any) -> dict[str, Any]:
    section = values
    for key in path[:-1]:
        section = section[key]
    section[path[-1]] = value
    return values


@pytest.mark.parametrize(
    ("path", "value"),
    [
        # written into the PBS script as str(value)
        (("targets", "crux", "pbs", "walltime"), 3600),  # an unquoted 01:00:00 (YAML 1.1 reads it as seconds)
        (("targets", "crux", "pbs", "walltime"), 1.5),
        (("targets", "crux", "pbs", "account"), 12345),
        # read with int()
        (("targets", "crux", "pbs", "nodes"), "4"),
        (("targets", "crux", "pbs", "nodes"), 4.0),
        (("targets", "aps", "pbs", "cpus"), "32"),
        (("targets", "crux", "execution", "memory_limit_mb"), 8192.0),
        (("targets", "crux", "execution", "threads_per_worker"), "2"),
        (("dm", "timeout_seconds"), None),  # no timeout (subprocess's timeout=None)
        (("dm", "permissions_timeout_seconds"), None),  # null: no timeout either
        (("dm", "permissions_timeout_seconds"), 600),
    ],
)
def test_values_the_code_can_use_are_accepted_and_kept_as_written(path, value):
    """A deployed file works as it is: a value the code can use is accepted, and the plan copies it without conversion
    (same value, same type)."""
    values = _set(_template(), path, value)
    settings = parse_settings(values)
    if path[0] != "targets":
        assert settings.model_dump(exclude_unset=True) == values
        return
    document = settings.target_document(path[1])
    assert document == values["targets"][path[1]]
    copied = document[path[2]][path[3]]
    assert copied == value and type(copied) is type(value)


@pytest.mark.parametrize("value", [True, False, None, "yes", "false", 1, 0.5])
def test_the_files_own_enabled_is_ignored_whatever_scalar_it_holds(tmp_path, value):
    """The portal's switch replaces the file's top-level ``enabled`` and the launcher never reads it, so any scalar
    loads; only ``true`` reads as enabled, and the portal's switch decides (``portal_settings``)."""
    layout = Layout(tmp_path)
    path = _write(tmp_path / "bdp.yaml", {**bdp_settings(layout), "enabled": value})
    assert load_settings(path).enabled is (value is True)
    settings = portal_settings(_section(True, path), runs_root=layout.experiment)
    assert settings is not None and settings.enabled is True


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("targets", "crux", "pbs", "nodes"), "four"),
        (("targets", "crux", "pbs", "nodes"), 4.5),  # int() would drop the fraction
        (("targets", "crux", "pbs", "nodes"), None),
        (("targets", "aps", "execution", "workers"), 1.5),
        (("targets", "aps", "execution", "workers"), "32"),
        (("targets", "crux", "execution", "memory_limit_mb"), "8 GB"),
        (("targets", "crux", "module_commands"), "module load a-value-that-must-not-be-echoed"),
        (("dm", "owner"), ["a-value-that-must-not-be-echoed"]),
        (("dm", "timeout_seconds"), "30 s"),
        (("dm", "permissions_timeout_seconds"), "5 min"),
        (("enabled",), ["a-value-that-must-not-be-echoed"]),  # not a scalar
        # a run could start with these two; they are refused on purpose (the module docstring and the runbook list them)
        (("auth_profile",), None),  # the beamline profile, with a gladier-None.cfg
        (("targets", "aps", "enabled"), "false"),  # a truthiness test reads any non-empty value as enabled
        (("targets", "crux", "enabled"), 1),
    ],
)
def test_values_the_code_could_not_use_are_refused_by_name(path, value):
    with pytest.raises(RemoteSettingsError, match=".".join(path)) as error:
        parse_settings(_set(_template(), path, value))
    assert "a-value-that-must-not-be-echoed" not in str(error.value)


@pytest.mark.parametrize("path", [("dm", "ssh_host"), ("dm", "launcher_ssh_host"), ("targets", "aps", "submit_host")])
def test_an_ssh_host_ssh_would_read_as_an_option_is_refused_by_name(path):
    """The host is ssh's argument before the remote command: ``-oProxyCommand=...`` would be an option. Refused by name,
    without the value; an ordinary host loads as written."""
    hostile = "-oProxyCommand=a-value-that-must-not-be-echoed"
    with pytest.raises(RemoteSettingsError, match=".".join(path)) as error:
        parse_settings(_set(_template(), path, hostile))
    assert "a-value-that-must-not-be-echoed" not in str(error.value) and "option" in str(error.value)
    settings = parse_settings(_set(_template(), path, "dm-host.example.org"))
    section = settings.model_dump(exclude_unset=True)
    for key in path:
        section = section[key]
    assert section == "dm-host.example.org"


@pytest.mark.parametrize(
    ("backend", "section", "missing"),
    [
        ("crux", {"enabled": False}, ["analysis_python", "code_directory", "pbs", "execution", "compute_endpoint"]),
        ("aps", {"enabled": False}, ["analysis_python", "code_directory", "pbs", "execution"]),
        ("aps", "without threads_per_worker", ["execution.threads_per_worker"]),
        ("crux", "without the PBS queue", ["pbs.queue"]),
    ],
)
def test_an_incomplete_target_loads_and_planning_names_what_it_lacks(backend, section, missing):
    """Nothing reads a disabled target, so it may be incomplete; a run planned for an incomplete one is refused naming
    the keys every run on it reads (not a KeyError, in the plan or on the remote side)."""
    values = _template()
    if section == "without threads_per_worker":
        del values["targets"][backend]["execution"]["threads_per_worker"]
    elif section == "without the PBS queue":
        values["targets"][backend]["pbs"]["queue"] = None
    else:
        values["targets"][backend] = section
    settings = parse_settings(values)
    with pytest.raises(RemoteSettingsError) as error:
        settings.target_document(backend)
    for key in missing:
        assert f"targets.{backend}.{key}" in str(error.value)
    other = "aps" if backend == "crux" else "crux"
    assert settings.target_document(other) == values["targets"][other]


@pytest.mark.parametrize(
    ("remove", "needed"),
    [
        (("dm", "setup_file"), "dm.setup_file"),
        (("dm",), "dm.setup_file, dm.owner, dm.workflow"),
        (("storage",), "storage.aps.filesystem_root"),
        (("submission_directory",), "submission_directory"),
    ],
)
def test_the_launchers_copy_needs_only_what_the_launcher_reads(tmp_path, remove, needed):
    """``load_settings`` requires no key (the launcher's copy may leave out the DM API); the portal refuses a
    file without what every submission and poll reads, naming the keys."""
    layout = Layout(tmp_path)
    values = bdp_settings(layout)
    section = values
    for key in remove[:-1]:
        section = section[key]
    del section[remove[-1]]
    path = _write(tmp_path / "bdp.yaml", values)
    assert load_settings(path).model_dump(exclude_unset=True) == values
    with pytest.raises(RemoteSettingsError, match=needed):
        portal_settings(_section(True, path), runs_root=layout.experiment)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("poll_seconds",), 0),
        (("poll_seconds",), -5),
        (("poll_seconds",), float("inf")),
        (("poll_seconds",), float("nan")),
        (("dm", "timeout_seconds"), 0),
        (("dm", "timeout_seconds"), -1.5),
        (("dm", "permissions_timeout_seconds"), 0),
    ],
)
def test_the_portal_refuses_a_poll_interval_or_dm_timeout_that_is_not_positive(tmp_path, path, value):
    """``poll_seconds: 0`` would have the poller call DM back to back, ``dm.timeout_seconds: 0`` time out every DM
    call. The launcher's copy loads (it reads neither); the portal names the key, never the value."""
    layout = Layout(tmp_path)
    file = _write(tmp_path / "bdp.yaml", _set(bdp_settings(layout), path, value))
    load_settings(file)
    with pytest.raises(RemoteSettingsError, match=f"{'.'.join(path)} must be a positive, finite number") as error:
        portal_settings(_section(True, file), runs_root=layout.experiment)
    assert str(value) not in str(error.value).replace(str(file), "")


def test_the_launchers_listing_takes_its_defaults_and_refuses_a_duration_that_is_not_positive():
    """``targets.crux.listing``: 30 s between listings, 8 s for one, stale after 90 s, unless the file says otherwise;
    a duration that is not a positive, finite number is refused by name."""
    values = bdp_settings(Layout(Path("/base")))

    def listing() -> tuple[object, object, object]:
        crux = parse_settings(values).targets.crux
        assert crux is not None
        return crux.listing.interval_seconds, crux.listing.timeout_seconds, crux.listing.stale_after_seconds

    assert listing() == (30, 8, 90)
    assert "listing" not in parse_settings(values).target_document("crux")  # the plan holds what the file holds
    values["targets"]["crux"]["listing"] = {"interval_seconds": 60, "timeout_seconds": 2.5}
    assert listing() == (60, 2.5, 90)
    for key in ("interval_seconds", "timeout_seconds", "stale_after_seconds"):
        for value in (0, -1, float("inf"), float("nan"), "30", True, None):
            values["targets"]["crux"]["listing"] = {key: value}
            with pytest.raises(RemoteSettingsError, match=f"targets.crux.listing.{key}"):
                parse_settings(values)


def test_the_launchers_status_failure_budget_takes_its_default_and_refuses_a_duration_that_is_not_positive():
    """``targets.crux.status_failure_budget_seconds``: 600 s unless the file says otherwise; a duration that is not a
    positive, finite number is refused by name."""
    values = bdp_settings(Layout(Path("/base")))

    def budget() -> object:
        crux = parse_settings(values).targets.crux
        assert crux is not None
        return crux.status_failure_budget_seconds

    assert budget() == 600
    assert "status_failure_budget_seconds" not in parse_settings(values).target_document("crux")
    values["targets"]["crux"]["status_failure_budget_seconds"] = 1800.5
    assert budget() == 1800.5
    for value in (0, -1, float("inf"), float("nan"), "600", True, None):
        values["targets"]["crux"]["status_failure_budget_seconds"] = value
        with pytest.raises(RemoteSettingsError, match="targets.crux.status_failure_budget_seconds"):
            parse_settings(values)


def test_load_settings_errors_name_the_file(tmp_path):
    with pytest.raises(RemoteSettingsError, match="does not exist"):
        load_settings(tmp_path / "missing.yaml")
    (tmp_path / "list.yaml").write_text("- a\n- b\n")
    with pytest.raises(RemoteSettingsError, match="must be a YAML mapping"):
        load_settings(tmp_path / "list.yaml")
    (tmp_path / "broken.yaml").write_text("dm: [unclosed\n")
    with pytest.raises(RemoteSettingsError, match="not valid YAML"):
        load_settings(tmp_path / "broken.yaml")
    with pytest.raises(RemoteSettingsError, match="cannot be read: Is a directory"):
        load_settings(tmp_path)


def _section(enabled: bool, settings_file: Path | None) -> SimpleNamespace:
    return SimpleNamespace(enabled=enabled, settings_file=settings_file)


def test_portal_settings_follow_the_portals_switch(tmp_path):
    layout = Layout(tmp_path)
    values = {**bdp_settings(layout), "enabled": False}  # the file's own value is ignored
    path = _write(tmp_path / "bdp.yaml", values)
    assert portal_settings(_section(False, path)) is None
    settings = portal_settings(_section(True, path), runs_root=layout.experiment)
    assert isinstance(settings, RemoteSettings) and settings.enabled
    with pytest.raises(RemoteSettingsError, match="settings_file is required"):
        portal_settings(_section(True, None))


def test_the_experiment_directory_must_be_the_runs_root(tmp_path):
    layout = Layout(tmp_path)
    path = _write(tmp_path / "bdp.yaml", bdp_settings(layout))
    with pytest.raises(RemoteSettingsError) as error:
        portal_settings(_section(True, path), runs_root=tmp_path / "elsewhere")
    assert "experiment_directory" in str(error.value) and "files.runs_root" in str(error.value)
    # The default experiment directory is <APS filesystem root>/LauePortal.
    values = bdp_settings(layout)
    del values["experiment_directory"]
    path = _write(tmp_path / "default.yaml", values)
    settings = portal_settings(_section(True, path), runs_root=layout.aps / "LauePortal")
    assert settings is not None and settings.experiment_path() == layout.aps / "LauePortal"
    with pytest.raises(RemoteSettingsError, match="the default experiment"):
        portal_settings(_section(True, path), runs_root=layout.aps)


def test_an_experiment_directory_other_than_the_runs_root_is_named_by_its_key_not_its_value(tmp_path):
    """Settings errors name keys, never values (docs/contributing/backend.md), the directories included."""
    layout = Layout(tmp_path / "a-directory-that-must-not-be-echoed")
    path = _write(tmp_path / "bdp.yaml", bdp_settings(layout))
    runs_root = tmp_path / "another-directory-that-must-not-be-echoed"
    with pytest.raises(RemoteSettingsError) as error:
        portal_settings(_section(True, path), runs_root=runs_root)
    message = str(error.value)
    assert "experiment_directory" in message and "files.runs_root" in message
    assert "must-not-be-echoed" not in message and str(tmp_path) not in message
    values = bdp_settings(layout)
    del values["experiment_directory"]
    with pytest.raises(RemoteSettingsError) as error:
        portal_settings(_section(True, _write(tmp_path / "default.yaml", values)), runs_root=runs_root)
    assert "storage.aps.filesystem_root" in str(error.value) and str(tmp_path) not in str(error.value)


def _targets(aps: bool, crux: bool) -> RemoteSettings:
    values = bdp_settings(Layout(Path("/base")))
    values["targets"]["aps"]["enabled"] = aps
    values["targets"]["crux"]["enabled"] = crux
    return parse_settings(values)


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        (None, [("APS", "local")]),
        (_targets(False, False).model_copy(update={"enabled": False}), [("APS", "local")]),
        (_targets(False, False), [("APS", "local")]),
        (_targets(True, False), [("APS", "aps")]),
        (_targets(False, True), [("APS", "local"), ("ALCF", "crux")]),
        (_targets(True, True), [("APS", "aps"), ("ALCF", "crux")]),
    ],
)
def test_available_backends_follow_the_enabled_targets(settings, expected):
    assert [(option.label, option.value) for option in available_backends(settings)] == expected
    for _label, value in expected:
        require_backend(value, settings)
    offered = {value for _label, value in expected}
    for value in {"local", "aps", "crux"} - offered:
        with pytest.raises(ValueError, match="is not enabled"):
            require_backend(value, settings)
