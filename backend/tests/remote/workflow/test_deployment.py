"""Exercise generic deployment tools using temporary paths and fake executables.

The stage command is ``deploy/remote/dm-stage.sh``, which runs ``python -m laueportal.remote.stage`` from the checkout's
``backend/src``; the settings are ``laueportal.remote.settings``. No DM, Globus or network.
"""

import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from laueportal.remote.settings import load_settings

ROOT = Path(__file__).resolve().parents[4]
DEPLOY = ROOT / "deploy" / "remote"


def tool(name):
    return runpy.run_path(str(DEPLOY / name))


def site(tmp_path):
    root = tmp_path / "operator's installation"
    source = root / "src"
    (source / "deploy" / "remote").mkdir(parents=True)
    shutil.copy2(DEPLOY / "dm-stage.sh", source / "deploy/remote/dm-stage.sh")
    python = root / "envs/custom/python"
    python.parent.mkdir(parents=True)
    python.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['RECEIPT']).write_text(json.dumps({'args':sys.argv[1:],'source':os.environ['PYTHONPATH']}))\n"
    )
    python.chmod(0o700)
    return {
        "dm": {
            "owner": "test-owner",
            "user_account": "test-executor",
            "workflow": "test-analysis",
            "cancel_workflow": "test-cancel",
            "launcher_code": str(source),
            "launcher_python": str(python),
            "launcher_config": str(root / "config/private settings.yaml"),
        }
    }


def test_rendered_workflow_executes_wrapper_with_quoted_site_paths(outside_checkout):
    """The workflow: ``03-PERMISSIONS`` between the monitor and the check (its ``runIf`` and each stage's action:
    ``tests/remote/test_stage_permissions.py``, DM's workflow emulated)."""
    tmp_path = outside_checkout
    settings = site(tmp_path)
    output = tmp_path / "private definitions"
    files = tool("render-workflows.py")["render_workflows"](settings, output)
    assert len(files) == 2
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in files)
    main = json.loads((output / "workflow-bdp.json").read_text())
    cancel = json.loads((output / "workflow-bdp-cancel.json").read_text())
    assert (main["name"], main["owner"], main["userAccount"]) == ("test-analysis", "test-owner", "test-executor")
    assert cancel["name"] == "test-cancel"
    assert list(main["stages"]) == ["01-SUBMIT", "02-MONITOR", "03-PERMISSIONS", "04-CHECK"]
    assert "$cancelSubmissionId" in cancel["stages"]["01-CANCEL"]["command"]
    receipt = tmp_path / "receipt.json"
    identifier = "00000000-0000-0000-0000-000000000001"
    subprocess.run(
        ["bash", "-c", main["stages"]["01-SUBMIT"]["command"]],
        env={**os.environ, "submissionId": identifier, "RECEIPT": str(receipt)},
        check=True,
        capture_output=True,
        text=True,
    )
    invocation = json.loads(receipt.read_text())
    # The package is under the checkout's backend/src.
    assert invocation["source"] == settings["dm"]["launcher_code"] + "/backend/src"
    assert invocation["args"] == [
        "-m",
        "laueportal.remote.stage",
        "--config",
        settings["dm"]["launcher_config"],
        "submit",
        identifier,
    ]


