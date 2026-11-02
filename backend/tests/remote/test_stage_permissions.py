"""The launcher's ``permissions`` stage (the workflow's ``03-PERMISSIONS``; ``remote.stage.restore_permissions``): after
a ``crux`` run's completed Globus return, one DM call restores DM's permissions on exactly the submission's
``<run>/incoming/<submission>``, and DM's workflow runs it only for a ``crux`` run whose Flow succeeded (its ``runIf``).
Fakes only: DM (a stand-in client, or the real client on DM's API script over a stand-in ``dm`` package), the Flow and
PBS; no DM, Globus or network.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest

from laueportal.remote import stage
from laueportal.remote.dm import DMClient
from tests.fixtures.observation_goldens import cases
from tests.remote.test_stage import FakePBS, _planned
from tests.remote.test_stage_monitor import FakeFlow
from tests.remote_support import SUBMISSION_ID, FakeDMService

WORKFLOW = Path(__file__).resolve().parents[3] / "deploy" / "remote" / "workflow-bdp.json.template"


class Restorer:
    """``stage.DMClient``'s stand-in: ``clients``, the DM settings each client was built with; ``calls``, each
    ``restore_permissions``'s arguments; ``error``, raised by it when set."""

    def __init__(self) -> None:
        self.clients: list[Any] = []
        self.calls: list[tuple[str, str]] = []
        self.error: BaseException | None = None

    def __call__(self, dm: Any) -> Restorer:
        self.clients.append(dm)
        return self

    def restore_permissions(self, experiment: str, relative_path: str) -> None:
        self.calls.append((experiment, relative_path))
        if self.error is not None:
            raise self.error


def _returned(
    tmp_path, monkeypatch, *, name: str = "crux-wire", status: str = "SUCCEEDED", returned: bool = True, **dm: Any
):
    """A submitted run whose results came back (``returned``: its return directory, ``incoming/<submission>``, with a
    file), its launcher state ``status``, and the launcher's DM client a :class:`Restorer`; ``dm``: keys of the
    launcher's ``dm`` settings."""
    plan, settings, config = _planned(tmp_path, name)
    if dm:
        settings = settings.model_copy(update={"dm": settings.dm.model_copy(update=dm)})
    incoming = Path(plan.get("incoming_directory", plan["output_directory"]))  # aps: its output directory
    if returned:
        incoming.mkdir(parents=True)
        (incoming / "probe.txt").write_text("returned output", encoding="utf-8")
    state = Path(settings.state_directory) / f"{SUBMISSION_ID}.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"status": status, "flow_id": "flow-run-1"}), encoding="utf-8")
    restorer = Restorer()
    monkeypatch.setattr(stage, "DMClient", restorer)
    return SimpleNamespace(
        plan=plan, settings=settings, config=config, state=state, incoming=incoming, restorer=restorer
    )


@pytest.mark.parametrize(("timeout", "bound"), [(None, 300), (600, 600)], ids=["default", "set"])
def test_the_stage_has_dm_restore_permissions_on_exactly_the_returned_directory(tmp_path, monkeypatch, timeout, bound):
    """One DM client, bounded by ``dm.permissions_timeout_seconds`` (300 s unless set) in place of
    ``dm.timeout_seconds``, and one call: the experiment folder's name and ``analysis/<run>/incoming/<submission>``
    relative to it. The returned files are left as they are."""
    run = _returned(tmp_path, monkeypatch, **({} if timeout is None else {"permissions_timeout_seconds": timeout}))
    assert stage.restore_permissions(run.plan, run.settings) == {"status": "PERMISSIONS_RESTORED"}
    [dm] = run.restorer.clients
    assert dm == run.settings.dm.model_copy(update={"timeout_seconds": bound})
    experiment = Path(run.settings.experiment_directory)
    relative = Path(run.plan["run_directory"]).relative_to(experiment) / "incoming" / SUBMISSION_ID
    assert run.restorer.calls == [(experiment.name, relative.as_posix())]
    assert relative.parts[0] == "analysis"
    assert (run.incoming / "probe.txt").read_text(encoding="utf-8") == "returned output"


