"""Remote execution boundaries: immutable inputs, duplicate prevention, and validated return.

The run comes from ``tests.remote_support`` (the portal's run creation under an APS root, the BDP settings as
``remote.settings.RemoteSettings``), DM is a fake, and the tests that run native compute are slow.
``test_four_node_pbs_layout`` checks the multi-node layout of ``remote.pbs``. No network, DM, Globus or PBS.
"""

from __future__ import annotations

import copy
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import h5py
import pytest
from sqlalchemy.orm import Session

from laueportal.indexing.models import IndexingRun
from laueportal.jobs import controls, reconciler, runner
from laueportal.jobs.rundir import ManifestError, read_manifest
from laueportal.jobs.schemas import CancelState, MoveState
from laueportal.jobs.state import ExecutionStateError, JobStatus, RunPhase
from laueportal.remote import plan, poller, submission
from laueportal.remote.dm import CallAborted, DMClient, DMRecordError, Records, launcher_command, parse_records
from laueportal.remote.pbs import build_script
from laueportal.remote.settings import DMSettings, RemoteSettingsError, StorageSettings, collection_path
from tests.remote_support import (
    BatchedFake,
    Layout,
    bdp_settings,
    create_lauego_run,
    create_wire_run,
    job_view,
    remote_settings,
    stage,
)
from tests.run_support import load_job, make_engine
from tests.worker_support import alive, kill_quietly, wait_for


class FakeDM(BatchedFake):
    def __init__(self, *, timeout=False):
        self.submissions = []
        self.timeout = timeout
        self.record = {"id": "dm-001", "status": "pending"}

    def submit(self, document):
        self.submissions.append(document)
        if self.timeout:
            raise TimeoutError("response lost")
        return self.record

    def get(self, job_id):
        assert job_id == "dm-001"
        return self.record

    def find(self, submission_id):
        return [self.record]


@pytest.fixture
def remote_run(tmp_path):
    """A published two-point wire reconstruction on ``crux`` and the BDP settings for it."""

    layout = Layout(tmp_path)
    engine = make_engine(tmp_path / "portal")
    run = create_wire_run(engine, layout, count=2, backend="crux")
    values = bdp_settings(layout)
    yield engine, run, remote_settings(values), values
    engine.dispose()


def stage_fixture(document, values):
    """Emulate collection paths on two independent temporary filesystems."""
    stage(document, values)


def test_collection_roots_match_the_supplied_transfer_paths(tmp_path):
    storage = StorageSettings(filesystem_root=str(tmp_path / "gdata"), collection_root="/")
    assert collection_path(storage, tmp_path / "gdata/LauePortal/data/point.h5") == "/LauePortal/data/point.h5"
    with pytest.raises(ValueError):
        collection_path(storage, tmp_path / "gdata_other/file")
    (tmp_path / "gdata").mkdir()
    (tmp_path / "gdata/link").symlink_to(tmp_path)
    with pytest.raises(ValueError):
        collection_path(storage, tmp_path / "gdata/link/secret")


def test_successful_submission_is_not_repeated(remote_run):
    engine, run, settings, _ = remote_run
    client = FakeDM()
    assert submission.submit_run(run.job_id, engine=engine, settings=settings, client=client) == "dm-001"
    with pytest.raises(ExecutionStateError):
        submission.submit_run(run.job_id, engine=engine, settings=settings, client=client)
    assert len(client.submissions) == 1
    job = load_job(engine, run.job_id)
    assert job.external_id == "dm-001" and job.phase == RunPhase.QUEUED
    assert not reconciler.reconcile_runs(engine, stale_after_seconds=1, creation_timeout_seconds=1)
    assert controls.move_run_to_front(run.job_id, engine=engine).state == MoveState.REMOTE


def test_lost_submit_response_is_reconciled_without_resubmission(remote_run):
    engine, run, settings, _ = remote_run
    client = FakeDM(timeout=True)
    with pytest.raises(TimeoutError):
        submission.submit_run(run.job_id, engine=engine, settings=settings, client=client)
    assert load_job(engine, run.job_id).external_state == "submission_uncertain"
    poller.poll_once(engine=engine, settings=settings, client=client)
    assert load_job(engine, run.job_id).external_id == "dm-001"
    assert len(client.submissions) == 1


