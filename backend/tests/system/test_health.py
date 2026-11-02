"""``GET /api/v1/health``: the database and the worker (running, late or not running), no Redis."""

from __future__ import annotations

import socket
import sqlite3
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psutil
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from laueportal import __version__
from laueportal.app import create_app
from laueportal.core.db import make_engine
from laueportal.core.timeutil import utcnow
from laueportal.jobs.models import Worker
from laueportal.system import status
from tests.settings_support import create_database, make_database, make_settings

HEALTH = "/api/v1/health"


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (None, "not_running"),
        ({"started": 600, "stopped": 0, "dispatcher": 1}, "not_running"),  # its stop recorded
        ({"started": 2}, "running"),  # no mark yet: its start counts
        ({"started": 600, "reconciler": 50, "dispatcher": 44}, "running"),  # the newest mark counts
        ({"started": 600, "dispatcher": 46, "reconciler": 50, "poller": 120}, "late"),
        ({"started": 46}, "late"),
    ],
    ids=["no row", "stopped", "started", "newest mark", "marks old", "no mark since its start"],
)
def test_health_reports_the_worker_by_its_row_and_its_newest_mark(
    tmp_path: Path, row: dict[str, int] | None, expected: str
) -> None:
    """The worker is ``running`` while its newest mark (its start, before any) is at most 45 s old, then ``late``;
    ``not_running`` with no row or with ``stopped_at`` set. Values are seconds ago."""
    settings, engine = make_database(tmp_path)
    now = utcnow()
    if row is not None:
        ago = {key: now - timedelta(seconds=seconds) for key, seconds in row.items()}
        with Session(engine) as session, session.begin():
            session.add(
                Worker(
                    name="worker",
                    host="beamline-host",
                    pid=4321,
                    started_at=ago["started"],
                    stopped_at=ago.get("stopped"),
                    dispatcher_at=ago.get("dispatcher"),
                    reconciler_at=ago.get("reconciler"),
                    poller_at=ago.get("poller"),
                )
            )
    engine.dispose()

    response = TestClient(create_app(settings)).get(HEALTH)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"db": "ok", "worker": expected, "version": __version__}


@pytest.fixture
def sleeper() -> Iterator[psutil.Process]:
    process = subprocess.Popen(["sleep", "60"])
    try:
        yield psutil.Process(process.pid)
    finally:
        process.kill()
        process.wait()


def _gone_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


@pytest.mark.parametrize(
    ("process", "row_after_start", "newest_mark_age", "expected"),
    [
        ("gone", 1, 2, "not_running"),  # killed outright: no process with its pid, at once
        ("reused", -60, 600, "not_running"),  # its pid's process started after the row: another process's now
        ("reused", -60, 10, "running"),  # marks fresh: the pid alone counts (a clock stepped forward since)
        ("alive", 1, 600, "late"),  # its own process, marks old: late, as before
        ("another host", 1, 600, "late"),  # the API cannot look at it: the marks alone
    ],
    ids=["gone", "pid reused, marks late", "pid reused, marks fresh", "alive, marks late", "another host"],
)
def test_a_worker_whose_process_is_gone_on_the_apis_host_is_not_running(
    sleeper: psutil.Process, process: str, row_after_start: float, newest_mark_age: float, expected: str
) -> None:
    """Without a worker, the status page and navbar say it is not running, also after a worker killed outright (no stop
    recorded). On the API's own host, the row's pid with no process, or (once the marks are late) a process that started
    after the row's ``started_at``, is not running; elsewhere the marks decide."""
    pid = _gone_pid() if process in ("gone", "another host") else sleeper.pid
    host = "another-host" if process == "another host" else socket.gethostname()
    started_at = datetime.fromtimestamp(sleeper.create_time() + row_after_start, UTC)
    row = status.WorkerRow(host, pid, started_at, None, {"dispatcher": None, "reconciler": None, "poller": None})
    now = started_at + timedelta(seconds=newest_mark_age)
    assert status.worker_state(row, now=now) == expected


def test_health_and_the_status_page_say_not_running_once_the_workers_process_is_gone(tmp_path: Path) -> None:
    settings, engine = make_database(tmp_path)
    now = utcnow()
    with Session(engine) as session, session.begin():
        session.add(
            Worker(
                name="worker",
                host=socket.gethostname(),
                pid=_gone_pid(),
                started_at=now - timedelta(seconds=60),
                dispatcher_at=now - timedelta(seconds=2),
            )
        )
    engine.dispose()

    with TestClient(create_app(settings)) as client:
        health = client.get(HEALTH).json()
        worker = client.get("/api/v1/system/status").json()["worker"]

    assert (health["worker"], worker["state"]) == ("not_running", "not_running")
    assert [thread["state"] for thread in worker["threads"]] == ["off"] * 3


def test_health_reports_a_missing_database_without_creating_it(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    response = TestClient(create_app(settings)).get(HEALTH)
    assert response.status_code == 200
    assert response.json() == {"db": "error", "worker": None, "version": __version__}
    assert not settings.database.path.exists()


def test_a_database_without_the_worker_table_reports_no_worker(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    create_database(settings)  # a SQLite file with a probe table only
    assert TestClient(create_app(settings)).get(HEALTH).json() == {"db": "ok", "worker": None, "version": __version__}


def test_collect_health_never_raises(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, database={"path": tmp_path})  # a directory, not a database file
    engine = make_engine(settings)
    health = status.collect_health(settings, engine)
    assert (health.db, health.worker) == ("error", None)
    engine.dispose()


def test_database_probe_answers_within_its_budget_while_the_database_is_locked(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, database={"journal_mode": "delete"})
    create_database(settings)
    holder = sqlite3.connect(settings.database.path, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    engine = make_engine(settings)
    try:
        started = time.perf_counter()
        assert status.database_status(engine) == "error"
        elapsed = time.perf_counter() - started
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert elapsed < status.PROBE_TIMEOUT_SECONDS + 1, f"the database probe took {elapsed:.1f}s"
    assert status.database_status(engine) == "ok"  # and recovers once the writer is done
    engine.dispose()
