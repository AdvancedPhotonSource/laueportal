"""The launcher's ``monitor`` stage on ``crux``: the remote observation it prints.

``02-MONITOR`` runs ``python -m laueportal.remote.stage --config <bdp.yaml> monitor <submission>`` every 10 s; on
``crux`` it prints ``Observation: <json>`` (``remote.observation``'s format, the goldens in
``tests/fixtures/observation_goldens/``), from the Flow's step and, while the Flow is in ``RunPbsJob``, from one listing
of the run's ``results/`` on Eagle at most every ``targets.crux.listing.interval_seconds``; while ``get_status`` fails,
the kept observation, stale, with a back-off, until ``targets.crux.status_failure_budget_seconds``. Fakes only: the Flow
(``get_status``'s run document, or its error) and a Transfer client that lists the local stand-in for Eagle; no Globus,
no network.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import socket
import stat
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any
from unittest import mock

import globus_sdk
import pytest
import requests
import yaml
from gladier.managers.login_manager import BaseLoginManager, UserAppLoginManager
from globus_sdk.authorizers import RenewingAuthorizer
from globus_sdk.scopes import FlowsScopes, TransferScopes

from laueportal.remote import stage
from laueportal.remote.observation import accept
from laueportal.remote.poller import poll_once
from laueportal.remote.submission import submit_run
from tests.fixtures.observation_goldens import cases
from tests.remote.test_poller import FakeDM
from tests.remote.test_stage import FakePBS, _planned
from tests.remote_support import SUBMISSION_ID, Layout, bdp_settings, create_wire_run, remote_settings
from tests.run_support import load_job, make_engine

GOLDENS: dict[str, str] = json.loads(cases.GOLDENS.read_text(encoding="utf-8"))["observations"]
WORKFLOW = Path(__file__).resolve().parents[3] / "deploy" / "remote" / "workflow-bdp.json.template"
INPUT_STATES = ("InitInputTransfer", "InputTransfer", "AdvanceInputTransfer", "MoreInputTransfers", "InputsReady")
TRANSFER_CLIENT = stage._transfer_client
FLOWS_CLIENT = stage._flows_client
"""The launcher's own ``_transfer_client`` and ``_flows_client`` (``_launcher`` replaces them with fakes)."""


def _record(state: str | None, status: str = "ACTIVE") -> dict[str, Any]:
    """A Flow's run document as ``get_status`` returns it, its current action in ``state`` (``cases.py``'s)."""
    actions = [] if state is None else [{"state_name": state, "status": "ACTIVE"}]
    return {"status": status, "details": {"action_statuses": actions}}


def _answer(status: int, url: str, body: dict[str, Any] | None = None) -> requests.Response:
    """A Globus service's HTTP answer to a request of ``url``: its ``status`` and, when given, its JSON ``body``."""
    response = requests.Response()
    response.status_code = status
    if body is not None:
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(body).encode()
    response.request = requests.Request("GET", url).prepare()
    return response


def _transfer_error(status: int, code: str | None = None, message: str = "") -> globus_sdk.TransferAPIError:
    """Transfer's error answer to a listing: its HTTP ``status`` and, when given, the JSON body's ``code``."""
    body = None if code is None else {"code": code, "message": message}
    return globus_sdk.TransferAPIError(
        _answer(status, "https://transfer.api.globus.org/v0.10/operation/endpoint/e/ls", body)
    )


def _flows_error(status: int) -> globus_sdk.FlowsAPIError:
    """Globus Flows' error answer to ``get_status`` (a 5xx: the service failing)."""
    return globus_sdk.FlowsAPIError(_answer(status, f"https://flows.globus.org/runs/{cases.FLOW_ID}"))


TOKEN = "AgTokenThatMustNeverBeEchoed"
"""A token value in an error's message: stderr names the error's type and HTTP status only."""


def _renewal_error() -> globus_sdk.AuthAPIError:
    """Globus Auth's answer to a token renewal it refuses, its message holding a token."""
    body = {"error": "invalid_grant", "error_description": f"refresh token {TOKEN} is not valid"}
    return globus_sdk.AuthAPIError(_answer(400, "https://auth.globus.org/v2/oauth2/token", body))


def _network_error() -> globus_sdk.GlobusConnectionError:
    """A request to Flows that reached no answer."""
    return globus_sdk.GlobusConnectionError("connection refused", requests.ConnectionError("connection refused"))


def _not_found(path: str) -> globus_sdk.TransferAPIError:
    """Transfer's answer to a listing of a folder that does not exist."""
    return _transfer_error(404, "ClientError.NotFound", f"Directory {path} not found")


class FakeListing:
    """A Globus Transfer client whose ``operation_ls`` lists the local stand-in for Eagle (``storage.eagle``'s
    filesystem root) as Transfer does (``last_modified`` in its format; a 404 ``TransferAPIError`` for a missing
    folder); ``error`` is raised instead, and while ``hold`` is set the listing waits for it (a slow one)."""

    def __init__(self, settings):
        self.eagle = settings.storage.eagle
        self.calls: list[tuple[str, str]] = []
        self.error: BaseException | None = None
        self.hold: threading.Event | None = None

    def operation_ls(self, endpoint_id, *, path):
        self.calls.append((endpoint_id, path))
        if self.hold is not None:
            self.hold.wait(30)
        if self.error is not None:
            raise self.error
        folder = Path(self.eagle.filesystem_root) / PurePosixPath(path).relative_to(self.eagle.collection_root)
        if not folder.is_dir():
            raise _not_found(path)
        return [
            {
                "name": entry.name,
                "type": "dir" if entry.is_dir() else "file",
                "last_modified": datetime.fromtimestamp(entry.stat().st_mtime, UTC).strftime("%Y-%m-%d %H:%M:%S+00:00"),
            }
            for entry in sorted(folder.iterdir())
        ]


class FakeFlow:
    """The launcher's gladier client (``stage._client``'s) and its Flows client for one status call
    (``stage._flows_client``'s): ``get_run`` answers ``record``, or raises ``error`` when it is set; ``calls``: the
    launcher's clock at each ``get_run``, in seconds after the goldens' ``NOW``; ``built``: the clients built;
    ``timeouts``: the bound each Flows client was made with."""

    def __init__(self, clock: SimpleNamespace):
        self.clock, self.record, self.error = clock, _record("RunPbsJob"), None
        self.calls: list[float] = []
        self.built = 0
        self.timeouts: list[float] = []
        self.login_manager = None

    def client(self, _settings) -> FakeFlow:
        self.built += 1
        return self

    def flows_client(self, client, *, timeout: float) -> FakeFlow:
        assert client is self
        self.timeouts.append(timeout)
        return self

    def run_flow(self, flow_input, *, label, tags):
        return {"action_id": cases.FLOW_ID}

    def get_run(self, run_id):
        self.calls.append((self.clock.now - cases.NOW).total_seconds())
        if self.error is not None:
            raise self.error
        return SimpleNamespace(data=self.record)


def _launcher(tmp_path, monkeypatch, *, flow_id=cases.FLOW_ID, **target_changes) -> SimpleNamespace:
    """A submitted ``crux`` run (its plan in the DM inbox, the launcher's state with its Flow run), the Flow
    (``flow``, a :class:`FakeFlow`: its status ``flow.record``), a Transfer client listing the Eagle stand-in
    (``transfer``; ``clients``: the timeouts the launcher's ``_transfer_client`` was asked for, once per client
    built) and the launcher's clock (``clock.now``, from the goldens' ``NOW``)."""
    plan, settings, config = _planned(tmp_path, "crux-wire", **target_changes)
    state = Path(settings.state_directory) / f"{SUBMISSION_ID}.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps({"status": "ACTIVE", "flow_id": flow_id, "url": f"https://app.globus.org/runs/{flow_id}"})
    )
    clock = SimpleNamespace(now=cases.NOW)
    flow = FakeFlow(clock)
    transfer, clients = FakeListing(settings), []

    def transfer_client(_settings, *, timeout=None):
        clients.append(timeout)
        return transfer

    monkeypatch.setattr(stage, "_client", flow.client)
    monkeypatch.setattr(stage, "_flows_client", flow.flows_client)
    monkeypatch.setattr(stage, "_transfer_client", transfer_client)
    monkeypatch.setattr(stage, "_now", lambda: clock.now)
    return SimpleNamespace(
        plan=plan,
        config=config,
        state=state,
        flow=flow,
        transfer=transfer,
        clients=clients,
        clock=clock,
        results=Path(plan["output_directory"]),  # <workspace>/runs/<submission>/results on the Eagle stand-in
    )