@pytest.mark.slow
def test_real_wire_runner_and_return_publication_preserve_original_inputs(remote_run):
    engine, run, settings, values = remote_run
    request_path, manifest_path = Path(run.output_path) / "request.json", Path(run.output_path) / "inputs.jsonl"
    original = request_path.read_bytes(), manifest_path.read_bytes()
    client = FakeDM()
    submission.submit_run(run.job_id, engine=engine, settings=settings, client=client)
    document = client.submissions[0]
    stage_fixture(document, values)
    summary = runner.run(document["control_directory"])
    assert summary["status"] == "Finished"
    assert summary["counters"]["n_succeeded"] == 2
    shutil.copytree(document["output_directory"], document["incoming_directory"])
    # A late cancel must not block collection of an already completed DM result.
    assert controls.cancel_run(run.job_id, engine=engine).state == CancelState.REQUESTED
    client.record["status"] = "done"
    assert poller.poll_once(engine=engine, settings=settings, client=client) == [run.job_id]
    job = load_job(engine, run.job_id)
    assert (job.status, job.n_succeeded, job.n_not_run) == (JobStatus.FINISHED, 2, 0)
    assert (Path(run.output_path) / "reconstruction/scan.h5").exists()
    assert original == (request_path.read_bytes(), manifest_path.read_bytes())
    assert poller.publish_returned(document)["status"] == "Finished"
    with pytest.raises(FileExistsError):
        runner.run(document["control_directory"])


@pytest.mark.slow
def test_real_indexing_runner_returns_results_xml_and_catalog_counts(remote_run, tmp_path):
    engine, _wire_run, settings, values = remote_run
    run = create_lauego_run(engine, Layout(tmp_path), count=3, backend="crux")
    client = FakeDM()
    submission.submit_run(run.job_id, engine=engine, settings=settings, client=client)
    document = client.submissions[0]
    stage_fixture(document, values)
    summary = runner.run(document["control_directory"])
    assert summary["status"] == "Finished"
    assert summary["counters"]["n_succeeded"] == 3
    shutil.copytree(document["output_directory"], document["incoming_directory"])
    client.record["status"] = "done"
    poller.poll_once(engine=engine, settings=settings, client=client)
    assert load_job(engine, run.job_id).status == JobStatus.FINISHED
    with Session(engine) as session:
        stored = session.get(IndexingRun, run.id)
        assert stored is not None
        assert stored.n_frames_indexed == 2
        assert stored.results_path is not None and Path(stored.results_path).is_file()
    assert (Path(run.output_path) / "output.xml").is_file()
    expected_sources = [entry.source for entry in read_manifest(Path(run.output_path) / "inputs.jsonl")]
    with h5py.File(Path(run.output_path) / "output.h5", "r") as handle:
        assert handle["frames/input_images"].asstr()[:].tolist() == expected_sources  # type: ignore[union-attr]
        assert all(
            str(Path(document["control_directory"]).parent / "inputs") in value
            for value in handle["provenance/remote_input_images"].asstr()[:]  # type: ignore[union-attr]
        )
    xml = ET.parse(Path(run.output_path) / "output.xml")
    assert [node.text for node in xml.iter("inputImage")] == expected_sources


@pytest.mark.parametrize("bad_fields", [{"job_id": -1}, {"counters": {"n_inputs": "2"}}])
def test_invalid_return_finishes_failed_without_publishing_output(remote_run, bad_fields):
    engine, run, settings, _ = remote_run
    client = FakeDM()
    submission.submit_run(run.job_id, engine=engine, settings=settings, client=client)
    document = client.submissions[0]
    incoming = Path(document["incoming_directory"])
    incoming.mkdir(parents=True)
    (incoming / "run.json").write_text(
        json.dumps(
            {
                "job_id": run.job_id,
                "submission_id": document["submission_id"],
                "kind": document["kind"],
                **bad_fields,
            }
        )
    )
    client.record["status"] = "done"
    poller.poll_once(engine=engine, settings=settings, client=client)
    assert load_job(engine, run.job_id).status == JobStatus.FAILED
    assert not (Path(run.output_path) / "run.json").exists()


def test_tampered_manifest_is_rejected_before_compute(remote_run):
    engine, run, settings, values = remote_run
    document = plan.save_plan(job_view(engine, run.job_id), settings, str(uuid4()))
    stage_fixture(document, values)
    with open(Path(document["control_directory"]) / "inputs.jsonl", "a") as handle:
        handle.write("{}\n")
    with pytest.raises(ManifestError):
        runner.run(document["control_directory"])
    assert not (Path(document["output_directory"]) / "reconstruction").exists()


@pytest.mark.slow
def test_return_does_not_overwrite_an_existing_result(remote_run):
    engine, run, settings, values = remote_run
    document = plan.save_plan(job_view(engine, run.job_id), settings, str(uuid4()))
    stage_fixture(document, values)
    runner.run(document["control_directory"])
    shutil.copytree(document["output_directory"], document["incoming_directory"])
    old = Path(run.output_path) / "reconstruction"
    old.mkdir()
    (old / "keep.txt").write_text("existing data")
    with pytest.raises(FileExistsError):
        poller.publish_returned(document)
    assert (old / "keep.txt").read_text() == "existing data"


