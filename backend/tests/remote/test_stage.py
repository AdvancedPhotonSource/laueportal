"""The launcher's DM stage commands (``remote.stage``) with fakes for PBS (``qsub``, ``qstat``, ``qdel``) and the Globus
flow: what DM's workflow stages run on the DM host.

The files they act on (the plan from the DM inbox, the state file, ``job.pbs``) are the run directory's. No PBS, Globus
or network.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
import yaml

from laueportal.remote import stage
from laueportal.remote.flow import flow_input
from laueportal.remote.pbs import build_script
from tests.remote_support import (
    CASES_BY_NAME,
    SUBMISSION_ID,
    Layout,
    case_settings,
    create_run,
    remote_settings,
    save_plan,
)
from tests.run_support import make_engine
from tests.settings_support import fresh_interpreter_env


class FakePBS:
    """``subprocess.run`` for the PBS commands: qsub answers a job ID, qstat the state set here, qdel nothing."""

    def __init__(self, job_id="4321.aps-pbs"):
        self.job_id = job_id
        self.state = {"job_state": "Q"}
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        command = shlex.split(argv[-1]) if argv[0] == "ssh" else argv
        if command[0].endswith("qsub"):
            return subprocess.CompletedProcess(argv, 0, self.job_id + "\n", "")
        if command[0].endswith("qstat"):
            return subprocess.CompletedProcess(argv, 0, json.dumps({"Jobs": {self.job_id: self.state}}), "")
        if command[0].endswith("qdel"):
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected command {argv}")


def _planned(tmp_path, name, **target_changes):
    """The case's run with its plan in the run directory and the DM inbox (not staged: the launcher does that)."""
    case = CASES_BY_NAME[name]
    layout = Layout(tmp_path)
    values = case_settings(case, layout)
    values["targets"][case.backend].update(target_changes)
    engine = make_engine(tmp_path / "portal")
    try:
        job = create_run(case, layout, engine)
    finally:
        engine.dispose()
    plan = save_plan(job, values, SUBMISSION_ID)
    config = tmp_path / "bdp.yaml"
    config.write_text(yaml.safe_dump(values), encoding="utf-8")
    return plan, remote_settings(values), config


def _state(settings) -> dict:
    return json.loads((Path(settings.state_directory) / f"{SUBMISSION_ID}.json").read_text())


def test_aps_submit_writes_the_pbs_script_once_and_records_the_job(tmp_path, monkeypatch):
    plan, settings, _ = _planned(tmp_path, "aps-wire")
    pbs = FakePBS()
    monkeypatch.setattr(stage.subprocess, "run", pbs)

    assert stage.submit(plan, settings) == {"status": "RUNNING", "pbs_id": "4321.aps-pbs"}

    control, output = Path(plan["control_directory"]), Path(plan["output_directory"])
    assert (control / "job.pbs").read_text() == build_script(plan)
    assert "-m laueportal.jobs.runner " in (control / "job.pbs").read_text()
    assert output.is_dir() and control == Path(plan["run_directory"])
    assert pbs.calls == [["/opt/pbs/bin/qsub", "-o", str(output / "pbs-output.log"), str(control / "job.pbs")]]
    assert _state(settings) == {"status": "RUNNING", "pbs_id": "4321.aps-pbs"}
    with pytest.raises(FileExistsError, match="already attempted"):
        stage.submit(plan, settings)
    assert len(pbs.calls) == 1


def test_aps_submit_goes_through_the_submit_host(tmp_path, monkeypatch):
    plan, settings, _ = _planned(tmp_path, "aps-wire", submit_host="user@scheduler.example.invalid")
    pbs = FakePBS()
    monkeypatch.setattr(stage.subprocess, "run", pbs)
    stage.submit(plan, settings)
    [argv] = pbs.calls
    assert argv[:5] == ["ssh", "-T", "-o", "BatchMode=yes", "user@scheduler.example.invalid"]
    assert shlex.split(argv[5])[0] == "/opt/pbs/bin/qsub"


