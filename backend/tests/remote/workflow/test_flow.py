"""Offline orchestration tests of the launcher's Globus side (gladier, globus-sdk: ``requirements/launcher.in``).

The settings are ``remote.settings.RemoteSettings`` (``deploy/remote/bdp.yaml.template``'s, with a temporary state
directory), and the mocks ``unittest.mock.patch.object``. No login, network, Globus or PBS: every token store lives in
the test's temporary directory.
"""

import inspect
import json
import os
import subprocess
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import pytest

from laueportal.remote.flow import TRANSFER_BATCH_EXPRESSION, client_class, flow_input
from laueportal.remote.pbs import run_pbs_job
from laueportal.remote.settings import RemoteSettings, load_settings
from laueportal.remote.stage import configure_auth

TEMPLATE = Path(__file__).resolve().parents[4] / "deploy" / "remote" / "bdp.yaml.template"


def launcher_settings(tmp_path, **changes) -> RemoteSettings:
    """The template's settings with the state directory in ``tmp_path`` (and ``changes``)."""
    return load_settings(TEMPLATE).model_copy(update={"state_directory": str(tmp_path), **changes})


def test_personal_login_prepares_service_and_flow_scopes(tmp_path, monkeypatch):
    from globus_sdk.scopes import AuthScopes, GCSCollectionScopes, SpecificFlowScopes, TransferScopes

    monkeypatch.delenv("GLADIER_CLIENT_ID", raising=False)
    monkeypatch.delenv("GLADIER_CLIENT_SECRET", raising=False)
    client = client_class(launcher_settings(tmp_path))(auto_registration=False)
    manager = client.login_manager
    flow_id = "00000000-0000-0000-0000-000000000001"
    collection_id = "00000000-0000-0000-0000-000000000002"
    flow_scope = SpecificFlowScopes(flow_id).user.with_dependency(
        TransferScopes.all.with_dependency(GCSCollectionScopes(collection_id).data_access)
    )
    manager.add_requirements([AuthScopes.openid, TransferScopes.all, flow_scope])
    try:
        with mock.patch.object(manager.globus_app, "login") as login:
            client.login()
            login.assert_called_once_with(auth_params=manager.globus_auth_parameters)
        requirements = manager.globus_app.scope_requirements
        assert set(requirements) == {
            "auth.globus.org",
            "flows.globus.org",
            "funcx_service",
            "transfer.api.globus.org",
            flow_id,
        }
        assert TransferScopes.all in requirements["transfer.api.globus.org"]
        assert flow_scope in requirements[flow_id]
        assert manager.get_resource_server(str(TransferScopes.all)) == "transfer.api.globus.org"
        with pytest.raises(ValueError, match="Unable to find resource server"):
            manager.get_resource_server("https://example.org/scopes/unknown/all")
    finally:
        manager.globus_app.close()
        manager.token_storage.close()


def test_beamline_profile_keeps_confidential_login(tmp_path, monkeypatch):
    from gladier.managers.login_manager import ConfidentialClientLoginManager

    monkeypatch.setenv("GLADIER_CLIENT_ID", "test-client")
    monkeypatch.setenv("GLADIER_CLIENT_SECRET", "test-secret")
    client = client_class(launcher_settings(tmp_path, auth_profile="beamline"))(auto_registration=False)
    try:
        assert isinstance(client.login_manager, ConfidentialClientLoginManager)
    finally:
        client.login_manager.globus_app.close()