@pytest.mark.parametrize("existing", [False, True])
def test_return_publication_handles_foreign_owned_files_without_overwriting(tmp_path, monkeypatch, existing):
    source, destination = tmp_path / "incoming.json", tmp_path / "published.json"
    source.write_bytes(b"complete returned output")
    if existing:
        destination.write_bytes(b"existing data")
    link = poller.os.link

    def protected_link(src, dst):
        if Path(src) == source:
            raise PermissionError("protected hard link to another account's file")
        return link(src, dst)

    monkeypatch.setattr(poller.os, "link", protected_link)
    if existing:
        with pytest.raises(FileExistsError):
            poller._publish_returned_file(source, destination)
        assert source.read_bytes() == b"complete returned output"
        assert destination.read_bytes() == b"existing data"
    else:
        poller._publish_returned_file(source, destination)
        assert destination.read_bytes() == b"complete returned output"
        assert not source.exists()
        assert destination.stat().st_mode & 0o777 == 0o660
    assert not list(tmp_path.glob(".bdp-return-*"))


def test_failed_return_copy_leaves_no_partially_published_file(tmp_path, monkeypatch):
    source, destination = tmp_path / "incoming", tmp_path / "published"
    source.write_bytes(b"complete returned output")
    monkeypatch.setattr(poller.os, "link", Mock(side_effect=PermissionError("protected hard link")))

    def interrupted_copy(incoming, outgoing):
        outgoing.write(b"partial")
        raise OSError("write interrupted")

    monkeypatch.setattr(poller.shutil, "copyfileobj", interrupted_copy)
    with pytest.raises(OSError, match="write interrupted"):
        poller._publish_returned_file(source, destination)
    assert not destination.exists()
    assert source.read_bytes() == b"complete returned output"
    assert not list(tmp_path.glob(".bdp-return-*"))


def test_cancel_before_compute_returns_a_cancelled_summary(remote_run):
    engine, run, settings, values = remote_run
    document = plan.save_plan(job_view(engine, run.job_id), settings, str(uuid4()))
    stage_fixture(document, values)
    (Path(document["control_directory"]) / "cancel.request").touch()
    summary = runner.run(document["control_directory"], compute=lambda *_: pytest.fail("compute ran"))
    assert summary["status"] == "Cancelled" and summary["counters"]["n_not_run"] == 2


def test_pbs_script_uses_allocation_and_bash_launcher(remote_run):
    engine, run, settings, _ = remote_run
    document = plan.build_plan(job_view(engine, run.job_id), settings, str(uuid4()))
    script = build_script(document)
    assert "#PBS -N Laue\n#PBS -A TEST_ACCOUNT" in script
    assert "#PBS -q debug\n" in script
    assert "mpiexec" in script and "/bin/bash" in script and "--cpu-bind none" in script
    assert f"-m laueportal.jobs.runner {document['control_directory']}\n" in script
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    unsafe = copy.deepcopy(document)
    unsafe["pbs"]["queue"] = "debug; touch /tmp/wrong"
    with pytest.raises(ValueError):
        build_script(unsafe)


@pytest.mark.parametrize("kind,ranks", [("wire_reconstruction", 4), ("lauego_indexing", 16)])
def test_four_node_pbs_layout(remote_run, kind, ranks):
    """Several Crux nodes start the MPI runner under ``mpiexec``."""
    engine, run, settings, _ = remote_run
    document = plan.build_plan(job_view(engine, run.job_id), settings, str(uuid4()))
    document["kind"] = kind
    document["pbs"]["nodes"] = 4
    document["execution"]["indexing_ranks_per_node"] = 4
    script = build_script(document)
    assert "#PBS -l select=4:system=crux" in script
    assert f"mpiexec -n {ranks} --ppn {ranks // 4}" in script
    assert "-m laueportal.jobs.mpi_runner" in script
    assert json.loads(json.dumps(document))["pbs"]["nodes"] == 4