def _write(launcher, name: str, age: float) -> None:
    """A status file in the run's ``results/`` on Eagle, last modified ``age`` seconds before the launcher's now."""
    launcher.results.mkdir(parents=True, exist_ok=True)
    path = launcher.results / name
    path.write_text("{}", encoding="utf-8")
    stamp = (launcher.clock.now - timedelta(seconds=age)).timestamp()
    os.utime(path, (stamp, stamp))


def _monitor(launcher, monkeypatch, capsys, *, at: float | None = None) -> tuple[list[str], dict[str, Any] | None]:
    """One run of DM's ``02-MONITOR`` (``at``: seconds after the goldens' ``NOW``): the printed lines, and the
    observation the ``Observation:`` line holds (None without one). The stage never fails."""
    if at is not None:
        launcher.clock.now = cases.NOW + timedelta(seconds=at)
    monkeypatch.setattr(sys, "argv", ["stage", "--config", str(launcher.config), "monitor", SUBMISSION_ID])
    assert stage.main() == 0
    output = capsys.readouterr()
    lines, launcher.err = output.out.splitlines(), output.err
    texts = [line.removeprefix("Observation: ") for line in lines if line.startswith("Observation: ")]
    assert len(texts) <= 1
    return lines, json.loads(texts[0]) if texts else None


def _shown(observation: dict[str, Any]) -> tuple[str, str, bool]:
    return observation["phase"], observation["detail"], observation["stale"]


# ---------------------------------------------------------------------------
# The listing table


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        pytest.param({}, ("queued", "Waiting for endpoint", False), id="no-folder"),
        pytest.param({"frame_00001.h5": 5}, ("queued", "Waiting for endpoint", False), id="no-pbs-json"),
        pytest.param({"pbs.json": 3}, ("queued", "Waiting for PBS allocation", False), id="pbs-json"),
        pytest.param({"pbs.json": 3, "progress.json": 2}, ("running", "Processing", False), id="progress-json"),
        pytest.param(
            {"pbs.json": 3, "progress.json": 40, "run.json": 40},
            ("running", "Finishing processing", False),
            id="run-json",
        ),
        # the newer of pbs.json and progress.json decides
        pytest.param({"pbs.json": 300, "progress.json": 4}, ("running", "Processing", False), id="pbs-json-old"),
        pytest.param({"pbs.json": 4, "progress.json": 300}, ("running", "Processing", False), id="progress-json-old"),
        # more than 90 s old is stale; 90 s is not
        pytest.param({"pbs.json": 90}, ("queued", "Waiting for PBS allocation", False), id="heartbeat-90-s"),
        # stale with no listed phase before: the listing's own row
        pytest.param({"pbs.json": 91}, ("queued", "Waiting for PBS allocation", True), id="first-listing-stale"),
        # run.json: the runner has ended, so its heartbeats' age makes it no stale listing
        pytest.param(
            {"pbs.json": 300, "progress.json": 300, "run.json": 300},
            ("running", "Finishing processing", False),
            id="run-json-old-heartbeats",
        ),
    ],
)
def test_each_listing_case_gives_its_phase_and_detail(tmp_path, monkeypatch, capsys, files, expected):
    launcher = _launcher(tmp_path, monkeypatch)
    for name, age in files.items():
        _write(launcher, name, age)
    lines, observation = _monitor(launcher, monkeypatch, capsys)
    assert observation is not None and _shown(observation) == expected
    assert observation["flow_state"] == "RunPbsJob" and "pbs" not in observation  # no PBS ID while in PBS
    # one listing of the plan's result transfer's source, in the Eagle collection
    eagle = launcher.plan["result_transfer"]["source_endpoint_id"]
    assert launcher.transfer.calls == [(eagle, f"/workspace/runs/{SUBMISSION_ID}/results")]
    assert launcher.clients == [13]  # targets.crux.listing.timeout_seconds' default (8), past it by the margin (5)
    assert lines[0] == "Status: ACTIVE"


def test_old_heartbeats_keep_the_last_phase_stale_but_a_listed_run_json_is_not(tmp_path, monkeypatch, capsys):
    launcher = _launcher(tmp_path, monkeypatch)
    _write(launcher, "pbs.json", 2)
    _write(launcher, "progress.json", 5)  # written at NOW - 5 s, and not again
    _lines, processing = _monitor(launcher, monkeypatch, capsys, at=0)
    assert processing is not None and _shown(processing) == ("running", "Processing", False)

    (launcher.results / "pbs.json").unlink()  # pbs.json and progress.json stopped: the listing cannot tell why
    _lines, late = _monitor(launcher, monkeypatch, capsys, at=86)  # progress.json 91 s old
    assert late is not None and _shown(late) == ("running", "Processing", True)
    assert late["phase_since"] == processing["phase_since"] == cases.at(0)
    assert late["observed_at"] == cases.at(86)

    _write(launcher, "run.json", 0)  # the runner's last file: nothing is left to rewrite the heartbeats
    _lines, finishing = _monitor(launcher, monkeypatch, capsys, at=120)  # progress.json 125 s old
    assert finishing is not None and _shown(finishing) == ("running", "Finishing processing", False)
    assert finishing["phase_since"] == cases.at(0)  # the same phase since

    launcher.transfer.error = _transfer_error(503)
    _lines, failed = _monitor(launcher, monkeypatch, capsys, at=150)
    assert failed is not None and _shown(failed) == ("running", "Finishing processing", True)  # failed: stale


def test_a_flow_waiting_in_runpbsjob_is_listed(tmp_path, monkeypatch, capsys):
    """An ``INACTIVE`` Flow in ``RunPbsJob`` waiting for anything but a sign-in (a paused action; a sign-in is Login
    needed, ``test_a_flow_waiting_for_a_sign_in_is_login_needed_unlisted_and_keeps_no_challenge``): its PBS job may
    run."""
    launcher = _launcher(tmp_path, monkeypatch)
    launcher.flow.record = _record("RunPbsJob", status="INACTIVE")
    _write(launcher, "pbs.json", 1)
    lines, observation = _monitor(launcher, monkeypatch, capsys, at=0)
    assert lines[0] == "Status: INACTIVE"
    assert observation is not None and _shown(observation) == ("queued", "Waiting for PBS allocation", False)
    assert len(launcher.transfer.calls) == 1


# ---------------------------------------------------------------------------
# At most every 30 s, and only while the Flow is in RunPbsJob


def test_the_listing_is_at_most_every_30_seconds_and_the_runs_between_print_the_kept_observation(
    tmp_path, monkeypatch, capsys
):
    launcher = _launcher(tmp_path, monkeypatch)
    _write(launcher, "pbs.json", 1)
    printed: dict[int, dict[str, Any] | None] = {}
    for second in (0, 10, 20, 29, 30, 40, 50, 59, 60):
        _lines, printed[second] = _monitor(launcher, monkeypatch, capsys, at=second)
        if second == 30:
            _write(launcher, "progress.json", 0)
    assert len(launcher.transfer.calls) == 3 and len(launcher.clients) == 3  # at 0, 30 and 60 s
    assert printed[10] == printed[20] == printed[29] == printed[0]  # the kept one, as it was printed
    assert printed[0] is not None and printed[0]["observed_at"] == cases.at(0)
    assert printed[40] == printed[50] == printed[59] == printed[30]
    assert printed[60] is not None and _shown(printed[60]) == ("running", "Processing", False)
    kept = json.loads(launcher.state.read_text())
    assert kept["listed_at"] == cases.at(60) and kept["observation"] == printed[60]


def test_a_shorter_interval_set_in_bdp_yaml_is_followed(tmp_path, monkeypatch, capsys):
    launcher = _launcher(tmp_path, monkeypatch, listing={"interval_seconds": 15})
    for second in (0, 10, 20, 30, 40):
        _monitor(launcher, monkeypatch, capsys, at=second)
    assert len(launcher.transfer.calls) == 3  # at 0, 20 and 40 s (DM runs the stage every 10 s)


def test_the_interval_is_measured_at_the_clocks_full_precision(tmp_path, monkeypatch, capsys):
    launcher = _launcher(tmp_path, monkeypatch)
    _write(launcher, "pbs.json", 1)
    _monitor(launcher, monkeypatch, capsys, at=0.6)
    _monitor(launcher, monkeypatch, capsys, at=30.5)  # 29.9 s later: not yet
    assert len(launcher.transfer.calls) == 1
    _monitor(launcher, monkeypatch, capsys, at=30.6)
    assert len(launcher.transfer.calls) == 2


