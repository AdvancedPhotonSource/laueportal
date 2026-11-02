"""The scan-log import endpoints: index a log (an upload or a server file), then import its selected scans; the import
page's quick select; the size cap, the safe parser, the staged log's expiry and the roots on both sources. On the
``tests/scan_logs`` fixtures and a real SQLite file."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from laueportal.app import create_app
from laueportal.core.errors import PROBLEM_JSON
from laueportal.core.settings import Settings
from laueportal.files import browser
from laueportal.scans import importer, service
from laueportal.scans.models import Catalog, Metadata, Scan
from laueportal.scans.schemas import ScanLogRow
from tests.api_support import PORTAL_HEADERS
from tests.settings_support import make_database, make_settings

INDEX = "/api/v1/scans/import/index"
IMPORT = "/api/v1/scans/import"
LOGS = Path(__file__).resolve().parent.parent / "scan_logs"
LOG = (LOGS / "test_log.xml").read_bytes()
MIB = 1024 * 1024


def with_duplicate_and_invalid(log: bytes) -> bytes:
    """test_log.xml with scan 276992 renumbered 276990 (a duplicate) and 276994 renumbered "x94" (invalid)."""
    return log.replace(b'scanNumber="276992"', b'scanNumber="276990"').replace(
        b'scanNumber="276994"', b'scanNumber="x94"'
    )


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    return root


@pytest.fixture
def setup(tmp_path: Path, data_root: Path) -> Iterator[tuple[Settings, TestClient]]:
    """A database holding scan 276991 (test_log.xml's second scan), the roots ``{"data": data_root}`` and a 1 MB
    size limit."""
    settings, engine = make_database(tmp_path, files={"roots": {"data": data_root}, "max_scan_log_mb": 1})
    try:
        existing = importer.parse_selected_scans_from_xml(LOG, [3], xmlns=settings.beamline.scan_log_namespace)
        importer.bulk_import_scans(
            existing, {}, engine=engine, motor_groups={}, timezone=ZoneInfo(settings.database.legacy_timezone)
        )
    finally:
        engine.dispose()
    with TestClient(create_app(settings), headers=PORTAL_HEADERS) as client:
        yield settings, client


@pytest.fixture
def client(setup: tuple[Settings, TestClient]) -> TestClient:
    return setup[1]


def staged(settings: Settings) -> list[str]:
    directory = importer.staging_directory(settings.files)
    return sorted(path.name for path in directory.iterdir()) if directory.exists() else []


def upload(client: TestClient, log: bytes, name: str = "scan_log.xml") -> Any:
    return client.post(INDEX, files={"file": (name, log, "text/xml")})


def problem(response: Any, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_JSON
    return response.json()


def index_rows(client: TestClient, log: bytes) -> tuple[str, list[dict[str, Any]]]:
    response = upload(client, log)
    assert response.status_code == 200, response.text
    body = response.json()
    return body["token"], body["rows"]


def stored(settings: Settings) -> dict[int, tuple[Any, ...]]:
    """Each stored scan's number: its catalog's values and its count of scan rows."""
    from laueportal.core.db import make_engine

    engine = make_engine(settings)
    try:
        with Session(engine) as session:
            catalogs = {row.scan_number: row for row in session.query(Catalog)}
            return {
                number: (
                    None
                    if number not in catalogs
                    else (
                        catalogs[number].filefolder,
                        catalogs[number].filename_prefix,
                        catalogs[number].aperture,
                        catalogs[number].sample_name,
                        catalogs[number].notes,
                    ),
                    session.query(Scan).filter(Scan.scan_number == number).count(),
                )
                for (number,) in session.query(Metadata.scan_number)
            }
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# The index


@pytest.mark.parametrize("source", ["upload", "server"])
def test_the_index_lists_every_scan_with_its_status_and_stages_the_log(
    setup: tuple[Settings, TestClient], data_root: Path, source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, client = setup
    monkeypatch.setattr(importer, "EXISTING_BATCH", 2)  # the database is asked in batches: 276991 is in the second
    log = with_duplicate_and_invalid(LOG)
    if source == "upload":
        response = upload(client, log)
    else:
        (data_root / "logs").mkdir()
        (data_root / "logs" / "scanlog.xml").write_bytes(log)
        response = client.post(INDEX, data={"root": "data", "path": str(data_root / "logs" / "scanlog.xml")})

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert [(row["scan_index"], row["scan_number"], row["status"]) for row in body["rows"]] == [
        (2, 276990, "new"),
        (3, 276991, "exists"),
        (4, 276990, "duplicate"),
        (5, 276993, "new"),
        (6, None, "invalid"),
    ]
    assert body["rows"][0] == {
        "scan_index": 2,
        "scan_number": 276990,
        "status": "new",
        "time": "2023-02-25T08:09:58Z",  # the log's 02:09:58, the beamline's local time (CST), in UTC
        "user_name": "User1",
        "energy": 21.3047,
        "energy_unit": "keV",
        "sample_xyz": "7411.3 -492.2 -917",
        "n_dimensions": 1,
    }
    assert [row["n_dimensions"] for row in body["rows"]] == [1, 1, 1, 3, 4]
    # Staged under files.uploads_dir with an unguessable token (a random UUID's hex).
    assert len(body["token"]) == 32 and int(body["token"], 16) >= 0
    assert staged(settings) == [f"{body['token']}.xml"]
    path = importer.staging_directory(settings.files) / f"{body['token']}.xml"
    assert path.read_bytes() == log


ODD_VALUES = [
    (b'scanNumber="276990"', b'scanNumber="99999999999999999999"', "invalid"),  # past SQLite's INTEGER
    (b'scanNumber="276990"', b'scanNumber="9223372036854775807"', "new"),  # SQLite's largest INTEGER
    (b'scanNumber="276990"', b'scanNumber="-5"', "invalid"),
    (b'scanNumber="276990"', b'scanNumber="276_990"', "invalid"),
    (b'scanNumber="276990"', 'scanNumber="٢٧٦٩٩٠"'.encode(), "invalid"),  # Arabic-Indic digits
    (b'scanNumber="276990"', b'scanNumber=" 276990 "', "new"),
    (b">2023-02-25T02:09:58<", b">9999-12-31T23:59:59<", "invalid"),  # its UTC is past year 9999
    (b">2023-02-25T02:09:58<", b">yesterday<", "invalid"),
    # a value that is not its column's number is stored as null, as the index shows it ...
    (b'<energy unit="keV">21.3047<', b'<energy unit="keV">n/a<', "new"),
    (b'<time epoch="1046160598">', b'<time epoch="99999999999999999999">', "new"),  # past SQLite's INTEGER
    (b'npts="41"', b'npts="many"', "new"),
    (b"<cpt>41<", b"<cpt>n/a<", "invalid"),  # ... but completed points the scan table needs (NOT NULL)
]


@pytest.mark.parametrize(
    ("old", "new", "status"),
    ODD_VALUES,
    ids=[
        "20-digits",
        "integer-max",
        "negative",
        "underscore",
        "non-ascii-digits",
        "spaces",
        "year-9999",
        "no-time",
        "text-energy",
        "20-digit-epoch",
        "text-points",
        "text-completed-points",
    ],
)
def test_odd_values_in_a_log_never_fail_the_index_and_every_new_row_imports(
    setup: tuple[Settings, TestClient], old: bytes, new: bytes, status: str
) -> None:
    """The first scan's number, start time or another number is one the database cannot store as such. The index answers
    (no 500), lists that scan as ``invalid`` when its number, start time or completed points cannot be stored (any other
    number is stored as null), and every ``new`` row imports."""
    settings, client = setup
    log = LOG.replace(old, new, 1)
    assert log != LOG

    token, rows = index_rows(client, log)

    assert rows[0]["status"] == status
    if status == "invalid" and old.startswith(b"scanNumber"):
        assert rows[0]["scan_number"] is None
    new_ids = [row["scan_number"] for row in rows if row["status"] == "new"]
    report = client.post(IMPORT, json={"token": token, "scan_ids": new_ids})
    assert report.status_code == 200, report.text
    assert report.json()["summary"] == {"imported": len(new_ids), "exists": 0, "failed": 0}
    assert set(stored(settings)) == {276991, *new_ids}  # stored as integers


def test_a_scan_the_database_refuses_fails_with_the_databases_reason(setup: tuple[Settings, TestClient]) -> None:
    """A failed import names the database's error, not SQLAlchemy's statement and parameters. Scan 276990 without a
    start time is listed ``invalid``; imported anyway, the metadata table refuses it."""
    settings, client = setup
    log = LOG.replace(b'<time epoch="1046160598">2023-02-25T02:09:58</time>', b"", 1)
    token, rows = index_rows(client, log)
    assert (rows[0]["scan_number"], rows[0]["status"], rows[0]["time"]) == (276990, "invalid", None)

    report = client.post(IMPORT, json={"token": token, "scan_ids": [276990]}).json()

    assert report["results"] == [
        {
            "scan_number": 276990,
            "outcome": "failed",
            "error": "IntegrityError: NOT NULL constraint failed: metadata.time",
        }
    ]
    assert set(stored(settings)) == {276991}


def test_a_failure_of_a_statements_parameters_never_quotes_the_statement(
    setup: tuple[Settings, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error SQLAlchemy raises while it prepares a statement's parameters (a ``StatementError``, not the driver's) is
    reported by its cause too, without the statement and its values."""
    settings, client = setup
    original = importer.import_metadata_row

    def text_energy(values: Any, **options: Any) -> Any:
        row = original(values, **options)
        row.source_energy = "n/a"  # a log's text stored as is, in a number column
        return row

    monkeypatch.setattr(importer, "import_metadata_row", text_energy)
    token, _ = index_rows(client, LOG)

    report = client.post(IMPORT, json={"token": token, "scan_ids": [276990]}).json()

    assert report["results"] == [
        {"scan_number": 276990, "outcome": "failed", "error": "ValueError: could not convert string to float: 'n/a'"}
    ]


def test_quick_select_picks_new_rows_by_scan_id_or_start_time(client: TestClient) -> None:
    """The import page's quick select (by scan ID or by start time): ``service.quick_select`` on test_log_2.xml's rows,
    with 274038 already imported (so not new). The rows and cases are in ``tests/scan_logs/quick_select.json``, which
    the page's Vitest reads too."""
    shared = json.loads((LOGS / "quick_select.json").read_text())
    token, _ = index_rows(client, (LOGS / shared["log"]).read_bytes())
    assert client.post(IMPORT, json={"token": token, "scan_ids": shared["imported_first"]}).status_code == 200
    _, raw = index_rows(client, (LOGS / shared["log"]).read_bytes())
    assert [{key: row[key] for key in ("scan_number", "status", "time")} for row in raw] == shared["rows"]
    rows = [ScanLogRow.model_validate(row) for row in raw]

    for case in shared["cases"]:
        criteria = dict(case["criteria"])
        if "created_at_or_after" in criteria:
            criteria["created_at_or_after"] = datetime.fromisoformat(criteria["created_at_or_after"])
        assert service.quick_select(rows, **criteria) == case["expected"], case["criteria"]
    with pytest.raises(ValueError, match="time zone"):
        service.quick_select(rows, created_at_or_after=datetime(2022, 11, 18))


def test_a_log_over_the_size_limit_is_refused_before_it_is_read(
    setup: tuple[Settings, TestClient], data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``files.max_scan_log_mb`` (1 MB here) bounds both sources before anything parses the log."""
    settings, client = setup
    padded = LOG.replace(b"<!-- scan log file from APS, 34ID-E -->", b"<!--" + b" " * MIB + b"-->")

    assert problem(upload(client, padded), 413)["title"] == "Scan log too large"  # just over: read, then refused
    (data_root / "big.xml").write_bytes(padded)
    assert (
        "exceeds the 1 MB limit"
        in problem(client.post(INDEX, data={"path": str(data_root / "big.xml")}), 422)["detail"]
    )

    def never(*_: object) -> None:
        raise AssertionError("the upload was read")

    monkeypatch.setattr(service, "read_upload", never)
    monkeypatch.setattr(importer, "parse_scan_log", never)
    # Past the limit and the form's overhead, the Content-Length refuses it before FastAPI receives the body.
    assert problem(upload(client, b" " * (MIB + 65 * 1024)), 413)["title"] == "Scan log too large"
    # Without a Content-Length (a chunked body), the size cannot be checked first.
    chunked = client.post(
        INDEX,
        content=iter([b"--x\r\n", b'Content-Disposition: form-data; name="path"\r\n\r\n/x\r\n--x--\r\n']),
        headers={"content-type": "multipart/form-data; boundary=x"},
    )
    assert problem(chunked, 411)["title"] == "Length required"
    assert staged(settings) == []


def test_a_server_log_that_grew_past_the_limit_after_its_check_is_refused_unread_past_it(
    setup: tuple[Settings, TestClient], data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The size check by stat, then the read: a file that grows between them is still refused."""
    settings, client = setup
    padded = LOG.replace(b"<!-- scan log file from APS, 34ID-E -->", b"<!--" + b" " * MIB + b"-->")
    (data_root / "growing.xml").write_bytes(padded)
    original = browser.validate_selection

    def stat_saw_it_small(path: str, settings: Settings, **options: Any) -> Path:
        return original(path, settings, **{**options, "max_file_size_mb": None})

    monkeypatch.setattr(browser, "validate_selection", stat_saw_it_small)

    body = problem(client.post(INDEX, data={"path": str(data_root / "growing.xml")}), 422)

    assert "exceeds the 1 MB limit" in body["detail"]
    assert staged(settings) == []


def test_a_cross_site_upload_is_refused_before_its_size_or_body(
    setup: tuple[Settings, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write guard answers first, before the size check and before FastAPI receives the body: an over-cap upload
    without the portal's header is a 403, not a 413, and nothing reads it."""
    settings, _ = setup

    def never(*_: object) -> None:
        raise AssertionError("the upload was read")

    monkeypatch.setattr(service, "read_upload", never)
    with TestClient(create_app(settings)) as foreign:
        response = upload(foreign, b" " * (MIB + 65 * 1024))
    assert problem(response, 403)["title"] == "Cross-site request refused"


@pytest.mark.parametrize(
    ("content", "content_type", "reason"),
    [
        (b"--y\r\nxx", "multipart/form-data; boundary=x", ""),
        (b"abc", "multipart/form-data", "Missing boundary"),
        (None, None, "Field exceeded maximum size"),
        (None, None, "Too many fields"),
    ],
    ids=["bad-boundary", "no-boundary", "field-over-1-mb", "too-many-fields"],
)
def test_a_form_that_cannot_be_parsed_is_a_422(
    client: TestClient, content: bytes | None, content_type: str | None, reason: str
) -> None:
    """Malformed input is a 422, not Starlette's 400."""
    if content is not None:
        response = client.post(INDEX, content=content, headers={"content-type": content_type or ""})
    elif reason.startswith("Field"):
        response = client.post(INDEX, data={"path": "/" + "a" * (MIB + 1000)})
    else:
        response = client.post(INDEX, data={f"field{index}": "x" for index in range(1001)})

    body = problem(response, 422)
    assert [(item["loc"], item["code"]) for item in body["errors"]] == [([], "form_invalid")]
    assert body["errors"][0]["message"].startswith("The request body is not a valid form")
    assert reason in body["errors"][0]["message"]


BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE scanLog [
 <!ENTITY lol "lol">
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<scanLog xmlns="http://sector34.xray.aps.anl.gov/34ide/scanLog"><fullScan scanNumber="1">&lol3;</fullScan></scanLog>"""
EXTERNAL_ENTITY = b"""<?xml version="1.0"?>
<!DOCTYPE scanLog [<!ENTITY secret SYSTEM "file:///etc/passwd">]>
<scanLog xmlns="http://sector34.xray.aps.anl.gov/34ide/scanLog"><fullScan scanNumber="1">&secret;</fullScan></scanLog>"""


@pytest.mark.parametrize(
    ("log", "reason"),
    [
        (BILLION_LAUGHS, "declares a document type"),
        (EXTERNAL_ENTITY, "declares a document type"),
        (LOG[: len(LOG) // 2], "not well-formed XML"),
        (b'<?xml version="1.0"?><notes><note>scanNumber</note></notes>', "holds no scans"),
    ],
    ids=["entity-expansion", "external-entity", "truncated", "no-scans"],
)
def test_a_log_that_is_unsafe_or_not_a_scan_log_is_refused_and_not_staged(
    setup: tuple[Settings, TestClient], log: bytes, reason: str
) -> None:
    settings, client = setup
    body = problem(upload(client, log), 422)
    assert body["title"] == "Unreadable scan log" and reason in body["detail"]
    assert staged(settings) == []


@pytest.mark.parametrize(
    ("data", "files", "loc"),
    [
        ({}, None, ["file"]),
        ({"path": "/somewhere/log.xml"}, {"file": ("log.xml", LOG)}, ["file"]),
        ({"root": "data"}, None, ["file"]),
        ({"path": "/data/log\x00.xml"}, None, ["path"]),
    ],
    ids=["no-source", "both-sources", "root-without-path", "nul-in-a-field"],
)
def test_a_request_without_exactly_one_clean_source_is_a_422(
    client: TestClient, data: dict[str, str], files: Any, loc: list[str]
) -> None:
    body = problem(client.post(INDEX, data=data, files=files), 422)
    assert [item["loc"] for item in body["errors"]] == [loc]


# ---------------------------------------------------------------------------
# The import


def test_the_import_reports_each_selected_scan_and_commits_each_on_its_own(
    setup: tuple[Settings, TestClient], data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, client = setup
    token, _ = index_rows(client, with_duplicate_and_invalid(LOG))
    original = importer._parse_scan_element

    def fail_276993(scan: Any, *, xmlns: str, empty: str = "\n\t\t") -> Any:
        if scan.get("scanNumber") == "276993":
            raise ValueError("bad positioner")
        return original(scan, xmlns=xmlns, empty=empty)

    monkeypatch.setattr(importer, "_parse_scan_element", fail_276993)
    defaults = {
        "sample_name": "Si wafer",
        "filefolder": f" {data_root}/frames ",
        "filename_prefix": [" Si_", "", "Ge_"],
        "notes": "first beam",
    }

    response = client.post(
        IMPORT, json={"token": token, "scan_ids": [276993, 276990, 276991, 276990], "catalog_defaults": defaults}
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "results": [  # in log order; the repeated 276990 counts once (its first scan in the log)
            {"scan_number": 276990, "outcome": "imported", "error": None},
            {"scan_number": 276991, "outcome": "exists", "error": None},
            {"scan_number": 276993, "outcome": "failed", "error": "ValueError: bad positioner"},
        ],
        "summary": {"imported": 1, "exists": 1, "failed": 1},
    }
    scans = stored(settings)
    assert scans[276990] == ((f"{data_root}/frames", ["Si_", "Ge_"], "wire", "Si wafer", "first beam"), 1)
    assert set(scans) == {276990, 276991}  # the failed scan wrote nothing; the scan before it stayed committed

    # The defaults' aperture is the wire unless chosen; the folder may be empty (none yet).
    token, _ = index_rows(client, LOG)
    monkeypatch.setattr(importer, "_parse_scan_element", original)
    again = client.post(IMPORT, json={"token": token, "scan_ids": [276992], "catalog_defaults": {"aperture": "mask"}})
    assert again.json()["summary"] == {"imported": 1, "exists": 0, "failed": 0}
    assert stored(settings)[276992] == (("", [], "mask", "", ""), 1)


def test_a_scan_id_the_log_does_not_hold_is_a_422_and_nothing_is_imported(
    setup: tuple[Settings, TestClient],
) -> None:
    settings, client = setup
    token, _ = index_rows(client, LOG)

    body = problem(client.post(IMPORT, json={"token": token, "scan_ids": [276990, 5, 276992, 7]}), 422)

    assert [(item["loc"], item["code"]) for item in body["errors"]] == [
        (["scan_ids", 1], "unknown_scan"),
        (["scan_ids", 3], "unknown_scan"),
    ]
    assert set(stored(settings)) == {276991}


def test_the_staged_log_expires_24_hours_after_its_last_use(setup: tuple[Settings, TestClient]) -> None:
    settings, client = setup
    token, _ = index_rows(client, LOG)
    path = importer.staging_directory(settings.files) / f"{token}.xml"
    almost_a_day = time.time() - importer.SCAN_LOG_CACHE_TTL_SECONDS + 60
    os.utime(path, (almost_a_day, almost_a_day))

    used = client.post(IMPORT, json={"token": token, "scan_ids": [276990]})
    assert used.json()["summary"]["imported"] == 1
    assert path.stat().st_mtime > time.time() - 60  # a use starts the 24 hours again

    a_day_ago = time.time() - importer.SCAN_LOG_CACHE_TTL_SECONDS - 1
    os.utime(path, (a_day_ago, a_day_ago))
    expired = problem(client.post(IMPORT, json={"token": token, "scan_ids": [276992]}), 404)
    assert expired["title"] == "Scan log not found" and "index the log again" in expired["detail"]
    assert not path.exists()  # removed
    assert set(stored(settings)) == {276990, 276991}
    unknown = client.post(IMPORT, json={"token": "0" * 32, "scan_ids": [276992]})
    assert problem(unknown, 404)["title"] == "Scan log not found"
    malformed = problem(client.post(IMPORT, json={"token": f"../{token}", "scan_ids": [276992]}), 422)
    assert [item["loc"] for item in malformed["errors"]] == [["token"]]


# ---------------------------------------------------------------------------
# The roots, on both sources


def test_paths_outside_the_roots_are_refused_on_both_sources(
    setup: tuple[Settings, TestClient], tmp_path: Path, data_root: Path
) -> None:
    settings, client = setup
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "log.xml").write_bytes(LOG)
    (data_root / "link.xml").symlink_to(outside / "log.xml")
    (data_root / "inner").mkdir()
    (data_root / "inner" / "log.xml").write_bytes(LOG)
    other_root = tmp_path / "other"
    other_root.mkdir()
    (other_root / "log.xml").write_bytes(LOG)
    settings = make_settings(tmp_path, files={"roots": {"data": data_root, "other": other_root}})

    with TestClient(create_app(settings), headers=PORTAL_HEADERS) as two_roots:
        for form in (
            {"path": f"{data_root}/../outside/log.xml"},  # `..` out of the root
            {"path": str(data_root / "link.xml")},  # a symlink out
            {"path": "~user/log.xml"},  # `~user` is never expanded
            {"path": "inner/log.xml"},  # relative
            {"root": "data", "path": str(other_root / "log.xml")},  # inside a root, but not the one named
            {"root": "nowhere", "path": str(data_root / "inner" / "log.xml")},  # an unknown root
        ):
            assert problem(two_roots.post(INDEX, data=form), 400)["title"] == "Path not allowed", form
        assert two_roots.post(INDEX, data={"root": "other", "path": str(other_root / "log.xml")}).status_code == 200
        (data_root / "notes.csv").write_bytes(LOG)
        assert problem(two_roots.post(INDEX, data={"path": str(data_root / "notes.csv")}), 422)
        assert problem(two_roots.post(INDEX, data={"path": str(data_root / "missing.xml")}), 404)

        token = two_roots.post(INDEX, data={"path": str(data_root / "inner" / "log.xml")}).json()["token"]
        refused = two_roots.post(
            IMPORT,
            json={"token": token, "scan_ids": [276990], "catalog_defaults": {"filefolder": str(outside)}},
        )
        body = problem(refused, 400)
        assert [(item["loc"], item["code"]) for item in body["errors"]] == [
            (["catalog_defaults", "filefolder"], "path_not_allowed")
        ]
    assert set(stored(settings)) == {276991}
