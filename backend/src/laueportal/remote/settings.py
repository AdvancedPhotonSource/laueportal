"""The remote-execution settings: ``bdp.yaml``, read by the portal and by the launcher on the DM host.

The YAML file's keys are fixed (``deploy/remote/bdp.yaml.template``), so a file written for an earlier deployment
loads as it is; it stays a separate file because the launcher reads it on another host. The portal's TOML settings only
name it (``[remote]``: ``enabled``, ``settings_file``).

What is checked, and when:

- :func:`load_settings` (both sides) reads the YAML into :class:`RemoteSettings`. It refuses unknown keys at every
  level, and values the code could not use: each key takes the types its readers accept (a PBS queue, walltime or
  account any scalar, written as ``str(value)``; a node, CPU, rank or thread count, or a memory limit, a whole number,
  also as a float or a numeric string, read as ``int(value)``; worker counts integers; ``dm.timeout_seconds`` and
  ``dm.permissions_timeout_seconds`` a number, or null for no timeout). Values are never converted, so a section copied
  into the plan (:meth:`RemoteSettings.target_document`, ``model_dump(exclude_unset=True)``) is exactly what the file
  holds: the keys present, and only those, with their values. No key is required here: the launcher's copy of the file
  needs only what the launcher reads. The file's top-level ``enabled`` is ignored (any scalar loads; only ``true`` reads
  as enabled). Two values a run could start with are refused on purpose: ``auth_profile: null`` (it would use the
  beamline profile with a ``gladier-None.cfg``) and a target ``enabled`` that is not a boolean (a truthiness test would
  read any non-empty value, even ``"false"``, as enabled). An SSH host (``dm.ssh_host``, ``dm.launcher_ssh_host``,
  ``targets.aps.submit_host``) that starts with ``-`` is refused too: ssh would read it as an option.
- :func:`portal_settings` is the portal's view: the YAML when ``[remote]`` enables it (``enabled`` forced
  true), else ``None``. It requires what every submission and poll reads (the DM settings, the APS
  storage root, the submission directory), a positive ``poll_seconds``, ``dm.timeout_seconds`` and
  ``dm.permissions_timeout_seconds`` (or none), and refuses an experiment directory other than ``files.runs_root``.
- A target is checked when a run is planned for it (:meth:`RemoteSettings.target_document`): the keys every
  run on it reads. A disabled target may be incomplete: nothing reads it.
- The launcher checks the keys each of its commands reads (``remote.stage``, ``remote.flow``,
  ``remote.auth``).

``child_path`` and ``map_path`` live in ``remote.paths`` (the compute side uses them too).

Standard library, PyYAML and pydantic only (the launcher's lock set has them): no database, no portal
settings (import-linter rule 3), no I/O at import.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, Protocol

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
)

from laueportal.jobs.backends import REMOTE_BACKENDS
from laueportal.remote.paths import child_path


class RemoteSettingsError(ValueError):
    """The remote settings are missing, invalid or disagree with the portal's. Names keys, never values."""


def _whole_number(value: int | float | str) -> int | float | str:
    """A count read with ``int(value)``: an integer, a float without a fraction, or text ``int()`` reads.
    The value is kept as written; a fraction ``int()`` would drop is refused."""
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("must be a whole number (it is read with int())")
    if isinstance(value, str):
        try:
            int(value)
        except ValueError:
            raise ValueError("must be a whole number (it is read with int())") from None
    return value


def _positive(value: int | float) -> int | float:
    """A positive, finite number of seconds."""
    if not (math.isfinite(value) and value > 0):
        raise ValueError("must be a positive, finite number of seconds")
    return value


def _ssh_host(value: str) -> str:
    """An SSH destination goes to ``ssh`` as an argument before the remote command: a value starting with ``-``
    would be read as an option (``-oProxyCommand=...``), so it is refused."""
    if value.startswith("-"):
        raise ValueError("must not start with '-' (ssh would read it as an option)")
    return value