@pytest.mark.parametrize(
    "record",
    [
        # one input state, and the last before RunPbsJob (FLOW_STEPS is pinned by the generated Flow's test)
        pytest.param(_record("InputTransfer"), id="InputTransfer"),
        pytest.param(_record("InputsReady"), id="InputsReady"),
        pytest.param(_record("ResultTransfer"), id="ResultTransfer"),
        pytest.param(_record(None, status="SUCCEEDED"), id="succeeded"),
        pytest.param(_record("RunPbsJob", status="FAILED"), id="failed-in-RunPbsJob"),
        pytest.param(_record("RunPbsJob", status="SUCCEEDED"), id="succeeded-in-RunPbsJob"),
        pytest.param(_record(None), id="no-step"),
        pytest.param({"status": "ACTIVE"}, id="no-details"),
    ],
)
def test_nothing_is_listed_while_the_flow_is_not_in_runpbsjob(tmp_path, monkeypatch, capsys, record):
    launcher = _launcher(tmp_path, monkeypatch)
    _write(launcher, "pbs.json", 1)
    launcher.flow.record = record
    for second in (0, 10, 40, 80):
        _monitor(launcher, monkeypatch, capsys, at=second)
    assert launcher.transfer.calls == [] and launcher.clients == []  # no Transfer client is even built
    assert "listed_at" not in json.loads(launcher.state.read_text())


def test_nothing_is_listed_for_an_aps_run(tmp_path, monkeypatch, capsys):
    plan, settings, config = _planned(tmp_path, "aps-wire")
    monkeypatch.setattr(stage.subprocess, "run", FakePBS())
    stage.submit(plan, settings)
    monkeypatch.setattr(stage, "_transfer_client", mock.Mock(side_effect=AssertionError("no listing for aps")))
    monkeypatch.setattr(sys, "argv", ["stage", "--config", str(config), "monitor", SUBMISSION_ID])
    assert stage.main() == 0
    assert capsys.readouterr().out.splitlines() == ["Status: RUNNING"]  # no observation for aps


def test_the_flows_steps_are_the_generated_flows_states(tmp_path):
    from laueportal.remote.flow import client_class
    from tests.remote.workflow.test_flow import launcher_settings

    definition = client_class(launcher_settings(tmp_path))(auto_registration=False).get_flow_definition()
    assert set(stage.FLOW_STEPS) == set(definition["States"])
    assert {state for state, step in stage.FLOW_STEPS.items() if step == "InputTransfer"} == set(INPUT_STATES)


# ---------------------------------------------------------------------------
# A failing or slow listing: stale, never a failed stage, and no retry before the next 30 s


def test_a_failing_listing_is_stale_never_a_failed_stage_and_backs_off_to_60_seconds(tmp_path, monkeypatch, capsys):
    """Each failed listing doubles the wait from ``interval_seconds`` (30 s) up to 60 s, kept in the state file, reset
    when one answers; stderr has one line per change."""
    launcher = _launcher(tmp_path, monkeypatch)
    _write(launcher, "pbs.json", 1)
    _write(launcher, "progress.json", 1)
    _lines, processing = _monitor(launcher, monkeypatch, capsys, at=0)
    assert processing is not None and _shown(processing) == ("running", "Processing", False)

    launcher.transfer.error = _transfer_error(503)
    lines, failed = _monitor(launcher, monkeypatch, capsys, at=30)  # main returned 0: the stage did not fail
    assert lines[0] == "Status: ACTIVE"
    assert failed is not None and _shown(failed) == ("running", "Processing", True)
    assert failed["observed_at"] == cases.at(30) and failed["phase_since"] == cases.at(0)
    assert launcher.err.splitlines() == [
        "The results listing on Eagle failed: TransferAPIError (HTTP 503); the observation is stale; the next listing"
        " in 60 s, twice as long after each failure up to 60 s"
    ]
    for second in (40, 59, 60, 89):
        assert _monitor(launcher, monkeypatch, capsys, at=second)[1] == failed  # no listing: the kept one
    assert len(launcher.transfer.calls) == 2

    _monitor(launcher, monkeypatch, capsys, at=90)  # the same failure: not logged again
    assert len(launcher.transfer.calls) == 3 and launcher.err == ""
    assert json.loads(launcher.state.read_text())["listing_wait"] == 60
    _monitor(launcher, monkeypatch, capsys, at=120)
    assert len(launcher.transfer.calls) == 3

    launcher.transfer.error = _transfer_error(502)
    _monitor(launcher, monkeypatch, capsys, at=150)  # another failure: logged
    assert len(launcher.transfer.calls) == 4 and "TransferAPIError (HTTP 502)" in launcher.err

    launcher.transfer.error = None
    _write(launcher, "progress.json", -210)  # rewritten at 210 s
    _lines, recovered = _monitor(launcher, monkeypatch, capsys, at=210)
    assert recovered is not None and _shown(recovered) == ("running", "Processing", False)
    assert launcher.err.splitlines() == ["The results listing on Eagle answers again; it lists every 30 s"]
    assert not {"listing_wait", "listing_failure"} & set(json.loads(launcher.state.read_text()))
    _monitor(launcher, monkeypatch, capsys, at=240)  # every 30 s again
    assert len(launcher.transfer.calls) == 6 and launcher.err == ""


class Killed(BaseException):
    """The stage ended during its listing (a signal, DM's own timeout)."""


def test_the_listing_time_is_saved_before_the_listing_so_a_killed_stage_does_not_list_again_before_30_seconds(
    tmp_path, monkeypatch, capsys
):
    launcher = _launcher(tmp_path, monkeypatch)
    _write(launcher, "pbs.json", 1)
    _lines, queued = _monitor(launcher, monkeypatch, capsys, at=0)
    launcher.transfer.error = Killed()
    with pytest.raises(Killed):
        _monitor(launcher, monkeypatch, capsys, at=30)
    assert json.loads(launcher.state.read_text())["listed_at"] == cases.at(30)

    launcher.transfer.error = None
    assert _monitor(launcher, monkeypatch, capsys, at=40)[1] == queued  # the kept one, no listing
    assert len(launcher.transfer.calls) == 2


def test_a_listing_that_cannot_start_is_stale_and_names_no_value(tmp_path, monkeypatch, capsys):
    """The Transfer client cannot be built (no consent, Auth unreachable): the first listing is stale, Queued."""
    launcher = _launcher(tmp_path, monkeypatch)
    secret = "a-value-that-must-not-be-echoed"
    monkeypatch.setattr(stage, "_transfer_client", mock.Mock(side_effect=RuntimeError(secret)))
    _lines, observation = _monitor(launcher, monkeypatch, capsys, at=0)
    assert observation is not None and _shown(observation) == ("queued", "Waiting for endpoint", True)
    assert json.loads(launcher.state.read_text())["listed_at"] == cases.at(0)
    assert "RuntimeError" in launcher.err and secret not in launcher.err


def test_a_slow_listing_is_given_up_at_the_timeout_stale_and_not_retried_before_30_seconds(
    tmp_path, monkeypatch, capsys
):
    launcher = _launcher(tmp_path, monkeypatch, listing={"timeout_seconds": 0.3})
    _write(launcher, "pbs.json", 1)
    _lines, queued = _monitor(launcher, monkeypatch, capsys, at=0)
    assert queued is not None and _shown(queued) == ("queued", "Waiting for PBS allocation", False)

    launcher.transfer.hold = threading.Event()
    try:
        started = time.monotonic()
        lines, slow = _monitor(launcher, monkeypatch, capsys, at=30)
        elapsed = time.monotonic() - started
        assert elapsed < 3, elapsed  # bounded by timeout_seconds (0.3 s here), not by the listing
        assert lines[0] == "Status: ACTIVE"
        assert slow is not None and _shown(slow) == ("queued", "Waiting for PBS allocation", True)
        assert launcher.clients == [stage.request_timeout(0.3)] * 2  # the HTTP timeout past the bound (5.3 s)
        assert _monitor(launcher, monkeypatch, capsys, at=40)[1] == slow
        assert len(launcher.transfer.calls) == 2
    finally:
        launcher.transfer.hold.set()


def test_the_listing_client_makes_one_request_bounded_by_the_timeout(tmp_path, monkeypatch):
    """The real Transfer client of the listing: no retry, each wait bounded by ``timeout_seconds``, the authorization
    renewed before the listing's thread uses it; the cancel's and the clean-up's client keep the SDK's defaults."""
    from globus_sdk.authorizers import RenewingAuthorizer
    from globus_sdk.scopes import TransferScopes
    from globus_sdk.transport import RetryConfig

    _plan, settings, _config = _planned(tmp_path, "crux-wire")
    authorizer = mock.Mock(spec=RenewingAuthorizer)
    client = mock.Mock()
    client.login_manager.get_manager_authorizers.return_value = {str(TransferScopes.all): authorizer}
    monkeypatch.setattr(stage, "_client", lambda _settings: client)
    listing = stage._transfer_client(settings, timeout=8)
    assert isinstance(listing, globus_sdk.TransferClient) and listing.authorizer is authorizer
    assert listing.transport.http_timeout == 8 and listing.retry_config.max_retries == 0
    authorizer.ensure_valid_token.assert_called_once_with()
    other = stage._transfer_client(settings)
    assert other.retry_config.max_retries == RetryConfig().max_retries
    assert other.transport.http_timeout != 8