def test_auth_requests_both_collection_consents_and_registers_without_running(tmp_path, monkeypatch):
    from globus_sdk.scopes import GCSCollectionScopes, TransferScopes

    from laueportal.remote.auth import authorize

    monkeypatch.delenv("GLADIER_CLIENT_ID", raising=False)
    monkeypatch.delenv("GLADIER_CLIENT_SECRET", raising=False)
    settings = launcher_settings(tmp_path)  # collection IDs ...0001 (aps) and ...0002 (eagle)
    client = client_class(settings)(auto_registration=False)
    manager = client.login_manager
    try:
        with ExitStack() as patches:
            browser_login = patches.enter_context(mock.patch.object(manager.globus_app, "login"))
            functions = patches.enter_context(
                mock.patch.object(client, "get_compute_function_ids", return_value={"pbs": "function-id"})
            )
            sync = patches.enter_context(mock.patch.object(client, "sync_flow"))
            patches.enter_context(mock.patch.object(client, "get_flow_id", return_value="flow-id"))
            patches.enter_context(mock.patch.object(client, "login"))
            run = patches.enter_context(mock.patch.object(client, "run_flow"))
            assert authorize(client, settings) == {"flow_id": "flow-id", "function_ids": {"pbs": "function-id"}}
            browser_login.assert_called_once_with()
            scopes = manager.globus_app.scope_requirements[TransferScopes.resource_server]
            dependencies = {dep.scope_string for scope in scopes for dep in scope.dependencies}
            assert dependencies == {
                GCSCollectionScopes(storage.collection_id).data_access.scope_string
                for storage in (settings.storage.aps, settings.storage.eagle)
                if storage is not None and storage.collection_id is not None
            }
            assert len(dependencies) == 2
            # Dependencies stay in GlobusApp; Gladier compares flat token scopes.
            assert all(not scope.dependencies for scope in manager.required_scopes)
            functions.assert_called_once_with()
            sync.assert_called_once_with()
            run.assert_not_called()
    finally:
        manager.globus_app.close()
        manager.token_storage.close()


def test_cancellation_accepts_sdk4_transfer_authorizer_keys(tmp_path):
    import globus_sdk
    from globus_sdk.scopes import TransferScopes

    from laueportal.remote import stage

    client = mock.Mock()
    authorizer = globus_sdk.AccessTokenAuthorizer("test-token")
    client.login_manager.get_manager_authorizers.return_value = {str(TransferScopes.all): authorizer}
    plan = {
        "backend": "crux",
        "cancel_transfer": {
            "source_endpoint_id": "00000000-0000-0000-0000-000000000001",
            "destination_endpoint_id": "00000000-0000-0000-0000-000000000002",
            "transfer_items": [{"source_path": "/cancel.request", "destination_path": "/control/cancel.request"}],
        },
    }
    with (
        mock.patch.object(stage, "_client", return_value=client),
        mock.patch.object(globus_sdk, "TransferClient") as transfer_class,
    ):
        transfer = transfer_class.return_value
        transfer.submit_transfer.return_value = {"task_id": "test-task"}
        transfer.task_wait.return_value = True
        transfer.get_task.return_value = {"status": "SUCCEEDED"}
        assert stage.cancel(plan, launcher_settings(tmp_path)) == {"status": "CANCEL_REQUESTED"}
        transfer_class.assert_called_once_with(authorizer=authorizer)


def test_generated_flow_orders_transfer_compute_and_return(tmp_path):
    client = client_class(launcher_settings(tmp_path))(auto_registration=False)
    definition = client.get_flow_definition()
    assert definition["StartAt"] == "InitInputTransfer"
    states = definition["States"]
    assert states["InputTransfer"]["Next"] == "AdvanceInputTransfer"
    assert states["InputsReady"]["Next"] == "RunPbsJob"
    assert states["MoreInputTransfers"]["Choices"][0]["Next"] == "InputTransfer"
    assert states["RunPbsJob"]["Next"] == "ResultTransfer"
    assert states["ResultTransfer"]["End"] is True
    assert states["RunPbsJob"]["ActionUrl"].endswith("/v3")
    assert states["InputTransfer"]["Parameters"]["verify_checksum"] is True
    assert states["ResultTransfer"]["Parameters"]["verify_checksum"] is True
    assert states["InputTransfer"]["ExceptionOnActionFailure"] is True
    task = states["RunPbsJob"]["Parameters"]["tasks"][0]
    assert "kwargs.$" not in task and "input_transfer.$" not in task["kwargs"]
    # Gladier passes the whole input dictionary as function kwargs.
    inspect.signature(run_pbs_job).bind(
        job_directory="job",
        script="script",
        fallback_summary={},
        cancel_file="cancel",
        compute_endpoint="ep",
        input_transfer={},
    )