@pytest.mark.parametrize("printed", ["123.pbs; touch PWNED", "-x", "", "Job submitted", "4321.aps pbs"])
def test_aps_submit_refuses_a_pbs_job_id_the_crux_task_would_refuse(tmp_path, monkeypatch, printed):
    """qsub's output is kept for qstat and qdel only when it is a PBS job ID (the crux task's pattern); otherwise the
    stage fails and the state stays SUBMISSION_UNCERTAIN (no retry submits again; monitor asks for reconciliation)."""
    plan, settings, _ = _planned(tmp_path, "aps-wire")
    pbs = FakePBS(job_id=printed)
    monkeypatch.setattr(stage.subprocess, "run", pbs)
    with pytest.raises(ValueError, match="PBS returned an invalid job ID"):
        stage.submit(plan, settings)
    assert _state(settings) == {"status": "SUBMISSION_UNCERTAIN"}
    with pytest.raises(ValueError, match="manual reconciliation"):
        stage.monitor(plan, settings)
    with pytest.raises(FileExistsError, match="already attempted"):
        stage.submit(plan, settings)
    assert [call[0] for call in pbs.calls] == ["/opt/pbs/bin/qsub"]
    for valid in ("4321", "4321.aps-pbs", "12.pbs01.example_org"):
        assert stage.re.fullmatch(stage.PBS_JOB_ID, valid)


def test_the_aps_launcher_checks_the_crux_tasks_pbs_job_id_pattern():
    import inspect

    from laueportal.remote import pbs

    assert f'r"{stage.PBS_JOB_ID}"' in inspect.getsource(pbs.run_pbs_job)


def test_aps_monitor_check_and_cancel_follow_pbs(tmp_path, monkeypatch):
    plan, settings, config = _planned(tmp_path, "aps-wire")
    pbs = FakePBS()
    monkeypatch.setattr(stage.subprocess, "run", pbs)
    stage.submit(plan, settings)

    pbs.state = {"job_state": "R"}
    assert stage.monitor(plan, settings)["status"] == "RUNNING"
    pbs.state = {"job_state": "F", "Exit_status": 0}
    assert stage.monitor(plan, settings)["status"] == "SUCCEEDED"
    assert _state(settings) == {"status": "SUCCEEDED", "pbs_id": "4321.aps-pbs"}

    summary = Path(plan["output_directory"]) / "run.json"
    for status, exit_code in (("Finished", 0), ("Failed", 1)):
        summary.write_text(json.dumps({"status": status}))
        monkeypatch.setattr(sys, "argv", ["stage", "--config", str(config), "check", SUBMISSION_ID])
        assert stage.main() == exit_code

    assert stage.cancel(plan, settings) == {"status": "CANCEL_REQUESTED"}
    assert pbs.calls[-1] == ["/opt/pbs/bin/qdel", "4321.aps-pbs"]


def test_a_failed_pbs_job_is_reported_failed(tmp_path, monkeypatch):
    plan, settings, _ = _planned(tmp_path, "aps-lauego")
    pbs = FakePBS()
    monkeypatch.setattr(stage.subprocess, "run", pbs)
    stage.submit(plan, settings)
    pbs.state = {"job_state": "F", "Exit_status": 271}
    assert stage.monitor(plan, settings)["status"] == "FAILED"


def test_crux_submit_runs_the_flow_and_monitor_follows_it(tmp_path, monkeypatch, capsys):
    plan, settings, config = _planned(tmp_path, "crux-wire")
    runs = []

    class Client:
        def run_flow(self, flow_input, *, label, tags):
            runs.append((flow_input, label, tags))
            return {"action_id": "flow-run-1"}

        def get_run(self, run_id):  # the Flows client of the monitor's one status call (stage._flows_client)
            assert run_id == "flow-run-1"
            return SimpleNamespace(data={"status": "SUCCEEDED"})

    monkeypatch.setattr(stage, "_client", lambda _settings: Client())
    monkeypatch.setattr(stage, "_flows_client", lambda client, *, timeout: client)
    monkeypatch.setattr(sys, "argv", ["stage", "--config", str(config), "submit", SUBMISSION_ID])
    assert stage.main() == 0
    assert capsys.readouterr().out.splitlines() == [
        "Status: RUNNING",
        "Flow Action ID: flow-run-1",
        "URL: https://app.globus.org/runs/flow-run-1",
    ]  # what the DM workflow's outputVariableRegexList reads
    [(payload, label, tags)] = runs
    assert payload == flow_input(plan) and label == f"Laue {SUBMISSION_ID[:8]}"
    assert tags == ["aps", "34IDE", "LauePortal", SUBMISSION_ID]
    assert "-m laueportal.jobs.runner " in payload["input"]["script"]
    assert stage.monitor(plan, settings)["status"] == "SUCCEEDED"