class UserLogin(UserAppLoginManager):
    """gladier's login manager of a user identity (the ``personal`` profile) with no Transfer token (``no-token``:
    gladier would log in) or one that does not validate (``invalid-token``: GlobusApp's handler would log in): a
    login prompts on the terminal, here it sets ``prompted`` and waits like one."""

    def __init__(self, case: str):
        BaseLoginManager.__init__(self)  # no GlobusApp, no token storage
        self.case, self.prompted, self.answer = case, threading.Event(), threading.Event()
        self.globus_app = SimpleNamespace(login=self._prompt)

    def _prompt(self, *_args, **_kwargs):
        self.prompted.set()
        self.answer.wait(30)

    def login(self, scopes):
        self._prompt()

    def get_authorizers(self):
        if self.case == "invalid-token":
            self.globus_app.login(auth_params=None)  # GlobusApp.get_authorizer's token validation error handler
        return {}


class SlowRenewal:
    """A login manager whose Transfer token's renewal (Globus Auth) does not answer until ``answer`` is set."""

    def __init__(self, case: str):
        self.prompted, self.answer = threading.Event(), threading.Event()
        self.authorizer = mock.Mock(spec=RenewingAuthorizer)
        self.authorizer.ensure_valid_token.side_effect = lambda: self.answer.wait(30)

    def add_requirements(self, scopes):
        pass

    def get_manager_authorizers(self):
        return {str(TransferScopes.all): self.authorizer}


@pytest.mark.parametrize(
    ("manager", "case", "reason"),
    [
        pytest.param(SlowRenewal, "renewal", "TimeoutError", id="slow-renewal"),
        pytest.param(UserLogin, "no-token", "LoginRefused: no Transfer consent or token", id="no-token"),
        pytest.param(UserLogin, "invalid-token", "LoginRefused: no Transfer consent or token", id="invalid-token"),
    ],
)
def test_the_listing_step_is_bounded_whole_and_never_prompts_for_a_login(
    tmp_path, monkeypatch, capsys, manager, case, reason
):
    """The bound covers the Transfer client's making, its token's renewal included; a login it would need fails at
    once (no interactive flow in a DM stage), logged once."""
    launcher = _launcher(tmp_path, monkeypatch, listing={"timeout_seconds": 0.3})
    monkeypatch.setattr(stage, "_transfer_client", TRANSFER_CLIENT)
    launcher.flow.login_manager = login = manager(case)
    _write(launcher, "pbs.json", 1)
    try:
        started = time.monotonic()
        lines, observation = _monitor(launcher, monkeypatch, capsys, at=0)
        assert time.monotonic() - started < 3
        assert lines[0] == "Status: ACTIVE"
        assert observation is not None and _shown(observation) == ("queued", "Waiting for endpoint", True)
        assert not login.prompted.is_set() and launcher.transfer.calls == []
        assert launcher.err.startswith(f"The results listing on Eagle failed: {reason}")
        _monitor(launcher, monkeypatch, capsys, at=60)  # after the back-off: the same failure, not logged again
        assert launcher.err == "" and not login.prompted.is_set()
    finally:
        login.answer.set()


@pytest.mark.parametrize(
    ("status", "code", "no_folder"),
    [
        pytest.param(404, "ClientError.NotFound", True, id="ClientError.NotFound"),
        pytest.param(404, "NotFound", True, id="NotFound"),
        pytest.param(502, "ExternalError.DirListingFailed.NotFound", True, id="DirListingFailed.NotFound"),
        pytest.param(404, None, True, id="404-without-code"),
        pytest.param(404, "EndpointNotFound", False, id="EndpointNotFound"),
        pytest.param(403, "EndpointPermissionDenied", False, id="EndpointPermissionDenied"),
        pytest.param(502, "ExternalError.DirListingFailed", False, id="DirListingFailed"),
    ],
)
def test_a_missing_results_folder_is_recognised_by_transfers_not_found_forms(
    tmp_path, monkeypatch, capsys, status, code, no_folder
):
    launcher = _launcher(tmp_path, monkeypatch)
    launcher.transfer.error = _transfer_error(status, code, "a message")
    _lines, observation = _monitor(launcher, monkeypatch, capsys, at=0)
    assert observation is not None and _shown(observation) == ("queued", "Waiting for endpoint", not no_folder)
    assert (launcher.err == "") is no_folder
    if not no_folder:
        assert f"TransferAPIError (HTTP {status})" in launcher.err and "a message" not in launcher.err


# ---------------------------------------------------------------------------
# A failed Flow status call: the kept observation, stale, a back-off, and a budget


def _errors(launcher, errors: list[str], *runs: float) -> list[dict[str, Any] | None]:
    """The stage run at each of ``runs`` (seconds after ``NOW``; it never fails): the observations printed, each
    run's stderr lines added to ``errors``."""
    printed = []
    for second in runs:
        lines, observation = _monitor(launcher, launcher.monkeypatch, launcher.capsys, at=second)
        assert lines[0] == "Status: ACTIVE"  # the last status Flows gave: DM's 02-MONITOR repeats
        printed.append(observation)
        errors += launcher.err.splitlines()
    return printed


def _failing(tmp_path, monkeypatch, capsys, **target_changes) -> SimpleNamespace:
    launcher = _launcher(tmp_path, monkeypatch, **target_changes)
    launcher.monkeypatch, launcher.capsys = monkeypatch, capsys
    return launcher


def _failed(failure: str, wait: int, budget: int = 600) -> str:
    return (
        f"The Flow status call failed: {failure}; the observation is stale; the next call in {wait} s, twice as long"
        f" after each failure up to 60 s; the stage fails once the calls have failed for over {budget} s"
    )


def test_a_failed_status_call_prints_the_kept_observation_stale_and_backs_off_until_flows_answers(
    tmp_path, monkeypatch, capsys
):
    """No failed stage; no call (no client even built) until the wait has passed, 10 s after the first failed call's
    run, doubling up to 60 s; one call per stage run at most; the state file keeps the observation as it was and the
    back-off, which an answer resets; stderr has one line per change, never a token."""
    launcher = _failing(tmp_path, monkeypatch, capsys)
    _write(launcher, "pbs.json", 1)
    _write(launcher, "progress.json", 1)
    _lines, fresh = _monitor(launcher, monkeypatch, capsys, at=0)
    assert fresh is not None and _shown(fresh) == ("running", "Processing", False)

    errors: list[str] = []
    launcher.flow.error = _flows_error(503)
    [stale] = _errors(launcher, errors, 10)
    assert stale == {**fresh, "stale": True}  # the kept one, its times unchanged: the job page's "status delayed"
    kept = json.loads(launcher.state.read_text())
    assert kept["observation"] == fresh  # kept as it was, for when Flows answers
    assert kept["status_wait"] == 10 and kept["status_failing_since"] == kept["status_failed_at"]

    # Each wait at its boundary: 10, 20, 40, then 60 s after the failed call's run
    printed = _errors(launcher, errors, 19.9, 20, 39.9, 40, 79.9, 80, 139.9)
    assert printed == [stale] * 7
    assert launcher.flow.calls == [0, 10, 20, 40, 80] and launcher.flow.built == 5  # none while waiting
    assert json.loads(launcher.state.read_text())["status_wait"] == 60

    launcher.flow.error = _network_error()  # another failure: one more line
    _errors(launcher, errors, 140, 199.9)
    launcher.flow.error = _renewal_error()
    _errors(launcher, errors, 200)
    assert launcher.flow.calls[-2:] == [140, 200] and json.loads(launcher.state.read_text())["status_wait"] == 60
    assert len(launcher.transfer.calls) == 1  # nothing listed while Flows does not answer

    launcher.flow.error = None
    _write(launcher, "progress.json", -260)  # rewritten at 260 s
    [recovered, again] = _errors(launcher, errors, 260, 270)
    assert recovered is not None and _shown(recovered) == ("running", "Processing", False)
    assert recovered["observed_at"] == cases.at(260) and recovered["phase_since"] == cases.at(0)
    assert again == recovered and launcher.flow.calls[-2:] == [260, 270]  # each run calls again
    assert not set(stage.STATUS_FAILURE_KEYS) & set(json.loads(launcher.state.read_text()))
    assert errors == [
        _failed("FlowsAPIError (HTTP 503)", 10),
        _failed("GlobusConnectionError", 60),
        _failed("AuthAPIError (HTTP 400)", 60),
        "Flows answers the status call again; the stage calls it on each run",
    ]
    assert TOKEN not in launcher.capsys.readouterr().err + "".join(errors)


