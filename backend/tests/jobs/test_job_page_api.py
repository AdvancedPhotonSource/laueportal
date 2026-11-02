"""The job page's API: ``GET /api/v1/jobs/{job_id}``, ``.../failures`` and ``.../log``.

On real run directories: a local run the executor ran end to end (a test compute), a remote run submitted,
reconciled and published by the remote poller against a fake DM (``remote.poller``, as ``tests/remote/
test_poller.py`` drives it), a remote run whose return was refused, and a run migrated from v1's per-input
records (its failure report and ``history.json`` in v1's migration export, its run directory not on this host).
Every run file the endpoints read reports its state in the response: a missing, misplaced or malformed file is a
200 that says so, never a 500, and only an unknown job is a 404.
"""

from __future__ import annotations

import json
import os
import platform
import threading
import tracemalloc
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from laueportal.app import create_app
from laueportal.core.errors import PROBLEM_JSON
from laueportal.core.timeutil import isoformat_z, utcnow
from laueportal.jobs import contract, service, state, summary
from laueportal.jobs import executor as executors
from laueportal.jobs.contract import RunOutcome
from laueportal.jobs.models import Job
from laueportal.jobs.rundir import FailureRecord, write_json_file
from laueportal.jobs.state import JobStatus, RunPhase
from laueportal.reconstruction.models import ReconstructionRun
from laueportal.remote import submission
from laueportal.remote.paths import map_path
from laueportal.remote.poller import poll_once
from tests.conftest import create_test_metadata
from tests.fixtures.observation_goldens import cases
from tests.remote_support import BatchedFake, Layout, bdp_settings, create_wire_run, remote_settings
from tests.run_lists_support import parse_z, problem_items
from tests.run_support import FAST_POLICY, claimed, enqueue_with, load_job, publish_wire_run
from tests.settings_support import make_database

JOBS = "/api/v1/jobs"
WIRE_KIND = "wire_reconstruction"
GLOBUS_RUN = "https://app.globus.org/runs/flow-1"
STALE_AFTER = 300  # execution.stale_heartbeat_seconds: the test settings keep the default


@pytest.fixture
def api(tmp_path: Path) -> Iterator[tuple[Engine, TestClient]]:
    """A fresh database, and the API on it."""
    settings, engine = make_database(tmp_path)
    assert settings.execution.stale_heartbeat_seconds == STALE_AFTER
    with TestClient(create_app(settings)) as client:
        yield engine, client
    engine.dispose()


def _get(client: TestClient, path: str, **params: Any) -> dict[str, Any]:
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def _file(name: str, path: Path | str | None, state: str, detail: str | None = None) -> dict[str, Any]:
    return {"name": name, "path": None if path is None else str(path), "state": state, "detail": detail}


# ---------------------------------------------------------------------------
# A local run, executed end to end


def _run_locally(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    count: int = 3,
    fail: frozenset[int] = frozenset({1}),
    log_lines: int = 60,
    name: str = "local",
) -> ReconstructionRun:
    """A wire run of ``count`` inputs that the executor runs with a test compute: the inputs at the positions in
    ``fail`` fail, the others succeed; the compute logs ``log_lines`` messages and reports a reconstruction
    artifact, a warning and its provenance."""
    run = publish_wire_run(engine, tmp_path, count=count, name=name)
    claimed(engine, run.job_id)
    catalog = os.path.join(run.output_path, "reconstruction", "scan.h5")

    def compute(request: Any, entries: Any, hooks: Any) -> RunOutcome:
        succeeded = failed = 0
        for entry in entries:
            if entry.index in fail:
                failed += 1
                hooks.record_failure(FailureRecord.from_error(entry, ValueError(f"bad frame {entry.input_id}")))
            else:
                succeeded += 1
            hooks.report_progress(succeeded=succeeded, failed=failed)
        for index in range(log_lines):
            hooks.log(f"step {index}")
        return RunOutcome(
            n_succeeded=succeeded,
            n_failed=failed,
            artifacts={"reconstruction": catalog},
            warnings=["the catalog has no geometry text"],
            provenance={"engine": "test", "lauelab_version": "0.9"},
            summary="test compute",
        )

    monkeypatch.setitem(contract._REGISTRY, WIRE_KIND, compute)
    executors.execute_run(run.job_id, engine=engine, policy=FAST_POLICY)
    return run


