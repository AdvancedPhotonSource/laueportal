"""The scans API: the grid list, the detail, the catalog and its notes, on a seeded SQLite file."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, inspect, select
from sqlalchemy.orm import Session

from laueportal.app import create_app
from laueportal.core.db import make_engine
from laueportal.core.errors import PROBLEM_JSON
from laueportal.core.settings import Settings
from laueportal.core.timeutil import utcnow
from laueportal.scans.importer import build_technique_strings
from laueportal.scans.models import Catalog, Metadata, Scan
from laueportal.scans.schemas import ScanDimension, ScanMetadata
from tests.api_support import PORTAL_HEADERS
from tests.conftest import create_test_metadata
from tests.settings_support import make_database

SCANS = "/api/v1/scans"

SAMPLE_X = "34ide:mxv:c0:m1.VAL"
SAMPLE_Y = "34ide:mxv:c0:m3.VAL"
DEPTH = "34ide:t80:c0:m1.VAL"
ENERGY = "34ide:monoE:keV.VAL"
UNLISTED = "34ide:unknown:m9.VAL"


def scan_rows(scan_number: int, *rows: list[str], ranges: str = "-10 10 1") -> list[Scan]:
    """Scan rows in log order (the outer dimension first), each moving the positioners whose PVs it lists."""
    result = []
    for position, pvs in enumerate(rows):
        values: dict[str, Any] = {
            "scan_number": scan_number,
            "scan_dim": len(rows) - position,
            "scan_npts": 21,
            "scan_cpt": 21 - position,
            "scan_after": "Prior",
        }
        for slot in (1, 2, 3, 4):
            pv = pvs[slot - 1] if slot <= len(pvs) else ""
            values[f"scan_positioner{slot}_pv"] = pv
            values[f"scan_positioner{slot}_ar"] = "relative" if pv else ""
            values[f"scan_positioner{slot}_mode"] = "linear" if pv else ""
            values[f"scan_positioner{slot}"] = ranges if pv else ""
            values[f"scan_detector_trig{slot}_pv"] = "34ide:det:Acquire" if slot == 1 else ""
            values[f"scan_detector_trig{slot}_val"] = "1" if slot == 1 else ""
        result.append(Scan(**values))
    return result


def add_scan(
    session: Session,
    scan_number: int,
    *rows: list[str],
    user: str = "alice",
    day: int = 5,
    catalog: dict[str, Any] | None = None,
) -> None:
    metadata = create_test_metadata(scan_number)
    metadata.user_name = user
    metadata.time = datetime(2026, 1, day, 10, 0, tzinfo=UTC)
    metadata.source_id_gap = 11.5
    metadata.source_id_taper = 0.25
    session.add(metadata)
    session.flush()
    session.add_all(scan_rows(scan_number, *rows))
    if catalog is not None:
        values = {"filefolder": f"/data/{scan_number}", "filename_prefix": [f"s{scan_number}_%d"], **catalog}
        session.add(Catalog(scan_number=scan_number, **values))


def seed(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        add_scan(
            session,
            1001,
            [SAMPLE_Y],
            [SAMPLE_X],
            catalog={"aperture": "wire", "sample_name": "Si", "notes": "first note"},
        )
        add_scan(
            session,
            1002,
            [SAMPLE_X],
            [DEPTH],
            user="bob",
            day=6,
            catalog={"aperture": "mask", "sample_name": "Ni", "notes": None},
        )
        add_scan(session, 1003, [ENERGY, UNLISTED], day=7)
        add_scan(session, 1004, user="carol", day=8, catalog={"aperture": "None", "sample_name": None, "notes": ""})
        add_scan(
            session,
            1005,
            [DEPTH],
            [SAMPLE_X, SAMPLE_Y],
            user="bob",
            day=9,
            catalog={"aperture": "wire", "sample_name": "Al", "notes": "x"},
        )


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    return root


@pytest.fixture
def seeded(tmp_path: Path, data_root: Path) -> Iterator[tuple[Settings, TestClient]]:
    settings, engine = make_database(tmp_path, files={"roots": {"data": data_root}})
    seed(engine)
    engine.dispose()
    with TestClient(create_app(settings), headers=PORTAL_HEADERS) as client:
        yield settings, client


@pytest.fixture
def client(seeded: tuple[Settings, TestClient]) -> TestClient:
    return seeded[1]


def rows(client: TestClient, **params: Any) -> list[int]:
    if "filter" in params and not isinstance(params["filter"], str):
        params["filter"] = json.dumps(params["filter"])
    response = client.get(SCANS, params=params)
    assert response.status_code == 200, response.text
    return [row["scan_number"] for row in response.json()["rows"]]


def assert_problem(response: Any, status: int, title: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_JSON
    body = response.json()
    assert (body["status"], body["title"]) == (status, title)
    return body


def path_problem(response: Any) -> list[tuple[list[Any], str]]:
    """A path outside the roots: files.roots.PathNotAllowed's 400, located at the field."""
    body = assert_problem(response, 400, "Path not allowed")
    assert body["detail"] == body["errors"][0]["message"]
    return [(item["loc"], item["code"]) for item in body["errors"]]