def test_with_no_observation_kept_a_failed_status_call_prints_workflow_status_unavailable_stale(
    tmp_path, monkeypatch, capsys
):
    """The first stage runs of a submission while Flows does not answer: the goldens' detail for a failed ``get_status``
    (``workflow-unavailable``), Queued (its phase with none kept), stale, observed at the first failed call, no Flow
    step; the keys of the goldens' Flow-only observation in their order, which the portal's intake keeps; never "Waiting
    for status" (the portal's "At ALCF")."""
    launcher = _failing(tmp_path, monkeypatch, capsys)
    launcher.flow.error = _flows_error(502)
    errors: list[str] = []
    first, waiting = _errors(launcher, errors, 0, 5)
    assert first == {
        "phase": "queued",
        "detail": json.loads(GOLDENS["workflow-unavailable"])["detail"],  # "Workflow status unavailable"
        "observed_at": cases.at(0),
        "stale": True,
        "flow_state": None,
        "phase_since": cases.at(0),
        "submission_id": SUBMISSION_ID,
        "flow_run_id": cases.FLOW_ID,
    }
    assert list(first) == list(json.loads(GOLDENS["at-alcf"]))
    assert waiting == first and launcher.flow.calls == [0]
    text = json.dumps(first, separators=(",", ":"))
    assert accept(text, backend="crux", submission_id=SUBMISSION_ID, stored=None) == first
    assert "observation" not in json.loads(launcher.state.read_text())  # nothing observed is kept


def test_failures_lasting_longer_than_the_budget_fail_the_stage(tmp_path, monkeypatch, capsys):
    """Every 10 s from the first failed call (10 s), the calls at the waits, until the first that fails over 600 s after
    it: it raises ``get_status``'s own error out of ``main`` (``sys.exit(main())``: a traceback and exit 1), prints
    nothing (no ``Status:``, so DM keeps no variable) and leaves the state file as it was; DM then ends the job."""
    launcher = _failing(tmp_path, monkeypatch, capsys)
    _write(launcher, "pbs.json", 1)
    _monitor(launcher, monkeypatch, capsys, at=0)
    error = _flows_error(503)
    launcher.flow.error = error
    errors: list[str] = []
    _errors(launcher, errors, *range(10, 620, 10))
    assert launcher.flow.calls == [0, 10, 20, 40, 80, 140, 200, 260, 320, 380, 440, 500, 560]
    assert errors == [_failed("FlowsAPIError (HTTP 503)", 10)]

    before = launcher.state.read_bytes()
    launcher.clock.now = cases.NOW + timedelta(seconds=620)  # failing for 610 s
    monkeypatch.setattr(sys, "argv", ["stage", "--config", str(launcher.config), "monitor", SUBMISSION_ID])
    with pytest.raises(globus_sdk.FlowsAPIError) as caught:
        stage.main()
    assert caught.value is error
    assert capsys.readouterr() == ("", "")
    assert launcher.state.read_bytes() == before


@pytest.mark.parametrize(("second", "fails"), [(30, False), (30.5, True)])
def test_the_stage_fails_only_once_the_calls_have_failed_for_longer_than_the_budget_set_in_bdp_yaml(
    tmp_path, monkeypatch, capsys, second, fails
):
    launcher = _failing(tmp_path, monkeypatch, capsys, status_failure_budget_seconds=30)
    launcher.flow.error = _flows_error(503)
    errors: list[str] = []
    _errors(launcher, errors, 0, 10)  # failing since 0 s; the next call 20 s after the second
    assert errors == [_failed("FlowsAPIError (HTTP 503)", 10, budget=30)]
    if not fails:  # failing for exactly the budget: not longer
        assert _errors(launcher, errors, second)[0] is not None and launcher.flow.calls == [0, 10, 30]
        return
    launcher.clock.now = cases.NOW + timedelta(seconds=second)
    monkeypatch.setattr(sys, "argv", ["stage", "--config", str(launcher.config), "monitor", SUBMISSION_ID])
    with pytest.raises(globus_sdk.FlowsAPIError):
        stage.main()
    assert launcher.flow.calls == [0, 10, 30.5]


@pytest.mark.parametrize(
    "record",
    [
        pytest.param(_record(None, status="SUCCEEDED"), id="succeeded"),
        pytest.param(_record("RunPbsJob", status="FAILED"), id="failed"),
        pytest.param(_record("RunPbsJob", status="INACTIVE"), id="inactive"),
        pytest.param(_record("InputTransfer"), id="active"),
    ],
)
def test_a_status_flows_answers_after_failures_is_handled_as_without_them(tmp_path, monkeypatch, capsys, record):
    launcher = _failing(tmp_path, monkeypatch, capsys)
    _write(launcher, "pbs.json", 1)
    _monitor(launcher, monkeypatch, capsys, at=0)
    answered = launcher.state.read_text()
    launcher.flow.error = _flows_error(503)
    _monitor(launcher, monkeypatch, capsys, at=10)
    launcher.flow.error, launcher.flow.record = None, record
    after_failures = _monitor(launcher, monkeypatch, capsys, at=40)
    state = json.loads(launcher.state.read_text())
    launcher.state.write_text(answered)  # the same run, its status calls never failed
    assert _monitor(launcher, monkeypatch, capsys, at=40) == after_failures
    assert json.loads(launcher.state.read_text()) == state
    assert after_failures[0][0] == f"Status: {record['status']}"  # DM's repeatUntil reads it, as without failures


def test_a_state_file_without_status_keys_and_the_listings_own_back_off_are_kept_apart_from_the_status_calls(
    tmp_path, monkeypatch, capsys
):
    """A state file an earlier launcher wrote (no status keys; a failing listing's ``listing_wait`` and
    ``listing_failure``): the status calls' failures leave its keys as they are, and the listing keeps its own wait once
    Flows answers again."""
    launcher = _failing(tmp_path, monkeypatch, capsys)
    _write(launcher, "pbs.json", 1)
    _monitor(launcher, monkeypatch, capsys, at=0)
    launcher.transfer.error = _transfer_error(503)
    _monitor(launcher, monkeypatch, capsys, at=30)  # the next listing 60 s after
    written = json.loads(launcher.state.read_text())
    assert written["listing_wait"] == 60 and not set(stage.STATUS_FAILURE_KEYS) & set(written)

    launcher.flow.error = _flows_error(503)
    _lines, printed = _monitor(launcher, monkeypatch, capsys, at=40)
    assert printed == {**written["observation"], "stale": True}
    state = json.loads(launcher.state.read_text())
    assert {key: state[key] for key in written} == written and set(state) - set(written) == set(
        stage.STATUS_FAILURE_KEYS
    )

    launcher.flow.error = launcher.transfer.error = None
    assert _monitor(launcher, monkeypatch, capsys, at=50)[1] == written["observation"]  # no listing before 90 s
    assert len(launcher.transfer.calls) == 2
    _write(launcher, "pbs.json", -90)  # rewritten at 90 s
    _lines, listed = _monitor(launcher, monkeypatch, capsys, at=90)
    assert len(launcher.transfer.calls) == 3 and listed is not None and listed["stale"] is False
    assert not {"listing_wait", "listing_failure"} & set(json.loads(launcher.state.read_text()))


def test_the_first_wait_is_the_workflows_stage_interval():
    stages = json.loads(WORKFLOW.read_text(encoding="utf-8"))["stages"]
    assert stages["02-MONITOR"]["repeatPeriod"] == stage.STATUS_INTERVAL_SECONDS


def _run_document(state: str) -> dict[str, Any]:
    """A Flow's run document as Globus Flows answers ``GET /runs/<id>`` (the part the launcher reads)."""
    return {"run_id": cases.FLOW_ID, "status": "ACTIVE", "details": {"action_statuses": [{"state_name": state}]}}