def test_a_local_runs_page_has_its_job_row_progress_and_configuration(
    api: tuple[Engine, TestClient], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, client = api
    run = _run_locally(engine, tmp_path, monkeypatch)
    job = load_job(engine, run.job_id)
    run_directory = Path(run.output_path)
    request = json.loads((run_directory / "request.json").read_text())
    run_summary = json.loads((run_directory / "run.json").read_text())
    assert job.start_time is not None and job.finish_time is not None

    before = utcnow()
    body = _get(client, f"{JOBS}/{run.job_id}")
    as_of = parse_z(body["as_of"])
    assert before <= as_of <= utcnow()
    progress = body.pop("progress")
    configuration = body.pop("configuration")
    assert body == {
        "job_id": run.job_id,
        "run": {"kind": "reconstruction", "id": run.id, "label": f"R{run.id}"},
        "scan_number": 1,
        "status": "failed",
        "phase": "failed",
        "computer_name": "test-host",
        "submit_time": "2026-09-16T10:00:00Z",
        "start_time": isoformat_z(job.start_time),
        "finish_time": isoformat_z(job.finish_time),
        "duration_seconds": round((job.finish_time - job.start_time).total_seconds(), 3),
        "messages": job.messages,
        "remote": None,
        "held": False,
        "live": None,
        "as_of": body["as_of"],
    }
    assert job.messages is not None and job.messages.splitlines()[-1].startswith("Incomplete: 2 succeeded, 1 failed")
    heartbeat_age = progress.pop("heartbeat_age_seconds")
    assert job.heartbeat_at is not None
    assert heartbeat_age == pytest.approx((as_of - job.heartbeat_at).total_seconds(), abs=0.002)
    assert progress == {
        "n_inputs": 3,
        "n_processed": 3,
        "n_succeeded": 2,
        "n_failed": 1,
        "n_not_run": 0,
        "n_pending": 0,
        "stale_after_seconds": STALE_AFTER,
        "stale": False,  # it has ended
        "stop_requested": False,
        "stop_requested_at": None,
    }

    parameters = {key: value for key, value in request["request"].items() if key not in summary.NOT_CONFIGURATION_KEYS}
    assert parameters["geometry_file"] == "/config/geometry.xml" and parameters["notes"] is None
    assert not {"submitted_at", "computer_name", "priority"} & set(parameters)
    assert configuration == {
        "kind": WIRE_KIND,
        "run_directory": {"path": str(run_directory), "state": "available", "detail": None},
        "files": [
            _file("request.json", run_directory / "request.json", "available"),
            _file("run.json", run_directory / "run.json", "available"),
        ],
        "problems": [],
        "validation_error": None,
        "warnings": ["the catalog has no geometry text"],
        "partial_results": True,  # failed, but its catalog holds the completed points
        "request": {
            "parameters": parameters,
            "output": request["output"],
            "manifest": {"path": "inputs.jsonl", "n_inputs": 3, "sha256": job.manifest_digest},
        },
        "execution": {
            "provenance": {"engine": "test", "lauelab_version": "0.9"},
            "policy": run_summary["policy"],
            "host": platform.node(),
            "started_at": run_summary["started_at"],  # the run directory's text: naive local time
            "finished_at": run_summary["finished_at"],
            "n_indexed": None,
        },
        "history": None,
        "artifacts": [
            {
                "name": "reconstruction",
                "path": str(run_directory / "reconstruction" / "scan.h5"),
                "viewer": "reconstruction",
            }
        ],
    }
    assert list(configuration["request"]["parameters"]) == list(parameters)  # submission order


def test_a_local_runs_failed_inputs_and_log_tail(
    api: tuple[Engine, TestClient], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, client = api
    run = _run_locally(engine, tmp_path, monkeypatch)
    run_directory = Path(run.output_path)

    page = _get(client, f"{JOBS}/{run.job_id}/failures")
    [row] = page.pop("rows")
    recorded_at = row.pop("recorded_at")
    assert datetime.fromisoformat(recorded_at).tzinfo is None  # the run directory's text, as written
    assert row == {
        "index": 1,
        "input_id": "local_2",
        "source": str(tmp_path / "local-input" / "local_2.h5"),  # a local run's paths are the portal's
        "category": "input",
        "error_type": "ValueError",
        "message": "bad frame local_2",
    }
    assert page == {
        "total": 1,
        "has_more": False,
        "report": _file("failures.jsonl", run_directory / "failures.jsonl", "available"),
    }

    log = _get(client, f"{JOBS}/{run.job_id}/log")
    assert log == {
        "job_id": run.job_id,
        "run_log": {
            "lines": [f"step {index}" for index in range(10, 60)],  # the last 50 lines
            "truncated": True,
            "n_lines": 60,
            "file": _file("run.json", run_directory / "run.json", "available"),
        },
        "pbs_output": None,  # a local run has none
    }
    assert _get(client, f"{JOBS}/{run.job_id}/log", tail=1)["run_log"]["lines"] == ["step 59"]
    whole = _get(client, f"{JOBS}/{run.job_id}/log", tail=1000)["run_log"]
    assert (len(whole["lines"]), whole["truncated"], whole["n_lines"]) == (60, False, 60)


def test_the_failure_report_is_paged_on_the_server(
    api: tuple[Engine, TestClient], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, client = api
    run = _run_locally(engine, tmp_path, monkeypatch, count=120, fail=frozenset(range(5, 120)), log_lines=0)
    url = f"{JOBS}/{run.job_id}/failures"

    def page(**params: Any) -> tuple[list[int], int, bool]:
        body = _get(client, url, **params)
        assert body["report"]["state"] == "available"
        return [row["index"] for row in body["rows"]], body["total"], body["has_more"]

    assert page() == (list(range(5, 55)), 115, True)  # 50 per page by default
    assert page(offset=50) == (list(range(55, 105)), 115, True)
    assert page(offset=100) == (list(range(105, 120)), 115, False)
    assert page(offset=115) == ([], 115, False)  # past the end: an empty page
    assert page(offset=10, limit=3) == ([15, 16, 17], 115, True)
    assert page(limit=200) == (list(range(5, 120)), 115, False)  # at most 200 per page
    assert _get(client, f"{JOBS}/{run.job_id}/log")["run_log"] == {
        "lines": [],
        "truncated": False,
        "n_lines": 0,
        "file": _file("run.json", Path(run.output_path) / "run.json", "available"),
    }


def test_a_running_local_run_reports_a_stale_heartbeat_and_a_stop_request(
    api: tuple[Engine, TestClient], tmp_path: Path
) -> None:
    engine, client = api
    run = publish_wire_run(engine, tmp_path, name="silent")
    enqueue_with(engine, run.job_id)
    started = utcnow() - timedelta(hours=1)
    with Session(engine) as session, session.begin():
        job = session.get(Job, run.job_id)
        assert job is not None
        state.mark_running(job, now=started)
        state.record_progress(job, succeeded=1, failed=0, now=started)
        state.request_cancellation(job, reason="user", now=started + timedelta(minutes=1))

    body = _get(client, f"{JOBS}/{run.job_id}")
    assert (body["status"], body["phase"], body["finish_time"]) == ("running", "running", None)
    assert body["duration_seconds"] == pytest.approx((parse_z(body["as_of"]) - started).total_seconds(), abs=0.002)
    progress = body["progress"]
    assert progress["heartbeat_age_seconds"] == pytest.approx(3600, abs=60)
    assert {key: progress[key] for key in ("n_processed", "n_pending", "stale_after_seconds", "stale")} == {
        "n_processed": 1,
        "n_pending": 2,
        "stale_after_seconds": STALE_AFTER,
        "stale": True,  # no heartbeat for an hour: the run process is probably gone
    }
    assert progress["stop_requested"] is True
    assert parse_z(progress["stop_requested_at"]) == started + timedelta(minutes=1)
    configuration = body["configuration"]
    run_directory = Path(run.output_path)
    assert configuration["files"] == [
        _file("request.json", run_directory / "request.json", "available"),
        _file("run.json", run_directory / "run.json", "missing"),  # written when the run ends
    ]
    assert (configuration["execution"], configuration["partial_results"]) == (None, False)
    assert configuration["request"]["manifest"]["n_inputs"] == 3
    assert _get(client, f"{JOBS}/{run.job_id}/failures") == {
        "rows": [],
        "total": 0,
        "has_more": False,
        "report": _file("failures.jsonl", None, "not_recorded"),  # published when the run ends
    }


def test_a_job_not_yet_published_has_nothing_recorded(api: tuple[Engine, TestClient]) -> None:
    engine, client = api
    with Session(engine) as session, session.begin():
        job = Job(computer_name="test-host", status=int(JobStatus.QUEUED), priority=0, phase=RunPhase.CREATED)
        session.add(job)
        session.flush()
        job_id = job.job_id
    body = _get(client, f"{JOBS}/{job_id}")
    assert (body["run"], body["scan_number"], body["remote"], body["duration_seconds"]) == (None, None, None, None)
    assert body["progress"]["heartbeat_age_seconds"] is None
    assert body["configuration"] == {
        "kind": None,
        "run_directory": {"path": None, "state": "not_recorded", "detail": None},
        "files": [_file("request.json", None, "not_recorded"), _file("run.json", None, "not_recorded")],
        "problems": [],
        "validation_error": None,
        "warnings": [],
        "partial_results": False,
        "request": None,
        "execution": None,
        "history": None,
        "artifacts": [],
    }
    log = _get(client, f"{JOBS}/{job_id}/log")
    assert log["run_log"]["file"] == _file("run.json", None, "not_recorded")


# ---------------------------------------------------------------------------
# Remote runs, through submission, the poller and publication (a fake DM)


class FakeDM(BatchedFake):
    """DM's processing jobs by ID, the calls ``remote.submission`` and ``remote.poller`` make."""

    def __init__(self) -> None:
        self.job: dict[str, Any] = {"id": "dm-001", "status": "pending"}
        self.submissions: list[dict[str, Any]] = []

    def submit(self, plan: dict[str, Any]) -> dict[str, Any]:
        self.submissions.append(plan)
        return self.job

    def find(self, submission_id: str) -> list[dict[str, Any]]:
        return [self.job]

    def get(self, external_id: str) -> dict[str, Any]:
        assert external_id == self.job["id"]
        return self.job

    def status(self, value: str, **record: Any) -> None:
        self.job.update(status=value, **record)


@pytest.fixture
def remote(tmp_path: Path) -> SimpleNamespace:
    """The remote settings (``bdp.yaml``) over an APS root and an Eagle root under ``tmp_path/remote``."""
    layout = Layout(tmp_path / "remote")
    return SimpleNamespace(layout=layout, settings=remote_settings(bdp_settings(layout)))


def _running_crux_run(engine: Engine, remote: SimpleNamespace) -> tuple[ReconstructionRun, dict[str, Any], FakeDM]:
    """A wire run submitted to crux, which DM reports running (with its Globus run), as the poller recorded it."""
    run = create_wire_run(engine, remote.layout, count=2, backend="crux")
    dm = FakeDM()
    submission.submit_run(run.job_id, engine=engine, settings=remote.settings, client=dm)
    poll_once(engine=engine, settings=remote.settings, client=dm)
    dm.status("running", url=GLOBUS_RUN)
    poll_once(engine=engine, settings=remote.settings, client=dm)
    assert load_job(engine, run.job_id).status == JobStatus.RUNNING
    return run, dm.submissions[0], dm


def _observed(engine: Engine, job_id: int, value: Any) -> None:
    """``value`` stored as the job's remote observation, as the remote poller keeps one."""
    with Session(engine) as session, session.begin():
        row = session.get(Job, job_id)
        assert row is not None
        row.remote_observation = value


def _grid_row(client: TestClient, job_id: int) -> dict[str, Any]:
    """The job's row of the run monitor."""
    rows = _get(
        client, JOBS, filter=json.dumps({"job_id": {"filterType": "number", "type": "equals", "filter": job_id}})
    )
    [row] = rows["rows"]
    return row


def _grid_observation(client: TestClient, job_id: int) -> dict[str, Any] | None:
    """The job's row of the run monitor: its observation, the Status cell's."""
    return _grid_row(client, job_id)["observation"]


GOLDEN_OBSERVATIONS: dict[str, str] = json.loads(cases.GOLDENS.read_text(encoding="utf-8"))["observations"]
SHOWN = {
    "uplink": ("uplink", "Transferring inputs", None, False, 0, 0, "InputTransfer"),
    "at-alcf": ("queued", "Waiting for status", None, False, 0, 0, "RunPbsJob"),
    "at-alcf-late": ("queued", "Waiting for status", None, True, 0, -120, "RunPbsJob"),
    "submitting": ("queued", "Submitting to PBS", cases.PBS_ID, False, 0, 0, "RunPbsJob"),
    "pbs-queued": ("queued", "Waiting for PBS allocation", cases.PBS_ID, False, 0, 0, "RunPbsJob"),
    "pbs-held": ("queued", "Held in PBS", cases.PBS_ID, False, 0, 0, "RunPbsJob"),
    "running": ("running", "Processing", cases.PBS_ID, False, 0, 0, "RunPbsJob"),
    "finishing": ("running", "Finishing processing", cases.PBS_ID, False, 0, -600, "RunPbsJob"),
    "pbs-finished": ("downlink", "Awaiting result return", cases.PBS_ID, False, 0, 0, "RunPbsJob"),
    "observer-stale": ("running", "Processing", None, True, 0, -600, "RunPbsJob"),
    "scheduler-unavailable": ("running", "PBS status unavailable", cases.PBS_ID, True, 0, -600, "RunPbsJob"),
    "downlink": ("downlink", "Returning results", None, False, 0, 0, "ResultTransfer"),
    "flow-succeeded": ("downlink", "Returning results", None, False, 0, 0, "ResultTransfer"),
    "workflow-unavailable": ("running", "Workflow status unavailable", None, True, -60, -600, "RunPbsJob"),
    # Login needed
    "login-required": ("login_required", "Sign in to Globus and resume this run", None, False, 0, 0, "RunPbsJob"),
    "login-required-on-the-run": (
        "login_required",
        "Sign in to Globus and resume this run",
        None,
        False,
        0,
        0,
        "InputTransfer",
    ),
    "login-resumed": ("queued", "Awaiting workflow", None, False, 0, 0, None),
    "inactive-without-login": ("uplink", "Transferring inputs", None, False, 0, 0, "InputTransfer"),
}
"""The launcher goldens (``tests/fixtures/observation_goldens/``) as the job page shows them: the phase, the detail, the
PBS ID, stale (30 s after ``cases.NOW``: the launcher's flag), the observation's and the phase's times (seconds from
``cases.NOW``) and the Flow's step."""


def _shown(name: str) -> dict[str, Any]:
    phase, detail, pbs_id, stale, observed, since, flow_state = SHOWN[name]
    return {
        "phase": phase,
        "detail": detail,
        "pbs_id": pbs_id,
        "stale": stale,
        "observed_at": isoformat_z(cases.NOW + timedelta(seconds=observed)),
        "phase_since": isoformat_z(cases.NOW + timedelta(seconds=since)),
        "flow_state": flow_state,
    }


def test_the_golden_observations_show_on_the_job_page_and_in_the_grid(
    api: tuple[Engine, TestClient], remote: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, client = api
    run, _plan, _dm = _running_crux_run(engine, remote)
    monkeypatch.setattr(service, "utcnow", lambda: cases.NOW + timedelta(seconds=30))
    assert set(GOLDEN_OBSERVATIONS) == set(SHOWN)
    for name, text in GOLDEN_OBSERVATIONS.items():
        _observed(engine, run.job_id, json.loads(text))
        assert _get(client, f"{JOBS}/{run.job_id}")["remote"]["observation"] == _shown(name), name
        assert _grid_observation(client, run.job_id) == _shown(name), name  # the Status cell's is the page's


def test_an_observation_kept_at_the_depth_bound_shows_alike_on_the_page_and_in_the_grid(
    api: tuple[Engine, TestClient], remote: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deepest observation the poller keeps (``observation.MAX_DEPTH``, within 16 KiB) passes through the poller,
    the job page and the grid without an error, and both show its phase; a stored value SQLite's JSON functions cannot
    read (a deeper one or one holding NaN, which a v1 ``cb1159c`` database may hold) or that is not an object shows on
    neither."""
    engine, client = api
    run, plan, dm = _running_crux_run(engine, remote)
    deepest = {**json.loads(cases.intake_cases()["kept-at-the-depth-bound"]["value"])}
    deepest["submission_id"] = plan["submission_id"]
    dm.status("running", remoteObservation=json.dumps(deepest))
    poll_once(engine=engine, settings=remote.settings, client=dm)
    assert load_job(engine, run.job_id).remote_observation == deepest
    monkeypatch.setattr(service, "utcnow", lambda: cases.NOW + timedelta(seconds=30))
    shown = _get(client, f"{JOBS}/{run.job_id}")["remote"]["observation"]
    assert (shown["phase"], shown["detail"]) == ("running", "Processing")
    assert _grid_observation(client, run.job_id) == shown

    deeper = {**deepest, "nested": [deepest["nested"]]}
    for stored in (deeper, {**deepest, "nested": float("nan")}, [deepest], "running"):
        _observed(engine, run.job_id, stored)
        assert (
            _get(client, f"{JOBS}/{run.job_id}")["remote"]["observation"],
            _grid_observation(client, run.job_id),
        ) == (
            None,
            None,
        )


def test_an_observation_is_stale_after_90_seconds_and_shown_only_for_an_active_crux_run(
    api: tuple[Engine, TestClient], remote: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, client = api
    run, _plan, _dm = _running_crux_run(engine, remote)
    running = json.loads(GOLDEN_OBSERVATIONS["running"])
    _observed(engine, run.job_id, running)
    for after, stale in ((90, False), (91, True)):  # stale ("status delayed") past 90 s
        monkeypatch.setattr(service, "utcnow", lambda after=after: cases.NOW + timedelta(seconds=after))
        assert _get(client, f"{JOBS}/{run.job_id}")["remote"]["observation"]["stale"] is stale

    aps_id, _ = _published(engine, tmp_path, "aps", backend="aps", external_state="running", remote_observation=running)
    assert (_get(client, f"{JOBS}/{aps_id}")["remote"]["observation"], _grid_observation(client, aps_id)) == (
        None,
        None,
    )

    with Session(engine) as session, session.begin():
        row = session.get(Job, run.job_id)
        assert row is not None
        state.finalize(row, status=JobStatus.FAILED, message="DM job dm-001 failed")
    assert (_get(client, f"{JOBS}/{run.job_id}")["remote"]["observation"], _grid_observation(client, run.job_id)) == (
        None,
        None,
    )


PBS_OUTPUT = "".join(f"rank 0: input {index} done\n" for index in range(80)) + "runner exit status 1\n"


def _return(plan: dict[str, Any], failures: list[FailureRecord], **changes: Any) -> Path:
    """What the crux runner returns into the inbox: its summary (with ``changes``), progress, failure report and PBS
    output, every input failed (``failures``, sources as the runner saw them)."""
    incoming = Path(plan["incoming_directory"])
    incoming.mkdir(parents=True, exist_ok=True)
    counters = {"n_inputs": plan["n_inputs"], "n_succeeded": 0, "n_failed": len(failures), "n_not_run": 0}
    write_json_file(incoming / "progress.json", {"heartbeat_at": "2026-10-16T09:04:00+00:00", **counters})
    (incoming / "failures.jsonl").write_text("".join(record.to_json() + "\n" for record in failures))
    (incoming / "pbs-output.log").write_text(PBS_OUTPUT)
    returned = {
        "format": "laue-portal-run-summary",
        "version": 1,
        "job_id": plan["job_id"],
        "submission_id": plan["submission_id"],
        "display_id": "Reconstruction R1",
        "kind": plan["kind"],
        "status": "Failed",
        "phase": "failed",
        "message": "Not all inputs completed successfully",
        "started_at": "2026-10-16T09:01:00+00:00",
        "finished_at": "2026-10-16T09:04:00+00:00",
        "host": "crux-node-7",
        "counters": {**counters, "n_indexed": None},
        "artifacts": {},
        "provenance": {"engine": "wire", "path_map": plan["path_map"]},
        "warnings": [],
        "log": ["admitted 2 inputs", "wire_1: no peaks", "wire_2: unreadable"],
        "failure_report": "failures.jsonl",
        "policy": plan["execution"],
        **changes,
    }
    write_json_file(incoming / "run.json", returned)
    (incoming / ".runner.lock").write_text("1234")
    return incoming


def _remote_failures(plan: dict[str, Any], aps: Path) -> list[FailureRecord]:
    """Failure records with the sources the crux runner saw: Eagle paths (``map_path`` with the plan's map), and
    one that leaves the inputs' prefix."""
    remote_source = map_path(str(aps / "LauePortal" / "data" / "wire" / "wire_1.h5"), plan["path_map"])
    inputs = map_path(str(aps / "LauePortal"), plan["path_map"])
    return [
        FailureRecord(
            0, "wire_1", remote_source, "numerical", "no peaks", "ReconstructionError", {}, "2026-10-16T09:02:00+00:00"
        ),
        FailureRecord(1, "wire_2", f"{inputs}/../../../../../../etc/passwd", "input", "unreadable", "OSError"),
    ]


def test_a_remote_run_shows_its_target_dm_state_and_globus_run_and_no_heartbeat_check(
    api: tuple[Engine, TestClient], remote: SimpleNamespace
) -> None:
    engine, client = api
    run, plan, _dm = _running_crux_run(engine, remote)
    run_directory = Path(plan["run_directory"])

    body = _get(client, f"{JOBS}/{run.job_id}")
    assert (body["status"], body["phase"], body["run"]["label"]) == ("running", "running", f"R{run.id}")
    assert body["remote"] == {
        "target": "crux",
        "execution": "ALCF",
        "dm_state": "running",
        "globus_url": GLOBUS_RUN,
        "observation": None,
    }
    progress = body["progress"]
    assert progress["heartbeat_age_seconds"] is not None  # the poller's
    assert (progress["stale_after_seconds"], progress["stale"]) == (None, False)
    assert body["configuration"]["files"] == [
        _file("request.json", run_directory / "request.json", "available"),
        _file("run.json", run_directory / "run.json", "missing"),  # still on Eagle
    ]
    assert _get(client, f"{JOBS}/{run.job_id}/log") == {
        "job_id": run.job_id,
        "run_log": {
            "lines": [],
            "truncated": False,
            "n_lines": None,
            "file": _file("run.json", run_directory / "run.json", "missing"),
        },
        "pbs_output": {
            "lines": [],
            "truncated": False,
            "n_lines": None,
            "file": _file("pbs-output.log", run_directory / "pbs-output.log", "missing"),
        },
    }


def test_a_published_remote_run_maps_its_failure_sources_and_shows_its_pbs_output(
    api: tuple[Engine, TestClient], remote: SimpleNamespace
) -> None:
    engine, client = api
    run, plan, dm = _running_crux_run(engine, remote)
    run_directory = Path(plan["run_directory"])
    _return(plan, _remote_failures(plan, remote.layout.aps))
    dm.status("done")
    poll_once(engine=engine, settings=remote.settings, client=dm)
    assert load_job(engine, run.job_id).status == JobStatus.FAILED
    assert (run_directory / "pbs-output.log").is_file()  # published

    body = _get(client, f"{JOBS}/{run.job_id}")
    assert (body["status"], body["remote"]) == (
        "failed",
        {"target": "crux", "execution": "ALCF", "dm_state": "done", "globus_url": GLOBUS_RUN, "observation": None},
    )
    configuration = body["configuration"]
    assert [read["state"] for read in configuration["files"]] == ["available", "available"]
    execution = configuration["execution"]
    assert execution["host"] == "crux-node-7"
    assert execution["started_at"] == "2026-10-16T09:01:00+00:00"  # a remote run's text: UTC
    assert execution["policy"] == plan["execution"]
    assert execution["provenance"]["path_map"] == plan["path_map"]  # shown as returned, never used
    assert execution["provenance"]["remote_output_directory"] == plan["output_directory"]  # added by publication
    assert (configuration["artifacts"], configuration["partial_results"]) == ([], False)

    page = _get(client, f"{JOBS}/{run.job_id}/failures")
    assert [(row["index"], row["input_id"], row["source"], row["category"]) for row in page["rows"]] == [
        (0, "wire_1", str(remote.layout.aps / "LauePortal" / "data" / "wire" / "wire_1.h5"), "numerical"),
        (1, "wire_2", None, "input"),  # it leaves the inputs' prefix: unavailable
    ]
    assert page["rows"][0]["recorded_at"] == "2026-10-16T09:02:00+00:00"
    assert (page["total"], page["has_more"]) == (2, False)
    assert page["report"] == _file("failures.jsonl", run_directory / "failures.jsonl", "available")
    first = _get(client, f"{JOBS}/{run.job_id}/failures", limit=1)
    assert ([row["index"] for row in first["rows"]], first["has_more"]) == ([0], True)

    log = _get(client, f"{JOBS}/{run.job_id}/log", tail=2)
    assert log["run_log"] == {
        "lines": ["wire_1: no peaks", "wire_2: unreadable"],
        "truncated": True,
        "n_lines": 3,
        "file": _file("run.json", run_directory / "run.json", "available"),
    }
    assert log["pbs_output"] == {
        "lines": ["rank 0: input 79 done", "runner exit status 1"],
        "truncated": True,
        "n_lines": None,
        "file": _file("pbs-output.log", run_directory / "pbs-output.log", "available"),
    }
    whole = _get(client, f"{JOBS}/{run.job_id}/log", tail=1000)["pbs_output"]
    assert (whole["lines"], whole["truncated"]) == (PBS_OUTPUT.splitlines(), False)


def test_a_refused_returns_pbs_output_is_read_from_the_inbox(
    api: tuple[Engine, TestClient], remote: SimpleNamespace
) -> None:
    """Publication refused the return (another submission's summary): nothing was published, so the PBS output,
    the one diagnostic, is still in the return inbox."""
    engine, client = api
    run, plan, dm = _running_crux_run(engine, remote)
    run_directory = Path(plan["run_directory"])
    incoming = _return(plan, [], submission_id=str(uuid.UUID(int=7)))
    dm.status("done")
    poll_once(engine=engine, settings=remote.settings, client=dm)
    job = load_job(engine, run.job_id)
    assert job.status == JobStatus.FAILED and "could not be validated" in (job.messages or "")
    assert not (run_directory / "pbs-output.log").exists()

    log = _get(client, f"{JOBS}/{run.job_id}/log", tail=1)
    assert log["run_log"]["file"] == _file("run.json", run_directory / "run.json", "missing")
    assert log["pbs_output"] == {
        "lines": ["runner exit status 1"],
        "truncated": True,
        "n_lines": None,
        "file": _file("pbs-output.log", incoming / "pbs-output.log", "available"),
    }
    assert incoming == run_directory / "incoming" / plan["submission_id"]


# ---------------------------------------------------------------------------
# A run migrated from v1's per-input records: its run directory is not on this host


def test_a_migrated_runs_history_and_failures_come_from_the_migration_export(
    api: tuple[Engine, TestClient], tmp_path: Path
) -> None:
    engine, client = api
    not_mounted = tmp_path / "net" / "fileserver" / "export" / "data1" / "LauePortal" / "analysis" / "rec_1" / "data"
    export = tmp_path / "migration" / "jobs" / "job_1"
    export.mkdir(parents=True)
    source = f"{not_mounted.parent}/wire_3.h5"
    record = {  # v1's migration export (scripts/workflow_migration.py)
        "index": None,
        "input_id": "wire_3",
        "source": source,
        "category": "error",
        "message": "Segmentation fault",
        "error_type": None,
        "context": {"subjob_id": 13, "status": "Failed", "command": "wire_reconstruct ..."},
        "recorded_at": "2025-03-04 10:11:12",
    }
    (export / "failures.jsonl").write_text(
        json.dumps(record) + "\n" + json.dumps({**record, "input_id": "wire_4"}) + "\n"
    )
    history = {
        "job_id": 1,
        "original_status": 3,
        "original_status_name": "Failed",
        "counters": {"n_inputs": 4, "n_succeeded": 2, "n_failed": 2, "n_not_run": 0},
        "subjob_time_range": ["2025-03-04 10:00:00", "2025-03-04 10:30:00"],
        "message_classes": {"Segmentation fault": 2, "ok": 2, "": 0},
        "failure_report": str(export / "failures.jsonl"),
    }
    (export / "history.json").write_text(json.dumps(history, indent=2, sort_keys=True) + "\n")
    with Session(engine) as session, session.begin():
        session.add(create_test_metadata(274036))
        moment = datetime(2025, 3, 4, 16, 30, tzinfo=UTC)
        job = Job(
            computer_name="analysis-host",
            status=int(JobStatus.FAILED),
            priority=0,
            submit_time=moment - timedelta(hours=1),
            start_time=moment - timedelta(minutes=30),
            finish_time=moment,
            messages="Failed",
            phase=RunPhase.FAILED,
            updated_at=moment,
            n_inputs=4,
            n_succeeded=2,
            n_failed=2,
            run_directory=str(not_mounted),
            failure_report_path=str(export / "failures.jsonl"),
        )
        session.add(job)
        session.flush()
        session.add(
            ReconstructionRun(
                job_id=job.job_id, scan_number=274036, method="wire", input_path="/data", created_at=job.submit_time
            )
        )
        job_id = job.job_id

    body = _get(client, f"{JOBS}/{job_id}")
    assert (body["scan_number"], body["duration_seconds"], body["remote"]) == (274036, 1800.0, None)
    configuration = body["configuration"]
    assert configuration["run_directory"] == {"path": str(not_mounted), "state": "missing", "detail": None}
    assert configuration["files"] == [
        _file("request.json", None, "not_recorded"),
        _file("run.json", not_mounted / "run.json", "missing"),  # not available on this host
        _file("history.json", export / "history.json", "available"),
    ]
    assert configuration["history"] == {
        "original_status_name": "Failed",
        "first_input_at": "2025-03-04 10:00:00",
        "last_input_at": "2025-03-04 10:30:00",
        "message_classes": [
            {"message": "Segmentation fault", "count": 2},
            {"message": "ok", "count": 2},
            {"message": "", "count": 0},
        ],
    }
    assert (configuration["kind"], configuration["request"], configuration["execution"]) == (
        "wire_reconstruction",
        None,
        None,
    )
    assert (configuration["problems"], configuration["artifacts"]) == ([], [])

    page = _get(client, f"{JOBS}/{job_id}/failures")
    assert [(row["index"], row["input_id"], row["source"], row["error_type"]) for row in page["rows"]] == [
        (None, "wire_3", source, None),  # no plan: the sources are portal paths, as written
        (None, "wire_4", source, None),
    ]
    assert (page["total"], page["report"]["state"]) == (2, "available")
    log = _get(client, f"{JOBS}/{job_id}/log")
    assert (log["run_log"]["file"]["state"], log["pbs_output"]) == ("missing", None)


# ---------------------------------------------------------------------------
# Files that are missing, misplaced or malformed: a 200 that says so


def _published(engine: Engine, tmp_path: Path, name: str, **job: Any) -> tuple[int, Path]:
    """A published (queued) wire run, its job's columns set from ``job``; its ID and run directory."""
    run = publish_wire_run(engine, tmp_path, name=name)
    with Session(engine) as session, session.begin():
        row = session.get(Job, run.job_id)
        assert row is not None
        for key, value in job.items():
            setattr(row, key, value)
    return run.job_id, Path(run.output_path)


def _symlink_to_secret(path: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"provenance": {"token": "s3cr3t"}, "log": ["s3cr3t"], "kind": "s3cr3t"}))
    path.symlink_to(secret)


RUN_SUMMARY_CASES = {
    "not a JSON object": (lambda path, tmp_path: path.write_text("[1, 2]"), "not a JSON object"),
    "not JSON": (lambda path, tmp_path: path.write_text("{"), "Expecting property name enclosed in double quotes"),
    "not UTF-8": (lambda path, tmp_path: path.write_bytes(b'{"log": ["\xff"]}'), "'utf-8' codec can't decode"),
    "a directory": (lambda path, tmp_path: path.mkdir(), "not a regular file"),
    "a FIFO": (lambda path, tmp_path: os.mkfifo(path), "not a regular file"),  # opening it must not block
    "a symbolic link": (_symlink_to_secret, "a symbolic link"),
    "too large": (
        lambda path, tmp_path: path.write_text('{"log": [' + " " * summary.MAX_JSON_BYTES + "]}"),
        "larger than 4 MB",
    ),
    "nested too deeply": (lambda path, tmp_path: path.write_text("[" * 100_000), "maximum recursion depth"),
}


@pytest.mark.parametrize("case", list(RUN_SUMMARY_CASES))
def test_an_unreadable_run_summary_is_reported_never_an_error(
    api: tuple[Engine, TestClient], tmp_path: Path, case: str
) -> None:
    engine, client = api
    job_id, run_directory = _published(engine, tmp_path, "unreadable")
    make, detail = RUN_SUMMARY_CASES[case]
    make(run_directory / "run.json", tmp_path)

    configuration = _get(client, f"{JOBS}/{job_id}")["configuration"]
    [request, run_summary] = configuration["files"]
    assert request["state"] == "available"
    assert (run_summary["state"], run_summary["path"]) == ("unreadable", str(run_directory / "run.json"))
    assert detail in run_summary["detail"]
    [problem] = configuration["problems"]
    assert problem.startswith(f"run.json at {run_directory / 'run.json'} ")
    assert configuration["execution"] is None
    log = _get(client, f"{JOBS}/{job_id}/log")["run_log"]
    assert (log["lines"], log["n_lines"], log["file"]) == ([], None, run_summary)
    assert "s3cr3t" not in client.get(f"{JOBS}/{job_id}").text + client.get(f"{JOBS}/{job_id}/log").text


# Valid JSON the response cannot serve: a lone surrogate escape (UTF-8 cannot encode the text it gives) and
# nesting deeper than the serializer's 255 levels. A run file holding either is unreadable; a failure record's text
# is served with U+FFFD in place of each lone surrogate.
LONE = r"\ud800"
PAIR = r"😀"  # an escaped surrogate pair is one character, U+1F600


def _with_value(path: Path, section: str, key: str, raw_json: str) -> None:
    """Rewrite the JSON object at ``path`` with ``raw_json`` (JSON text, written as is) as ``[section][key]``."""
    document = json.loads(path.read_text()) if path.exists() else {}
    document.setdefault(section, {})[key] = "__RAW__"
    path.unlink(missing_ok=True)
    path.write_text(json.dumps(document).replace('"__RAW__"', raw_json))


def _nested(levels: int) -> str:
    return "[" * levels + "]" * levels


@pytest.mark.parametrize(
    ("raw_json", "detail"),
    [
        (f'"{LONE}"', summary.LONE_SURROGATE),
        (f'{{"{LONE}": 1}}', summary.LONE_SURROGATE),  # in a key
        (_nested(254), f"nesting deeper than {summary.MAX_JSON_DEPTH} levels"),  # within the parser's limit
        (_nested(101), f"nesting deeper than {summary.MAX_JSON_DEPTH} levels"),
    ],
    ids=["a lone surrogate", "a lone surrogate in a key", "254 levels", "just too deep"],
)
def test_run_files_the_response_cannot_serve_are_unreadable(
    api: tuple[Engine, TestClient], tmp_path: Path, raw_json: str, detail: str
) -> None:
    """In request.json's parameters and in run.json's provenance (the page's and the log's view of it)."""
    engine, client = api
    job_id, run_directory = _published(engine, tmp_path, "unservable")
    _with_value(run_directory / "request.json", "request", "notes", raw_json)
    _with_value(run_directory / "run.json", "provenance", "extra", raw_json)
    (run_directory / "run.json").write_text(
        (run_directory / "run.json").read_text()[:-1] + ', "log": ["ok"], "warnings": []}'
    )

    body = _get(client, f"{JOBS}/{job_id}")
    configuration = body["configuration"]
    assert [(read["name"], read["state"], read["detail"]) for read in configuration["files"]] == [
        ("request.json", "unreadable", detail),
        ("run.json", "unreadable", detail),
    ]
    assert configuration["problems"] == [
        f"request.json at {run_directory / 'request.json'} was not read: it holds {detail}",
        f"run.json at {run_directory / 'run.json'} was not read: it holds {detail}",
    ]
    assert (configuration["request"], configuration["execution"]) == (None, None)
    run_log = _get(client, f"{JOBS}/{job_id}/log")["run_log"]
    assert (run_log["lines"], run_log["file"]["state"], run_log["file"]["detail"]) == ([], "unreadable", detail)


def test_a_lone_surrogate_in_the_run_log_or_a_history_makes_the_file_unreadable(
    api: tuple[Engine, TestClient], tmp_path: Path
) -> None:
    engine, client = api
    job_id, run_directory = _published(engine, tmp_path, "surrogates")
    (run_directory / "run.json").write_text(f'{{"log": ["ok", "{LONE}"], "warnings": ["{LONE}"]}}')
    for suffix in ("", "/log"):
        assert client.get(f"{JOBS}/{job_id}{suffix}").status_code == 200
    assert _get(client, f"{JOBS}/{job_id}/log")["run_log"]["file"]["detail"] == summary.LONE_SURROGATE

    export = tmp_path / "export"
    export.mkdir()
    (export / "failures.jsonl").write_text("")
    (export / "history.json").write_text(f'{{"original_status_name": "{LONE}", "message_classes": {{"{LONE}": 1}}}}')
    migrated, _ = _published(
        engine, tmp_path, "migrated", run_directory=None, failure_report_path=str(export / "failures.jsonl")
    )
    configuration = _get(client, f"{JOBS}/{migrated}")["configuration"]
    assert configuration["files"][-1] == _file(
        "history.json", export / "history.json", "unreadable", summary.LONE_SURROGATE
    )
    assert configuration["history"] is None


def test_run_files_within_the_limits_are_served(api: tuple[Engine, TestClient], tmp_path: Path) -> None:
    """An escaped surrogate pair is a character like any other, and nesting up to the limit is served."""
    engine, client = api
    job_id, run_directory = _published(engine, tmp_path, "served")
    _with_value(run_directory / "request.json", "request", "notes", f'"{PAIR}"')
    _with_value(run_directory / "run.json", "provenance", "deep", _nested(summary.MAX_JSON_DEPTH - 2))
    configuration = _get(client, f"{JOBS}/{job_id}")["configuration"]
    assert [read["state"] for read in configuration["files"]] == ["available", "available"]
    assert configuration["request"]["parameters"]["notes"] == "\U0001f600"
    deep, levels = configuration["execution"]["provenance"]["deep"], 1
    while deep:
        deep, levels = deep[0], levels + 1
    assert levels == summary.MAX_JSON_DEPTH - 2  # the document, provenance, then these lists: the limit exactly


def test_failure_records_are_served_with_their_lone_surrogates_replaced(
    api: tuple[Engine, TestClient], tmp_path: Path
) -> None:
    """A record's text is served with U+FFFD for each lone surrogate (an escape, or an undecodable byte of a file
    name as Python escapes it); deeply nested values are served as text. Never a 500."""
    engine, client = api
    report = tmp_path / "failures.jsonl"
    values = {"input_id": f"a{LONE}", "source": r"/data/caf\udce9.h5", "category": f"in{LONE}put"}
    values |= {"message": f"bad {PAIR} {LONE}", "error_type": f"E{LONE}", "recorded_at": f"{LONE}"}
    raw = "{" + ", ".join(f'"{key}": "{value}"' for key, value in values.items()) + ', "index": 0}'
    deep = json.dumps({"index": 1, "category": "input", "message": "x", "context": {}, "error_type": "__DEEP__"})
    report.write_text(raw + "\n" + deep.replace('"__DEEP__"', _nested(300)) + "\n")
    job_id, _ = _published(engine, tmp_path, "records", failure_report_path=str(report), n_failed=2)
    page = _get(client, f"{JOBS}/{job_id}/failures")
    assert page["report"]["state"] == "available"
    first, second = page["rows"]
    assert first == {
        "index": 0,
        "input_id": "a�",
        "source": "/data/caf�.h5",
        "category": "in�put",
        "error_type": "E�",
        "message": "bad \U0001f600 �",
        "recorded_at": "�",
    }
    assert second["error_type"] == _nested(300)


def test_missing_run_files_and_a_run_directory_not_on_this_host(api: tuple[Engine, TestClient], tmp_path: Path) -> None:
    engine, client = api
    job_id, run_directory = _published(engine, tmp_path, "gone")
    (run_directory / "request.json").unlink()
    configuration = _get(client, f"{JOBS}/{job_id}")["configuration"]
    assert configuration["run_directory"]["state"] == "available"
    assert [read["state"] for read in configuration["files"]] == ["missing", "missing"]
    assert configuration["problems"] == []  # a missing file is no problem: it is reported as missing

    elsewhere = tmp_path / "net" / "storage" / "data-c" / "run_1"  # never created: not mounted here
    moved, _ = _published(
        engine,
        tmp_path,
        "moved",
        run_directory=str(elsewhere),
        request_path=str(elsewhere / "request.json"),
        failure_report_path=str(elsewhere / "failures.jsonl"),
        n_failed=1,
    )
    body = _get(client, f"{JOBS}/{moved}")
    assert body["configuration"]["run_directory"] == {"path": str(elsewhere), "state": "missing", "detail": None}
    assert body["configuration"]["files"] == [  # no history.json: its failure report is in its run directory
        _file("request.json", elsewhere / "request.json", "missing"),
        _file("run.json", elsewhere / "run.json", "missing"),
    ]
    assert _get(client, f"{JOBS}/{moved}/failures") == {
        "rows": [],
        "total": 1,  # by the counters: one input did not succeed, but its report is not on this host
        "has_more": False,
        "report": _file("failures.jsonl", elsewhere / "failures.jsonl", "missing"),
    }

    a_file = tmp_path / "not-a-directory"
    a_file.write_text("")
    odd, _ = _published(engine, tmp_path, "odd", run_directory=str(a_file))
    assert _get(client, f"{JOBS}/{odd}")["configuration"]["run_directory"] == {
        "path": str(a_file),
        "state": "unreadable",
        "detail": "not a directory",
    }


def test_history_is_looked_for_only_in_a_migration_export(api: tuple[Engine, TestClient], tmp_path: Path) -> None:
    """A run migrated from v1's per-input records keeps ``history.json`` next to its failure report, in the
    migration export, outside its run directory: only there is it looked for (and reported missing when it is not
    there). A run whose report is in its run directory was never migrated: it has no history to miss."""
    engine, client = api
    never_migrated, run_directory = _published(engine, tmp_path, "local")
    (run_directory / "failures.jsonl").write_text("")
    with Session(engine) as session, session.begin():
        job = session.get(Job, never_migrated)
        assert job is not None
        job.failure_report_path = os.path.join(job.run_directory or "", "sub", "..", "failures.jsonl")
    files = _get(client, f"{JOBS}/{never_migrated}")["configuration"]["files"]
    assert [read["name"] for read in files] == ["request.json", "run.json"]

    export = tmp_path / "migration" / "jobs" / "job_2"
    export.mkdir(parents=True)
    (export / "failures.jsonl").write_text("")
    migrated, _ = _published(engine, tmp_path, "migrated", failure_report_path=str(export / "failures.jsonl"))
    files = _get(client, f"{JOBS}/{migrated}")["configuration"]["files"]
    assert files[-1] == _file("history.json", export / "history.json", "missing")


def _write_report(path: Path, lines: list[str] | bytes) -> None:
    if isinstance(lines, bytes):
        path.write_bytes(lines)
    else:
        path.write_text("".join(line + "\n" for line in lines))


GOOD_RECORD = json.dumps({"index": 0, "input_id": "a", "source": "/data/a.h5", "category": "input", "message": "bad"})
FAILURE_REPORT_CASES = {
    "a record that is not an object": ([GOOD_RECORD, "[1, 2]"], "malformed failure record: not a JSON object (list)"),
    "a record that is not JSON": ([GOOD_RECORD, "{oops"], "record 1 is not JSON"),
    "a record without its message": ([GOOD_RECORD, '{"index": 1, "category": "input"}'], "malformed failure record"),
    "an index out of range": (
        [GOOD_RECORD, '{"index": 1e400, "category": "input", "message": "x"}'],
        "malformed failure record: its index is not a whole number",
    ),
    "an index that is text": (  # the detail names what is wrong, never the record's values
        [GOOD_RECORD, '{"index": "SECRET-' + "s" * 300 + '", "category": "input", "message": "x"}'],
        "malformed failure record: its index is not a whole number",
    ),
    "not UTF-8": (b'{"index": 0, "category": "input", "message": "\xff"}\n', "record 0 is not UTF-8"),
    "nested too deeply to parse": ([GOOD_RECORD, "[" * 50_000], "record 1 is nested too deeply"),  # within 64 KiB
}


@pytest.mark.parametrize("case", list(FAILURE_REPORT_CASES))
def test_a_malformed_failure_report_is_an_empty_page_that_says_why(
    api: tuple[Engine, TestClient], tmp_path: Path, case: str
) -> None:
    engine, client = api
    report = tmp_path / "failures.jsonl"
    lines, detail = FAILURE_REPORT_CASES[case]
    _write_report(report, lines)
    job_id, _ = _published(engine, tmp_path, "malformed", failure_report_path=str(report), n_failed=2)
    page = _get(client, f"{JOBS}/{job_id}/failures")
    assert (page["rows"], page["has_more"], page["total"]) == ([], False, 2)
    assert (page["report"]["state"], page["report"]["path"]) == ("unreadable", str(report))
    assert detail in page["report"]["detail"]
    assert "SECRET" not in page["report"]["detail"]


def test_a_huge_failure_record_is_never_read_whole(api: tuple[Engine, TestClient], tmp_path: Path) -> None:
    """A record longer than ``MAX_FAILURE_RECORD_BYTES`` (here 16 MiB, written by the remote side) on the page makes
    the page unreadable, saying so; the next page skips it in pieces. Neither response, nor the memory either
    request takes, grows with the record."""
    engine, client = api
    report = tmp_path / "failures.jsonl"
    huge = json.dumps({"index": 0, "input_id": "a", "category": "input", "message": "x" * (16 * 1024 * 1024)})
    report.write_text(huge + "\n" + GOOD_RECORD.replace('"index": 0', '"index": 1') + "\n")
    job_id, _ = _published(engine, tmp_path, "huge", failure_report_path=str(report), n_failed=2)
    url = f"{JOBS}/{job_id}/failures"

    tracemalloc.start()
    try:
        first = client.get(url)
        _, peak_first = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        second = client.get(url, params={"offset": 1})
        _, peak_second = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert (first.status_code, second.status_code) == (200, 200)
    assert len(first.content) < 2048
    assert first.json() == {
        "rows": [],
        "total": 2,
        "has_more": False,
        "report": _file(
            "failures.jsonl",
            report,
            "unreadable",
            f"{report}: record 0 is longer than {summary.MAX_FAILURE_RECORD_BYTES} bytes",
        ),
    }
    assert [row["index"] for row in second.json()["rows"]] == [1]
    assert second.json()["report"]["state"] == "available"
    assert max(peak_first, peak_second) < 8 * 1024 * 1024  # the record is 16 MiB


def test_a_failure_report_larger_than_the_cap_is_not_read(api: tuple[Engine, TestClient], tmp_path: Path) -> None:
    engine, client = api
    report = tmp_path / "failures.jsonl"
    with open(report, "wb") as handle:  # sparse: no disk is used
        handle.write((GOOD_RECORD + "\n").encode())
        handle.truncate(summary.MAX_FAILURE_REPORT_BYTES + 1)
    job_id, _ = _published(engine, tmp_path, "capped", failure_report_path=str(report), n_failed=1)
    assert _get(client, f"{JOBS}/{job_id}/failures")["report"] == _file(
        "failures.jsonl", report, "unreadable", "larger than 256 MB"
    )


@pytest.mark.parametrize("kind", ["a symbolic link", "a FIFO", "a directory"])
def test_a_failure_report_that_is_no_regular_file_is_not_opened(
    api: tuple[Engine, TestClient], tmp_path: Path, kind: str
) -> None:
    engine, client = api
    report = tmp_path / "failures.jsonl"
    if kind == "a symbolic link":
        target = tmp_path / "elsewhere.jsonl"
        target.write_text(GOOD_RECORD + "\n")
        report.symlink_to(target)
    elif kind == "a FIFO":
        os.mkfifo(report)
    else:
        report.mkdir()
    job_id, _ = _published(engine, tmp_path, "special", failure_report_path=str(report))
    page = _get(client, f"{JOBS}/{job_id}/failures")
    assert page["rows"] == []
    assert page["report"] == _file(
        "failures.jsonl", report, "unreadable", "a symbolic link" if kind == "a symbolic link" else "not a regular file"
    )


def test_failure_records_of_other_types_are_shown_as_text(api: tuple[Engine, TestClient], tmp_path: Path) -> None:
    """``failures.jsonl`` is JSON: a value of another type than the format's is shown as text, a source that is
    not text as unavailable, and a page never fails on it."""
    engine, client = api
    report = tmp_path / "failures.jsonl"
    record = {"index": "3", "input_id": 12, "source": 7, "category": "input", "message": 5, "error_type": ["x"]}
    report.write_text(json.dumps(record) + "\n")
    job_id, _ = _published(engine, tmp_path, "types", failure_report_path=str(report))
    [row] = _get(client, f"{JOBS}/{job_id}/failures")["rows"]
    assert row == {
        "index": 3,
        "input_id": "12",
        "source": None,
        "category": "input",
        "error_type": "['x']",
        "message": "5",
        "recorded_at": None,
    }


# ---------------------------------------------------------------------------
# A remote run's PBS output: its tail, read from the end; links and FIFOs refused


def _remote_job(engine: Engine, tmp_path: Path, name: str, **job: Any) -> tuple[int, Path, str]:
    """A published run recorded as submitted to the aps target (its other columns from ``job``); its ID, run
    directory and submission ID."""
    submission_id = str(uuid.uuid4())
    job_id, run_directory = _published(
        engine, tmp_path, name, backend="aps", submission_id=submission_id, external_state="running", **job
    )
    return job_id, run_directory, submission_id


def _get_within(client: TestClient, url: str, fifo: Path | None = None, seconds: float = 10.0) -> Any:
    """``client.get(url)``, failing the test (instead of hanging it) when the request blocks, e.g. on a FIFO at
    ``fifo`` (opened for writing then, so that the blocked reader ends)."""
    result: list[Any] = []
    caller = threading.Thread(target=lambda: result.append(client.get(url)), daemon=True)
    caller.start()
    caller.join(seconds)
    if caller.is_alive():
        if fifo is not None:
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        pytest.fail(f"GET {url} blocked")
    return result[0]


@pytest.mark.parametrize("kind", ["a FIFO", "a symbolic link", "nested too deeply to parse"])
def test_a_plan_that_cannot_be_used_maps_no_failure_source_and_never_blocks(
    api: tuple[Engine, TestClient], tmp_path: Path, kind: str
) -> None:
    """The failure report's sources are mapped through the run's plan, read without following a link and without
    blocking: a plan that cannot be used maps nothing (the sources are unavailable), and it is not the report that
    is unreadable."""
    engine, client = api
    report = tmp_path / "failures.jsonl"
    job_id, run_directory, _ = _remote_job(engine, tmp_path, "planned", failure_report_path=str(report), n_failed=1)
    remote_inputs = "/eagle/workspace/runs/s/inputs"
    record = {"index": 0, "input_id": "wire_1", "source": f"{remote_inputs}/wire_1.h5", "category": "input"}
    report.write_text(json.dumps({**record, "message": "bad"}) + "\n")
    plan = run_directory / "bdp.json"
    if kind == "a FIFO":
        os.mkfifo(plan)
    elif kind == "a symbolic link":  # to a plan that would map the source, were it followed
        elsewhere = tmp_path / "elsewhere.json"
        elsewhere.write_text(json.dumps({"version": 1, "path_map": {str(tmp_path / "data"): remote_inputs}}))
        plan.symlink_to(elsewhere)
    else:
        plan.write_text('{"path_map": ' + "[" * 100_000)
    response = _get_within(client, f"{JOBS}/{job_id}/failures", fifo=plan if kind == "a FIFO" else None)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["report"] == _file("failures.jsonl", report, "available")
    assert [(row["input_id"], row["source"]) for row in body["rows"]] == [("wire_1", None)]


def test_a_large_pbs_output_is_read_from_its_end(
    api: tuple[Engine, TestClient], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 1 GiB log (sparse) whose last 200,000 lines are text: the tail reads at most ``MAX_TAIL_BYTES``, never the
    whole file."""
    engine, client = api
    job_id, run_directory, _ = _remote_job(engine, tmp_path, "large")
    lines = [f"line {index:06d}" for index in range(200_000)]
    with open(run_directory / "pbs-output.log", "wb") as handle:
        handle.truncate(1 << 30)
        handle.seek(1 << 30)
        handle.write(("\n" + "\n".join(lines) + "\n").encode())
    reads: list[int] = []
    pread = os.pread

    def counting_pread(descriptor: int, size: int, offset: int) -> bytes:
        reads.append(size)
        return pread(descriptor, size, offset)

    monkeypatch.setattr(os, "pread", counting_pread)
    output = _get(client, f"{JOBS}/{job_id}/log", tail=1000)["pbs_output"]
    assert (output["lines"], output["truncated"], output["n_lines"]) == (lines[-1000:], True, None)
    assert output["file"] == _file("pbs-output.log", run_directory / "pbs-output.log", "available")
    assert 0 < sum(reads) <= summary.MAX_TAIL_BYTES
    assert sum(reads) < 1000 * len("line 000000\n") + summary.TAIL_BLOCK_BYTES  # about the lines asked for


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        (GLOBUS_RUN, GLOBUS_RUN),
        ("http://dm.example/runs/1", "http://dm.example/runs/1"),
        ("javascript:alert(1)", None),  # never a link the page would follow
        ("app.globus.org/runs/1", None),
        ("", None),
        (None, None),
    ],
)
def test_only_a_web_address_is_shown_as_the_globus_run(
    api: tuple[Engine, TestClient], tmp_path: Path, url: str | None, shown: str | None
) -> None:
    engine, client = api
    job_id, _ = _published(engine, tmp_path, "linked", backend="aps", external_state="pending", external_url=url)
    assert _get(client, f"{JOBS}/{job_id}")["remote"] == {
        "target": "aps",
        "execution": "APS",
        "dm_state": "pending",
        "globus_url": shown,
        "observation": None,
    }


PBS_OUTPUT_REFUSALS = [
    "a symbolic link in the run directory",
    "a symbolic link out of the run directory",
    "an inbox linked out of the run directory",
    "a FIFO",
]


@pytest.mark.parametrize("kind", PBS_OUTPUT_REFUSALS)
def test_a_pbs_output_is_read_only_as_a_regular_file_inside_the_run_directory(
    api: tuple[Engine, TestClient], tmp_path: Path, kind: str
) -> None:
    engine, client = api
    job_id, run_directory, submission_id = _remote_job(engine, tmp_path, "linked")
    secret = tmp_path / "secret.log"
    secret.write_text("s3cr3t\n")
    path = run_directory / "pbs-output.log"
    if kind == "a symbolic link in the run directory":
        (run_directory / "other.log").write_text("s3cr3t\n")
        path.symlink_to(run_directory / "other.log")
        expected = _file("pbs-output.log", path, "unreadable", "a symbolic link")
    elif kind == "a symbolic link out of the run directory":
        path.symlink_to(secret)
        expected = _file("pbs-output.log", path, "unreadable", "outside the run directory")
    elif kind == "an inbox linked out of the run directory":  # a directory holding a pbs-output.log
        (run_directory / "incoming").mkdir()
        (run_directory / "incoming" / submission_id).symlink_to(tmp_path, target_is_directory=True)
        (tmp_path / "pbs-output.log").write_text("s3cr3t\n")
        path = run_directory / "incoming" / submission_id / "pbs-output.log"
        expected = _file("pbs-output.log", path, "unreadable", "outside the run directory")
    else:
        os.mkfifo(path)  # opening it must not block
        expected = _file("pbs-output.log", path, "unreadable", "not a regular file")
    response = client.get(f"{JOBS}/{job_id}/log")
    assert response.status_code == 200, response.text
    assert response.json()["pbs_output"] == {"lines": [], "truncated": False, "n_lines": None, "file": expected}
    assert "s3cr3t" not in response.text


# ---------------------------------------------------------------------------
# Unknown jobs and bad parameters


def test_an_unknown_job_is_a_404_on_every_job_page_endpoint(api: tuple[Engine, TestClient]) -> None:
    _, client = api
    for suffix in ("", "/failures", "/log"):
        response = client.get(f"{JOBS}/999{suffix}")
        assert (response.status_code, response.headers["content-type"]) == (404, PROBLEM_JSON)
        assert response.json() == {
            "type": "about:blank",
            "title": "Job not found",
            "status": 404,
            "detail": "No job 999",
        }
        assert problem_items(client.get(f"{JOBS}/{2**63}{suffix}")) == [(["path", "job_id"], "less_than_equal")]
        assert problem_items(client.get(f"{JOBS}/I3{suffix}")) == [(["path", "job_id"], "int_parsing")]


@pytest.mark.parametrize(
    ("suffix", "params", "expected"),
    [
        ("/failures", {"offset": -1}, [(["query", "offset"], "greater_than_equal")]),
        ("/failures", {"limit": 0}, [(["query", "limit"], "greater_than_equal")]),
        ("/failures", {"limit": 201}, [(["query", "limit"], "less_than_equal")]),
        ("/failures", {"sort": "index"}, [(["query", "sort"], "extra_forbidden")]),
        ("/log", {"tail": 0}, [(["query", "tail"], "greater_than_equal")]),
        ("/log", {"tail": 1001}, [(["query", "tail"], "less_than_equal")]),
        ("/log", {"tail": "all"}, [(["query", "tail"], "int_parsing")]),
        ("/log", {"lines": 5}, [(["query", "lines"], "extra_forbidden")]),
    ],
)
def test_bad_paging_and_tail_parameters_are_422s(
    api: tuple[Engine, TestClient], tmp_path: Path, suffix: str, params: dict[str, Any], expected: list[Any]
) -> None:
    engine, client = api
    job_id, _ = _published(engine, tmp_path, "params")
    assert problem_items(client.get(f"{JOBS}/{job_id}{suffix}", params=params)) == expected