@pytest.mark.parametrize(
    ("backend", "kind", "change", "message"),
    [
        ("crux", "wire", ("pbs", "account", None), "An ALCF allocation is required"),
        ("aps", "wire", ("pbs", "nodes", 2), "Multiple nodes are supported only by the Crux MPI runner"),
        ("crux", "wire", ("pbs", "queue", "debug; touch /tmp/wrong"), "Unsafe PBS queue"),
        ("aps", "lauego", ("execution", "indexing_ranks_per_node", 2), "indexing_ranks_per_node must be 1"),
        ("crux", "lauego", ("execution", "indexing_ranks_per_node", 5), "between one and four"),
        ("aps", "wire", ("execution", "indexing_ranks_per_node", 4), None),  # ranks per node are LaueGo's only
    ],
)
def test_a_run_the_launcher_could_not_start_is_refused_before_dm(tmp_path, backend, kind, change, message):
    """A plan the launcher could not build its PBS script from is refused at submission, with nothing written and DM
    never called, rather than failing remotely as a DM job."""
    layout = Layout(tmp_path)
    engine = make_engine(tmp_path / "portal")
    create = create_wire_run if kind == "wire" else create_lauego_run
    run = create(engine, layout, count=2, backend=backend)
    values = bdp_settings(layout)
    section, key, value = change
    values["targets"][backend][section][key] = value
    client = FakeDM()
    if message is None:
        assert submission.submit_run(run.job_id, engine=engine, settings=remote_settings(values), client=client)
        assert len(client.submissions) == 1
        engine.dispose()
        return
    with pytest.raises(RemoteSettingsError, match=message):
        submission.submit_run(run.job_id, engine=engine, settings=remote_settings(values), client=client)
    assert client.submissions == []
    job = load_job(engine, run.job_id)
    assert job.submission_id is None and job.phase == RunPhase.PUBLISHED and job.external_state is None
    directory = Path(job.run_directory)
    assert not (directory / plan.PLAN_NAME).exists() and not (directory / plan.SUBMISSION_LOCK_NAME).exists()
    assert not layout.submissions.exists() or not any(layout.submissions.iterdir())
    engine.dispose()


def test_compute_imports_never_load_database_or_portal_configuration():
    script = """
import sys
class Block:
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'sqlalchemy', 'redis', 'rq'} or fullname in {
            'laueportal.core.settings', 'laueportal.core.db'
        }:
            raise AssertionError('Unexpected remote dependency: ' + fullname)
sys.meta_path.insert(0, Block())
from laueportal.jobs.contract import get_compute_function
get_compute_function('wire_reconstruction')
get_compute_function('lauego_indexing')
"""
    from tests.settings_support import fresh_interpreter_env

    subprocess.run([sys.executable, "-c", script], check=True, env=fresh_interpreter_env())


def _dm_settings(**values) -> DMSettings:
    return DMSettings.model_validate(
        {"setup_file": "/dm/etc/dm.setup.sh", "owner": "test-user", "workflow": "laue-workflow", **values}
    )