def problem_items(response: Any) -> list[tuple[list[Any], str]]:
    body = assert_problem(response, 422, "Validation failed")
    return [(item["loc"], item["code"]) for item in body["errors"]]


# ---------------------------------------------------------------------------
# GET /scans: the grid list


def test_list_scans_returns_the_grid_rows_newest_first(client: TestClient) -> None:
    response = client.get(SCANS)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["total"] == 5
    assert [row["scan_number"] for row in body["rows"]] == [1005, 1004, 1003, 1002, 1001]
    by_number = {row["scan_number"]: row for row in body["rows"]}
    assert by_number[1002] == {
        "scan_number": 1002,
        "sample_name": "Ni",
        "aperture": "mask",
        "user_name": "bob",
        "n_dimensions": 2,
        "technique": "line + depth",
        "time": "2026-01-06T10:00:00Z",
        "notes": None,
    }
    assert by_number[1003] == {
        "scan_number": 1003,
        "sample_name": None,
        "aperture": None,
        "user_name": "alice",
        "n_dimensions": 1,
        "technique": "energy + other",
        "time": "2026-01-07T10:00:00Z",
        "notes": None,
    }
    assert {number: row["technique"] for number, row in by_number.items()} == {
        1001: "area",
        1002: "line + depth",
        1003: "energy + other",
        1004: "none",
        1005: "depth + area",
    }
    assert by_number[1004]["n_dimensions"] == 0


def test_list_scans_sorts_filters_and_pages(client: TestClient) -> None:
    assert rows(client, sort="technique") == [1001, 1005, 1003, 1002, 1004]
    assert rows(client, sort="user_name,-time") == [1003, 1001, 1005, 1002, 1004]
    assert rows(client, filter={"technique": {"filterType": "text", "type": "contains", "filter": "depth"}}) == [
        1005,
        1002,
    ]
    assert rows(client, filter={"n_dimensions": {"filterType": "number", "type": "equals", "filter": 0}}) == [1004]
    assert rows(client, filter={"aperture": {"filterType": "text", "type": "blank"}}) == [1003]
    assert rows(client, filter={"user_name": {"filterType": "text", "type": "equals", "filter": "BOB"}}) == [1005, 1002]
    in_range = {
        "filterType": "date",
        "type": "inRange",
        "dateFrom": "2026-01-06 00:00:00",
        "dateTo": "2026-01-07 00:00:00",
    }
    assert rows(client, filter={"time": in_range}, sort="scan_number") == [1002, 1003]
    response = client.get(SCANS, params={"offset": 1, "limit": 2})
    assert ([row["scan_number"] for row in response.json()["rows"]], response.json()["total"]) == ([1004, 1003], 5)
    filtered = client.get(SCANS, params={"limit": 1, "filter": json.dumps({"notes": {"type": "notBlank"}})}).json()
    assert ([row["scan_number"] for row in filtered["rows"]], filtered["total"]) == ([1005], 2)