@pytest.mark.parametrize("target", ["analysis", "run", "another-submission", "link", "missing"])
def test_the_stage_refuses_any_other_directory_without_a_dm_call(tmp_path, monkeypatch, target):
    """Never a folder outside the run (its parent), the run itself, another folder in it (another submission's return),
    a link that leads elsewhere or a return that is not there (one case each)."""
    run = _returned(tmp_path, monkeypatch)
    experiment, folder = Path(run.settings.experiment_directory), Path(run.plan["run_directory"])
    others = {
        "analysis": experiment / "analysis",
        "run": folder,
        "another-submission": folder / "incoming" / str(uuid4()),
    }
    if target == "link":  # the submission's own name, a link to a folder beside it
        (run.incoming / "probe.txt").unlink()
        run.incoming.rmdir()
        (folder / "unrelated").mkdir()
        run.incoming.symlink_to(folder / "unrelated", target_is_directory=True)
    elif target == "missing":
        (run.incoming / "probe.txt").unlink()
        run.incoming.rmdir()
    else:
        others[target].mkdir(parents=True, exist_ok=True)
        run.plan["incoming_directory"] = str(others[target])
    with pytest.raises(ValueError, match="this submission's incoming directory|must be inside"):
        stage.restore_permissions(run.plan, run.settings)
    assert run.restorer.clients == [] and run.restorer.calls == []


@pytest.mark.parametrize(("name", "status"), [("crux-wire", "FAILED"), ("aps-wire", "SUCCEEDED")])
def test_the_stage_needs_a_crux_run_whose_flow_succeeded(tmp_path, monkeypatch, name, status):
    run = _returned(tmp_path, monkeypatch, name=name, status=status)
    with pytest.raises(ValueError, match="completed Globus workflow"):
        stage.restore_permissions(run.plan, run.settings)
    assert run.restorer.clients == []


def test_a_dm_call_that_fails_fails_the_stage_and_so_does_a_launcher_without_an_experiment_directory(
    tmp_path, monkeypatch
):
    run = _returned(tmp_path, monkeypatch)
    run.restorer.error = PermissionError("DM denied access")
    monkeypatch.setattr(sys, "argv", ["stage", "--config", str(run.config), "permissions", SUBMISSION_ID])
    with pytest.raises(PermissionError, match="DM denied access"):
        stage.main()
    run.restorer.error = None
    unset = run.settings.model_copy(update={"experiment_directory": None})
    with pytest.raises(ValueError, match="experiment_directory is required"):
        stage.restore_permissions(run.plan, unset)


def test_the_stage_command_prints_its_status_and_leaves_the_launcher_state_as_it_was(tmp_path, monkeypatch, capsys):
    run = _returned(tmp_path, monkeypatch)
    kept = run.state.read_text(encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["stage", "--config", str(run.config), "permissions", SUBMISSION_ID])
    assert stage.main() == 0
    assert capsys.readouterr().out.splitlines() == ["Status: PERMISSIONS_RESTORED"]
    assert len(run.restorer.calls) == 1
    assert run.state.read_text(encoding="utf-8") == kept


def test_the_dm_call_runs_dms_api_script_in_a_shell_that_is_no_login_shell(tmp_path, monkeypatch, capsys):
    """Through the real DM client: one process (``bash -c``, DM's setup file sourced), bounded by the permissions
    timeout, whose API script calls DM's ``FileDsApi.restorePermissions`` (the stand-in)."""
    run = _returned(tmp_path, monkeypatch)
    monkeypatch.setattr(stage, "DMClient", DMClient)
    dm = FakeDMService(tmp_path / "dm", monkeypatch)
    monkeypatch.setattr(sys, "argv", ["stage", "--config", str(run.config), "permissions", SUBMISSION_ID])
    assert stage.main() == 0
    [call] = dm.runs
    assert call["argv"][:2] == ["/bin/bash", "-c"] and call["timeout"] == 300
    relative = str(run.incoming.relative_to(Path(run.settings.experiment_directory)))
    experiment = Path(run.settings.experiment_directory).name
    assert call["request"] == {
        "operation": "restore_permissions",
        "args": {"experiment": experiment, "relative_path": relative},
    }
    [request] = dm.requests()
    assert request["restorePermissions"] == {"experimentPath": relative, "experimentName": experiment}
    assert capsys.readouterr().out.splitlines() == ["Status: PERMISSIONS_RESTORED"]


