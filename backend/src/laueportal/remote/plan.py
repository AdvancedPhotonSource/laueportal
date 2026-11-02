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
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from laueportal.jobs.rundir import read_manifest, read_request, verify_manifest, write_json_file
from laueportal.remote.paths import PLAN_NAME, child_path
from laueportal.remote.pbs import build_script
from laueportal.remote.settings import RemoteSettings, RemoteSettingsError, collection_path

SUBMISSION_LOCK_NAME = ".bdp-submission.lock"


def submission_path(settings: RemoteSettings, submission_id: str) -> Path:
    submission_id = str(UUID(submission_id))
    return settings.submissions() / f"{submission_id}.json"


def build_plan(
    job: Any, settings: RemoteSettings, submission_id: str, *, progress: Callable[[], None] = lambda: None
) -> dict[str, Any]:
    """Keep request/manifest immutable, recording their digest and effective path map. ``progress`` is called
    after the manifest's check and after each input's (``crux``: one stat per input, slow on a slow mount)."""
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
    if job.backend == "aps":
        plan.update(
            control_directory=str(run),
            output_directory=str(run / "incoming" / submission_id),
            path_map={
                str(run): str(run / "incoming" / submission_id),
                source_root: source_root,
            },
            target=dict(target),
        )
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


def _check_script(plan: dict[str, Any]) -> None:
    """Refuse a plan the launcher could not build its PBS script from (``remote.pbs.build_script``)."""
    backend = plan["backend"]
    ranks = plan["execution"].get("indexing_ranks_per_node", 1)
    if backend == "aps" and plan["kind"] == "lauego_indexing" and int(ranks) != 1:
        raise RemoteSettingsError(
            "remote settings: targets.aps.execution.indexing_ranks_per_node must be 1: the aps target starts one "
            "process (no mpiexec), so more ranks fail the MPI runner's size check"
        )
    try:
        build_script(plan)
    except ValueError as error:
        raise RemoteSettingsError(f"remote settings: targets.{backend} cannot run this job: {error}") from None


def save_plan(
    job: Any, settings: RemoteSettings, submission_id: str, *, progress: Callable[[], None] = lambda: None
) -> dict[str, Any]:
    plan = build_plan(job, settings, submission_id, progress=progress)
    incoming = Path(plan.get("incoming_directory", plan["output_directory"]))
    if incoming.exists():
        raise FileExistsError(f"Return destination already exists: {incoming}")
    # A second process must not race the shared partial-file publisher.
    with open(Path(job.run_directory) / SUBMISSION_LOCK_NAME, "x", encoding="utf-8") as lock:
        lock.write(submission_id)
    path = Path(job.run_directory) / PLAN_NAME
    write_json_file(path, plan)  # Exclusive publication; no existing file is replaced.
    inbox = submission_path(settings, submission_id)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    write_json_file(inbox, plan)
    return plan