def test_the_beamline_profile_needs_its_credentials_file(tmp_path):
    _plan, settings, _ = _planned(tmp_path, "crux-wire")
    with pytest.raises(ValueError, match="credentials_file is required"):
        stage.configure_auth(settings.model_copy(update={"auth_profile": "beamline", "credentials_file": None}))


def test_the_stage_command_runs_without_the_portal_settings(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "laueportal.remote.stage", "--help"],
        env=fresh_interpreter_env(),  # no LAUEPORTAL_CONFIG: the launcher reads only its YAML
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "{submit,monitor,permissions,check,cancel,cleanup,auth}" in result.stdout and "--config" in result.stdout


class FakeTransfer:
    """A Globus Transfer client whose delete tasks remove the collection path on the local stand-in for Eagle;
    ``error`` is raised by ``submit_delete`` (as globus-sdk raises a ``GlobusAPIError``), and a task still
    running (``status="ACTIVE"``) makes ``task_wait`` time out (it returns False)."""

    def __init__(self, settings, *, status="SUCCEEDED"):
        self.eagle = settings.storage.eagle
        self.status = status
        self.error: Exception | None = None
        self.deletes: list[dict] = []

    def submit_delete(self, payload):
        if self.error is not None:
            raise self.error
        self.deletes.append(dict(payload))
        if self.status == "SUCCEEDED":
            for item in payload["DATA"]:
                relative = PurePosixPath(item["path"]).relative_to(self.eagle.collection_root)
                shutil.rmtree(Path(self.eagle.filesystem_root) / relative, ignore_errors=True)
        return {"task_id": f"delete-{len(self.deletes)}"}

    def task_wait(self, task_id, *, timeout, polling_interval):
        return self.status != "ACTIVE"

    def get_task(self, task_id):
        return {"status": self.status}


def _published_with_leftovers(tmp_path, name, monkeypatch):
    """A submitted run's launcher state and (``crux``) Eagle folder, and a transfer client that deletes on it."""
    plan, settings, config = _planned(tmp_path, name)
    state = Path(settings.state_directory) / f"{SUBMISSION_ID}.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"status": "SUCCEEDED", "pbs_id": "4321.aps-pbs"}))
    eagle = Path(plan["control_directory"]).parent  # crux: <workspace>/runs/<submission> on the Eagle stand-in
    if plan["backend"] == "crux":
        (eagle / "inputs").mkdir(parents=True)
        (eagle / "inputs" / "frame.h5").write_bytes(b"input copy")
    transfer = FakeTransfer(settings)
    monkeypatch.setattr(stage, "_transfer_client", lambda _settings: transfer)
    return plan, settings, config, state, eagle, transfer


def _publish(plan, status="Finished") -> None:
    summary = {"submission_id": plan["submission_id"], "status": status}
    (Path(plan["run_directory"]) / "run.json").write_text(json.dumps(summary))


def test_crux_cleanup_deletes_the_eagle_folder_then_the_state_file_once_the_run_is_published(
    tmp_path, monkeypatch, capsys
):
    plan, settings, config, state, eagle, transfer = _published_with_leftovers(tmp_path, "crux-wire", monkeypatch)
    with pytest.raises(ValueError, match="not published"):
        stage.cleanup(plan, settings)
    assert eagle.is_dir() and state.is_file() and transfer.deletes == []

    _publish(plan)
    monkeypatch.setattr(sys, "argv", ["stage", "--config", str(config), "cleanup", SUBMISSION_ID])
    assert stage.main() == 0
    assert capsys.readouterr().out.splitlines() == ["Status: CLEANED"]

    assert not eagle.exists() and not state.exists()
    [delete] = transfer.deletes
    assert delete["endpoint"] == settings.storage.eagle.collection_id
    assert (delete["recursive"], delete["ignore_missing"]) == (True, True)
    assert [item["path"] for item in delete["DATA"]] == [f"/workspace/runs/{SUBMISSION_ID}"]
    assert stage.cleanup(plan, settings) == {"status": "CLEANED"}  # again: nothing left, nothing refused