@pytest.mark.parametrize("launcher_host", [None, "test-user@workflow.example.invalid"])
def test_cancellation_uses_launcher_host_independently_of_dm_api_bridge(tmp_path, launcher_host):
    source = tmp_path / "source files"
    source.mkdir()
    interpreter = tmp_path / "python launcher"
    interpreter.write_text(f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    interpreter.chmod(0o700)
    config = tmp_path / "config ' $(touch unexpected).yaml"
    settings = _dm_settings(
        ssh_host="test-user@bridge.example.invalid",
        launcher_ssh_host=launcher_host,
        launcher_python=str(interpreter),
        launcher_code=str(source),
        launcher_config=str(config),
    )
    submission_id = str(uuid4())
    command = launcher_command(settings, "cancel", submission_id)
    if launcher_host:
        assert command[:6] == ["ssh", "-T", "-o", "BatchMode=yes", launcher_host, command[-1]]
        # Execute the remote shell payload locally, without making an SSH call.
        command = ["/bin/bash", "-c", command[-1]]
    else:
        assert command[:2] == ["/bin/bash", "-c"]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == [
        "-m",
        "laueportal.remote.stage",
        "--config",
        str(config),
        "cancel",
        submission_id,
    ]
    assert not (source / "unexpected").exists()


_STAGE_STUB = """import json, os, sys
print(json.dumps({"stage": os.path.realpath(sys.argv[0]), "argv": sys.argv[1:], "cwd": os.getcwd(),
                  "nousersite": os.environ.get("PYTHONNOUSERSITE")}))
"""


def test_the_direct_cancellation_finds_the_stage_in_a_launcher_checkout(tmp_path):
    """The launcher's environment does not install the package (``deploy/remote/dm-stage.sh`` puts the
    checkout's ``backend/src`` on ``PYTHONPATH``). Run the command with a real interpreter that has no
    ``laueportal``, in a checkout (a stub stage module)."""
    interpreter = tmp_path / "bin" / "python"
    interpreter.parent.mkdir()
    interpreter.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -S "$@"\n')  # no site-packages
    interpreter.chmod(0o700)
    environment = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONNOUSERSITE", "PYTHONHOME"}}
    probe = subprocess.run(
        [str(interpreter), "-c", "import laueportal"], cwd=tmp_path, capture_output=True, env=environment
    )
    assert probe.returncode != 0 and b"No module named 'laueportal'" in probe.stderr

    code = tmp_path / "launcher root" / "src"
    package = code / "backend/src" / "laueportal/remote"
    package.mkdir(parents=True)
    for parent in [package, *package.parents]:
        if parent == code or parent == code / "backend" or parent == code / "backend" / "src":
            break
        (parent / "__init__.py").write_text("")
    (package / "stage.py").write_text(_STAGE_STUB)
    config = tmp_path / "config" / "bdp.yaml"
    settings = _dm_settings(
        launcher_ssh_host="test-user@workflow.example.invalid",
        launcher_python=str(interpreter),
        launcher_code=str(code),
        launcher_config=str(config),
    )
    submission_id = str(uuid4())
    command = launcher_command(settings, "cancel", submission_id)
    assert command[:5] == ["ssh", "-T", "-o", "BatchMode=yes", "test-user@workflow.example.invalid"]
    remote_shell = shlex.split(command[-1])  # ['/bin/bash', '-c', wrapper]: run the wrapper here, without SSH
    result = subprocess.run(remote_shell, check=True, capture_output=True, text=True, cwd=tmp_path, env=environment)
    ran = json.loads(result.stdout)
    assert ran["stage"] == str((package / "stage.py").resolve())
    assert ran["argv"] == ["--config", str(config), "cancel", submission_id]
    assert ran["cwd"] == str(code.resolve())
    assert ran["nousersite"] == "1"


def _cancel_job(status: str) -> Records:
    """A pass's batched answer (``DMClient.fetch``) holding the cancel workflow's job ``cancel-1``."""
    return Records({"cancel-1": {"id": "cancel-1", "status": status}})


def test_dm_cancellation_waits_for_delivery_without_repeated_submissions(tmp_path):
    """The cancel job's state comes with the pass's batched call: the pass that submits it reads it at the next pass."""
    job = SimpleNamespace(run_directory=str(tmp_path), submission_id=str(uuid4()), cancel_requested_at="now")
    client = Mock()
    client.cancel.return_value = {"id": "cancel-1"}
    settings = SimpleNamespace(dm=_dm_settings(cancel_workflow="laue-portal-bdp-cancel"))
    poller.request_cancellation(job, settings, client, Records())  # type: ignore[arg-type]
    poller.request_cancellation(job, settings, client, _cancel_job("running"))  # type: ignore[arg-type]
    client.cancel.assert_called_once_with(job.submission_id)
    client.get.assert_not_called()
    assert not (tmp_path / ".cancel-sent").exists()
    poller.request_cancellation(job, settings, client, _cancel_job("done"))  # type: ignore[arg-type]
    assert (tmp_path / ".cancel-sent").exists()
    client.cancel.assert_called_once_with(job.submission_id)


def test_failed_dm_cancellation_is_reported_and_not_marked_delivered(tmp_path):
    job = SimpleNamespace(run_directory=str(tmp_path), submission_id=str(uuid4()), cancel_requested_at="now")
    client = Mock()
    client.cancel.return_value = {"id": "cancel-1"}
    settings = SimpleNamespace(dm=_dm_settings(cancel_workflow="cancel"))
    poller.request_cancellation(job, settings, client, Records())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="DM cancellation job"):
        poller.request_cancellation(job, settings, client, _cancel_job("failed"))  # type: ignore[arg-type]
    assert not (tmp_path / ".cancel-sent").exists()


def test_dm_cancel_argument_does_not_conflict_with_parent_submission_lookup():
    client = DMClient(_dm_settings(cancel_workflow="laue-cancel", launcher_config="/config/bdp.yaml"))
    client._call = Mock(return_value={"id": "cancel-1"})
    submission_id = str(uuid4())
    client.cancel(submission_id)
    client._call.assert_called_once_with(
        "submit",
        owner="test-user",
        workflow="laue-cancel",
        arguments={
            "cancelSubmissionId": submission_id,
            "source": "laue-portal",
            "filePath": "/config/bdp.yaml",
        },
    )
    with pytest.raises(ValueError):
        client.cancel("invalid-id")


def test_dm_submission_uses_one_frozen_shared_file_without_remote_file_iteration():
    client = DMClient(_dm_settings())
    client._call = Mock(return_value={"id": "dm-1"})
    submission_id = str(uuid4())
    client.submit(
        {
            "submission_id": submission_id,
            "backend": "crux",
            "control_directory": "/compute/control",
            "run_directory": "/experiment/analysis/run",
        }
    )
    args = client._call.call_args.kwargs["arguments"]
    assert args["submissionId"] == submission_id
    assert args["filePath"] == "/experiment/analysis/run/request.json"


