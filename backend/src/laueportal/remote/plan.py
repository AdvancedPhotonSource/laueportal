"""Plan only the selected files and new run destinations; never copy raw data locally.

The submission plan ``bdp.json``, written once into the run directory and, with the same bytes, into the DM inbox
(``<submission_directory>/<submission>.json``), where the launcher finds it by submission ID. Its keys, values and
formatting are a fixed format, which the launcher reads (``tests/fixtures/remote_goldens/*/bdp.json`` pins it). ``job``
is anything with the job row's attributes (``job_id``, ``backend``, ``run_directory``, ``manifest_path``,
``manifest_digest``, ``n_inputs``, ``request_path``). No database, no portal settings (import-linter rule 3): the
launcher imports it.

A plan is refused (``RemoteSettingsError``, before anything is written) when its target lacks a key every run on
it reads (``RemoteSettings.target_document``), or when the launcher could not build the PBS script from it
(``remote.pbs.build_script``'s rules: one node and, for LaueGo, one rank per node on ``aps``, which starts no
``mpiexec``; one to four ranks per node; an allocation on ``crux``; safe PBS values): such a
submission's DM job would fail on the remote side.

A ``pioneer`` plan (LaueMatching on the GPU queue) also has:

- ``continuation``: the run goes on over sequential PBS allocations, one at a time, each attempting at most
  ``max_frames_per_allocation`` frames (``remote.stage`` submits them; ``jobs.runner`` runs one). The whole manifest
  stays one run, of any size.
- ``staged_inputs``, when the run reads a geometry, crystal, mask or background file that is not on the shared
  storage (``storage.aps.filesystem_root``, which the portal, the launcher and the GPU nodes mount at the same path):
  :func:`save_plan` copies each such file into ``<run>/staged-inputs/<key>/`` before the plan is written, and the plan
  records where it came from (``source``, its real path ``resolved``), its ``size``, ``sha256`` and file identity
  (``device``, ``inode``). The runner reads the copy, checked against that checksum (``jobs.runner.load_request``). A
  file on the shared storage is read where it is, and frames are never copied: every input must be on the shared
  storage. The request keeps the paths the run was created with.

Every geometry, crystal, mask and background file a ``pioneer`` plan reads, and its input folder, is checked against
the portal's actual file roots twice (``source_check``, which the portal passes: this module reads no portal settings,
import-linter rule 3): when the plan is built, and, for a staged file, again immediately before it is copied. The copy
reads the file it opened (``O_NOFOLLOW``), which must be the checked real path (``/proc/self/fd``) with the identity,
size and checksum the plan recorded: a path that resolves elsewhere since, a replaced file or a link swapped in is
refused, never copied. Without ``source_check`` a ``pioneer`` plan is refused.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from laueportal.indexing.laue_matching.cache import (
    DATABASE_NAME,
    ORIENTATION_BYTES,
    cache_folder,
    key_from_parameters,
    reuse_problem,
)
from laueportal.jobs.backends import SHARED, transport
from laueportal.jobs.rundir import (
    STAGED_INPUTS_DIRECTORY,
    create_unique_partial,
    publish_file,
    read_manifest,
    read_request,
    verify_manifest,
    write_json_file,
)
from laueportal.remote.paths import PLAN_NAME, child_path
from laueportal.remote.pbs import build_script
from laueportal.remote.settings import MAX_STREAM_IMAGES, RemoteSettings, RemoteSettingsError, collection_path

SourceCheck = Callable[[str], Path]
"""The portal's check of a path a run reads (``remote.submission.source_check``): its real path when it is inside the
portal's file roots (``files.roots.resolve_inside_roots``); an error otherwise."""

