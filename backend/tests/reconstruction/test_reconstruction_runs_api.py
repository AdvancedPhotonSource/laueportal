"""The reconstructions grid: ``GET /api/v1/reconstruction-runs`` on a seeded SQLite file."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from laueportal.app import create_app
from laueportal.core.paging import CHANGED_SINCE_OVERLAP
from laueportal.core.timeutil import isoformat_z, utcnow
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
from tests.settings_support import make_database

RUNS = "/api/v1/reconstruction-runs"


@pytest.fixture
def seeded(tmp_path: Path) -> Iterator[tuple[TestClient, dict[str, Seeded]]]:
    settings, engine = make_database(tmp_path)
    ids = seed_run_lists(engine)
    engine.dispose()
    with TestClient(create_app(settings)) as client:
        yield client, ids


@pytest.fixture
def client(seeded: tuple[TestClient, dict[str, Seeded]]) -> TestClient:
    return seeded[0]


def ids(client: TestClient, **params: Any) -> list[int]:
    if "filter" in params and not isinstance(params["filter"], str):
        params["filter"] = json.dumps(params["filter"])
    return [row["id"] for row in get_rows(client, RUNS, **params)]


def test_list_reconstruction_runs_returns_the_grid_rows_newest_first(client: TestClient) -> None:
    before = utcnow()
    response = client.get(RUNS)
    after = utcnow()
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert before <= parse_z(body["as_of"]) <= after
    assert body["total"] == 4
    assert [row["id"] for row in body["rows"]] == [4, 3, 2, 1]
    by_id = {row["id"]: row for row in body["rows"]}
    assert by_id[1] == {
        "id": 1,
        "method": "wire",
        "scan_number": 101,
        "sample_name": "Si",  # its scan's catalog
        "n_points": 10,
        "percent_brightest": 10.0,
        "depth_start": -10.0,
        "depth_end": 10.0,
        "author": "alice",
        "notes": "first pass",
        "created_at": "2026-10-18T10:50:00Z",
        "start_time": "2026-10-18T11:00:00Z",
        "finish_time": "2026-10-18T11:30:00Z",
        "status": "finished",
        "n_inputs": 10,
        "n_succeeded": 10,
        "n_failed": 0,
        "n_not_run": 0,
        "n_processed": 10,
    }
    running = by_id[2]
    assert (running["status"], running["start_time"], running["finish_time"]) == (
        "running",
        "2026-10-18T11:55:00Z",
        None,
    )
    assert [running[key] for key in ("n_inputs", "n_succeeded", "n_failed", "n_not_run", "n_processed")] == [
        10,
        3,
        1,
        0,
        4,
    ]
    assert (by_id[3]["scan_number"], by_id[3]["sample_name"], by_id[3]["status"]) == (None, None, "queued")  # unlinked
    # a run without its wire parameters row is listed, those columns blank
    cancelled = by_id[4]
    assert [cancelled[key] for key in ("n_points", "percent_brightest", "depth_start", "depth_end")] == [None] * 4
    assert (cancelled["status"], cancelled["start_time"], cancelled["n_not_run"]) == ("cancelled", None, 6)


def test_scan_id_lists_one_scans_reconstructions(client: TestClient) -> None:
    assert ids(client, scan_id=101) == [2, 1]
    assert ids(client, scan_id=102) == [4]
    body = client.get(RUNS, params={"scan_id": 999}).json()
    assert (body["rows"], body["total"]) == ([], 0)
    assert ids(client, scan_id=101, filter={"status": {"type": "equals", "filter": "finished"}}) == [1]
    assert problem_items(client.get(RUNS, params={"scan_id": -1})) == [(["query", "scan_id"], "greater_than_equal")]
    assert problem_items(client.get(RUNS, params={"scan_id": "SN101"})) == [(["query", "scan_id"], "int_parsing")]


def test_list_reconstruction_runs_sorts_filters_and_pages(client: TestClient) -> None:
    assert ids(client, sort="author") == [3, 1, 2, 4]  # blank first, then alice, bob, carol
    assert ids(client, sort="-n_processed,id") == [1, 2, 3, 4]
    assert ids(client, sort="-start_time") == [2, 1, 3, 4]  # blanks last descending, then by ID
    assert ids(client, filter={"scan_number": {"filterType": "number", "type": "blank"}}) == [3]  # "Unlinked"
    assert ids(client, filter={"status": {"filterType": "text", "type": "equals", "filter": "Running"}}) == [2]
    assert ids(client, filter={"n_processed": {"filterType": "number", "type": "greaterThan", "filter": 0}}) == [2, 1]
    assert ids(client, filter={"n_points": {"filterType": "number", "type": "blank"}}) == [4]
    assert ids(client, filter={"notes": {"filterType": "text", "type": "contains", "filter": "PASS"}}) == [1]
    assert ids(client, filter={"sample_name": {"filterType": "text", "type": "contains", "filter": "n"}}) == [4]
    assert ids(client, filter={"method": {"filterType": "text", "type": "equals", "filter": "wire"}}) == [4, 3, 2, 1]
    assert ids(client, filter={"finish_time": {"filterType": "date", "type": "equals", "dateFrom": "2026-10-18"}}) == [
        4,
        1,
    ]
    created = {"filterType": "date", "type": "greaterThan", "dateFrom": "2026-10-17T00:00:00Z"}
    depth = {"filterType": "number", "type": "inRange", "filter": -10, "filterTo": 10}
    assert ids(client, filter={"created_at": created, "depth_start": depth}) == [3, 2, 1]
    page = client.get(RUNS, params={"offset": 1, "limit": 2}).json()
    assert ([row["id"] for row in page["rows"]], page["total"]) == ([3, 2], 4)


def test_status_sorts_in_lifecycle_order(client: TestClient) -> None:
    """Queued, Running, Finished, Failed, Cancelled (the lifecycle order, by status code), not alphabetically."""
    rows = get_rows(client, RUNS, sort="status")
    assert [row["id"] for row in rows] == [3, 2, 1, 4]  # queued, running, finished, cancelled
    assert in_lifecycle_order(rows)
    rows = get_rows(client, RUNS, sort="-status")
    assert [row["id"] for row in rows] == [4, 1, 2, 3]
    assert in_lifecycle_order(rows, descending=True)


def test_list_reconstruction_runs_rejects_bad_grid_parameters(client: TestClient) -> None:
    assert problem_items(client.get(RUNS, params={"sort": "job_id"})) == [(["query", "sort"], "unknown_field")]
    wrong_type = json.dumps({"status": {"filterType": "number", "type": "equals", "filter": 1}})
    assert problem_items(client.get(RUNS, params={"filter": wrong_type})) == [
        (["query", "filter", "status", "filterType"], "filter_type")
    ]
    assert problem_items(client.get(RUNS, params={"run": "R1"})) == [(["query", "run"], "extra_forbidden")]


def test_changed_since_returns_active_runs_and_runs_changed_within_the_overlap(client: TestClient) -> None:
    # R1 finished at T0 - 30 min; R4 was cancelled at T0 - 40 min; R2 (running) and R3 (queued) are active
    finished_at = at(30)
    within = isoformat_z(finished_at + CHANGED_SINCE_OVERLAP)
    body = client.get(RUNS, params={"changed_since": within}).json()
    assert ([row["id"] for row in body["rows"]], body["total"]) == ([3, 2, 1], 3)
    beyond = isoformat_z(finished_at + CHANGED_SINCE_OVERLAP + timedelta(milliseconds=1))
    assert ids(client, changed_since=beyond) == [3, 2]
    assert ids(client, changed_since=isoformat_z(at(41))) == [4, 3, 2, 1]
    # with the other parameters, and as another zone writes the same instant
    assert ids(client, changed_since="2026-10-18T06:30:02-05:00", scan_id=101, sort="id") == [1, 2]


def test_as_of_sent_back_as_changed_since_returns_only_the_active_runs(client: TestClient) -> None:
    as_of = client.get(RUNS).json()["as_of"]
    assert ids(client, changed_since=as_of) == [3, 2]


@pytest.mark.parametrize(("value", "code"), BAD_CHANGED_SINCE)
def test_a_bad_changed_since_is_a_422(client: TestClient, value: str, code: str) -> None:
    assert problem_items(client.get(RUNS, params={"changed_since": value})) == [(["query", "changed_since"], code)]


def test_reconstruction_runs_route_has_the_contract_operation_id(client: TestClient) -> None:
    operation = client.get("/api/v1/openapi.json").json()["paths"][RUNS]["get"]
    assert (operation["operationId"], operation["tags"]) == ("listReconstructionRuns", ["reconstruction"])
    parameters = [parameter["name"] for parameter in operation["parameters"]]
    assert parameters == ["offset", "limit", "sort", "filter", "changed_since", "scan_id"]