@pytest.mark.parametrize("bridge", [None, "test-user@bridge.example.invalid"], ids=["here", "bridge"])
def test_a_dm_call_runs_in_a_shell_that_is_no_login_shell_here_and_over_the_bridge(bridge, monkeypatch):
    """DM's setup file, sourced first, gives the call its environment; a login profile's banner or modules could corrupt
    the JSON the call prints."""
    import base64

    client = DMClient(_dm_settings(ssh_host=bridge))
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, '{"id": "dm-1"}', "")

    monkeypatch.setattr(client, "run", run)
    assert client.get("dm-1") == {"id": "dm-1"}
    [argv] = calls
    if bridge is None:
        shell, script = argv[:2], argv[2]
    else:
        assert argv[:5] == ["ssh", "-T", "-o", "BatchMode=yes", bridge]
        remote = shlex.split(argv[5])  # the remote account's shell runs: /bin/bash -c <wrapper>
        assert remote[:2] == ["/bin/bash", "-c"]
        wrapper = shlex.split(remote[2])  # exec /bin/bash -c "$(printf %s <base64> | base64 -d)"
        assert wrapper[:3] == ["exec", "/bin/bash", "-c"]
        shell, script = wrapper[1:3], base64.b64decode(wrapper[3].split()[2]).decode()
    assert shell == ["/bin/bash", "-c"]
    assert script.startswith("source /dm/etc/dm.setup.sh >/dev/null; exec python -c ")


def _run_api_script(operation, api, arguments, monkeypatch, capsys):
    """DM's API script (``remote.dm._API_SCRIPT``) on ``api`` (a stand-in ``WorkflowProcApi``): its JSON answer."""
    from laueportal.remote.dm import _API_SCRIPT

    for name in ("dm", "dm.proc_web_service", "dm.proc_web_service.api"):
        monkeypatch.setitem(sys.modules, name, SimpleNamespace())
    monkeypatch.setitem(
        sys.modules, "dm.proc_web_service.api.workflowProcApi", SimpleNamespace(WorkflowProcApi=lambda: api)
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"operation": operation, "args": arguments})))
    exec(_API_SCRIPT, {})
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("operation", ["submit", "get", "fetch"])
def test_dm_bridge_preserves_processing_job_objects_as_structured_json(operation, monkeypatch, capsys):
    """``fetch`` is the remote poller's one call per pass: the records asked for, and the look-ups by submission ID
    (each the list ``find`` answers), with the record of a single job found."""
    record = {"id": "dm-1", "status": "pending", "submissionId": "submission-1"}
    job = SimpleNamespace(data=record)
    api = Mock()
    api.startProcessingJob.return_value = job
    api.getProcessingJobById.return_value = job
    api.listProcessingJobRecords.return_value = [job]
    arguments = {
        "owner": "test-user",
        "workflow": "laue",
        "arguments": {},
        "id": "dm-1",
        "ids": [],
        "submission_ids": ["submission-1"],
    }
    answer = _run_api_script(operation, api, arguments, monkeypatch, capsys)
    if operation == "fetch":
        assert answer == {"records": {"dm-1": {"record": record}}, "found": {"submission-1": {"records": [record]}}}
        api.getProcessingJobById.assert_called_once_with("test-user", "dm-1")
    else:
        assert answer == record


def test_dm_fetch_reports_a_record_dm_cannot_give_per_record(monkeypatch, capsys):
    """One record failing (both of DM's look-ups by ID raise; a submission's look-up raises) is that record's error;
    the call's other records stand. A submission with no job or several gets no record look-up."""
    records = {"dm-1": {"id": "dm-1", "status": "running"}, "dm-3": {"id": "dm-3", "status": "done"}}
    api = Mock()

    def by_id(owner, job_id):
        if job_id not in records:
            raise KeyError(job_id)
        return SimpleNamespace(data=records[job_id])

    def by_record(job_id):
        raise LookupError(f"no processing job record {job_id}")

    def found(queryDict, limit):  # noqa: N803 (DM's keyword)
        wanted = queryDict["submissionId"]
        if wanted == "broken":
            raise RuntimeError("DM's database is unavailable")
        twice = [SimpleNamespace(data={"id": f"dm-{n}"}) for n in (4, 5)]
        return {"lost": [SimpleNamespace(data=records["dm-3"])], "twice": twice}.get(wanted, [])

    api.getProcessingJobById.side_effect = by_id
    api.getProcessingJobRecord.side_effect = by_record
    api.listProcessingJobRecords.side_effect = found
    arguments = {"owner": "o", "ids": ["dm-1", "dm-2"], "submission_ids": ["lost", "none", "twice", "broken"]}

    answer = _run_api_script("fetch", api, arguments, monkeypatch, capsys)

    assert answer["records"] == {
        "dm-1": {"record": records["dm-1"]},
        "dm-2": {"error": "LookupError: no processing job record dm-2"},
        "dm-3": {"record": records["dm-3"]},
    }
    assert answer["found"] == {
        "lost": {"records": [records["dm-3"]]},
        "none": {"records": []},
        "twice": {"records": [{"id": "dm-4"}, {"id": "dm-5"}]},
        "broken": {"error": "RuntimeError: DM's database is unavailable"},
    }
    parsed = parse_records(answer)
    assert parsed.get("dm-1") == records["dm-1"] and parsed.find("lost") == [records["dm-3"]]
    for failing in (lambda: parsed.get("dm-2"), lambda: parsed.find("broken"), lambda: parsed.get("dm-9")):
        with pytest.raises(DMRecordError):
            failing()


