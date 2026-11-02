"""``GET /api/v1/system/status``: the database, the API, the worker and its threads (its row), the queue (counted on the
job and conversion tables) and its next items in the dispatcher's order, and the active runs, on a seeded SQLite file.
No Redis."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from laueportal import __version__
from laueportal.app import create_app
from laueportal.core.timeutil import utcnow
from laueportal.indexing.models import Conversion, IndexingRun
from laueportal.jobs import state
from laueportal.jobs.models import Job, Worker
from laueportal.jobs.records import new_job
from laueportal.jobs.state import JobStatus
from laueportal.reconstruction.models import ReconstructionRun
from tests.api_support import PORTAL_HEADERS
from tests.run_support import dispatch_queue
from tests.settings_support import create_database, make_database, make_settings

STATUS = "/api/v1/system/status"
STALE_AFTER = 300


def job(session: Session, *, backend: str = "local", n_inputs: int = 10, at: datetime) -> Job:
    created = new_job(computer_name="test-host", priority=0, submitted_at=at, n_inputs=n_inputs, backend=backend)
    session.add(created)
    session.flush()
    return created


def queued(session: Session, *, at: datetime, backend: str = "local") -> Job:
    """A published run handed to the queue (a remote run: recorded as submitted to DM, as ``submit_run`` does)."""
    created = job(session, backend=backend, at=at)
    directory = f"/runs/job_{created.job_id}"
    state.mark_published(
        created,
        run_directory=directory,
        manifest_path=f"{directory}/inputs.jsonl",
        manifest_digest="digest",
        request_path=f"{directory}/request.json",
        n_inputs=created.n_inputs,
        now=at,
    )
    state.mark_queued(created, f"queue-{created.job_id}", now=at)
    return created


def running(session: Session, *, started: datetime, backend: str = "local") -> Job:
    created = queued(session, at=started, backend=backend)
    state.mark_running(created, now=started)
    return created


def indexing(session: Session, created: Job, at: datetime) -> int:
    run = IndexingRun(job_id=created.job_id, method="lauego", input_path="/data/in", created_at=at)
    session.add(run)
    session.flush()
    return run.id


def reconstruction(session: Session, created: Job, at: datetime) -> int:
    run = ReconstructionRun(job_id=created.job_id, method="wire", input_path="/data/in", created_at=at)
    session.add(run)
    session.flush()
    return run.id


def seed_runs(engine: Engine, now: datetime) -> dict[str, Any]:
    """One run of each kind the status page tells apart; returns their job and run IDs."""
    ids: dict[str, Any] = {}
    with Session(engine) as session, session.begin():
        finished = running(session, started=now - timedelta(hours=1))
        state.finalize(finished, status=JobStatus.FINISHED, now=now - timedelta(minutes=30))
        indexing(session, finished, now)

        live = running(session, started=now - timedelta(minutes=5))
        state.record_progress(live, succeeded=2, failed=1, now=now - timedelta(seconds=10))
        ids["live"] = (live.job_id, indexing(session, live, now))

        silent = running(session, started=now - timedelta(seconds=STALE_AFTER + 100))
        state.request_cancellation(silent, reason="user", now=now - timedelta(seconds=60))
        ids["silent"] = (silent.job_id, reconstruction(session, silent, now))

        waiting = queued(session, at=now - timedelta(seconds=30))
        ids["waiting"] = (waiting.job_id, indexing(session, waiting, now))

        remote = running(session, started=now - timedelta(seconds=STALE_AFTER * 4), backend="aps")
        remote.external_state = "running"  # as the remote poller records DM's state
        ids["remote"] = (remote.job_id, indexing(session, remote, now))

        submitting = queued(session, at=now - timedelta(seconds=5), backend="crux")
        ids["submitting"] = (submitting.job_id, reconstruction(session, submitting, now))

        bare = job(session, at=now)  # created, not yet published: Queued with no run row yet
        ids["bare"] = (bare.job_id, None)
    return ids


def test_status_reports_the_active_runs(tmp_path: Path) -> None:
    settings, engine = make_database(tmp_path, execution={"stale_heartbeat_seconds": STALE_AFTER})
    now = utcnow()
    ids = seed_runs(engine, now)
    engine.dispose()

    response = TestClient(create_app(settings)).get(STATUS)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["database"] == {"status": "ok", "path": str(settings.database.path)}
    runs = {run["job_id"]: run for run in body["active_runs"]}
    order = [ids[name][0] for name in ("bare", "submitting", "remote", "waiting", "silent", "live")]
    assert [run["job_id"] for run in body["active_runs"]] == order  # newest first; the finished run is not listed

    live_job, live_run = ids["live"]
    live = runs[live_job]
    assert 9 <= live.pop("heartbeat_age_seconds") < 120
    assert live == {
        "job_id": live_job,
        "run": {"kind": "indexing", "id": live_run, "label": f"I{live_run}"},
        "status": "running",
        "phase": "running",
        "n_inputs": 10,
        "n_succeeded": 2,
        "n_failed": 1,
        "n_not_run": 0,
        "stale": False,
        "stop_requested": False,
        "remote": None,
    }

    silent_job, silent_run = ids["silent"]
    silent = runs[silent_job]
    assert silent["run"] == {"kind": "reconstruction", "id": silent_run, "label": f"R{silent_run}"}
    assert silent["heartbeat_age_seconds"] > STALE_AFTER
    assert (silent["status"], silent["stale"], silent["stop_requested"]) == ("running", True, True)

    waiting = runs[ids["waiting"][0]]
    assert (waiting["status"], waiting["phase"], waiting["heartbeat_age_seconds"]) == ("queued", "queued", None)
    assert (waiting["stale"], waiting["remote"]) == (False, None)

    remote = runs[ids["remote"][0]]
    assert (
        remote["heartbeat_age_seconds"] > STALE_AFTER
    )  # the poller's heartbeat is old, yet a remote run is never stale
    assert (remote["status"], remote["stale"]) == ("running", False)
    assert remote["remote"] == {"target": "aps", "dm_state": "running"}

    submitting = runs[ids["submitting"][0]]
    assert (submitting["status"], submitting["remote"]) == ("queued", {"target": "crux", "dm_state": None})

    bare = runs[ids["bare"][0]]
    assert (bare["run"], bare["status"], bare["phase"]) == (None, "queued", "created")


def test_a_running_local_run_without_a_heartbeat_is_stale(tmp_path: Path) -> None:
    settings, engine = make_database(tmp_path)
    now = utcnow()
    with Session(engine) as session, session.begin():
        started = running(session, started=now)
        started.heartbeat_at = None  # a row written before heartbeats were recorded
    engine.dispose()
    run = TestClient(create_app(settings)).get(STATUS).json()["active_runs"][0]
    assert (run["heartbeat_age_seconds"], run["stale"]) == (None, True)


def test_status_without_a_database_is_still_a_200(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    response = TestClient(create_app(settings)).get(STATUS)
    assert response.status_code == 200
    assert response.json() == {
        "version": __version__,
        "database": {"status": "error", "path": str(settings.database.path)},
        "worker": None,
        "queue": None,
        "next_up": None,
        "active_runs": None,
    }
    assert not settings.database.path.exists()  # the probe never creates the database


def test_a_database_without_the_portals_tables_reports_no_runs(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    create_database(settings)  # a SQLite file with a probe table only
    response = TestClient(create_app(settings)).get(STATUS)
    assert response.status_code == 200
    body = response.json()
    assert body["database"]["status"] == "ok"
    assert (body["worker"], body["queue"], body["next_up"], body["active_runs"]) == (None, None, None, None)


def test_status_route_has_the_contract_operation_id(tmp_path: Path) -> None:
    paths = TestClient(create_app(make_settings(tmp_path))).get("/api/v1/openapi.json").json()["paths"]
    operation = paths[STATUS]["get"]
    assert (operation["operationId"], operation["tags"]) == ("getSystemStatus", ["system"])
    assert paths["/api/v1/health"]["get"]["operationId"] == "getHealth"  # unchanged


# ---------------------------------------------------------------------------
# The queue and its next items


def test_status_counts_the_queue_and_lists_the_next_five_in_the_dispatchers_order(tmp_path: Path) -> None:
    """The counts are the job table's Queued local runs, the running local runs and conversions, and the queued
    conversions (a remote run is its target's); the next five items are the dispatcher's own order
    (``jobs.order.queued_in_order``): the conversions, then the runs moved to the front (Move to Front through the API,
    the newest move first), then by creation."""
    settings, engine = make_database(tmp_path)
    now = utcnow()
    with Session(engine) as session, session.begin():
        waiting = [queued(session, at=now) for _ in range(5)]
        runs = {created.job_id: indexing(session, created, now) for created in waiting}
        queued(session, at=now, backend="aps")
        converting = indexing(session, running(session, started=now), now)
        session.add(Conversion(indexing_id=converting, status="queued", requested_at=now))
        first, second, third, fourth, fifth = (created.job_id for created in waiting)

    with TestClient(create_app(settings), headers=PORTAL_HEADERS) as client:
        for moved in (fourth, third):
            assert client.post(f"/api/v1/jobs/{moved}/move-to-front").status_code == 204
        body = client.get(STATUS).json()
    order = dispatch_queue(engine)
    engine.dispose()

    assert body["queue"] == {
        "queued_runs": 5,
        "running": 1,
        "queued_conversions": 1,
        "live_slots_in_use": 0,
        "live_slots": 2,
    }
    assert order[:5] == [("convert", converting), ("run", third), ("run", fourth), ("run", first), ("run", second)]
    assert len(order) == 6 and fifth not in [item["job_id"] for item in body["next_up"]]

    def run(job_id: int) -> dict[str, Any]:
        return {"kind": "indexing", "id": runs[job_id], "label": f"I{runs[job_id]}"}

    assert body["next_up"] == [
        {
            "kind": "conversion",
            "job_id": None,
            "run": {"kind": "indexing", "id": converting, "label": f"I{converting}"},
            "moved_to_front": False,
        },
        {"kind": "run", "job_id": third, "run": run(third), "moved_to_front": True},
        {"kind": "run", "job_id": fourth, "run": run(fourth), "moved_to_front": True},
        {"kind": "run", "job_id": first, "run": run(first), "moved_to_front": False},
        {"kind": "run", "job_id": second, "run": run(second), "moved_to_front": False},
    ]


def test_status_counts_the_live_slots_in_use_and_lists_the_queued_live_runs_first(tmp_path: Path) -> None:
    """The live slots in use are the Running live runs (of the worker's two); the next items are the dispatcher's order,
    the Queued live runs first (no batch item starts while one is open), then the conversions, then the batch runs."""
    settings, engine = make_database(tmp_path)
    now = utcnow()
    with Session(engine) as session, session.begin():
        batch = queued(session, at=now)
        wire, follower = running(session, started=now), queued(session, at=now)
        for live in (wire, follower):
            live.queue_job_id = f"live_{live.job_id}"  # a live run's queue identity
        converting = indexing(session, running(session, started=now), now)
        session.add(Conversion(indexing_id=converting, status="queued", requested_at=now))
        batch_id, follower_id = batch.job_id, follower.job_id

    with TestClient(create_app(settings), headers=PORTAL_HEADERS) as client:
        body = client.get(STATUS).json()
    engine.dispose()

    assert body["queue"] == {
        "queued_runs": 2,
        "running": 2,
        "queued_conversions": 1,
        "live_slots_in_use": 1,
        "live_slots": 2,
    }
    assert [(item["kind"], item["job_id"]) for item in body["next_up"]] == [
        ("run", follower_id),
        ("conversion", None),
        ("run", batch_id),
    ]


# ---------------------------------------------------------------------------
# The worker and its threads


def add_worker(engine: Engine, now: datetime, *, started: float, stopped: float | None = None, **marks: float) -> None:
    """The worker row, its times in seconds before ``now`` (``marks``: ``dispatcher``, ``reconciler``, ``poller``)."""

    def ago(seconds: float | None) -> datetime | None:
        return None if seconds is None else now - timedelta(seconds=seconds)

    with Session(engine) as session, session.begin():
        session.add(
            Worker(
                name="worker",
                host="beamline-host",
                pid=4321,
                started_at=ago(started),
                stopped_at=ago(stopped),
                dispatcher_at=ago(marks.get("dispatcher")),
                reconciler_at=ago(marks.get("reconciler")),
                poller_at=ago(marks.get("poller")),
            )
        )


def threads(body: dict[str, Any]) -> dict[str, tuple[str, float | None]]:
    return {thread["name"]: (thread["state"], thread["late_after_seconds"]) for thread in body["worker"]["threads"]}


def test_the_worker_and_its_threads_without_remote_execution(tmp_path: Path) -> None:
    """A thread is late once its mark is older than three of its cycles (its interval, at least the heartbeat) or
    45 s, whichever is longer; the poller is ``off`` without remote execution."""
    settings, engine = make_database(tmp_path, execution={"reconcile_seconds": 60, "heartbeat_seconds": 15})
    now = utcnow()
    add_worker(engine, now, started=3600, dispatcher=2, reconciler=181)
    engine.dispose()

    body = TestClient(create_app(settings)).get(STATUS).json()

    worker = body["worker"]
    assert {key: worker[key] for key in ("state", "host", "pid", "stopped_at")} == {
        "state": "running",
        "host": "beamline-host",
        "pid": 4321,
        "stopped_at": None,
    }
    assert 3590 < (now - datetime.fromisoformat(worker["started_at"])).total_seconds() < 3610
    assert [thread["name"] for thread in worker["threads"]] == ["dispatcher", "reconciler", "poller"]
    assert threads(body) == {"dispatcher": ("ok", 45), "reconciler": ("late", 180), "poller": ("off", None)}
    marks = {thread["name"]: (thread["last_mark_at"], thread["last_mark_age_seconds"]) for thread in worker["threads"]}
    assert marks["poller"] == (None, None) and marks["dispatcher"][0].endswith("Z")
    assert 2 <= marks["dispatcher"][1] < 30 and 181 <= marks["reconciler"][1] < 210


@pytest.mark.parametrize("poll_seconds", [None, 20], ids=["settings it cannot use", "poll_seconds 20"])
def test_the_poller_with_remote_execution_on_and_no_mark_is_late_never_off(
    tmp_path: Path, poll_seconds: int | None
) -> None:
    """With remote execution on, a poller with no mark since the worker's start (settings it cannot use leave the
    worker without the thread; another poller holds the lock) is late once that start is older than its limit:
    three ``poll_seconds`` (``bdp.yaml``) or 45 s."""
    bdp = tmp_path / "bdp.yaml"
    if poll_seconds is not None:
        bdp.write_text(yaml.safe_dump({"poll_seconds": poll_seconds}))
    settings, engine = make_database(tmp_path, remote={"enabled": True, "settings_file": bdp})
    now = utcnow()
    add_worker(engine, now, started=50, dispatcher=1, reconciler=1)
    engine.dispose()

    with TestClient(create_app(settings)) as client:
        body = client.get(STATUS).json()
        health = client.get("/api/v1/health").json()

    limit = 60 if poll_seconds else 45
    assert threads(body)["poller"] == ("late" if limit < 50 else "ok", limit)
    assert (body["worker"]["state"], health["worker"]) == ("running", "running")


def test_a_stopped_worker_keeps_its_row_with_every_thread_off(tmp_path: Path) -> None:
    settings, engine = make_database(tmp_path)
    now = utcnow()
    add_worker(engine, now, started=600, stopped=5, dispatcher=6, reconciler=30)
    engine.dispose()

    body = TestClient(create_app(settings)).get(STATUS).json()

    assert (body["worker"]["state"], body["worker"]["host"]) == ("not_running", "beamline-host")
    assert body["worker"]["stopped_at"] is not None
    assert threads(body) == {  # an off thread cannot be late: no limit
        "dispatcher": ("off", None),
        "reconciler": ("off", None),
        "poller": ("off", None),
    }
    assert body["worker"]["threads"][0]["last_mark_at"] is not None


def test_no_worker_row_is_not_running(tmp_path: Path) -> None:
    settings, engine = make_database(tmp_path)
    engine.dispose()
    worker = TestClient(create_app(settings)).get(STATUS).json()["worker"]
    assert {key: worker[key] for key in ("state", "host", "pid", "started_at", "stopped_at")} == {
        "state": "not_running",
        "host": None,
        "pid": None,
        "started_at": None,
        "stopped_at": None,
    }
    assert [(thread["state"], thread["last_mark_at"]) for thread in worker["threads"]] == [("off", None)] * 3