@pytest.mark.parametrize("status", ["Failed", "Cancelled"])
def test_cleanup_refuses_a_run_that_did_not_finish_and_its_mpi_shards_stay(tmp_path, monkeypatch, status):
    """The ranks' results and diagnostics of a Failed or Cancelled MPI run exist only on Eagle."""
    plan, settings, _, state, eagle, transfer = _published_with_leftovers(tmp_path, "crux-wire-mpi", monkeypatch)
    shard = eagle / "shards" / "rank-0001"
    shard.mkdir(parents=True)
    (shard / "run.json").write_text(json.dumps({"status": "Failed"}))
    _publish(plan, status)
    with pytest.raises(ValueError, match="did not finish"):
        stage.cleanup(plan, settings)
    assert (shard / "run.json").is_file() and (eagle / "inputs" / "frame.h5").is_file() and state.is_file()
    assert transfer.deletes == []


def test_a_failed_eagle_delete_keeps_the_state_file(tmp_path, monkeypatch):
    plan, settings, _, state, eagle, transfer = _published_with_leftovers(tmp_path, "crux-lauego", monkeypatch)
    transfer.status = "FAILED"
    _publish(plan)
    with pytest.raises(RuntimeError, match="delete task failed"):
        stage.cleanup(plan, settings)
    assert eagle.is_dir() and state.is_file()


def test_an_eagle_delete_that_cannot_be_submitted_or_outlasts_the_wait_keeps_the_state_file(tmp_path, monkeypatch):
    """Running the stage again later is the retry (the poller's DM job failed: remove its record)."""
    plan, settings, _, state, eagle, transfer = _published_with_leftovers(tmp_path, "crux-wire", monkeypatch)
    _publish(plan)
    transfer.error = RuntimeError("Transfer API error: 503")
    with pytest.raises(RuntimeError, match="503"):
        stage.cleanup(plan, settings)
    transfer.error, transfer.status = None, "ACTIVE"
    with pytest.raises(TimeoutError, match="still running"):
        stage.cleanup(plan, settings)
    assert eagle.is_dir() and state.is_file() and len(transfer.deletes) == 1


def test_aps_cleanup_removes_the_state_file_only(tmp_path, monkeypatch):
    plan, settings, _, state, _, transfer = _published_with_leftovers(tmp_path, "aps-wire", monkeypatch)
    _publish(plan)
    assert stage.cleanup(plan, settings) == {"status": "CLEANED"}
    assert not state.exists() and transfer.deletes == []


@pytest.mark.parametrize(
    ("endpoint", "source"),
    [
        (None, "/workspace/results"),  # not the submission's folder
        (None, f"/workspace/runs/../../../../other/runs/{SUBMISSION_ID}/results"),  # a climb out of the workspace
        (None, f"/workspace/runs/{SUBMISSION_ID}/../{SUBMISSION_ID}/results"),
        (None, f"workspace/runs/{SUBMISSION_ID}/results"),  # relative
        (None, f"/elsewhere/runs/{SUBMISSION_ID}/results"),  # another workspace
        ("another-collection", f"/workspace/runs/{SUBMISSION_ID}/results"),
    ],
)
def test_the_eagle_folder_must_be_the_submissions_own_under_the_launchers_settings(tmp_path, endpoint, source):
    """The folder comes from the launcher's settings (the Eagle collection, ``targets.crux.workspace``); a plan that
    names anything else is refused."""
    plan, settings, _ = _planned(tmp_path, "crux-wire")
    assert stage.eagle_run_folder(plan, settings) == (
        settings.storage.eagle.collection_id,
        f"/workspace/runs/{SUBMISSION_ID}",
    )
    spec = plan["result_transfer"]
    spec["transfer_items"][0]["source_path"] = source
    spec["source_endpoint_id"] = endpoint or spec["source_endpoint_id"]
    with pytest.raises(ValueError, match="this submission's Eagle folder"):
        stage.eagle_run_folder(plan, settings)


def test_a_plan_naming_another_submission_is_refused(tmp_path, monkeypatch):
    """The stage acts on the submission it was given; the state file and the folders are named by the plan's."""
    plan, settings, config, state, eagle, transfer = _published_with_leftovers(tmp_path, "crux-wire", monkeypatch)
    _publish(plan)
    other = "0b5c6a2e-1d3f-4a5b-8c7d-9e0f1a2b3c4d"
    inbox_copy = Path(settings.submission_directory) / f"{SUBMISSION_ID}.json"
    inbox_copy.write_text(json.dumps({**plan, "submission_id": other}))
    monkeypatch.setattr(sys, "argv", ["stage", "--config", str(config), "cleanup", SUBMISSION_ID])
    with pytest.raises(ValueError, match="names another submission"):
        stage.main()
    assert eagle.is_dir() and state.is_file() and transfer.deletes == []