SUBMISSION_LOCK_NAME = ".bdp-submission.lock"
LAUE_MATCHING_KIND = "laue_matching_indexing"
REMOTE_KINDS = ("lauego_indexing", "wire_reconstruction", LAUE_MATCHING_KIND)
"""The run kinds a remote target computes: the runners and the poller name each of them."""
TARGET_KINDS: dict[str, tuple[str, ...]] = {
    "aps": ("lauego_indexing", "wire_reconstruction"),
    "crux": ("lauego_indexing", "wire_reconstruction"),
    "pioneer": (LAUE_MATCHING_KIND,),
}
"""The run kinds each remote target computes."""
STAGED_KEYS = ("geometry_file", "crystal_file", "mask_file", "background_file")
"""The small files a LaueMatching run reads besides its frames: a ``pioneer`` run reads each on the shared storage, or
a copy staged into its run directory."""
STAGED_INPUTS_DIRNAME = STAGED_INPUTS_DIRECTORY
"""``<run>/staged-inputs/<key>/<file name>``: a ``pioneer`` run's copies of the files it reads that are not on the
shared storage."""
MAX_STAGED_BYTES = 1 << 30
"""The largest file a plan stages (1 GiB): a geometry, crystal, mask or background file is far smaller; anything
larger is refused rather than copied."""
CONTINUATION_VERSION = 1


def submission_path(settings: RemoteSettings, submission_id: str) -> Path:
    submission_id = str(UUID(submission_id))
    return settings.submissions() / f"{submission_id}.json"


def build_plan(
    job: Any,
    settings: RemoteSettings,
    submission_id: str,
    *,
    progress: Callable[[], None] = lambda: None,
    source_check: SourceCheck | None = None,
) -> dict[str, Any]:
    """Keep request/manifest immutable, recording their digest and effective path map. ``progress`` is called
    after the manifest's check and after each input's (``crux``: one stat per input, slow on a slow mount).
    ``source_check``: the portal's check of the files a ``pioneer`` run reads (the module's docstring)."""
    submission_id = str(UUID(submission_id))
    target = settings.target_document(job.backend)
    source = settings.aps_storage()
    source_root = source.root("aps")
    run = child_path(source_root, job.run_directory)
    experiment = settings.experiment_path()
    child_path(experiment / "analysis", run)
    verify_manifest(job.manifest_path, expected_digest=job.manifest_digest, expected_count=job.n_inputs)
    progress()
    document = read_request(job.request_path)
    if document["kind"] not in REMOTE_KINDS:
        raise RemoteSettingsError(f"A {document['kind']} run computes only on the portal's own worker")
    if document["kind"] not in TARGET_KINDS.get(job.backend, ()):
        raise RemoteSettingsError(f"A {document['kind']} run does not compute on the {job.backend} target")
    xml = document.get("output", {}).get("xml")
    if xml and os.path.isabs(xml):
        child_path(run, xml)  # Remote jobs must publish all outputs inside their run.
    plan: dict[str, Any] = {
        "version": 1,
        "submission_id": submission_id,
        "job_id": job.job_id,
        "backend": job.backend,
        "run_directory": str(run),
        "request_sha256": hashlib.sha256(Path(job.request_path).read_bytes()).hexdigest(),
        "manifest_sha256": job.manifest_digest,
        "n_inputs": job.n_inputs,
        "kind": document["kind"],
        "execution": dict(target["execution"]),
        "pbs": dict(target["pbs"]),
    }
    if transport(job.backend) == SHARED:
        path_map = {
            str(run): str(run / "incoming" / submission_id),
            source_root: source_root,
        }
        staged: dict[str, dict[str, Any]] = {}
        if document["kind"] == LAUE_MATCHING_KIND:
            entries = read_manifest(job.manifest_path)
            files, staged = _laue_matching_files(
                document["request"], target, Path(source_root), run, progress, entries, source_check=source_check
            )
            path_map.update(files)
        plan.update(
            control_directory=str(run),
            output_directory=str(run / "incoming" / submission_id),
            path_map=path_map,
            target=dict(target),
        )
        if document["kind"] == LAUE_MATCHING_KIND:
            plan["continuation"] = {
                "version": CONTINUATION_VERSION,
                "max_frames_per_allocation": _frames_per_allocation(target),
            }
            if staged:
                plan["staged_inputs"] = staged
        _check_script(plan)
        return plan
    destination = settings.storage.eagle
    if destination is None or not source.collection_id or not destination.collection_id:
        raise ValueError("APS and Eagle Transfer collection IDs must be configured")
    remote = PurePosixPath(target["workspace"]) / "runs" / submission_id
    control, output = remote / "control", remote / "results"
    mappings = {str(run): str(output), source_root: str(remote / "inputs")}
    inputs = {entry.source for entry in read_manifest(job.manifest_path)}
    for key in ("geometry_file", "crystal_file", "mask_file"):
        value = document["request"].get(key)
        if value:
            inputs.add(value)
    items = []
    for path in sorted(inputs):
        local = child_path(experiment, path)
        if not local.is_file():
            raise ValueError(f"Input does not exist: {local}")
        progress()
        remote_file = remote / "inputs" / local.relative_to(Path(source_root).resolve()).as_posix()
        items.append(
            {
                "source_path": collection_path(source, local),
                "destination_path": collection_path(destination, remote_file),
                "recursive": False,
            }
        )
    # Only frozen control files are sent. Outputs and failure files never become inputs.
    for filename in ("request.json", "inputs.jsonl", PLAN_NAME):
        items.append(
            {
                "source_path": collection_path(source, run / filename),
                "destination_path": collection_path(destination, control / filename),
                "recursive": False,
            }
        )
    incoming = run / "incoming" / submission_id
    plan.update(
        control_directory=str(control),
        output_directory=str(output),
        incoming_directory=str(incoming),
        path_map=mappings,
        target=dict(target),
        input_transfer={
            "source_endpoint_id": source.collection_id,
            "destination_endpoint_id": destination.collection_id,
            "transfer_items": items,
        },
        result_transfer={
            "source_endpoint_id": destination.collection_id,
            "destination_endpoint_id": source.collection_id,
            "transfer_items": [
                {
                    "source_path": collection_path(destination, output),
                    "destination_path": collection_path(source, incoming),
                    "recursive": True,
                }
            ],
        },
        cancel_transfer={
            "source_endpoint_id": source.collection_id,
            "destination_endpoint_id": destination.collection_id,
            "transfer_items": [
                {
                    "source_path": collection_path(source, run / "cancel.request"),
                    "destination_path": collection_path(destination, control / "cancel.request"),
                    "recursive": False,
                }
            ],
        },
    )
    _check_script(plan)
    return plan