Number = StrictInt | StrictFloat
Scalar = StrictStr | StrictInt | StrictFloat
"""A PBS value written into the script as ``str(value)`` (after checking it): a queue, a walltime, an account."""
WholeNumber = Annotated[StrictInt | StrictFloat | StrictStr, AfterValidator(_whole_number)]
"""A count read with ``int(value)``."""
SSHHost = Annotated[StrictStr, AfterValidator(_ssh_host)]
"""A host the portal or the launcher runs ``ssh`` to."""
Seconds = Annotated[Number, AfterValidator(_positive)]
"""A positive, finite duration in seconds."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DMSettings(_Model):
    """``dm``: the DM API (through its setup script, locally or over SSH) and the launcher it runs.

    The portal needs ``setup_file``, ``owner`` and ``workflow`` (:func:`portal_settings` checks them); the
    launcher's copy of the file may leave out ``owner`` and ``workflow``, and needs ``setup_file`` for its
    ``permissions`` stage (one DM call on the DM host)."""

    ssh_host: SSHHost | None = None
    setup_file: StrictStr | None = None
    owner: StrictStr | None = None
    user_account: StrictStr | None = None
    workflow: StrictStr | None = None
    cancel_workflow: StrictStr | None = None
    cleanup_workflow: StrictStr | None = None
    """The DM workflow that runs the launcher's ``cleanup`` stage once a run is
    published as Finished (``remote.cleanup``). Not set: the poller removes only the return inbox's remnants."""
    timeout_seconds: Number | None = 30  # null: no timeout
    permissions_timeout_seconds: Number | None = 300
    """The bound on the launcher's ``permissions`` stage's DM call (``restorePermissions``
    updates the returned files' metadata recursively), in place of ``timeout_seconds``; null: no timeout, as
    ``timeout_seconds``."""
    launcher_ssh_host: SSHHost | None = None
    launcher_python: StrictStr | None = None
    launcher_code: StrictStr | None = None
    launcher_config: StrictStr | None = None


class StorageSettings(_Model):
    """A filesystem and the Globus Transfer collection that exposes it."""

    collection_id: StrictStr | None = None
    filesystem_root: StrictStr | None = None
    collection_root: StrictStr | None = None

    def root(self, name: str) -> str:
        """``filesystem_root``, which every use of a storage section reads."""
        if self.filesystem_root is None:
            raise RemoteSettingsError(f"storage.{name}.filesystem_root is required")
        return self.filesystem_root


class StorageSections(_Model):
    aps: StorageSettings | None = None
    eagle: StorageSettings | None = None


class PBSSettings(_Model):
    account: Scalar | None = None
    queue: Scalar | None = None
    walltime: Scalar | None = None
    nodes: WholeNumber = 1
    cpus: WholeNumber = 32


class ExecutionPolicy(_Model):
    """A target's resources, copied into the plan's ``execution`` (the runners read them)."""

    workers: StrictInt | None = None
    max_in_flight: StrictInt | None = None
    reconstruction_workers: StrictInt | None = None
    threads_per_worker: WholeNumber | None = None
    indexing_ranks_per_node: WholeNumber = 1
    indexing_threads_per_worker: WholeNumber | None = None
    memory_limit_mb: WholeNumber | None = None


class _Target(_Model):
    enabled: StrictBool = False
    analysis_python: StrictStr | None = None
    code_directory: StrictStr | None = None
    module_commands: list[StrictStr] = []
    pbs: PBSSettings | None = None
    execution: ExecutionPolicy | None = None

    def missing_keys(self) -> list[str]:
        """The keys every run on this target reads that the file leaves out (or sets to null)."""
        missing = [
            key for key in ("analysis_python", "code_directory", "pbs", "execution") if getattr(self, key) is None
        ]
        if self.pbs is not None:
            missing += [f"pbs.{key}" for key in ("queue", "walltime") if getattr(self.pbs, key) is None]
        if self.execution is not None:
            missing += [
                f"execution.{key}"
                for key in ("workers", "reconstruction_workers", "threads_per_worker")
                if getattr(self.execution, key) is None
            ]
        return missing


class ApsTarget(_Target):
    """``targets.aps``: PBS on the APS cluster, submitted by the launcher (``qsub``, over SSH if set)."""

    submit_host: SSHHost | None = None


class ListingSettings(_Model):
    """``targets.crux.listing``: the launcher's ``monitor`` stage lists a run's
    ``results/`` on Eagle (one Transfer ``operation_ls``) only while its Flow is in ``RunPbsJob``, at most every
    ``interval_seconds`` (twice as long after each failed listing, up to 60 s), waiting at most ``timeout_seconds``
    for it, and reports the run stale when its newest status file is more than ``stale_after_seconds`` old. Only the
    launcher reads it. Keep ``interval_seconds`` well under 90 s: the runs between listings print the kept
    observation, and the portal shows one observed more than 90 s before as delayed (an interval over about 60 s
    makes the last runs before each listing read "status delayed"); keep ``timeout_seconds`` under
    ``interval_seconds``."""

    interval_seconds: Seconds = 30
    timeout_seconds: Seconds = 8
    stale_after_seconds: Seconds = 90


STATUS_FAILURE_BUDGET_SECONDS = 600
"""``targets.crux.status_failure_budget_seconds``'s default (10 minutes)."""


