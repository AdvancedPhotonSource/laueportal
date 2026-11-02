"""The run monitor's API: ``GET /api/v1/jobs`` on a seeded SQLite file, and the controls
``POST /api/v1/jobs/{job_id}/cancel`` and ``.../move-to-front``.

The controls are ``jobs.controls``' (``test_controls.py`` covers them against the dispatcher's claim); here they
are checked as the API answers them: with no worker (the job table is the queue), for remote runs (never the
worker's), and with a burst worker that then works the queue as the controls left it.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from laueportal.app import create_app
from laueportal.core.errors import PROBLEM_JSON
from laueportal.core.paging import CHANGED_SINCE_OVERLAP
from laueportal.core.timeutil import isoformat_z, utcnow
from laueportal.jobs import state
from laueportal.jobs.models import Job
from laueportal.jobs.queue import enqueue_run
from laueportal.jobs.state import JobStatus, RunPhase
from laueportal.remote.submission import queue_identity
from tests.api_support import PORTAL_HEADERS
from tests.jobs import job_computes
from tests.run_lists_support import (
    BAD_CHANGED_SINCE,
    Seeded,
    at,
    get_rows,
    in_lifecycle_order,
    parse_z,
    problem_items,
    seed_run_lists,
)
from tests.run_support import dispatch_queue, enqueue_with, load_job, publish_wire_run
from tests.settings_support import make_database, make_settings
from tests.worker_support import Live, run_burst_worker, start_worker, wait_for

JOBS = "/api/v1/jobs"


@pytest.fixture
def seeded(tmp_path: Path) -> Iterator[tuple[TestClient, dict[str, Seeded]]]:
    settings, engine = make_database(tmp_path)
    ids = seed_run_lists(engine)
    engine.dispose()
    with TestClient(create_app(settings), headers=PORTAL_HEADERS) as client:
        yield client, ids


@pytest.fixture
def client(seeded: tuple[TestClient, dict[str, Seeded]]) -> TestClient:
    return seeded[0]


def job_ids(client: TestClient, **params: Any) -> list[int]:
    if "filter" in params and not isinstance(params["filter"], str):
        params["filter"] = json.dumps(params["filter"])
    return [row["job_id"] for row in get_rows(client, JOBS, **params)]


# ---------------------------------------------------------------------------
# GET /jobs: the run monitor


def test_list_jobs_returns_every_job_newest_first(client: TestClient) -> None:
    before = utcnow()
    response = client.get(JOBS)
    after = utcnow()
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    as_of = parse_z(body["as_of"])
    assert before <= as_of <= after
    assert ([row["job_id"] for row in body["rows"]], body["total"]) == ([10, 9, 8, 7, 6, 5, 4, 3, 2, 1], 10)
    by_id = {row["job_id"]: row for row in body["rows"]}
    assert by_id[1] == {
        "job_id": 1,
        "run": {"kind": "reconstruction", "id": 1, "label": "R1"},
        "author": "alice",
        "status": "finished",
        "phase": "finished",
        "n_inputs": 10,
        "n_succeeded": 10,
        "n_failed": 0,
        "n_not_run": 0,
        "n_processed": 10,
        "start_time": "2026-10-18T11:00:00Z",
        "duration_seconds": 1800.0,
        "message": "Finished: 10 succeeded",  # the last line, its trailing line break left out
        "scan_number": 101,
        "target": "local",
        "execution": "APS",
        "dm_state": None,
        "observation": None,
        "live": False,
        "held": False,
    }
    assert by_id[6] == {  # a remote run that failed on the APS cluster
        "job_id": 6,
        "run": {"kind": "indexing", "id": 2, "label": "I2"},
        "author": "bob",
        "status": "failed",
        "phase": "failed",
        "n_inputs": 3,
        "n_succeeded": 1,
        "n_failed": 1,
        "n_not_run": 1,
        "n_processed": 2,
        "start_time": "2026-10-18T11:10:00Z",
        "duration_seconds": 300.0,
        "message": "PBS exit status 1",
        "scan_number": 102,
        "target": "aps",
        "execution": "APS",
        "dm_state": "failed",
        "observation": None,
        "live": False,
        "held": False,
    }
    queued_remote = by_id[3]
    keys = ("run", "status", "target", "execution", "dm_state", "scan_number")
    assert [queued_remote[key] for key in keys] == [
        {"kind": "reconstruction", "id": 3, "label": "R3"},
        "queued",
        "crux",
        "ALCF",
        "pending",
        None,
    ]
    # the job page's observation (jobs.service.observation_view), for the Status cell: stale, over 90 s old
    assert queued_remote["observation"] == {
        "phase": "uplink",
        "detail": "Transferring inputs",
        "pbs_id": None,
        "stale": True,
        "observed_at": isoformat_z(at(1)),
        "phase_since": None,
        "flow_state": None,
    }
    assert by_id[9] == {  # a job without a run (created, not yet published)
        "job_id": 9,
        "run": None,
        "author": None,
        "status": "queued",
        "phase": "created",
        "n_inputs": 0,
        "n_succeeded": 0,
        "n_failed": 0,
        "n_not_run": 0,
        "n_processed": 0,
        "start_time": None,
        "duration_seconds": None,
        "message": None,
        "scan_number": None,
        "target": "local",
        "execution": "APS",
        "dm_state": None,
        "observation": None,
        "live": False,
        "held": False,
    }
    assert by_id[4]["message"] == "Cancelled before the run started"
    assert by_id[4]["duration_seconds"] is None  # never started
    # a running run's duration runs to the page's as_of
    running = by_id[2]
    expected = (as_of - parse_z(running["start_time"])).total_seconds()
    assert running["status"] == "running"
    assert running["duration_seconds"] == pytest.approx(expected, abs=0.002)


def test_an_open_live_run_is_live_in_the_list(seeded: tuple[TestClient, dict[str, Seeded]]) -> None:
    """``live`` (the Run Monitor's Stop wording): the live queue identity (``live_<job id>``) of a queued or running
    job; not a batch run's, nor a live identity once the job has ended (the list reads no ``live.json``)."""
    client, _ = seeded
    with Session(client.app.state.engine) as session, session.begin():
        for job_id in (1, 2, 9):  # finished, running, queued
            job = session.get(Job, job_id)
            assert job is not None
            job.queue_job_id = f"live_{job_id}"
        other = session.get(Job, 7)  # queued, another job's live identity
        assert other is not None
        other.queue_job_id = "live_2"
    live = {row["job_id"]: row["live"] for row in client.get(JOBS).json()["rows"]}
    assert [job_id for job_id, value in live.items() if value] == [9, 2]


def test_list_jobs_sorts_and_filters(client: TestClient) -> None:
    assert job_ids(client, filter={"run": {"filterType": "text", "type": "equals", "filter": "i2"}}) == [6]
    assert job_ids(client, filter={"run": {"type": "startsWith", "filter": "R"}}) == [4, 3, 2, 1]
    assert job_ids(client, filter={"run": {"type": "blank"}}) == [9]
    assert job_ids(client, filter={"target": {"type": "notEqual", "filter": "local"}}) == [6, 3]
    assert job_ids(client, filter={"dm_state": {"type": "equals", "filter": "PENDING"}}) == [3]
    assert job_ids(client, filter={"scan_number": {"filterType": "number", "type": "equals", "filter": 102}}) == [6, 4]
    assert job_ids(client, filter={"author": {"type": "blank"}}) == [9, 3]
    assert job_ids(client, filter={"phase": {"type": "equals", "filter": "created"}}) == [9]
    assert job_ids(client, filter={"n_not_run": {"type": "greaterThan", "filter": 0}}) == [10, 6, 4]
    duration = {"filterType": "number", "type": "inRange", "filter": 60, "filterTo": 600}
    assert job_ids(client, filter={"duration_seconds": duration}) == [8, 6, 5]
    started = {"filterType": "date", "type": "equals", "dateFrom": "2026-10-18T00:00:00Z"}
    assert job_ids(client, filter={"start_time": started}) == [8, 6, 5, 2, 1]
    assert job_ids(client, sort="status,-job_id") == [9, 7, 3, 2, 8, 5, 1, 6, 10, 4]
    assert job_ids(client, sort="-duration_seconds") == [2, 1, 5, 6, 8, 3, 4, 7, 9, 10]  # blanks last, by ID


def test_execution_filters_and_sorts_the_names_shown(client: TestClient) -> None:
    """The Execution column filters and sorts the names the run monitor shows (``core.params.BACKEND_NAMES``), not the
    API's values: ALCF is the ``crux`` run, APS the ``local`` and ``aps`` ones."""
    assert job_ids(client, filter={"execution": {"type": "equals", "filter": "ALCF"}}) == [3]
    assert job_ids(client, filter={"execution": {"type": "equals", "filter": "APS"}}) == [10, 9, 8, 7, 6, 5, 4, 2, 1]
    assert job_ids(client, sort="execution,-job_id") == [3, 10, 9, 8, 7, 6, 5, 4, 2, 1]  # the aps run 6 among APS
    assert job_ids(client, sort="job_id", limit=3, offset=3) == [4, 5, 6]


def test_status_sorts_in_lifecycle_order(client: TestClient) -> None:
    """Queued, Running, Finished, Failed, Cancelled (the lifecycle order, by status code), not alphabetically; ties by
    job ID."""
    rows = get_rows(client, JOBS, sort="status")
    assert [row["job_id"] for row in rows] == [3, 7, 9, 2, 1, 5, 8, 6, 4, 10]
    assert [row["status"] for row in rows] == ["queued"] * 3 + ["running"] + ["finished"] * 3 + ["failed"] + [
        "cancelled"
    ] * 2
    assert in_lifecycle_order(rows)
    rows = get_rows(client, JOBS, sort="-status")
    assert [row["job_id"] for row in rows] == [4, 10, 6, 1, 5, 8, 2, 3, 7, 9]
    assert in_lifecycle_order(rows, descending=True)


def test_list_jobs_rejects_what_does_not_sort_or_filter(client: TestClient) -> None:
    # the run reference filters on its label but does not sort; the message's last line is no grid field
    assert problem_items(client.get(JOBS, params={"sort": "run"})) == [(["query", "sort"], "not_sortable")]
    assert problem_items(client.get(JOBS, params={"sort": "message"})) == [(["query", "sort"], "unknown_field")]
    message = json.dumps({"message": {"type": "contains", "filter": "PBS"}})
    assert problem_items(client.get(JOBS, params={"filter": message})) == [
        (["query", "filter", "message"], "unknown_field")
    ]
    assert problem_items(client.get(JOBS, params={"scan_id": 101})) == [(["query", "scan_id"], "extra_forbidden")]


def test_changed_since_returns_active_jobs_and_jobs_changed_within_the_overlap(client: TestClient) -> None:
    # active: 2 (running), 3, 7 and 9 (queued); changed: 5 at T0 - 10 min, 8 at T0 - 15 min, older ones before
    within = isoformat_z(at(15) + CHANGED_SINCE_OVERLAP)
    body = client.get(JOBS, params={"changed_since": within}).json()
    assert ([row["job_id"] for row in body["rows"]], body["total"]) == ([9, 8, 7, 5, 3, 2], 6)
    beyond = isoformat_z(at(15) + CHANGED_SINCE_OVERLAP + timedelta(microseconds=1))
    assert job_ids(client, changed_since=beyond) == [9, 7, 5, 3, 2]
    assert job_ids(client, changed_since=within, filter={"target": {"type": "equals", "filter": "local"}}) == [
        9,
        8,
        7,
        5,
        2,
    ]


def test_a_change_written_while_the_previous_poll_ran_is_in_the_next(
    seeded: tuple[TestClient, dict[str, Seeded]],
) -> None:
    """The poll sends back the previous page's ``as_of``; a change whose time was taken up to 2 s before it (e.g.
    by a writer that committed after the previous poll read) is in the next poll, an older one is not."""
    client, ids = seeded
    previous = client.get(JOBS).json()
    as_of = parse_z(previous["as_of"])
    with Session(client.app.state.engine) as session, session.begin():
        running = session.get(Job, ids["r_running"].job_id)
        assert running is not None
        state.finalize(running, status=JobStatus.FINISHED, now=as_of - timedelta(seconds=1))
        queued = session.get(Job, ids["i_queued"].job_id)
        assert queued is not None
        state.finalize(queued, status=JobStatus.CANCELLED, now=as_of - timedelta(seconds=3))
    rows = get_rows(client, JOBS, changed_since=previous["as_of"])
    assert [(row["job_id"], row["status"]) for row in rows] == [(9, "queued"), (3, "queued"), (2, "finished")]


def test_as_of_sent_back_as_changed_since_returns_only_the_active_jobs(client: TestClient) -> None:
    as_of = client.get(JOBS).json()["as_of"]
    assert job_ids(client, changed_since=as_of) == [9, 7, 3, 2]


@pytest.mark.parametrize(("value", "code"), BAD_CHANGED_SINCE)
def test_a_bad_changed_since_is_a_422(client: TestClient, value: str, code: str) -> None:
    assert problem_items(client.get(JOBS, params={"changed_since": value})) == [(["query", "changed_since"], code)]


def test_a_page_of_jobs_takes_two_queries(client: TestClient) -> None:
    engine: Engine = client.app.state.engine
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        assert len(get_rows(client, JOBS, limit=1000)) == 10
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(statements) == 2  # the total and the page; the run reference and the message need none


# ---------------------------------------------------------------------------
# The controls, with no worker and for remote runs


@pytest.fixture
def offline(tmp_path: Path) -> Iterator[tuple[Engine, TestClient]]:
    """A database and the API, with no worker."""
    settings, engine = make_database(tmp_path)
    with TestClient(create_app(settings), headers=PORTAL_HEADERS) as client:
        yield engine, client
    engine.dispose()


def _queued_local(engine: Engine, tmp_path: Path, name: str) -> int:
    run = publish_wire_run(engine, tmp_path, name=name)
    enqueue_with(engine, run.job_id)  # Queued: the row is its queue entry
    return run.job_id


def _submitted_remote(engine: Engine, tmp_path: Path, name: str, *, running: bool = False) -> int:
    """A remote run as ``remote.submission.submit_run`` leaves it (queued with its submission), maybe running."""
    run = publish_wire_run(engine, tmp_path, count=2, name=name)
    with Session(engine) as session, session.begin():
        job = session.get(Job, run.job_id)
        assert job is not None
        submission = str(uuid.UUID(int=run.job_id))
        job.backend, job.submission_id, job.external_state = "crux", submission, "pending"
        state.mark_queued(job, queue_identity(submission))
        if running:
            state.mark_running(job)
    return run.job_id


def _set_running(engine: Engine, job_id: int) -> None:
    with Session(engine) as session, session.begin():
        job = session.get(Job, job_id)
        assert job is not None
        state.mark_running(job)


def test_cancel_a_queued_run_cancels_it_at_once_with_no_worker(
    offline: tuple[Engine, TestClient], tmp_path: Path
) -> None:
    engine, client = offline
    job_id = _queued_local(engine, tmp_path, "queued")
    response = client.post(f"{JOBS}/{job_id}/cancel")
    assert response.status_code == 200, response.text
    assert response.json() == {"job_id": job_id, "state": "cancelled", "n_pending": 3}
    job = load_job(engine, job_id)
    assert (job.status, job.phase, job.n_not_run) == (JobStatus.CANCELLED, RunPhase.CANCELLED, 3)


FOREIGN_FORM = {  # an auto-submitted form on a page of another origin
    "content-type": "application/x-www-form-urlencoded",
    "origin": "http://127.0.0.1:2104",
    "sec-fetch-site": "same-site",
    "sec-fetch-mode": "navigate",
}
FOREIGN_FETCH = {**FOREIGN_FORM, "content-type": "text/plain;charset=UTF-8", "sec-fetch-mode": "no-cors"}


def test_a_stop_or_move_to_front_from_another_origin_changes_nothing(
    offline: tuple[Engine, TestClient], tmp_path: Path
) -> None:
    """A page on another origin tries to make the browser cancel a run and move another to the front. Neither shape it
    can send carries X-Laue-Portal: 1, so both are 403s, also with the header from another origin."""
    engine, client = offline
    job_id = _queued_local(engine, tmp_path, "queued")
    with TestClient(client.app) as foreign:
        responses = [
            foreign.post(f"{JOBS}/{job_id}/cancel", content=b"", headers=FOREIGN_FORM),
            foreign.post(f"{JOBS}/{job_id}/move-to-front", content=b"", headers=FOREIGN_FETCH),
            foreign.post(f"{JOBS}/{job_id}/cancel"),
        ]
    responses.append(client.post(f"{JOBS}/{job_id}/cancel", headers={"sec-fetch-site": "cross-site"}))
    for response in responses:
        assert response.status_code == 403, response.text
        assert response.headers["content-type"] == PROBLEM_JSON
        assert response.json()["title"] == "Cross-site request refused"
    job = load_job(engine, job_id)
    assert (job.status, job.phase, job.cancel_requested_at) == (JobStatus.QUEUED, RunPhase.QUEUED, None)


def test_cancel_a_running_run_requests_the_stop(offline: tuple[Engine, TestClient], tmp_path: Path) -> None:
    engine, client = offline
    job_id = _queued_local(engine, tmp_path, "running")
    _set_running(engine, job_id)
    response = client.post(f"{JOBS}/{job_id}/cancel")
    assert response.json() == {"job_id": job_id, "state": "requested", "n_pending": 3}
    job = load_job(engine, job_id)
    assert (job.status, job.cancel_requested_at is not None) == (JobStatus.RUNNING, True)
    # the run monitor shows it still running; it ends when the executor has drained it
    [row] = get_rows(client, JOBS, filter=json.dumps({"job_id": {"type": "equals", "filter": job_id}}))
    assert (row["status"], row["phase"]) == ("running", "running")


def test_cancel_an_ended_run_changes_nothing(offline: tuple[Engine, TestClient], tmp_path: Path) -> None:
    engine, client = offline
    job_id = _queued_local(engine, tmp_path, "ended")
    first = client.post(f"{JOBS}/{job_id}/cancel").json()
    assert first["state"] == "cancelled"
    assert client.post(f"{JOBS}/{job_id}/cancel").json() == {
        "job_id": job_id,
        "state": "already_terminal",
        "n_pending": 0,
    }


def test_the_controls_answer_an_unknown_job_with_a_404(offline: tuple[Engine, TestClient]) -> None:
    _, client = offline
    for action in ("cancel", "move-to-front"):
        response = client.post(f"{JOBS}/999/{action}")
        assert (response.status_code, response.headers["content-type"]) == (404, PROBLEM_JSON)
        assert response.json() == {
            "type": "about:blank",
            "title": "Job not found",
            "status": 404,
            "detail": "No job 999",
        }
        assert problem_items(client.post(f"{JOBS}/{2**63}/{action}")) == [(["path", "job_id"], "less_than_equal")]
        assert problem_items(client.post(f"{JOBS}/R3/{action}")) == [(["path", "job_id"], "int_parsing")]


def test_move_to_front_with_no_worker_moves_the_run_first(offline: tuple[Engine, TestClient], tmp_path: Path) -> None:
    """The job table is the queue: the move needs no worker."""
    engine, client = offline
    first, job_id = _queued_local(engine, tmp_path, "first"), _queued_local(engine, tmp_path, "queued")
    response = client.post(f"{JOBS}/{job_id}/move-to-front")
    assert (response.status_code, response.content) == (204, b"")
    assert dispatch_queue(engine) == [("run", job_id), ("run", first)]
    job = load_job(engine, job_id)
    assert (job.status, job.phase, job.moved_to_front_at is not None) == (JobStatus.QUEUED, RunPhase.QUEUED, True)


def test_move_to_front_of_a_job_that_is_not_queued_is_a_409(offline: tuple[Engine, TestClient], tmp_path: Path) -> None:
    engine, client = offline
    running = _queued_local(engine, tmp_path, "running")
    _set_running(engine, running)
    response = client.post(f"{JOBS}/{running}/move-to-front")
    assert response.status_code == 409
    assert response.json()["title"] == "Job not queued"
    assert response.json()["detail"] == (
        f"Job {running} is not waiting in the queue (phase running): only a queued job moves"
    )
    published = publish_wire_run(engine, tmp_path, name="published").job_id  # not handed to the queue yet
    assert client.post(f"{JOBS}/{published}/move-to-front").json()["title"] == "Job not queued"


def test_stopping_a_remote_run_is_requested_for_the_poller(offline: tuple[Engine, TestClient], tmp_path: Path) -> None:
    """A submitted remote run is never the worker's: the stop is recorded and the remote poller delivers it."""
    engine, client = offline
    queued = _submitted_remote(engine, tmp_path, "queued")
    running = _submitted_remote(engine, tmp_path, "running", running=True)
    for job_id, status in ((queued, JobStatus.QUEUED), (running, JobStatus.RUNNING)):
        response = client.post(f"{JOBS}/{job_id}/cancel")
        assert response.json() == {"job_id": job_id, "state": "requested", "n_pending": 2}
        job = load_job(engine, job_id)
        assert (job.status, job.cancel_requested_at is not None) == (status, True)


def test_move_to_front_of_a_remote_run_is_a_409_that_names_its_scheduler(
    offline: tuple[Engine, TestClient], tmp_path: Path
) -> None:
    engine, client = offline
    queued = _submitted_remote(engine, tmp_path, "queued")
    response = client.post(f"{JOBS}/{queued}/move-to-front")
    assert response.status_code == 409
    assert response.json() == {
        "type": "about:blank",
        "title": "Job runs remotely",
        "status": 409,
        "detail": f"Job {queued} runs on a remote target, whose scheduler orders it: it cannot be moved to the front",
    }
    running = _submitted_remote(engine, tmp_path, "running", running=True)
    assert client.post(f"{JOBS}/{running}/move-to-front").json()["title"] == "Job not queued"


def test_job_routes_have_the_contract_operation_ids(client: TestClient) -> None:
    paths = client.get("/api/v1/openapi.json").json()["paths"]
    operations = {
        (method, path): (operation["operationId"], operation["tags"])
        for path, item in paths.items()
        for method, operation in item.items()
        if path.startswith(JOBS)
    }
    assert operations == {
        ("get", JOBS): ("listJobs", ["jobs"]),
        ("get", f"{JOBS}/{{job_id}}"): ("getJob", ["jobs"]),
        ("get", f"{JOBS}/{{job_id}}/failures"): ("listJobFailures", ["jobs"]),
        ("get", f"{JOBS}/{{job_id}}/log"): ("getJobLog", ["jobs"]),
        ("post", f"{JOBS}/{{job_id}}/cancel"): ("cancelJob", ["jobs"]),
        ("post", f"{JOBS}/{{job_id}}/move-to-front"): ("moveJobToFront", ["jobs"]),
        ("post", f"{JOBS}/{{job_id}}/stop-now"): ("stopJobNow", ["jobs"]),
    }
    assert [parameter["name"] for parameter in paths[JOBS]["get"]["parameters"]] == [
        "offset",
        "limit",
        "sort",
        "filter",
        "changed_since",
    ]
    # 403: the request guard's refusal of a cross-site write, documented on every write.
    assert list(paths[f"{JOBS}/{{job_id}}/move-to-front"]["post"]["responses"]) == ["204", "422", "403"]
    # The job page: the job's ID, then its paging (failures) or tail (log) parameters.
    parameters = {
        suffix: [parameter["name"] for parameter in paths[f"{JOBS}/{{job_id}}{suffix}"]["get"]["parameters"]]
        for suffix in ("", "/failures", "/log")
    }
    assert parameters == {"": ["job_id"], "/failures": ["job_id", "offset", "limit"], "/log": ["job_id", "tail"]}


# ---------------------------------------------------------------------------
# The controls with a burst worker


@pytest.fixture
def live_client(tmp_path: Path, live: Live) -> Iterator[TestClient]:
    """The API on ``live``'s database."""
    settings = make_settings(tmp_path)
    assert settings.database.path == live.database
    with TestClient(create_app(settings), headers=PORTAL_HEADERS) as client:
        yield client


def _enqueue(live: Live, tmp_path: Path, name: str, **options: Any) -> int:
    run = publish_wire_run(live.engine, tmp_path, name=name, **options)
    enqueue_run(run.job_id, engine=live.engine)
    return run.job_id


def test_cancel_takes_a_queued_run_out_of_the_queue_and_the_burst_worker_runs_the_rest(
    tmp_path: Path, live: Live, live_client: TestClient
) -> None:
    first, second = _enqueue(live, tmp_path, "first"), _enqueue(live, tmp_path, "second")

    response = live_client.post(f"{JOBS}/{first}/cancel")

    assert response.json() == {"job_id": first, "state": "cancelled", "n_pending": 3}
    assert dispatch_queue(live.engine) == [("run", second)]
    worker = run_burst_worker(live.config, log=tmp_path / "worker.log", test_computes=True)
    assert worker.process.returncode == 0, worker.log_text()
    cancelled, finished = load_job(live.engine, first), load_job(live.engine, second)
    assert (cancelled.status, cancelled.phase, cancelled.start_time) == (JobStatus.CANCELLED, RunPhase.CANCELLED, None)
    assert (finished.status, finished.n_succeeded) == (JobStatus.FINISHED, 3)
    rows = {row["job_id"]: row["status"] for row in get_rows(live_client, JOBS)}
    assert rows == {second: "finished", first: "cancelled"}


def test_cancel_while_the_burst_worker_runs_the_run_asks_it_to_stop(
    tmp_path: Path, live: Live, live_client: TestClient
) -> None:
    job_id = _enqueue(live, tmp_path, "blocking", count=2, notes=job_computes.BLOCK)
    marker = Path(load_job(live.engine, job_id).run_directory) / job_computes.COMPUTE_MARKER
    worker = start_worker(live.config, log=tmp_path / "worker.log", burst=True, test_computes=True)
    try:
        assert wait_for(marker.exists, timeout=60), worker.log_text()
        compute = json.loads(marker.read_text())
        worker.extra_pids += [compute["job_process"], compute["grandchild"]]

        response = live_client.post(f"{JOBS}/{job_id}/cancel")

        assert response.json() == {"job_id": job_id, "state": "requested", "n_pending": 2}
        assert worker.wait(60) == 0, worker.log_text()
    finally:
        worker.cleanup()
    job = load_job(live.engine, job_id)
    assert (job.status, job.phase, job.n_not_run) == (JobStatus.CANCELLED, RunPhase.CANCELLED, 2)


def test_move_to_front_reorders_the_queue_and_the_burst_worker_follows_it(
    tmp_path: Path, live: Live, live_client: TestClient
) -> None:
    first, second, third = (_enqueue(live, tmp_path, name) for name in ("first", "second", "third"))

    response = live_client.post(f"{JOBS}/{third}/move-to-front")

    assert (response.status_code, response.content) == (204, b"")
    assert dispatch_queue(live.engine) == [("run", job_id) for job_id in (third, first, second)]
    worker = run_burst_worker(live.config, log=tmp_path / "worker.log", test_computes=True)
    assert worker.process.returncode == 0, worker.log_text()
    jobs = [load_job(live.engine, job_id) for job_id in (third, first, second)]
    assert [job.status for job in jobs] == [JobStatus.FINISHED] * 3
    started = [job.start_time for job in jobs]
    assert started == sorted(started), "the worker did not take the runs in the order the move left"