def _frames_per_allocation(target: dict[str, Any]) -> int:
    """``execution.max_frames_per_allocation``, at most one daemon's ``MAX_STREAM_IMAGES`` (also when not set)."""
    value = target["execution"].get("max_frames_per_allocation")
    frames = MAX_STREAM_IMAGES if value is None else int(value)
    if not 1 <= frames <= MAX_STREAM_IMAGES:
        raise RemoteSettingsError(
            f"remote settings: execution.max_frames_per_allocation must be from 1 to {MAX_STREAM_IMAGES}"
        )
    return frames


def on_shared_storage(shared: Path, path: str) -> bool:
    """Whether ``path``, its links resolved, is inside ``shared`` (the resolved shared root): never by its text."""
    try:
        child_path(shared, path)
    except ValueError:
        return False
    return True


def checked_source(source_check: SourceCheck | None, key: str, value: str) -> Path:
    """``value``'s real path, by the portal's check (``source_check``): ``RemoteSettingsError`` when there is no check
    or it refuses the path (outside the portal's file roots, a ``..``, a root that does not answer)."""
    if source_check is None:
        raise RemoteSettingsError(f"A GPU run's {key} is checked against the portal's file roots; no check was given")
    try:
        return Path(source_check(value))
    except Exception as error:  # the portal's refusal (PathNotAllowed, NotResponding): the plan is refused
        raise RemoteSettingsError(f"{key} is refused: {error}") from None