@contextmanager
def _flows_stand_in(case: str) -> Iterator[SimpleNamespace]:
    """A local stand-in for Globus Flows on 127.0.0.1 (``url``; ``requests``: the paths it was asked for): ``503``
    answers 503, ``no-answer`` holds each request until the stand-in ends, ``answer`` answers the run document of a
    Flow in ``RunPbsJob``; ``refused``: nothing listens on the port."""
    asked: list[str] = []
    release = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            asked.append(self.path)
            if case == "no-answer":
                release.wait(30)
                return
            status, body = (200, _run_document("RunPbsJob")) if case == "answer" else (503, {"code": "Unavailable"})
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    if case == "refused":
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        yield SimpleNamespace(url=f"http://127.0.0.1:{port}/", requests=asked)
        return
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}/", requests=asked)
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize(
    ("case", "failure"),
    [
        pytest.param("503", "FlowsAPIError (HTTP 503)", id="503"),
        pytest.param("refused", "GlobusConnectionError", id="refused-port"),
        pytest.param("no-answer", "TimeoutError", id="no-answer"),
        pytest.param("answer", None, id="answer"),
    ],
)
def test_a_status_call_is_one_request_bounded_in_time(tmp_path, monkeypatch, capsys, case, failure):
    """The launcher's real Flows client for the status call, against a local stand-in for Flows: one request and no
    retry (globus-sdk's default transport would make six), the whole step bounded by ``STATUS_TIMEOUT_SECONDS`` (0.5 s
    here); a failed call is a stale pass, an answer is read as gladier reads it."""
    launcher = _failing(tmp_path, monkeypatch, capsys)
    monkeypatch.setattr(stage, "_flows_client", FLOWS_CLIENT)
    monkeypatch.setattr(stage, "STATUS_TIMEOUT_SECONDS", 0.5)
    launcher.flow.login_manager = SimpleNamespace(
        get_manager_authorizers=lambda: {str(FlowsScopes.manage_flows): globus_sdk.NullAuthorizer()}
    )
    sent: list[str] = []
    send = requests.Session.send

    def counted(session, request, **kwargs):
        sent.append(request.url)
        return send(session, request, **kwargs)

    monkeypatch.setattr(requests.Session, "send", counted)
    with _flows_stand_in(case) as flows:
        monkeypatch.setenv("GLOBUS_SDK_SERVICE_URL_FLOWS", flows.url)
        started = time.monotonic()
        lines, observation = _monitor(launcher, monkeypatch, capsys, at=0)
        elapsed = time.monotonic() - started
    assert elapsed < 3, elapsed
    assert sent == [f"{flows.url}runs/{cases.FLOW_ID}"]
    assert len(flows.requests) == (0 if case == "refused" else 1)
    assert observation is not None
    if failure is None:
        assert lines[0] == "Status: ACTIVE" and _shown(observation) == ("queued", "Waiting for endpoint", False)
        assert launcher.err == ""
        return
    assert _shown(observation) == ("queued", "Workflow status unavailable", True)
    assert launcher.err.startswith(f"The Flow status call failed: {failure}")


LATE_SECONDS = 1.0
"""How late the waiting thread's check comes in the test below (a garbage collection or a loaded host, enlarged)."""


@pytest.mark.parametrize(
    ("step", "service", "failed"),
    [
        pytest.param("flow-status", "FLOWS", "The Flow status call failed", id="flow-status"),
        pytest.param("results-listing", "TRANSFER", "The results listing on Eagle failed", id="results-listing"),
    ],
)
def test_with_no_answer_a_bounded_step_s_error_is_its_bound_s_however_late_its_wait_ends(
    tmp_path, monkeypatch, capsys, step, service, failed
):
    """The Flow status call's and the Eagle listing's real clients against a stand-in that never answers. Their HTTP
    timeout lies past the step's bound (``stage.request_timeout``), so the error is the bound's ``TimeoutError`` even
    when the waiting thread checks the step 1 s late, never the client's own ``GlobusTimeoutError`` (equal timeouts
    would race); still one request."""
    launcher = _failing(tmp_path, monkeypatch, capsys, listing={"timeout_seconds": 0.3})
    monkeypatch.setattr(stage, "STATUS_TIMEOUT_SECONDS", 0.3)
    null = globus_sdk.NullAuthorizer()
    launcher.flow.login_manager = SimpleNamespace(
        add_requirements=lambda _scopes: None,
        get_manager_authorizers=lambda: {str(FlowsScopes.manage_flows): null, str(TransferScopes.all): null},
    )
    if step == "flow-status":
        monkeypatch.setattr(stage, "_flows_client", FLOWS_CLIENT)
    else:
        monkeypatch.setattr(stage, "_transfer_client", TRANSFER_CLIENT)
    join = threading.Thread.join

    def late(thread: threading.Thread, timeout: float | None = None) -> None:
        join(thread, timeout)
        if thread.name == step:
            time.sleep(LATE_SECONDS)

    monkeypatch.setattr(threading.Thread, "join", late)
    with _flows_stand_in("no-answer") as stand_in:
        monkeypatch.setenv(f"GLOBUS_SDK_SERVICE_URL_{service}", stand_in.url)
        _lines, observation = _monitor(launcher, monkeypatch, capsys, at=0)
        asked = len(stand_in.requests)
    assert observation is not None and observation["stale"] is True
    assert asked == 1
    assert launcher.err.startswith(f"{failed}: TimeoutError"), launcher.err


def test_the_status_calls_flows_client_is_one_request_bounded_by_the_timeout():
    """The Flows client the status call makes: gladier's authorization for the scope its ``get_status`` uses (a
    ``Scope`` key here), renewed before the request (in the bounded step), each wait bounded, no retry."""
    authorizer = mock.Mock(spec=RenewingAuthorizer)
    client = SimpleNamespace(
        login_manager=SimpleNamespace(get_manager_authorizers=lambda: {FlowsScopes.manage_flows: authorizer})
    )
    flows = FLOWS_CLIENT(client, timeout=10)
    assert isinstance(flows, globus_sdk.FlowsClient) and flows.authorizer is authorizer
    assert flows.transport.http_timeout == 10 and flows.retry_config.max_retries == 0
    authorizer.ensure_valid_token.assert_called_once_with()
    assert stage.STATUS_TIMEOUT_SECONDS == 10


class FlowsRenewal:
    """A login manager whose Flows token's renewal (Globus Auth) fails on the network (``renewal-fails``) or does
    not answer until ``answer`` is set (``renewal-hangs``)."""

    def __init__(self, case: str):
        self.prompted, self.answer = threading.Event(), threading.Event()
        self.authorizer = mock.Mock(spec=RenewingAuthorizer)
        self.authorizer.ensure_valid_token.side_effect = (
            _network_error() if case == "renewal-fails" else lambda: self.answer.wait(30)
        )

    def get_manager_authorizers(self):
        return {str(FlowsScopes.manage_flows): self.authorizer}


@pytest.mark.parametrize(
    ("case", "failure"),
    [
        pytest.param("renewal-fails", "GlobusConnectionError", id="renewal-fails"),
        pytest.param("renewal-hangs", "TimeoutError", id="renewal-hangs"),
    ],
)
def test_a_token_renewal_that_fails_or_hangs_is_a_failed_call(tmp_path, monkeypatch, capsys, case, failure):
    """The authorization's renewal runs inside the bounded step and the retry: tolerated like a failed call (stale, the
    next call 10 s later), never a login."""
    launcher = _failing(tmp_path, monkeypatch, capsys)
    monkeypatch.setattr(stage, "_flows_client", FLOWS_CLIENT)
    monkeypatch.setattr(stage, "STATUS_TIMEOUT_SECONDS", 0.3)
    launcher.flow.login_manager = login = FlowsRenewal(case)
    try:
        started = time.monotonic()
        _lines, observation = _monitor(launcher, monkeypatch, capsys, at=0)
        assert time.monotonic() - started < 3
    finally:
        login.answer.set()
    assert observation is not None and _shown(observation) == ("queued", "Workflow status unavailable", True)
    assert launcher.err.startswith(f"The Flow status call failed: {failure}")
    assert json.loads(launcher.state.read_text())["status_wait"] == 10 and not login.prompted.is_set()