class CruxTarget(_Target):
    """``targets.crux``: the Globus flow (transfer, Globus Compute's PBS task on Crux, return).
    ``status_failure_budget_seconds`` (only the launcher reads it): while the
    Flow status call of the launcher's ``monitor`` stage fails, the stage prints the kept observation, stale, and
    carries on; the first call that fails once the calls have failed for longer than this fails the stage, as any
    other failure does, so DM ends the run."""

    compute_endpoint: StrictStr | None = None
    workspace: StrictStr | None = None
    listing: ListingSettings = ListingSettings()
    status_failure_budget_seconds: Seconds = STATUS_FAILURE_BUDGET_SECONDS

    def missing_keys(self) -> list[str]:
        return [
            *super().missing_keys(),
            *(key for key in ("compute_endpoint", "workspace") if getattr(self, key) is None),
        ]


class TargetSections(_Model):
    aps: ApsTarget | None = None
    crux: CruxTarget | None = None


def _ignored_flag(value: object) -> object:
    """The file's own ``enabled``, which is ignored (the portal's ``[remote] enabled`` replaces it, and the launcher
    never reads it): any scalar loads, and only ``true`` reads as enabled. A mapping or a list is still refused."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value is True
    return value


class RemoteSettings(_Model):
    """``bdp.yaml``, with each key's default where the file leaves it out."""

    enabled: Annotated[StrictBool, BeforeValidator(_ignored_flag)] = False
    experiment_directory: StrictStr | None = None
    poll_seconds: Number = 10
    submission_directory: StrictStr | None = None
    state_directory: StrictStr | None = None
    auth_profile: StrictStr = "personal"
    credentials_file: StrictStr | None = None
    dm: DMSettings = DMSettings()
    storage: StorageSections = StorageSections()
    targets: TargetSections = TargetSections()

    def submissions(self) -> Path:
        """``submission_directory``: the DM inbox, which the portal and the launcher both read."""
        if self.submission_directory is None:
            raise RemoteSettingsError("submission_directory is required")
        return Path(self.submission_directory)

    def aps_storage(self) -> StorageSettings:
        """``storage.aps``, with its filesystem root: where run directories and their inputs live."""
        if self.storage.aps is None:
            raise RemoteSettingsError("storage.aps is required")
        self.storage.aps.root("aps")
        return self.storage.aps

    def experiment_path(self) -> Path:
        """The experiment directory: run directories live under ``<experiment>/analysis/`` (by default
        ``<APS filesystem root>/LauePortal``)."""
        if self.experiment_directory is not None:
            return Path(self.experiment_directory)
        return Path(self.aps_storage().root("aps")) / "LauePortal"

    def target(self, backend: str) -> ApsTarget | CruxTarget:
        target = getattr(self.targets, backend, None) if backend in REMOTE_BACKENDS else None
        if target is None:
            raise RemoteSettingsError(f"targets.{backend} is not configured")
        return target

    def target_document(self, backend: str) -> dict[str, Any]:
        """``targets.<backend>`` as the file holds it (the plan's ``target``, ``execution`` and ``pbs``), once it
        has every key a run on it reads."""
        target = self.target(backend)
        missing = target.missing_keys()
        if missing:
            keys = ", ".join(f"targets.{backend}.{key}" for key in missing)
            raise RemoteSettingsError(f"remote settings: target {backend} cannot run jobs without {keys}")
        return target.model_dump(exclude_unset=True)


def format_validation_error(error: ValidationError) -> str:
    """One line per problem, naming the key, never its value."""
    lines = []
    for item in error.errors(include_url=False, include_input=False, include_context=False):
        where = ".".join(str(part) for part in item["loc"]) or "(top level)"
        lines.append(f"  {where}: {item['msg']}")
    return "\n".join(lines)


def parse_settings(values: Mapping[str, Any], *, source: str = "remote settings") -> RemoteSettings:
    """Validate a mapping with ``bdp.yaml``'s keys."""
    try:
        return RemoteSettings.model_validate(dict(values))
    except ValidationError as error:
        raise RemoteSettingsError(f"invalid {source}:\n{format_validation_error(error)}") from None


def load_settings(path: str | os.PathLike[str]) -> RemoteSettings:
    """Read a ``bdp.yaml`` (YAML values are literal, nothing is expanded)."""
    try:
        with open(path, encoding="utf-8") as handle:
            values = yaml.safe_load(handle)
    except FileNotFoundError:
        raise RemoteSettingsError(f"remote settings file {path} does not exist") from None
    except OSError as error:  # a directory, or a private file this account may not read
        raise RemoteSettingsError(f"remote settings file {path} cannot be read: {error.strerror}") from None
    except yaml.YAMLError as error:
        raise RemoteSettingsError(f"remote settings file {path} is not valid YAML: {error}") from None
    if not isinstance(values, dict):
        raise RemoteSettingsError("BDP settings must be a YAML mapping")
    return parse_settings(values, source=f"remote settings file {path}")