def _open_source(key: str, resolved: Path) -> tuple[int, os.stat_result]:
    """Open the checked real path ``resolved`` for reading: not through a link (``O_NOFOLLOW``), never blocking on a
    FIFO, a regular file, and (where ``/proc/self/fd`` tells) the very file at ``resolved``, so that a folder or link
    swapped in after the check is refused. Returns the descriptor and its ``fstat``."""
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(resolved, flags)
    except OSError as error:
        raise RemoteSettingsError(f"{key} cannot be opened: {error.strerror}") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise RemoteSettingsError(f"{key} is not a regular file")
        link = f"/proc/self/fd/{descriptor}"
        if os.path.islink(link) and os.readlink(link) != str(resolved):
            raise RemoteSettingsError(f"{key} was moved or replaced while it was opened; submit the run again")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, info


def _staged_input(key: str, value: str, resolved: Path, run: Path) -> dict[str, Any]:
    """What the plan records of a file it stages (``key``, ``source``, ``resolved``, ``path``, ``size``, ``sha256``,
    ``device``, ``inode``), read from the file opened at the checked real path ``resolved`` (:func:`_open_source`):
    its copy's path is ``<run>/staged-inputs/<key>/<its name>``. Refused unless it is a regular file of at most
    ``MAX_STAGED_BYTES``."""
    descriptor, info = _open_source(key, resolved)
    try:
        if info.st_size > MAX_STAGED_BYTES:
            raise RemoteSettingsError(
                f"{key} is larger than {MAX_STAGED_BYTES >> 20} MiB and not on the shared storage: it is not copied"
            )
        digest = hashlib.sha256()
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            while chunk := stream.read(1 << 20):
                digest.update(chunk)
    finally:
        os.close(descriptor)
    return {
        "key": key,
        "source": value,
        "resolved": str(resolved),
        "path": str(run / STAGED_INPUTS_DIRNAME / key / resolved.name),
        "size": info.st_size,
        "sha256": digest.hexdigest(),
        "device": info.st_dev,
        "inode": info.st_ino,
    }