def test_list_scans_says_when_it_read_the_page(client: TestClient) -> None:
    """Every grid list answers with ``as_of``, the scans list too (it takes no changed_since)."""
    before = utcnow()
    as_of = client.get(SCANS).json()["as_of"]
    after = utcnow()
    assert as_of.endswith("Z")
    assert before <= datetime.fromisoformat(as_of) <= after


def test_list_scans_rejects_bad_grid_parameters_as_problems(client: TestClient) -> None:
    assert problem_items(client.get(SCANS, params={"sort": "bogus"})) == [(["query", "sort"], "unknown_field")]
    assert problem_items(client.get(SCANS, params={"filter": "{oops"})) == [(["query", "filter"], "json_invalid")]
    wrong_type = json.dumps({"technique": {"filterType": "number", "type": "equals", "filter": 1}})
    assert problem_items(client.get(SCANS, params={"filter": wrong_type})) == [
        (["query", "filter", "technique", "filterType"], "filter_type")
    ]
    unknown = json.dumps({"filefolder": {"filterType": "text", "type": "contains", "filter": "x"}})
    assert problem_items(client.get(SCANS, params={"filter": unknown})) == [
        (["query", "filter", "filefolder"], "unknown_field")
    ]
    assert problem_items(client.get(SCANS, params={"limit": 1001})) == [(["query", "limit"], "less_than_equal")]
    assert problem_items(client.get(SCANS, params={"offset": -1})) == [(["query", "offset"], "greater_than_equal")]
    assert problem_items(client.get(SCANS, params={"offset": 2**64})) == [(["query", "offset"], "less_than_equal")]
    too_big = json.dumps({"scan_number": {"filterType": "number", "type": "equals", "filter": 2**70}})
    assert problem_items(client.get(SCANS, params={"filter": too_big})) == [
        (["query", "filter", "scan_number", "filter"], "invalid_value")
    ]
    assert problem_items(client.get(SCANS, params={"changed_since": "2026-10-18T12:00:00Z"})) == [
        (["query", "changed_since"], "extra_forbidden")
    ]


def count_selects(engine: Engine, request: Any) -> int:
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        request()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return len(statements)


def test_the_technique_takes_no_query_per_scan(seeded: tuple[Settings, TestClient]) -> None:
    settings, client = seeded
    engine = client.app.state.engine
    assert count_selects(engine, lambda: client.get(SCANS)) == 2  # the total and the page
    with Session(engine) as session, session.begin():
        for number in range(2000, 2040):
            add_scan(session, number, [SAMPLE_X, SAMPLE_Y], [DEPTH], [ENERGY], catalog={"aperture": "wire"})
    queries = count_selects(engine, lambda: client.get(SCANS, params={"limit": 1000}))
    assert queries == 2
    body = client.get(SCANS, params={"limit": 1000}).json()
    assert body["total"] == 45
    assert {row["technique"] for row in body["rows"] if row["scan_number"] >= 2000} == {"area + depth + energy"}


# Motor groups that exercise find_motor_group's rules: names in any case, a group called "None" (skipped), "other" both
# listed and as the fallback, a group whose name equals a label ("line"), and an empty group.
TRICKY_GROUPS = {
    "Sample": ["s:x", "s:y"],
    "depth": ["d:z"],
    "None": ["n:skip"],
    "other": ["o:listed"],
    "line": ["l:motor"],
    "energy": [],
}
TECHNIQUE_CASES: list[tuple[list[list[str]], str]] = [
    ([["s:x.VAL"]], "line"),
    ([["s:x"], ["s:y.VAL"]], "area"),
    ([["s:x", "s:x"]], "area"),
    ([["d:z.VAL", "s:x"], ["s:y"]], "depth + area"),
    ([["s:y"], ["d:z"], ["s:x"]], "area + depth"),
    ([["n:skip.VAL"]], "none"),
    ([["n:skip", "d:z"]], "depth"),
    ([["unknown:pv"]], "other"),
    ([["o:listed", "unknown"]], "other"),
    ([["l:motor", "s:x"]], "line + line"),
    ([["s:x.VALUE"]], "line"),
    ([["s:x.VAL.VAL"]], "line"),
    ([["S:X"]], "other"),
    ([[" "]], "other"),
    ([["", "d:z"]], "depth"),
    ([[]], "none"),
    ([], "none"),
]


