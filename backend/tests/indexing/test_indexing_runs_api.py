"""The indexations grid: ``GET /api/v1/indexing-runs`` (LaueGo runs) on a seeded SQLite file."""

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

RUNS = "/api/v1/indexing-runs"


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


def test_list_indexing_runs_returns_every_method_newest_first(
    seeded: tuple[TestClient, dict[str, Seeded]],
) -> None:
    client, seeded_ids = seeded
    before = utcnow()
    response = client.get(RUNS)
    after = utcnow()
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert before <= parse_z(body["as_of"]) <= after
    assert seeded_ids["i_matching"].run_id == 4  # a LaueMatching run, on the same list
    assert ([row["id"] for row in body["rows"]], body["total"]) == ([5, 4, 3, 2, 1], 5)
    by_id = {row["id"]: row for row in body["rows"]}
    assert by_id[1] == {
        "id": 1,
        "method": "lauego",
        "method_label": "LaueGo",
        "scan_number": 101,
        "sample_name": "Si",  # its scan's catalog
        "reconstruction_id": seeded_ids["r_finished"].run_id,
        "n_points": 2,
        "n_depths": None,
        "box_size": 18,
        "crystal_file": "/test/crystal.xtal",
        "author": "alice",
        "notes": None,
        "created_at": "2026-10-18T11:35:00Z",
        "status": "finished",
        "n_inputs": 2,
        "n_succeeded": 2,
        "n_failed": 0,
        "n_not_run": 0,
        "n_processed": 2,
    }
    failed = by_id[2]
    assert [failed[key] for key in ("scan_number", "reconstruction_id", "n_depths", "box_size", "crystal_file")] == [
        102,
        None,
        5,
        12,
        "/x/Si.xtal",
    ]
    assert [failed[key] for key in ("status", "n_succeeded", "n_failed", "n_not_run", "n_processed")] == [
        "failed",
        1,
        1,
        1,
        2,
    ]
    assert (by_id[3]["scan_number"], by_id[3]["reconstruction_id"], by_id[3]["status"]) == (None, None, "queued")
    # a LaueGo run without its parameters row is listed, those columns blank
    assert seeded_ids["i_no_parameters"].run_id == 5
    assert by_id[5] == {
        "id": 5,
        "method": "lauego",
        "method_label": "LaueGo",
        "scan_number": 103,
        "sample_name": None,  # a scan without a catalog
        "reconstruction_id": None,
        "n_points": None,
        "n_depths": None,
        "box_size": None,
        "crystal_file": None,
        "author": "frank",
        "notes": None,
        "created_at": "2026-10-18T10:20:00Z",
        "status": "cancelled",
        "n_inputs": 4,
        "n_succeeded": 0,
        "n_failed": 0,
        "n_not_run": 4,
        "n_processed": 0,
    }


def test_scan_id_lists_one_scans_indexations(client: TestClient) -> None:
    assert ids(client, scan_id=101) == [1]
    assert ids(client, scan_id=102) == [2]
    assert ids(client, scan_id=103) == [5, 4]  # its LaueGo run and its LaueMatching run
    assert problem_items(client.get(RUNS, params={"scan_id": 2**63})) == [(["query", "scan_id"], "less_than_equal")]


def test_list_indexing_runs_sorts_filters_and_pages(client: TestClient) -> None:
    unlinked = {"scan_number": {"type": "blank"}, "reconstruction_id": {"type": "blank"}}
    assert ids(client, filter=unlinked) == [3]
    assert ids(client, filter={"reconstruction_id": {"filterType": "number", "type": "equals", "filter": 1}}) == [1]
    assert ids(client, filter={"box_size": {"filterType": "number", "type": "lessThan", "filter": 15}}) == [2]
    assert ids(client, filter={"n_depths": {"filterType": "number", "type": "notBlank"}}) == [2]
    assert ids(client, filter={"crystal_file": {"filterType": "text", "type": "endsWith", "filter": "SI.XTAL"}}) == [2]
    assert ids(client, filter={"status": {"filterType": "text", "type": "notEqual", "filter": "failed"}}) == [
        5,
        4,
        3,
        1,
    ]
    assert ids(client, filter={"author": {"type": "startsWith", "filter": "D"}}) == [3]
    assert ids(client, filter={"sample_name": {"type": "equals", "filter": "ni"}}) == [2]
    day = {"filterType": "date", "type": "inRange", "dateFrom": "2026-10-18", "dateTo": "2026-10-18"}
    assert ids(client, filter={"created_at": day}) == [5, 4, 3, 2, 1]
    assert ids(client, filter={"n_points": {"filterType": "number", "type": "blank"}}) == [5, 4]
    assert ids(client, sort="box_size,-id") == [5, 4, 2, 3, 1]  # blank first
    assert ids(client, sort="-n_points") == [3, 1, 2, 4, 5]  # ties by ID, blank last
    assert ids(client, sort="scan_number") == [3, 1, 2, 4, 5]  # blank first
    page = client.get(RUNS, params={"offset": 2, "limit": 5}).json()
    assert ([row["id"] for row in page["rows"]], page["total"]) == ([3, 2, 1], 5)


def test_method_filters_and_sorts_the_list(client: TestClient) -> None:
    assert ids(client, filter={"method": {"type": "equals", "filter": "laue_matching"}}) == [4]
    assert ids(client, filter={"method_label": {"type": "contains", "filter": "LaueMatching"}}) == [4]
    assert ids(client, sort="method,id") == [4, 1, 2, 3, 5]