@pytest.mark.parametrize("case", ["no-token", "invalid-token", "no-client"])
def test_a_login_the_call_would_need_or_a_client_that_cannot_be_built_fails_the_stage_at_once(
    tmp_path, monkeypatch, capsys, case
):
    """Not transient, so not retried for the budget: a login (the launcher's identity has no Flows consent or token;
    never an interactive one in a DM stage) raises :class:`stage.LoginRefused`, and an error building the launcher's
    client (its settings, the credentials file) raises; nothing printed, the state file as it was."""
    launcher = _failing(tmp_path, monkeypatch, capsys)
    monkeypatch.setattr(stage, "_flows_client", FLOWS_CLIENT)
    monkeypatch.setattr(stage, "STATUS_TIMEOUT_SECONDS", 3)
    login = UserLogin(case)
    error = ValueError("credentials_file is required by the beamline authentication profile")
    if case == "no-client":
        monkeypatch.setattr(stage, "_client", mock.Mock(side_effect=error))
    else:
        launcher.flow.login_manager = login
    before = launcher.state.read_bytes()
    monkeypatch.setattr(sys, "argv", ["stage", "--config", str(launcher.config), "monitor", SUBMISSION_ID])
    started = time.monotonic()
    try:
        with pytest.raises((ValueError, stage.LoginRefused)) as caught:
            stage.main()
    finally:
        login.answer.set()
    assert time.monotonic() - started < 2
    if case == "no-client":
        assert caught.value is error
    else:
        assert isinstance(caught.value, stage.LoginRefused) and "no Flows consent or token" in str(caught.value)
    assert not login.prompted.is_set()
    assert capsys.readouterr() == ("", "") and launcher.state.read_bytes() == before


def test_a_clock_stepped_back_during_failures_calls_at_once_and_counts_the_budget_from_now(
    tmp_path, monkeypatch, capsys
):
    """After the clock steps back, the last failed call's time is in the future: the next run calls at once (no wait),
    and the first failure's time, in the future too, is taken as now and saved, so the budget is not lengthened by the
    step."""
    launcher = _failing(tmp_path, monkeypatch, capsys)
    launcher.flow.error = _flows_error(503)
    errors: list[str] = []
    _errors(launcher, errors, 0, 10)  # failing since 0 s, the next call 20 s after 10 s
    _errors(launcher, errors, -3000)  # the clock stepped back
    assert launcher.flow.calls == [0, 10, -3000]
    since = json.loads(launcher.state.read_text())["status_failing_since"]
    assert since == (cases.NOW + timedelta(seconds=-3000)).isoformat()
    _errors(launcher, errors, -2400)  # failing for 600 s since the step: not longer than the budget
    launcher.clock.now = cases.NOW + timedelta(seconds=-2339.5)  # 660.5 s
    monkeypatch.setattr(sys, "argv", ["stage", "--config", str(launcher.config), "monitor", SUBMISSION_ID])
    with pytest.raises(globus_sdk.FlowsAPIError):
        stage.main()
    assert launcher.flow.calls == [0, 10, -3000, -2400, -2339.5]


def test_a_clock_stepped_forward_during_failures_counts_the_step_toward_the_budget(tmp_path, monkeypatch, capsys):
    """The documented limit: the waits and the budget are the clock's; a step forward during failures larger than what
    is left of the budget fails the stage at the next call."""
    launcher = _failing(tmp_path, monkeypatch, capsys)
    launcher.flow.error = _flows_error(503)
    _errors(launcher, [], 0)
    launcher.clock.now = cases.NOW + timedelta(seconds=10 + 900)  # 10 s later, the clock 15 min ahead
    monkeypatch.setattr(sys, "argv", ["stage", "--config", str(launcher.config), "monitor", SUBMISSION_ID])
    with pytest.raises(globus_sdk.FlowsAPIError):
        stage.main()
    assert launcher.flow.calls == [0, 910]


@pytest.mark.parametrize(
    ("kept", "wait"),
    [pytest.param(3600, 60, id="over-60"), pytest.param(1, 10, id="under-10"), pytest.param("x", 10, id="text")],
)
def test_a_kept_wait_out_of_bounds_is_taken_between_10_and_60_seconds(tmp_path, monkeypatch, capsys, kept, wait):
    """A ``status_wait`` the state file holds outside 10-60 s (an older or edited file)."""
    launcher = _failing(tmp_path, monkeypatch, capsys)
    state = json.loads(launcher.state.read_text())
    at = (cases.NOW).isoformat()
    state.update(status_failure="FlowsAPIError (HTTP 503)", status_wait=kept, status_failed_at=at)
    state["status_failing_since"] = at
    launcher.state.write_text(json.dumps(state))
    _errors(launcher, [], wait - 0.1)
    assert launcher.flow.calls == []
    _errors(launcher, [], wait)
    assert launcher.flow.calls == [wait]


def test_every_state_file_the_stage_writes_is_the_accounts_own_whatever_the_umask(tmp_path, monkeypatch, capsys):
    """``main`` sets the umask before any write, so a pass within the wait (no client built) and ``aps``'s ``submit``
    write the state file 0600 under DM's 022 too; the caller's umask is back after."""
    launcher = _failing(tmp_path, monkeypatch, capsys)
    launcher.flow.error = _flows_error(503)
    previous = os.umask(0o022)
    try:
        _errors(launcher, [], 0)
        launcher.state.chmod(0o644)  # as a write under 022 left it
        _errors(launcher, [], 5)  # a pass within the wait: no client built
        assert launcher.flow.built == 1 and stat.S_IMODE(launcher.state.stat().st_mode) == 0o600
        plan, settings, config = _planned(tmp_path / "aps", "aps-wire")
        monkeypatch.setattr(stage.subprocess, "run", FakePBS())
        monkeypatch.setattr(sys, "argv", ["stage", "--config", str(config), "submit", SUBMISSION_ID])
        assert stage.main() == 0
        aps_state = Path(settings.state_directory) / f"{SUBMISSION_ID}.json"
        assert stat.S_IMODE(aps_state.stat().st_mode) == 0o600
        assert os.umask(0o022) == 0o022  # main gave the caller's back
    finally:
        os.umask(previous)


# ---------------------------------------------------------------------------
# Login needed: the Flow waits for a Globus sign-in


LOGIN_GOLDENS = ("login-required", "login-required-on-the-run", "login-resumed", "inactive-without-login")
"""``cases.py``'s Login needed cases, each printed by a launcher that never lists."""


@pytest.mark.parametrize("name", LOGIN_GOLDENS)
def test_the_login_needed_cases_print_exactly_the_goldens_line(tmp_path, monkeypatch, capsys, name):
    """The Flow's run document and the kept observation of each case, as ``cases.py`` gives them: the launcher prints
    the goldens' line (the submission's ID apart), and the portal's intake keeps it."""
    case = cases.LAUNCHER_CASES[name]
    launcher = _launcher(tmp_path, monkeypatch)
    state = json.loads(launcher.state.read_text())
    if case["previous"] is not None:
        state["observation"] = case["previous"]
    launcher.state.write_text(json.dumps(state))
    launcher.flow.record = case["flow"]
    lines, _observation = _monitor(launcher, monkeypatch, capsys, at=0)
    printed = next(line.removeprefix("Observation: ") for line in lines if line.startswith("Observation: "))
    assert printed == GOLDENS[name].replace(cases.SUBMISSION_ID, SUBMISSION_ID)
    assert launcher.transfer.calls == []
    assert accept(printed, backend="crux", submission_id=SUBMISSION_ID, stored=case["previous"]) is not None


def _paused_in_pbs() -> dict[str, Any]:
    """A Flow waiting for a sign-in in ``RunPbsJob`` (``cases.CHALLENGE`` on its action)."""
    return cases.LAUNCHER_CASES["login-required"]["flow"]


def test_a_flow_waiting_for_a_sign_in_is_login_needed_unlisted_and_keeps_no_challenge(tmp_path, monkeypatch, capsys):
    """Detected before the kept observation would stand (10 s after a listing), never listed, its ``phase_since``
    kept while it waits; only the flag is kept: the challenge reaches neither the output nor the state file."""
    launcher = _launcher(tmp_path, monkeypatch)
    _write(launcher, "progress.json", 1)
    _lines, listed = _monitor(launcher, monkeypatch, capsys, at=0)
    assert listed is not None and _shown(listed) == ("running", "Processing", False)
    launcher.flow.record = _paused_in_pbs()
    shown = []
    for second in (10, 40, 70):
        lines, observation = _monitor(launcher, monkeypatch, capsys, at=second)
        assert lines[0] == "Status: INACTIVE" and observation is not None
        shown.append((observation["phase"], observation["detail"], "stale" in observation, observation["phase_since"]))
        output = "\n".join(lines) + launcher.err + launcher.state.read_text()
        assert cases.CHALLENGE_TEXT not in output and "authorization_parameters" not in output
    assert shown == [("login_required", "Sign in to Globus and resume this run", False, cases.at(10))] * 3
    assert len(launcher.transfer.calls) == 1  # the listing at 0 s alone