def test_dm_fetch_is_one_call_and_refuses_an_answer_without_its_shape():
    """``DMClient.fetch``: one ``_call`` (one subprocess) whatever the number of records, each ID once."""
    client = DMClient(_dm_settings())
    client._call = Mock(return_value={"records": {"dm-1": {"record": {"id": "dm-1"}}}, "found": {}})
    assert client.fetch(["dm-1", "dm-2", "dm-1"], ["s-1", "s-1"]).get("dm-1") == {"id": "dm-1"}
    client._call.assert_called_once_with("fetch", owner="test-user", ids=["dm-1", "dm-2"], submission_ids=["s-1"])
    for malformed in ([], {"records": {}}, {"records": {"dm-1": {"status": "x"}}, "found": {}}):
        client._call = Mock(return_value=malformed)
        with pytest.raises(ValueError, match="malformed"):
            client.fetch(["dm-1"], [])


def test_a_record_that_is_not_an_object_is_its_own_error_and_the_call_s_other_records_stand():
    """A record that is not a JSON object (``null``, a DM object the script sent as its text, a list), or a look-up
    holding one, is that record's ``DMRecordError``, so it fails its run alone; only an answer without the operation's
    shape (an entry with neither its value nor an error, a look-up that is no list) fails the call as a whole."""
    good = {"id": "dm-4", "status": "running"}
    answer = {
        "records": {
            "dm-1": {"record": None},
            "dm-2": {"record": "ProcessingJob({'id': 'dm-2'})"},
            "dm-3": {"record": ["dm-3"]},
            "dm-4": {"record": good},
        },
        "found": {"odd": {"records": [good, "ProcessingJob({'id': 'dm-5'})"]}, "lost": {"records": [good]}},
    }

    records = parse_records(answer)

    assert records.get("dm-4") == good and records.find("lost") == [good]
    for job_id in ("dm-1", "dm-2", "dm-3"):
        with pytest.raises(DMRecordError, match=f"^DM's record of job {job_id} is not an object$"):
            records.get(job_id)
    with pytest.raises(DMRecordError, match="^DM's look-up of submission odd holds a record that is not an object$"):
        records.find("odd")
    for shapeless in ({"records": {"dm-1": {}}, "found": {}}, {"records": {}, "found": {"s": {"records": good}}}):
        with pytest.raises(ValueError, match="malformed"):
            parse_records(shapeless)


def _shell_and_child(tmp_path: Path) -> tuple[list[str], Path]:
    """A command whose shell starts a child and waits for it, as the shell sourcing DM's setup file does; their
    pids go to ``pids``, the shell's first."""
    pids = tmp_path / "pids"
    script = f"echo $$ >> {shlex.quote(str(pids))}; sleep 41.5 & echo $! >> {shlex.quote(str(pids))}; wait"
    return ["/bin/bash", "-c", script], pids


def _started(pids: Path) -> list[int]:
    return [int(pid) for pid in pids.read_text().split()] if pids.exists() else []


def test_a_dm_client_s_process_runs_as_subprocess_run_does_and_with_a_session_ends_whole_at_its_timeout(tmp_path):
    """``DMClient.run``, through which a DM call and the launcher's command run, answers as
    ``subprocess.run(..., capture_output=True, text=True, check=True, timeout=...)``. The API's client runs it in the
    caller's process group (which the API program's stop reaches); the poller's (``own_session``) in a session of its
    own, which its timeout kills whole: the process with everything it started, not the shell alone."""
    group = ["/bin/bash", "-c", "ps -o pgid= -p $$"]
    client = DMClient(_dm_settings())
    done = client.run(["/bin/bash", "-c", "cat; echo warning >&2"], input="request", timeout=10)
    assert (done.returncode, done.stdout, done.stderr) == (0, "request", "warning\n")
    with pytest.raises(subprocess.CalledProcessError) as refused:
        client.run(["/bin/bash", "-c", "echo refused >&2; exit 3"], timeout=10)
    assert (refused.value.returncode, refused.value.stderr) == (3, "refused\n")
    assert int(client.run(group, timeout=10).stdout) == os.getpgrp()
    client = DMClient(_dm_settings(), own_session=True)
    assert int(client.run(group, timeout=10).stdout) != os.getpgrp()
    argv, pids = _shell_and_child(tmp_path)
    try:
        with pytest.raises(subprocess.TimeoutExpired) as late:
            client.run(argv, timeout=1)
        assert late.value.timeout == 1 and len(_started(pids)) == 2
        assert late.value.cmd == argv  # the call, not its ``timeout`` wrapper
        assert wait_for(lambda: not any(alive(pid) for pid in _started(pids)), timeout=2)
    finally:
        kill_quietly(_started(pids))