def test_status_sorts_in_lifecycle_order(client: TestClient) -> None:
    """Queued, Running, Finished, Failed, Cancelled (the lifecycle order, by status code), not alphabetically."""
    rows = get_rows(client, RUNS, sort="status")
    assert [row["id"] for row in rows] == [3, 1, 4, 2, 5]  # queued, finished (two), failed, cancelled
    assert in_lifecycle_order(rows)
    rows = get_rows(client, RUNS, sort="-status")
    assert [row["id"] for row in rows] == [5, 2, 1, 4, 3]
    assert in_lifecycle_order(rows, descending=True)


def test_list_indexing_runs_rejects_bad_grid_parameters(client: TestClient) -> None:
    assert problem_items(client.get(RUNS, params={"sort": "nope"})) == [(["query", "sort"], "unknown_field")]
    assert problem_items(client.get(RUNS, params={"limit": 0})) == [(["query", "limit"], "greater_than_equal")]


def test_changed_since_returns_active_runs_and_runs_changed_within_the_overlap(client: TestClient) -> None:
    # I1 finished at T0 - 10 min, I2 failed at T0 - 45 min; I3 (queued) is active
    within = isoformat_z(at(10) + CHANGED_SINCE_OVERLAP)
    body = client.get(RUNS, params={"changed_since": within}).json()
    assert ([row["id"] for row in body["rows"]], body["total"]) == ([3, 1], 2)
    beyond = isoformat_z(at(10) + CHANGED_SINCE_OVERLAP + timedelta(milliseconds=1))
    assert ids(client, changed_since=beyond) == [3]
    assert ids(client, changed_since=within, scan_id=101) == [1]


def test_as_of_sent_back_as_changed_since_returns_only_the_active_runs(client: TestClient) -> None:
    as_of = client.get(RUNS).json()["as_of"]
    assert ids(client, changed_since=as_of) == [3]


@pytest.mark.parametrize(("value", "code"), BAD_CHANGED_SINCE)
def test_a_bad_changed_since_is_a_422(client: TestClient, value: str, code: str) -> None:
    assert problem_items(client.get(RUNS, params={"changed_since": value})) == [(["query", "changed_since"], code)]


def test_indexing_runs_route_has_the_contract_operation_id(client: TestClient) -> None:
    operation = client.get("/api/v1/openapi.json").json()["paths"][RUNS]["get"]
    assert (operation["operationId"], operation["tags"]) == ("listIndexingRuns", ["indexing"])
    parameters = [parameter["name"] for parameter in operation["parameters"]]
    assert parameters == ["offset", "limit", "sort", "filter", "changed_since", "scan_id"]


def test_get_indexing_run_returns_the_run_its_job_and_its_stored_parameters(
    seeded: tuple[TestClient, dict[str, Seeded]],
) -> None:
    client, seeded_ids = seeded
    response = client.get(f"{RUNS}/1")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    parameters = body.pop("parameters")
    assert body == {
        "id": 1,
        "method": "lauego",
        "scan_number": 101,
        "reconstruction_id": seeded_ids["r_finished"].run_id,
        "job_id": seeded_ids["i_finished"].job_id,
        "input_path": "/data/in",
        "output_path": None,
        "author": "alice",
        "notes": None,
        "created_at": "2026-10-18T11:35:00Z",
        "laue_matching_parameters": None,
    }
    # every stored column but the key, as stored (the database, not request.json: a run migrated from v1 has them)
    assert parameters == {
        "filename_prefixes": ["test_%d.h5"],
        "threshold": 250,
        "threshold_ratio": None,
        "max_rfactor": 0.5,
        "box_size": 18,
        "max_number": 300,
        "min_separation": 20,
        "peak_shape": "Lorentzian",
        "scan_points": "1-2",
        "scan_points_len": 2,
        "depth_range": None,
        "depth_range_len": None,
        "detector_crop_x1": 0,
        "detector_crop_x2": 2047,
        "detector_crop_y1": 0,
        "detector_crop_y2": 2047,
        "min_size": 3.0,
        "max_peaks": 200,
        "smooth": False,
        "mask_file": None,
        "index_kev_max_calc": 17.2,
        "index_kev_max_test": 35.0,
        "index_angle_tolerance": 0.1,
        "index_h": 0,
        "index_k": 0,
        "index_l": 1,
        "index_cone": 72.0,
        "energy_unit": "keV",
        "exposure_unit": "sec",
        "cosmic_filter": True,
        "reciprocal_lattice_unit": "1/nm",
        "lattice_parameters_unit": "nm",
        "output_xml": "output.xml",
        "geometry_file": "/test/geometry.xml",
        "crystal_file": "/test/crystal.xtal",
        "depth": None,
        "beamline": "34ID-E",
    }
    # a run without its parameters row, and one of another method, answer with none
    for name in ("i_no_parameters", "i_matching"):
        other = client.get(f"{RUNS}/{seeded_ids[name].run_id}").json()
        assert (other["job_id"], other["parameters"]) == (seeded_ids[name].job_id, None)


def test_get_indexing_run_is_a_404_for_an_unknown_run(client: TestClient) -> None:
    response = client.get(f"{RUNS}/999")
    assert response.status_code == 404
    assert response.json()["detail"] == "No indexing run I999"