# ---------------------------------------------------------------------------
# DM's workflow, emulated: the stages in order, each runIf honoured


def _holds(condition: str, variables: dict[str, str]) -> bool:
    """A stage's ``runIf`` or ``repeatUntil`` as DM evaluates it: the workflow's variables substituted (the longest
    names first), then the Python expression."""
    for name in sorted(variables, key=len, reverse=True):
        condition = condition.replace(f"${name}", variables[name])
    return bool(eval(condition, {"__builtins__": {}}))  # the template's own expression


def _workflow(variables: dict[str, str], run_stage) -> tuple[list[str], bool]:
    """DM running ``deploy/remote/workflow-bdp.json.template``'s stages for one job: in order, a stage whose
    ``runIf`` is false skipped; ``02-MONITOR`` repeated until its ``repeatUntil`` holds; each stage's
    ``outputVariableRegexList`` keeping variables (each regex's first match); a stage that fails ends the job. The
    actions run, and whether the job ended done."""
    ran = []
    for spec in json.loads(WORKFLOW.read_text(encoding="utf-8"))["stages"].values():
        if "runIf" in spec and not _holds(spec["runIf"], variables):
            continue
        _command, action, submission = spec["command"].split()
        assert (_command, submission) == ("{{STAGE_COMMAND}}", "$submissionId")
        for _repeat in range(int(spec.get("maxRepeats", 1))):
            exit_code, output = run_stage(action, variables["submissionId"])
            ran.append(action)
            if exit_code != 0:
                return ran, False
            for pattern in spec.get("outputVariableRegexList", []):
                match = re.search(pattern, output)
                if match:
                    variables.update(match.groupdict())
            if "repeatUntil" not in spec or _holds(spec["repeatUntil"], variables):
                break
    return ran, True


@pytest.mark.parametrize(
    ("name", "flow", "permissions"),
    [("crux-wire", "SUCCEEDED", True), ("crux-wire", "FAILED", False), ("aps-wire", None, False)],
)
def test_dms_workflow_runs_the_permissions_stage_only_for_a_crux_run_whose_flow_succeeded(
    tmp_path, monkeypatch, capsys, name, flow, permissions
):
    """The rendered workflow's ``03-PERMISSIONS`` (its ``runIf``: ``analysisMachine`` ``crux``, the submission's own
    argument, and ``remoteStatus`` ``SUCCEEDED``, ``02-MONITOR``'s), between the monitor and ``04-CHECK``; the job is
    done when the run returned Finished."""
    run = _returned(tmp_path, monkeypatch, name=name, returned=False)
    run.state.unlink()  # the workflow's submit writes it
    client = DMClient(run.settings.dm)
    client._call = Mock(return_value={"id": "dm-1"})
    client.submit(run.plan)
    submitted = client._call.call_args.kwargs["arguments"]  # the DM job's arguments: its workflow variables
    if run.plan["backend"] == "crux":
        fake = FakeFlow(SimpleNamespace(now=cases.NOW))
        fake.record = {"status": flow}
        monkeypatch.setattr(stage, "_client", fake.client)
        monkeypatch.setattr(stage, "_flows_client", fake.flows_client)
    else:
        pbs = FakePBS()
        pbs.state = {"job_state": "F", "Exit_status": 0}
        monkeypatch.setattr(stage.subprocess, "run", pbs)
    capsys.readouterr()

    def run_stage(action: str, submission_id: str) -> tuple[int, str]:
        if action in ("permissions", "check"):  # by then the results are back, as the Flow or PBS leaves them
            run.incoming.mkdir(parents=True, exist_ok=True)
            (run.incoming / "run.json").write_text(json.dumps({"status": "Finished"}), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["stage", "--config", str(run.config), action, submission_id])
        try:
            exit_code = stage.main()
        except Exception:  # a traceback: the stage failed (python's exit 1)
            exit_code = 1
        return exit_code, capsys.readouterr().out

    variables = {"submissionId": submitted["submissionId"], "analysisMachine": submitted["analysisMachine"]}
    ran, done = _workflow(variables, run_stage)
    expected = ["submit", "monitor", *(["permissions"] if permissions else []), "check"]
    assert ran == expected
    assert done is (flow != "FAILED")
    assert len(run.restorer.calls) == int(permissions)