def test_full_scan_transfer_batches_preserve_exact_files_below_state_limit():
    source = "/example-experiment/data/raw/dataset/"
    destination = (
        "/compute/projects/example-project/example-user/portal/workspace/runs/00000000-0000-0000-0000-000000000001/inputs"
        + source
    )
    items = [
        {
            "source_path": source + f"point_{i}.h5",
            "destination_path": destination + f"point_{i}.h5",
            "recursive": False,
        }
        for i in range(1, 826)
    ]
    plan = {
        "job_id": 1,
        "submission_id": "00000000-0000-0000-0000-000000000001",
        "n_inputs": 825,
        "kind": "wire_reconstruction",
        "backend": "crux",
        "execution": {},
        "pbs": {"account": "test-allocation", "nodes": 4, "walltime": "01:00:00", "queue": "test-queue"},
        "control_directory": "/work/control",
        "output_directory": "/work/results",
        "target": {"analysis_python": "/env/python", "code_directory": "/src", "compute_endpoint": "endpoint"},
        "input_transfer": {
            "source_endpoint_id": "source",
            "destination_endpoint_id": "destination",
            "transfer_items": items,
        },
        "result_transfer": {},
    }
    payload = flow_input(plan)
    assert len(json.dumps(payload).encode()) < 60_000
    compact = payload["input"]["input_transfer"]
    expanded, batches = [], []
    for offset in range(0, len(items), compact["batch_size"]):
        # The expression is our static flow definition, never supplied by a user.
        batch = eval(TRANSFER_BATCH_EXPRESSION, {"input": payload["input"], "transfer_cursor": {"offset": offset}})
        batches.append(batch)
        expanded.extend(batch)
        assert len(json.dumps(batch).encode()) < 60_000
    assert len(batches) == 9 and expanded == items
    assert len(json.dumps(plan["input_transfer"]).encode()) > 200_000
    assert "-m laueportal.jobs.mpi_runner /work/control\n" in payload["input"]["script"]


def test_existing_credentials_are_read_without_modification(tmp_path, monkeypatch):
    monkeypatch.setenv("GLADIER_CLIENT_ID", "initial-id")
    monkeypatch.setenv("GLADIER_CLIENT_SECRET", "initial-secret")
    credential = tmp_path / "existing.sh"
    original = b"export GLADIER_CLIENT_ID=test-client\nexport GLADIER_CLIENT_SECRET=test-secret\n"
    credential.write_bytes(original)
    configure_auth(launcher_settings(tmp_path, auth_profile="beamline", credentials_file=str(credential)))
    assert credential.read_bytes() == original
    assert os.environ["GLADIER_CLIENT_ID"] == "test-client"
    configure_auth(launcher_settings(tmp_path, auth_profile="personal"))
    assert "GLADIER_CLIENT_SECRET" not in os.environ


def _job(tmp_path):
    job = tmp_path / "job"
    (job / "control").mkdir(parents=True)
    output = job / "results"
    fallback = {
        "output_directory": str(output),
        "summary": {
            "job_id": 1,
            "counters": {"n_inputs": 2, "n_succeeded": 0, "n_failed": 0, "n_not_run": 2},
            "artifacts": {},
        },
    }
    return job, output, fallback


def test_pbs_failure_returns_diagnostics_after_scheduler_termination(tmp_path, monkeypatch):
    job, output, fallback = _job(tmp_path)

    def command(args, **kwargs):
        if args[0].endswith("qsub"):
            return subprocess.CompletedProcess(args, 0, "123.crux-pbs\n", "")
        if args[0].endswith("qstat"):
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    {
                        "Jobs": {
                            "123": {
                                "job_state": "F",
                                "Exit_status": 1,
                            }
                        }
                    }
                ),
                "",
            )
        pytest.fail(f"Unexpected command {args}")

    monkeypatch.setattr(subprocess, "run", command)
    result = run_pbs_job(
        job_directory=str(job),
        script="#!/bin/bash\nexit 1\n",
        fallback_summary=fallback,
        cancel_file=str(job / "control/cancel.request"),
    )
    assert "error" in result
    assert json.loads((output / "run.json").read_text())["status"] == "Failed"


def test_lost_pbs_submit_reply_never_claims_the_job_stopped(tmp_path, monkeypatch):
    job, output, fallback = _job(tmp_path)

    def command(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 60)

    monkeypatch.setattr(subprocess, "run", command)
    with pytest.raises(RuntimeError, match="reconciliation"):
        run_pbs_job(
            job_directory=str(job),
            script="#!/bin/bash\ntrue\n",
            fallback_summary=fallback,
            cancel_file=str(job / "control/cancel.request"),
        )
    assert not (output / "run.json").exists()
    assert json.loads((output / "pbs.json").read_text())["status"] == "SUBMISSION_UNCERTAIN"