def test_a_resumed_run_does_not_inherit_the_login_pause_and_is_listed_at_once(tmp_path, monkeypatch, capsys):
    """Once the Flow no longer waits for a sign-in, the kept Login needed is no previous observation: in
    ``RunPbsJob`` the run is listed at once (here 15 s after the last listing), its phase since then; a Flow that
    ended in ``RunPbsJob`` meanwhile prints none."""
    launcher = _launcher(tmp_path, monkeypatch)
    _write(launcher, "pbs.json", 1)
    _monitor(launcher, monkeypatch, capsys, at=0)
    launcher.flow.record = _paused_in_pbs()
    _monitor(launcher, monkeypatch, capsys, at=5)
    launcher.flow.record = _record("RunPbsJob")
    _lines, resumed = _monitor(launcher, monkeypatch, capsys, at=15)
    assert resumed is not None and _shown(resumed) == ("queued", "Waiting for PBS allocation", False)
    assert resumed["phase_since"] == cases.at(15) and len(launcher.transfer.calls) == 2

    launcher.flow.record = _paused_in_pbs()
    _monitor(launcher, monkeypatch, capsys, at=20)
    launcher.flow.record = _record("RunPbsJob", status="FAILED")
    lines, ended = _monitor(launcher, monkeypatch, capsys, at=25)
    assert lines[0] == "Status: FAILED" and ended is None  # no Observation line
    assert "observation" not in json.loads(launcher.state.read_text())


def test_a_kept_login_needed_is_printed_stale_while_flows_does_not_answer(tmp_path, monkeypatch, capsys):
    """When ``get_status`` fails, the phase of the observation held is kept, and its detail too, as for every phase
    (``unanswered_observation``)."""
    launcher = _launcher(tmp_path, monkeypatch)
    launcher.flow.record = _paused_in_pbs()
    _lines, paused = _monitor(launcher, monkeypatch, capsys, at=0)
    launcher.flow.error = _flows_error(503)
    _lines, unanswered = _monitor(launcher, monkeypatch, capsys, at=10)
    assert paused is not None and unanswered == {**paused, "stale": True}


# ---------------------------------------------------------------------------
# The printed line: the goldens' format, and what the portal keeps of it through DM's regex


def test_the_printed_observations_have_the_goldens_format(tmp_path, monkeypatch, capsys):
    """The Flow's steps print the goldens' text (the submission's ID apart); a listing's observation has the keys of the
    goldens' Flow-only one in their order, never its detail "Waiting for status" (the portal's "At ALCF"), and the
    portal's intake (``remote.observation.accept``) keeps each."""
    launcher = _launcher(tmp_path, monkeypatch)
    submitted = launcher.state.read_text()

    def golden(name: str) -> str:
        return GOLDENS[name].replace(cases.SUBMISSION_ID, SUBMISSION_ID)

    def printed(record) -> str:
        launcher.flow.record = record
        lines, _observation = _monitor(launcher, monkeypatch, capsys, at=0)
        launcher.state.write_text(submitted)  # no observation kept
        return next(line.removeprefix("Observation: ") for line in lines if line.startswith("Observation: "))

    assert printed(_record("InputTransfer")) == golden("uplink")
    assert printed(_record("ResultTransfer")) == golden("downlink")
    assert printed(_record(None, status="SUCCEEDED")) == golden("flow-succeeded")

    flow_only = list(json.loads(GOLDENS["at-alcf"]))
    for files in ({}, {"pbs.json": 1}, {"progress.json": 1}, {"run.json": 1, "progress.json": 1}, {"pbs.json": 100}):
        shutil.rmtree(launcher.results, ignore_errors=True)  # each case's files only
        for name, age in files.items():
            _write(launcher, name, age)
        text = printed(_record("RunPbsJob"))
        observation = json.loads(text)
        assert list(observation) == flow_only and text == json.dumps(observation, separators=(",", ":"))
        assert observation["detail"] != "Waiting for status"
        assert observation["stale"] is (files == {"pbs.json": 100})
        assert accept(text, backend="crux", submission_id=SUBMISSION_ID, stored=None) == observation


@contextmanager
def _portal_and_launcher(tmp_path, monkeypatch, capsys) -> Iterator[SimpleNamespace]:
    """The portal with a ``crux`` run submitted to a fake DM, and the launcher on the plan's DM inbox copy (a
    :class:`FakeFlow`, the run in ``RunPbsJob`` with ``pbs.json`` on the Eagle stand-in, written 2 s before ``NOW``; the
    launcher's clock ``clock.now``): ``stage(action)`` runs a launcher stage and returns its output; ``keep(output)`` is
    DM keeping ``02-MONITOR``'s output by its ``outputVariableRegexList`` (emulated: each regex's first match, Python's
    ``re``), then the poller's pass: the variables, and ``job.remote_observation``."""
    layout = Layout(tmp_path)
    engine = make_engine(tmp_path / "portal")
    try:
        values = bdp_settings(layout)
        settings = remote_settings(values)
        run = create_wire_run(engine, layout, count=2, backend="crux")
        dm = FakeDM()
        submit_run(run.job_id, engine=engine, settings=settings, client=dm)
        plan = dm.submissions[0]
        config = tmp_path / "launcher.yaml"
        config.write_text(yaml.safe_dump(values), encoding="utf-8")

        clock = SimpleNamespace(now=cases.NOW)
        flow = FakeFlow(clock)
        transfer = FakeListing(settings)
        monkeypatch.setattr(stage, "_client", flow.client)
        monkeypatch.setattr(stage, "_flows_client", flow.flows_client)
        monkeypatch.setattr(stage, "_transfer_client", lambda _settings, *, timeout=None: transfer)
        monkeypatch.setattr(stage, "_now", lambda: clock.now)
        results = Path(plan["output_directory"])
        results.mkdir(parents=True)
        (results / "pbs.json").write_text("{}", encoding="utf-8")
        stamp = cases.NOW.timestamp() - 2
        os.utime(results / "pbs.json", (stamp, stamp))

        def run_stage(action: str) -> str:
            monkeypatch.setattr(sys, "argv", ["stage", "--config", str(config), action, plan["submission_id"]])
            assert stage.main() == 0
            return capsys.readouterr().out

        def keep(output: str) -> tuple[dict[str, str], Any]:
            stages = json.loads(WORKFLOW.read_text(encoding="utf-8"))["stages"]
            variables: dict[str, str] = {}
            for pattern in stages["02-MONITOR"]["outputVariableRegexList"]:
                match = re.search(pattern, output)
                if match:
                    variables.update(match.groupdict())
            dm.status("running", remoteObservation=variables["remoteObservation"])
            poll_once(engine=engine, settings=settings, client=dm)
            return variables, load_job(engine, run.job_id).remote_observation

        yield SimpleNamespace(flow=flow, clock=clock, stage=run_stage, keep=keep)
    finally:
        engine.dispose()


def test_the_portals_poller_keeps_what_the_launcher_prints_through_the_workflows_regex(tmp_path, monkeypatch, capsys):
    """End to end: the portal submits a ``crux`` run, the launcher runs its ``submit`` and ``monitor`` stages, and the
    poller's pass keeps the ``remoteObservation`` DM keeps in ``job.remote_observation``."""
    with _portal_and_launcher(tmp_path, monkeypatch, capsys) as portal:
        portal.stage("submit")
        variables, kept = portal.keep(portal.stage("monitor"))
        assert variables["remoteStatus"] == "ACTIVE"
        printed = json.loads(variables["remoteObservation"])
        assert _shown(printed) == ("queued", "Waiting for PBS allocation", False)
        assert kept == printed


def test_the_portals_poller_keeps_the_stale_observation_of_a_failed_status_call_through_the_workflows_regex(
    tmp_path, monkeypatch, capsys
):
    """End to end: the first monitor run's status call fails (none kept: Queued "Workflow status unavailable", stale),
    Flows answers 10 s later (fresh), fails again 10 s after that (the kept one, stale): the poller keeps each, and DM's
    ``remoteStatus`` stays one ``02-MONITOR`` repeats on."""
    with _portal_and_launcher(tmp_path, monkeypatch, capsys) as portal:
        portal.stage("submit")
        shown = []
        for second, error in ((0, _flows_error(503)), (10, None), (20, _network_error())):
            portal.clock.now = cases.NOW + timedelta(seconds=second)
            portal.flow.error = error
            variables, kept = portal.keep(portal.stage("monitor"))
            assert kept == json.loads(variables["remoteObservation"])
            shown.append((variables["remoteStatus"], *_shown(kept)))
        assert shown == [  # remoteStatus: submit's, the Flow's answer, then that answer kept
            ("RUNNING", "queued", "Workflow status unavailable", True),
            ("ACTIVE", "queued", "Waiting for PBS allocation", False),
            ("ACTIVE", "queued", "Waiting for PBS allocation", True),
        ]
        assert portal.flow.calls == [0, 10, 20]