def file_sha256(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _laue_matching_files(
    request: dict[str, Any],
    target: dict[str, Any],
    shared_root: Path,
    run: Path,
    progress: Callable[[], None],
    entries: Any = (),
    *,
    source_check: SourceCheck | None = None,
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """A LaueMatching run on ``pioneer``: its input folder and every input on the shared storage (``shared_root``,
    which the portal, the launcher and the GPU nodes mount at the same path; checked with links resolved, so a link
    leading off it is refused); its geometry, crystal, mask and background files read there, or staged into the run
    (:func:`_staged_input`) when they are not; and the target's data folder (``data_root``) holding the orientation
    database and the run's forward cache, exactly fitting the run's frozen values (a remote run never builds a cache).
    The path map entry from the portal's data folder (the request's ``data_root``) to the target's, and the staged
    files by key. The input folder and each of those files must pass the portal's ``source_check`` first."""
    shared = shared_root.resolve()
    folder = request.get("input_path")
    if folder:
        checked_source(source_check, "input_path", folder)
        if not on_shared_storage(shared, folder):
            raise RemoteSettingsError(f"input_path must be on the shared storage {shared}, which the GPU nodes mount")
    staged = {}
    for key in STAGED_KEYS:
        value = request.get(key)
        if not value:
            continue
        resolved = checked_source(source_check, key, value)
        if not on_shared_storage(shared, str(resolved)):
            staged[key] = _staged_input(key, value, resolved, run)
            progress()
    for entry in entries:  # each input as it resolves (a link inside the folder may lead out of it)
        if not on_shared_storage(shared, entry.source):
            raise RemoteSettingsError(
                f"Input {entry.input_id} must be on the shared storage {shared}, which the GPU nodes mount"
            )
        progress()
    root = Path(target["data_root"])
    database = root / DATABASE_NAME
    try:
        size = database.stat().st_size
    except OSError as error:
        raise RemoteSettingsError(f"The orientation database {database} cannot be read: {error.strerror}") from None
    if size != int(request["n_orientations"]) * ORIENTATION_BYTES:
        raise RemoteSettingsError(f"The orientation database {database} is not the one this run was created with")
    progress()
    name = str(request["forward_cache"])
    problem = reuse_problem(cache_folder(root, name), key_from_parameters(request))
    if problem is not None:
        raise RemoteSettingsError(f"Forward cache {name} is not installed on the target as this run needs: {problem}")
    progress()
    portal_root = request.get("data_root")
    if not portal_root:
        raise RemoteSettingsError("The run names no LaueMatching data folder")
    return {str(portal_root): str(root)}, staged


def _check_script(plan: dict[str, Any]) -> None:
    """Refuse a plan the launcher could not build its PBS script from (``remote.pbs.build_script``)."""
    backend = plan["backend"]
    ranks = plan["execution"].get("indexing_ranks_per_node", 1)
    if transport(backend) == SHARED and plan["kind"] == "lauego_indexing" and int(ranks) != 1:
        raise RemoteSettingsError(
            f"remote settings: targets.{backend}.execution.indexing_ranks_per_node must be 1: the {backend} target "
            "starts one process (no mpiexec), so more ranks fail the MPI runner's size check"
        )
    try:
        build_script(plan)
        if plan.get("continuation") is not None:
            build_script(plan, finalize=True)  # its finalizing allocations' CPU request
    except ValueError as error:
        raise RemoteSettingsError(f"remote settings: targets.{backend} cannot run this job: {error}") from None


def save_plan(
    job: Any,
    settings: RemoteSettings,
    submission_id: str,
    *,
    progress: Callable[[], None] = lambda: None,
    source_check: SourceCheck | None = None,
) -> dict[str, Any]:
    plan = build_plan(job, settings, submission_id, progress=progress, source_check=source_check)
    incoming = Path(plan.get("incoming_directory", plan["output_directory"]))
    if incoming.exists():
        raise FileExistsError(f"Return destination already exists: {incoming}")
    # A second process must not race the shared partial-file publisher.
    with open(Path(job.run_directory) / SUBMISSION_LOCK_NAME, "x", encoding="utf-8") as lock:
        lock.write(submission_id)
    for spec in plan.get("staged_inputs", {}).values():
        stage_input(spec, source_check)
    path = Path(job.run_directory) / PLAN_NAME
    write_json_file(path, plan)  # Exclusive publication; no existing file is replaced.
    inbox = submission_path(settings, submission_id)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    write_json_file(inbox, plan)
    return plan


def stage_input(spec: dict[str, Any], source_check: SourceCheck | None) -> None:
    """Copy one staged file (``spec``: :func:`_staged_input`'s record) to its ``path`` in the run, through a partial
    file synced before its rename. Immediately before, the source is checked again against the portal's file roots
    (``source_check``: it must still resolve to the recorded real path) and opened at that path
    (:func:`_open_source`): its device, inode and size must be those recorded, and the bytes copied the recorded
    ``sha256``. Anything else is refused: a source that changed, was replaced or now resolves elsewhere is never
    copied. An existing copy is kept only when it holds the recorded bytes (a copy is never replaced). The source is
    never written."""
    key = spec.get("key", "a staged file")
    destination = Path(spec["path"])
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or file_sha256(destination) != spec["sha256"]:
            raise FileExistsError(f"A different staged file already exists: {destination}")
        return
    resolved = checked_source(source_check, key, spec["source"])
    if str(resolved) != spec["resolved"]:
        raise RemoteSettingsError(f"{key} now resolves to another file than the plan checked; submit the run again")
    descriptor, info = _open_source(key, resolved)
    try:
        if (info.st_dev, info.st_ino, info.st_size) != (spec["device"], spec["inode"], spec["size"]):
            raise RemoteSettingsError(f"{key} was replaced or changed after the plan read it; submit the run again")
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial, target = create_unique_partial(destination)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(os.dup(descriptor), "rb") as source, open(target, "wb") as copy:
                while chunk := source.read(1 << 20):
                    digest.update(chunk)
                    size += len(chunk)
                    copy.write(chunk)
            if (digest.hexdigest(), size) != (spec["sha256"], spec["size"]):
                raise RemoteSettingsError(f"{key} changed while it was staged; submit the run again")
            os.chmod(partial, 0o444)
            publish_file(partial, destination)  # synced; never replaces a file that appeared meanwhile
        finally:
            partial.unlink(missing_ok=True)
    finally:
        os.close(descriptor)