def test_the_sql_technique_is_the_importers_build_technique_strings(tmp_path: Path) -> None:
    settings, engine = make_database(tmp_path, beamline={"motor_groups": TRICKY_GROUPS})
    with Session(engine) as session, session.begin():
        for number, (rows_pvs, _) in enumerate(TECHNIQUE_CASES, start=1):
            add_scan(session, number, *rows_pvs)
    with Session(engine) as session:
        python = {}
        for number in range(1, len(TECHNIQUE_CASES) + 1):
            scan_rows_ = session.scalars(select(Scan).where(Scan.scan_number == number).order_by(Scan.id)).all()
            python[number] = build_technique_strings(scan_rows_, motor_groups=TRICKY_GROUPS)[0]
    engine.dispose()

    with TestClient(create_app(settings), headers=PORTAL_HEADERS) as client:
        listed = {
            row["scan_number"]: row["technique"] for row in client.get(SCANS, params={"limit": 1000}).json()["rows"]
        }
        detailed = {number: client.get(f"{SCANS}/{number}").json()["technique"] for number in python}
    expected = {number: technique for number, (_, technique) in enumerate(TECHNIQUE_CASES, start=1)}
    assert python == expected
    assert listed == expected
    assert detailed == expected


# ---------------------------------------------------------------------------
# GET /scans/{scan_id}: the detail


def test_get_scan_returns_metadata_scan_rows_and_catalog(client: TestClient) -> None:
    response = client.get(f"{SCANS}/1002")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert (body["scan_number"], body["technique"]) == (1002, "line + depth")
    metadata = body["metadata"]
    assert (metadata["scan_number"], metadata["user_name"], metadata["time"]) == (1002, "bob", "2026-01-06T10:00:00Z")
    assert (metadata["source_id_gap"], metadata["source_id_taper"]) == (11.5, 0.25)  # the taper is its own
    assert metadata["scan_end_time"] == "2022-01-01T00:01:40"  # the log's text, as written
    assert [(row["scan_dim"], row["scan_npts"], row["scan_cpt"]) for row in body["dimensions"]] == [
        (2, 21, 21),
        (1, 21, 20),
    ]
    assert [row["positioners"] for row in body["dimensions"]] == [
        [{"index": 1, "pv": SAMPLE_X, "motor_group": "sample", "start": "-10", "end": "10", "step": "1"}],
        [{"index": 1, "pv": DEPTH, "motor_group": "depth", "start": "-10", "end": "10", "step": "1"}],
    ]
    first = body["dimensions"][0]
    assert (first["scan_positioner1_pv"], first["scan_positioner1"], first["scan_positioner2_pv"]) == (
        SAMPLE_X,
        "-10 10 1",
        "",
    )
    assert (first["scan_detector_trig1_pv"], first["scan_detector_trig1_val"]) == ("34ide:det:Acquire", "1")
    assert without_version(body["catalog"]) == {
        "filefolder": "/data/1002",
        "filename_prefix": ["s1002_%d"],
        "aperture": "mask",
        "sample_name": "Ni",
        "notes": None,
    }


def test_get_scan_lists_every_positioner_of_a_row_in_slot_order(client: TestClient) -> None:
    dimensions = client.get(f"{SCANS}/1003").json()["dimensions"]
    assert [(p["index"], p["motor_group"]) for p in dimensions[0]["positioners"]] == [(1, "energy"), (2, "other")]