class RemoteSection(Protocol):
    """The portal's ``[remote]`` settings (``core.settings.RemoteSection``), which this module never imports."""

    @property
    def enabled(self) -> bool: ...

    @property
    def settings_file(self) -> os.PathLike[str] | str | None: ...


def check_portal_settings(settings: RemoteSettings) -> None:
    """Refuse settings without what every submission and poll reads: ``dm.setup_file``, ``dm.owner`` and
    ``dm.workflow`` (the DM API), ``storage.aps.filesystem_root`` and ``submission_directory``; and a
    ``poll_seconds``, ``dm.timeout_seconds`` or ``dm.permissions_timeout_seconds`` that is not positive (the poller
    would call DM back to back, and every DM call, or the permissions stage's, would time out at once). The
    launcher's copy is not checked here."""
    missing = [f"dm.{key}" for key in ("setup_file", "owner", "workflow") if getattr(settings.dm, key) is None]
    if settings.storage.aps is None or settings.storage.aps.filesystem_root is None:
        missing.append("storage.aps.filesystem_root")
    if settings.submission_directory is None:
        missing.append("submission_directory")
    if missing:
        raise RemoteSettingsError(f"remote settings: the portal needs {', '.join(missing)}")
    for key, value in (
        ("poll_seconds", settings.poll_seconds),
        ("dm.timeout_seconds", settings.dm.timeout_seconds),
        ("dm.permissions_timeout_seconds", settings.dm.permissions_timeout_seconds),
    ):
        if value is not None and not (math.isfinite(value) and value > 0):
            raise RemoteSettingsError(f"remote settings: {key} must be a positive, finite number of seconds")


def check_experiment_directory(settings: RemoteSettings, runs_root: str | os.PathLike[str]) -> None:
    """Refuse an experiment directory other than the portal's ``files.runs_root``.

    The portal creates run directories under ``runs_root`` (``analysis/...``); the plan accepts only runs inside
    ``<experiment>/analysis/``. Two different directories would fail every submission, so they must agree.
    """
    experiment = settings.experiment_path()
    if os.path.realpath(experiment) != os.path.realpath(os.fspath(runs_root)):
        key = (
            "experiment_directory"
            if settings.experiment_directory is not None
            else "the default experiment directory (<storage.aps.filesystem_root>/LauePortal)"
        )
        raise RemoteSettingsError(
            f"remote settings: {key} is not the portal's files.runs_root: run directories must live under "
            "<experiment>/analysis/, so the two must name the same directory"
        )


def portal_settings(
    section: RemoteSection, *, runs_root: str | os.PathLike[str] | None = None
) -> RemoteSettings | None:
    """The remote settings the portal uses, or ``None`` when remote execution is disabled (the
    file's own ``enabled`` is replaced by the portal's)."""
    if not section.enabled:
        return None
    if section.settings_file is None:
        raise RemoteSettingsError("remote.settings_file is required when remote execution is enabled")
    settings = load_settings(section.settings_file).model_copy(update={"enabled": True})
    check_portal_settings(settings)
    if runs_root is not None:
        check_experiment_directory(settings, runs_root)
    return settings


def collection_path(storage: StorageSettings, path: str | os.PathLike[str]) -> str:
    """``path`` (inside ``storage``'s filesystem root) as a path in its Transfer collection."""
    if storage.filesystem_root is None or storage.collection_root is None:
        raise RemoteSettingsError("storage filesystem_root and collection_root are required for Globus transfers")
    root = storage.filesystem_root
    relative = child_path(root, path).relative_to(os.path.realpath(root))
    return str(PurePosixPath(storage.collection_root) / relative.as_posix())


class BackendOption(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: Literal["APS", "ALCF"]
    value: Literal["local", "aps", "crux"]


def available_backends(settings: RemoteSettings | None) -> list[BackendOption]:
    """The form's choices: one APS choice, ``local`` (the portal's worker) until the ``aps`` target is enabled;
    ALCF when the ``crux`` target is."""
    targets = settings.targets if settings is not None and settings.enabled else TargetSections()
    aps_backend = "aps" if targets.aps is not None and targets.aps.enabled else "local"
    options = [BackendOption(label="APS", value=aps_backend)]
    if targets.crux is not None and targets.crux.enabled:
        options.append(BackendOption(label="ALCF", value="crux"))
    return options


def require_backend(name: str | None, settings: RemoteSettings | None) -> None:
    if name not in {option.value for option in available_backends(settings)}:
        raise ValueError(f"Execution target {name!r} is not enabled")