def test_the_cleanup_workflow_is_rendered_when_it_is_set_and_runs_the_cleanup_stage(outside_checkout):
    """The clean-up DM workflow (``dm.cleanup_workflow``) runs the launcher's ``cleanup`` stage."""
    tmp_path = outside_checkout
    settings = site(tmp_path)
    settings["dm"]["cleanup_workflow"] = "test-cleanup"
    output = tmp_path / "private definitions"
    files = tool("render-workflows.py")["render_workflows"](settings, output)
    assert sorted(path.name for path in files) == [
        "workflow-bdp-cancel.json",
        "workflow-bdp-cleanup.json",
        "workflow-bdp.json",
    ]
    cleanup = json.loads((output / "workflow-bdp-cleanup.json").read_text())
    assert (cleanup["name"], cleanup["userAccount"]) == ("test-cleanup", "test-executor")
    receipt = tmp_path / "receipt.json"
    identifier = "00000000-0000-0000-0000-000000000001"
    subprocess.run(
        ["bash", "-c", cleanup["stages"]["01-CLEANUP"]["command"]],
        env={**os.environ, "cleanupSubmissionId": identifier, "RECEIPT": str(receipt)},
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(receipt.read_text())["args"][-2:] == ["cleanup", identifier]


def test_stage_wrapper_defaults_to_its_installation_root(tmp_path):
    """dm-stage.sh lives in <root>/src/deploy/remote/, so its default root is three levels up."""
    settings = site(tmp_path)
    root = Path(settings["dm"]["launcher_code"]).parent
    python = root / "envs/laue-portal/bin/python"
    python.parent.mkdir(parents=True)
    shutil.copy2(settings["dm"]["launcher_python"], python)
    receipt = tmp_path / "receipt.json"
    env = {key: value for key, value in os.environ.items() if not key.startswith("LAUE_")}
    subprocess.run(
        [str(root / "src/deploy/remote/dm-stage.sh"), "monitor", "00000000-0000-0000-0000-000000000002"],
        env={**env, "RECEIPT": str(receipt)},
        check=True,
        capture_output=True,
        text=True,
    )
    invocation = json.loads(receipt.read_text())
    assert invocation["source"] == str(root / "src/backend/src")
    assert invocation["args"][:4] == ["-m", "laueportal.remote.stage", "--config", str(root / "config/bdp.yaml")]


def test_renderer_preserves_existing_definition_without_partial_output(outside_checkout):
    tmp_path = outside_checkout
    settings = site(tmp_path)
    output = tmp_path / "private"
    output.mkdir()
    existing = output / "workflow-bdp-cancel.json"
    existing.write_bytes(b"existing configuration\n")
    with pytest.raises(FileExistsError):
        tool("render-workflows.py")["render_workflows"](settings, output)
    assert existing.read_bytes() == b"existing configuration\n"
    assert not (output / "workflow-bdp.json").exists()


def test_renderer_rejects_site_output_inside_checkout(tmp_path):
    with pytest.raises(ValueError, match="outside the source checkout"):
        tool("render-workflows.py")["render_workflows"](site(tmp_path), ROOT / "site-definitions")


def endpoint_environment(tmp_path):
    bin_directory = tmp_path / "endpoint env/bin"
    bin_directory.mkdir(parents=True)
    hostname = bin_directory / "hostname"
    hostname.write_text("#!/bin/sh\nprintf '%s\\n' test-node\n")
    hostname.chmod(0o700)
    endpoint = bin_directory / "globus-compute-endpoint"
    endpoint.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys\n"
        "with open(os.environ['RECEIPT'],'a') as handle: handle.write(json.dumps(sys.argv[1:])+'\\n')\n"
    )
    endpoint.chmod(0o700)
    return {
        **os.environ,
        "LAUE_ENDPOINT_ENV": str(bin_directory.parent),
        "LAUE_ENDPOINT_NAME": "test endpoint",
        "LAUE_ENDPOINT_HOST": "test-node",
        "RECEIPT": str(tmp_path / "endpoint-calls.jsonl"),
    }