def test_get_scan_without_catalog_or_scan_rows(client: TestClient) -> None:
    assert client.get(f"{SCANS}/1003").json()["catalog"] is None
    body = client.get(f"{SCANS}/1004").json()
    assert (body["dimensions"], body["technique"]) == ([], "none")
    assert body["catalog"]["aperture"] == "None"  # a stored "None" text comes back as it is


def test_a_positioner_range_that_is_not_three_values_is_kept_raw(tmp_path: Path) -> None:
    settings, engine = make_database(tmp_path)
    with Session(engine) as session, session.begin():
        add_scan(session, 7, [SAMPLE_X])
        session.flush()
        session.scalars(select(Scan)).one().scan_positioner1 = "1 2"
    engine.dispose()
    with TestClient(create_app(settings), headers=PORTAL_HEADERS) as client:
        dimension = client.get(f"{SCANS}/7").json()["dimensions"][0]
    assert dimension["scan_positioner1"] == "1 2"
    assert dimension["positioners"][0] == {
        "index": 1,
        "pv": SAMPLE_X,
        "motor_group": "sample",
        "start": None,
        "end": None,
        "step": None,
    }


def test_get_scan_unknown_is_a_404_problem(client: TestClient) -> None:
    body = assert_problem(client.get(f"{SCANS}/999"), 404, "Scan not found")
    assert body == {"type": "about:blank", "title": "Scan not found", "status": 404, "detail": "No scan 999"}
    assert problem_items(client.get(f"{SCANS}/abc")) == [(["path", "scan_id"], "int_parsing")]
    assert problem_items(client.get(f"{SCANS}/{2**70}")) == [(["path", "scan_id"], "less_than_equal")]
    assert problem_items(client.get(f"{SCANS}/-1")) == [(["path", "scan_id"], "greater_than_equal")]


def test_the_detail_schemas_cover_every_column() -> None:
    assert set(ScanMetadata.model_fields) == {attribute.key for attribute in inspect(Metadata).column_attrs}
    columns = {attribute.key for attribute in inspect(Scan).column_attrs}
    assert set(ScanDimension.model_fields) == columns - {"scan_number"} | {"positioners"}


# ---------------------------------------------------------------------------
# PUT /scans/{scan_id}/catalog


def catalog_body(folder: Path | str, **values: Any) -> dict[str, Any]:
    return {
        "filefolder": str(folder),
        "filename_prefix": ["x_%d"],
        "aperture": "wire",
        "sample_name": "Cu",
        "notes": "new",
        "version": None,
        **values,
    }