def test_the_abort_ends_the_running_process_with_everything_it_started_and_refuses_any_after_it(tmp_path):
    """``DMClient.abort`` (the worker's end, ``RemotePoller.close``, whose client has ``own_session``) kills the process
    running now with everything it started, and returns once they are gone; the call raises ``CallAborted``, and no
    process starts after it."""
    client = DMClient(_dm_settings(), own_session=True)
    argv, pids = _shell_and_child(tmp_path)
    outcome: list[BaseException] = []

    def call() -> None:
        try:
            client.run(argv, timeout=30)
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=call)
    thread.start()
    try:
        assert wait_for(lambda: len(_started(pids)) == 2, timeout=10)
        client.abort()
        assert not any(alive(pid) for pid in _started(pids))
        thread.join(10)
        assert not thread.is_alive() and [type(error) for error in outcome] == [CallAborted]
        marker = tmp_path / "started"
        with pytest.raises(CallAborted):
            client.run(["/bin/bash", "-c", f"touch {shlex.quote(str(marker))}"], timeout=10)
        assert not marker.exists()
    finally:
        kill_quietly(_started(pids))


def test_an_abort_for_later_ends_the_running_call_and_holds_the_next_until_resumed(tmp_path):
    """A live run's Stop (the catalog thread's ``before_seal``): ``abort(later=True)`` ends the call running now
    (``CallAborted``) and refuses the next one too, which a cycle past its stop check would start, until ``resume`` lets
    the last read's call start."""
    client = DMClient(_dm_settings(), own_session=True)
    argv, pids = _shell_and_child(tmp_path)
    outcome: list[BaseException] = []

    def call() -> None:
        try:
            client.run(argv, timeout=30)
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=call)
    thread.start()
    try:
        assert wait_for(lambda: len(_started(pids)) == 2, timeout=10)
        client.abort(later=True)
        assert not any(alive(pid) for pid in _started(pids))
        thread.join(10)
        assert not thread.is_alive() and [type(error) for error in outcome] == [CallAborted]
        marker = tmp_path / "started"
        touch = ["/bin/bash", "-c", f"touch {shlex.quote(str(marker))}"]
        with pytest.raises(CallAborted):
            client.run(touch, timeout=10)
        assert not marker.exists()
        client.resume()
        client.run(touch, timeout=10)
        assert marker.exists()
    finally:
        kill_quietly(_started(pids))


_KILLED_WORKER = """
import sys
from laueportal.remote.dm import DMClient
from laueportal.remote.settings import DMSettings

settings = DMSettings.model_validate({"setup_file": "/dm/etc/dm.setup.sh", "owner": "u", "workflow": "w"})
DMClient(settings, own_session=True).run(sys.argv[2:], timeout=float(sys.argv[1]))
"""


@pytest.mark.slow
def test_a_call_outlives_a_worker_killed_outright_only_until_its_own_limit(tmp_path):
    """A worker killed outright (an OOM kill, SIGKILL) runs no ``abort``, and its poller's call is in a session of its
    own that supervisor's group kill does not reach; the call bounds itself (coreutils ``timeout``, ``SELF_BOUND``), so
    nothing of it is left once its timeout and the margin have passed."""
    from laueportal.remote.dm import SELF_BOUND_MARGIN_SECONDS

    timeout = 3.0
    argv, pids = _shell_and_child(tmp_path)
    worker = subprocess.Popen([sys.executable, "-c", _KILLED_WORKER, str(timeout), *argv])
    try:
        assert wait_for(lambda: len(_started(pids)) == 2, timeout=20)
        started = time.monotonic()
        worker.kill()
        worker.wait(10)
        while time.monotonic() < started + timeout + 1:  # past the call's own timeout: the dead worker's
            time.sleep(0.1)
        assert all(alive(pid) for pid in _started(pids))
        limit = timeout + 2 * SELF_BOUND_MARGIN_SECONDS + 2
        assert wait_for(lambda: not any(alive(pid) for pid in _started(pids)), timeout=limit)
    finally:
        worker.kill()
        kill_quietly(_started(pids))


def test_a_laue_matching_request_gets_no_plan(tmp_path):
    """No remote target computes LaueMatching yet; its plan is refused before anything is written."""
    engine = make_engine(tmp_path)
    layout = Layout(tmp_path)
    run = create_lauego_run(engine, layout, count=1, backend="aps")
    view = job_view(engine, run.job_id)
    document = json.loads(Path(view.request_path).read_text())
    document["kind"] = "laue_matching_indexing"
    Path(view.request_path).write_text(json.dumps(document))
    with pytest.raises(RemoteSettingsError, match="only on the portal's own worker"):
        plan.build_plan(view, remote_settings(bdp_settings(layout)), "00000000-0000-4000-8000-000000000000")