def test_endpoint_control_uses_configured_environment_and_name(tmp_path):
    env = endpoint_environment(tmp_path)
    for action in ["start", "stop", "list"]:
        subprocess.run(
            ["bash", str(DEPLOY / "globus/endpoint.sh"), action],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    calls = [json.loads(line) for line in Path(env["RECEIPT"]).read_text().splitlines()]
    assert calls == [["start", "--detach", "test endpoint"], ["list"], ["stop", "test endpoint"], ["list"]]


def test_endpoint_control_rejects_another_host_before_invoking_service(tmp_path):
    env = endpoint_environment(tmp_path)
    env["LAUE_ENDPOINT_HOST"] = "another-node"
    result = subprocess.run(
        ["bash", str(DEPLOY / "globus/endpoint.sh"), "start"], env=env, capture_output=True, text=True
    )
    assert result.returncode != 0
    assert not Path(env["RECEIPT"]).exists()


def test_endpoint_probe_checks_supplied_paths(tmp_path):
    probe = tool("globus/endpoint_smoke.py")["endpoint_probe"]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    python, qsub = tmp_path / "python", tmp_path / "qsub"
    python.touch()
    qsub.touch()
    assert all(
        probe(workspace, python, qsub)[key] for key in ["workspace_exists", "analysis_python_exists", "qsub_exists"]
    )
    python.unlink()
    assert probe(workspace, python, qsub)["analysis_python_exists"] is False


def test_vm_verifier_accepts_batched_flow_with_unauthenticated_state(tmp_path, monkeypatch):
    monkeypatch.delenv("GLADIER_CLIENT_ID", raising=False)
    monkeypatch.delenv("GLADIER_CLIENT_SECRET", raising=False)
    settings = load_settings(DEPLOY / "bdp.yaml.template")  # auth_profile: personal
    states = tool("verify-dm-vm.py")["verify_flow"](settings, tmp_path)
    assert {"InitInputTransfer", "MoreInputTransfers", "InputsReady", "RunPbsJob", "ResultTransfer"} <= set(states)


def test_vm_verifier_reads_the_exact_pins_of_the_launcher_lock():
    pins = {
        str(requirement) for requirement in tool("verify-dm-vm.py")["requirements"](ROOT / "requirements/launcher.lock")
    }
    assert {"gladier==0.11.0", "globus-sdk==4.9.1", "globus-compute-sdk==4.19.0", "pyyaml==6.0.2"} <= pins


def test_vm_verifier_writes_the_source_manifest_it_checks(tmp_path):
    """The source manifest (``<root>/state/setup/source-manifest.json``, ``{"files": {path: sha256}}``) is written
    by ``--write-source-manifest`` and checked byte for byte; without one the verifier says how to write it."""
    verifier = tool("verify-dm-vm.py")
    source = tmp_path / "root" / "src"
    (source / "backend" / "src" / "__pycache__").mkdir(parents=True)
    (source / "README.md").write_text("reviewed\n")
    (source / "backend" / "src" / "module.py").write_text("x = 1\n")
    (source / "backend" / "src" / "__pycache__" / "module.cpython-312.pyc").write_bytes(b"cache")
    manifest = tmp_path / "root" / verifier["SOURCE_MANIFEST"]

    with pytest.raises(SystemExit, match="--write-source-manifest"):
        verifier["check_source"](source, manifest)
    written = verifier["write_source_manifest"](source, manifest)
    assert sorted(written["files"]) == ["README.md", "backend/src/module.py"]
    assert json.loads(manifest.read_text())["files"] == written["files"]
    assert manifest.parent.stat().st_mode & 0o777 == 0o700
    assert verifier["check_source"](source, manifest)["files"] == written["files"]
    (source / "README.md").write_text("changed after the review\n")
    with pytest.raises(AssertionError, match="Source checksum mismatch: README.md"):
        verifier["check_source"](source, manifest)


def test_vm_verifier_records_only_the_tracked_files_of_a_git_checkout(tmp_path):
    verifier = tool("verify-dm-vm.py")
    source = tmp_path / "src"
    source.mkdir()
    (source / "tracked.txt").write_text("tracked\n")
    (source / "untracked.log").write_text("not part of the revision\n")
    git = ["git", "-C", str(source), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "tracked.txt"], check=True)
    assert verifier["source_files"](source) == ["tracked.txt"]