def without_version(catalog: dict[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in catalog.items() if name != "version"}


def stored_catalogs(settings: Settings, scan_number: int) -> list[dict[str, Any]]:
    engine = make_engine(settings)
    with Session(engine) as session:
        found = session.scalars(select(Catalog).where(Catalog.scan_number == scan_number)).all()
        result = [
            {
                "filefolder": c.filefolder,
                "filename_prefix": c.filename_prefix,
                "aperture": c.aperture,
                "sample_name": c.sample_name,
                "notes": c.notes,
            }
            for c in found
        ]
    engine.dispose()
    return result


def test_put_catalog_creates_the_entry(seeded: tuple[Settings, TestClient], data_root: Path) -> None:
    settings, client = seeded
    body = catalog_body(f" {data_root}/1003 ", filename_prefix=[" a_%d ", "", "  ", "b_%d"])
    response = client.put(f"{SCANS}/1003/catalog", json=body)
    assert response.status_code == 200, response.text
    expected = {
        "filefolder": f"{data_root}/1003",
        "filename_prefix": ["a_%d", "b_%d"],
        "aperture": "wire",
        "sample_name": "Cu",
        "notes": "new",
    }
    assert without_version(response.json()) == expected
    assert stored_catalogs(settings, 1003) == [expected]
    assert client.get(f"{SCANS}/1003").json()["catalog"] == response.json()
    assert rows(client, filter={"sample_name": {"type": "equals", "filter": "cu"}}) == [1003]


def test_put_catalog_replaces_the_whole_entry(seeded: tuple[Settings, TestClient], data_root: Path) -> None:
    settings, client = seeded
    version = client.get(f"{SCANS}/1001").json()["catalog"]["version"]
    body = catalog_body(data_root / "si", filename_prefix=[], aperture="mask", sample_name=None, notes=None)
    response = client.put(f"{SCANS}/1001/catalog", json={**body, "version": version})
    assert response.status_code == 200, response.text
    expected = {
        "filefolder": str(data_root / "si"),
        "filename_prefix": [],
        "aperture": "mask",
        "sample_name": None,
        "notes": None,
    }
    assert without_version(response.json()) == expected
    assert response.json()["version"] != version
    assert stored_catalogs(settings, 1001) == [expected]


def test_a_catalog_save_over_a_changed_catalog_is_refused(seeded: tuple[Settings, TestClient], data_root: Path) -> None:
    """Client A edits the catalog it loaded while client B appends a note; A's save is a 409 and B's note
    survives. Reloaded, A's save goes through."""
    settings, a = seeded
    loaded = a.get(f"{SCANS}/1001").json()["catalog"]
    with TestClient(a.app, headers=PORTAL_HEADERS) as b:
        appended = b.post(f"{SCANS}/1001/notes", json={"text": "from B"})
    assert appended.status_code == 200, appended.text
    edit = catalog_body(data_root / "a", notes=f"{loaded['notes']}\nfrom A")
    body = assert_problem(
        a.put(f"{SCANS}/1001/catalog", json={**edit, "version": loaded["version"]}), 409, "Catalog changed"
    )
    assert body["type"] == "urn:laueportal:problem:catalog-changed"
    assert stored_catalogs(settings, 1001)[0]["notes"] == "first note\nfrom B"
    reloaded = a.get(f"{SCANS}/1001").json()["catalog"]
    assert reloaded["version"] == appended.json()["version"]
    # A applies its change again, to the reloaded notes: both notes are kept.
    again = {**edit, "notes": f"{reloaded['notes']}\nfrom A", "version": reloaded["version"]}
    saved = a.put(f"{SCANS}/1001/catalog", json=again)
    assert saved.status_code == 200, saved.text
    assert stored_catalogs(settings, 1001)[0]["notes"] == "first note\nfrom B\nfrom A"


@pytest.mark.parametrize("folder", ["/etc", "data/relative", "", "~/data", "{root}/../outside"])
def test_put_catalog_refuses_a_folder_outside_the_roots(
    seeded: tuple[Settings, TestClient], data_root: Path, folder: str
) -> None:
    settings, client = seeded
    before = stored_catalogs(settings, 1001)
    response = client.put(f"{SCANS}/1001/catalog", json=catalog_body(folder.format(root=data_root)))
    assert path_problem(response) == [(["filefolder"], "path_not_allowed")]
    assert stored_catalogs(settings, 1001) == before


def test_put_catalog_refuses_a_symlink_out_of_the_roots(
    seeded: tuple[Settings, TestClient], data_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (data_root / "link").symlink_to(outside)
    response = seeded[1].put(f"{SCANS}/1001/catalog", json=catalog_body(data_root / "link"))
    assert path_problem(response) == [(["filefolder"], "path_not_allowed")]


def test_put_catalog_with_no_roots_configured_refuses_every_folder(tmp_path: Path) -> None:
    settings, engine = make_database(tmp_path)
    with Session(engine) as session, session.begin():
        add_scan(session, 1, [SAMPLE_X])
    engine.dispose()
    with TestClient(create_app(settings), headers=PORTAL_HEADERS) as client:
        response = client.put(f"{SCANS}/1/catalog", json=catalog_body(tmp_path))
    assert path_problem(response) == [(["filefolder"], "path_not_allowed")]
    assert "No file roots are configured" in response.json()["errors"][0]["message"]


def test_put_catalog_validates_the_body(client: TestClient, data_root: Path) -> None:
    folder = data_root / "x"
    assert problem_items(client.put(f"{SCANS}/1001/catalog", json=catalog_body(folder, aperture="coded"))) == [
        (["aperture"], "literal_error")
    ]
    without_notes = catalog_body(folder)
    del without_notes["notes"]
    assert problem_items(client.put(f"{SCANS}/1001/catalog", json=without_notes)) == [(["notes"], "missing")]
    assert problem_items(client.put(f"{SCANS}/1001/catalog", json=catalog_body(folder, filenamePrefix=[]))) == [
        (["filenamePrefix"], "extra_forbidden")
    ]


def test_put_catalog_of_an_unknown_scan_is_a_404(client: TestClient, data_root: Path) -> None:
    assert_problem(client.put(f"{SCANS}/999/catalog", json=catalog_body(data_root)), 404, "Scan not found")
    # the folder is checked first, outside the write lock: a folder outside the roots is a 400 even then
    assert path_problem(client.put(f"{SCANS}/999/catalog", json=catalog_body("/etc"))) == [
        (["filefolder"], "path_not_allowed")
    ]


@pytest.mark.parametrize("bad", ["\x00", "\ud800", "\udcfc"], ids=["NUL", "lone surrogate", "surrogate escape"])
@pytest.mark.parametrize(
    ("field", "loc"),
    [("sample_name", ["sample_name"]), ("notes", ["notes"]), ("filename_prefix", ["filename_prefix", 1])],
)
def test_put_catalog_refuses_a_nul_or_lone_surrogate_in_its_text(
    seeded: tuple[Settings, TestClient], data_root: Path, field: str, loc: list[str | int], bad: str
) -> None:
    """These were 500s (SQLite cannot encode a lone surrogate), and the catalog stays as it was."""
    settings, client = seeded
    before = stored_catalogs(settings, 1001)
    value: Any = ["a_%d", f"x{bad}y"] if field == "filename_prefix" else f"x{bad}y"
    body = json.dumps(catalog_body(data_root, **{field: value}))  # json.dumps escapes it: "x\\ud800y"
    response = client.put(f"{SCANS}/1001/catalog", content=body, headers={"content-type": "application/json"})
    assert problem_items(response) == [(loc, "invalid_text")]
    assert stored_catalogs(settings, 1001) == before


@pytest.mark.parametrize("bad", ["\x00", "\ud800"], ids=["NUL", "lone surrogate"])
def test_put_catalog_refuses_a_nul_or_lone_surrogate_in_the_folder(
    seeded: tuple[Settings, TestClient], data_root: Path, bad: str
) -> None:
    settings, client = seeded
    before = stored_catalogs(settings, 1001)
    body = json.dumps(catalog_body(f"{data_root}/x{bad}y"))
    response = client.put(f"{SCANS}/1001/catalog", content=body, headers={"content-type": "application/json"})
    assert problem_items(response) == [(["filefolder"], "invalid_text")]
    assert stored_catalogs(settings, 1001) == before


FOREIGN_PAGE = {"origin": "http://127.0.0.1:2104", "sec-fetch-site": "same-site"}


def test_a_catalog_save_or_note_from_another_origin_changes_nothing(
    seeded: tuple[Settings, TestClient], data_root: Path
) -> None:
    """A page on another origin can send a form or a no-cors fetch, never X-Laue-Portal: 1 (a 403)."""
    settings, client = seeded
    before = stored_catalogs(settings, 1001)
    with TestClient(client.app) as foreign:
        responses = [
            foreign.put(f"{SCANS}/1001/catalog", json=catalog_body(data_root)),
            foreign.post(f"{SCANS}/1001/notes", data={"text": "forged"}, headers=FOREIGN_PAGE),
            foreign.post(
                f"{SCANS}/1001/notes",
                content='{"text": "forged"}',
                headers={**FOREIGN_PAGE, "content-type": "text/plain", "sec-fetch-mode": "no-cors"},
            ),
        ]
    responses.append(client.put(f"{SCANS}/1001/catalog", json=catalog_body(data_root), headers=FOREIGN_PAGE))
    for response in responses:
        assert_problem(response, 403, "Cross-site request refused")
    assert stored_catalogs(settings, 1001) == before


# ---------------------------------------------------------------------------
# POST /scans/{scan_id}/notes


def test_append_note_adds_a_line(seeded: tuple[Settings, TestClient]) -> None:
    settings, client = seeded
    response = client.post(f"{SCANS}/1001/notes", json={"text": "second note"})
    assert response.status_code == 200, response.text
    assert response.json()["notes"] == "first note\nsecond note"
    assert (
        client.post(f"{SCANS}/1001/notes", json={"text": "third"}).json()["notes"] == "first note\nsecond note\nthird"
    )
    assert stored_catalogs(settings, 1001)[0]["notes"] == "first note\nsecond note\nthird"


def test_append_note_to_empty_notes_sets_them(client: TestClient) -> None:
    assert client.post(f"{SCANS}/1004/notes", json={"text": "only"}).json()["notes"] == "only"  # notes ""
    assert client.post(f"{SCANS}/1002/notes", json={"text": " kept as written "}).json()["notes"] == " kept as written "


def test_append_note_needs_a_catalog_entry(client: TestClient) -> None:
    body = assert_problem(client.post(f"{SCANS}/1003/notes", json={"text": "x"}), 409, "No catalog entry")
    assert body["detail"] == "Scan 1003 has no catalog entry for notes; save its catalog first."
    assert_problem(client.post(f"{SCANS}/999/notes", json={"text": "x"}), 404, "Scan not found")


@pytest.mark.parametrize("bad", ["\x00", "\ud800"], ids=["NUL", "lone surrogate"])
def test_append_note_refuses_a_nul_or_lone_surrogate(seeded: tuple[Settings, TestClient], bad: str) -> None:
    settings, client = seeded
    body = json.dumps({"text": f"a{bad}b"})
    response = client.post(f"{SCANS}/1001/notes", content=body, headers={"content-type": "application/json"})
    assert problem_items(response) == [(["text"], "invalid_text")]
    assert stored_catalogs(settings, 1001)[0]["notes"] == "first note"


def test_append_note_refuses_blank_text(client: TestClient) -> None:
    assert problem_items(client.post(f"{SCANS}/1001/notes", json={"text": "   "})) == [(["text"], "value_error")]
    assert problem_items(client.post(f"{SCANS}/1001/notes", json={"text": ""})) == [(["text"], "string_too_short")]
    assert problem_items(client.post(f"{SCANS}/1001/notes", json={})) == [(["text"], "missing")]


# ---------------------------------------------------------------------------
# The contract


def test_scan_routes_have_the_contract_operation_ids(client: TestClient) -> None:
    paths = client.get("/api/v1/openapi.json").json()["paths"]
    operations = {
        (method, path): (operation["operationId"], operation["tags"])
        for path, item in paths.items()
        for method, operation in item.items()
        if path.startswith(SCANS)
    }
    assert operations == {
        ("get", SCANS): ("listScans", ["scans"]),
        ("get", f"{SCANS}/{{scan_id}}"): ("getScan", ["scans"]),
        ("put", f"{SCANS}/{{scan_id}}/catalog"): ("updateScanCatalog", ["scans"]),
        ("post", f"{SCANS}/{{scan_id}}/notes"): ("appendScanNote", ["scans"]),
        ("post", f"{SCANS}/import/index"): ("indexScanLog", ["scans"]),
        ("post", f"{SCANS}/import"): ("importScans", ["scans"]),
    }
    parameters = [parameter["name"] for parameter in paths[SCANS]["get"]["parameters"]]
    assert parameters == ["offset", "limit", "sort", "filter"]
