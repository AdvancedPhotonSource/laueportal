from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from laueportal.app import (
    API_PREFIX,
    GZIP_MINIMUM_SIZE,
    SECURITY_HEADERS,
    check_operation_ids,
    create_app,
    route_operation_id,
)
from laueportal.core.errors import PROBLEM_JSON
from laueportal.core.settings import AppSettings
from tests.settings_support import make_settings

INDEX_HTML = "<!doctype html><title>3DMN Portal</title><div id=root></div>"


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX_HTML)
    (dist / "assets" / "index-abc123.js").write_text("console.log('shell');\n" * 400)  # > 4 KB
    (dist / "favicon.svg").write_text("<svg/>")
    return dist


@pytest.fixture
def spa_client(tmp_path: Path, dist: Path) -> TestClient:
    return TestClient(create_app(make_settings(tmp_path, app={"static_dir": dist})))


@pytest.mark.parametrize("path", ["/", "/scans", "/scans/12", "/indexing/7", "/jobs/3", "/peakindexing"])
def test_client_routes_get_index_html(spa_client: TestClient, path: str) -> None:
    response = spa_client.get(path)
    assert response.status_code == 200
    assert response.text == INDEX_HTML
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-cache"


def test_static_files_are_served(spa_client: TestClient) -> None:
    asset = spa_client.get("/assets/index-abc123.js")
    assert asset.status_code == 200 and asset.text.startswith("console.log")
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert spa_client.get("/favicon.svg").text == "<svg/>"


def test_missing_files_are_not_replaced_by_the_shell(spa_client: TestClient) -> None:
    response = spa_client.get("/assets/missing-123.js")
    assert response.status_code == 404
    assert INDEX_HTML not in response.text
    # A problem+json 404, as every error of the app (Starlette's default would be application/json).
    assert response.headers["content-type"] == PROBLEM_JSON
    assert response.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": "No file at /assets/missing-123.js",
    }
    assert spa_client.get("/missing.svg").headers["content-type"] == PROBLEM_JSON


def test_without_index_html_client_routes_are_problem_404s(tmp_path: Path, dist: Path) -> None:
    (dist / "index.html").unlink()
    client = TestClient(create_app(make_settings(tmp_path, app={"static_dir": dist})))

    response = client.get("/scans/12")

    assert response.status_code == 404 and response.headers["content-type"] == PROBLEM_JSON
    assert response.json()["detail"] == "The SPA build has no index.html (make build)"
    assert client.get("/assets/index-abc123.js").status_code == 200


def test_unknown_api_paths_stay_problem_404s_with_a_spa(spa_client: TestClient) -> None:
    for path in ("/api/v1/nope", "/api/v1/scans/12/nope", "/api"):
        response = spa_client.get(path)
        assert response.status_code == 404, path
        assert response.headers["content-type"] == PROBLEM_JSON
    assert spa_client.get(f"{API_PREFIX}/health").json()["version"]


def test_without_a_built_spa_the_api_still_starts(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="laueportal.app"):
        app = create_app(make_settings(tmp_path, app={"static_dir": tmp_path / "missing-dist"}))
    assert "does not exist" in caplog.text
    client = TestClient(app)
    assert client.get(f"{API_PREFIX}/health").status_code == 200
    response = client.get("/scans")
    assert response.status_code == 404 and response.headers["content-type"] == "application/json"
    assert client.get("/api/v1/nope").headers["content-type"] == PROBLEM_JSON


def test_only_the_configured_host_names_are_served(
    tmp_path: Path, dist: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A request whose ``Host`` is not one of ``app.allowed_hosts`` is a 400 before any route runs, so a page whose DNS
    name was pointed at the portal (DNS rebinding) cannot read its answers (the browse listings, the database path in
    ``/system/status``). The configured names are served on any port; the default names are those the dev and e2e stacks
    are reached by. The refusal is a problem, as every error, and the API's log names each refused host once (not again
    within ``HOST_WARNING_SECONDS``)."""
    assert AppSettings().allowed_hosts == ["localhost", "127.0.0.1"]
    allowed = ["localhost", "127.0.0.1", "portal.example.org"]
    app = create_app(make_settings(tmp_path, app={"static_dir": dist, "allowed_hosts": allowed}))

    for base in ("http://localhost:2101", "http://127.0.0.1:2100", "http://portal.example.org"):
        assert TestClient(app, base_url=base).get("/scans").text == INDEX_HTML, base
    refused_names = ("rebound.example.net", "portal.example.org.example.net", "testserver")
    with caplog.at_level(logging.WARNING, logger="laueportal.app"):
        for name in refused_names:
            for path in ("/scans", f"{API_PREFIX}/system/status"):
                refused = TestClient(app, base_url=f"http://{name}").get(path)
                assert (refused.status_code, refused.headers["content-type"]) == (400, PROBLEM_JSON), (name, path)
                assert refused.json() == {
                    "type": "about:blank",
                    "title": "Invalid host header",
                    "status": 400,
                    "detail": f"The Host header names '{name}', which is not a name this portal is served by.",
                }
    warnings = [record.getMessage() for record in caplog.records if record.name == "laueportal.app"]
    assert [message.split(":")[0] for message in warnings] == [
        f"Refused a request for host '{name}'" for name in refused_names
    ]
    assert all(
        "not in [app] allowed_hosts (localhost, 127.0.0.1, portal.example.org)" in message for message in warnings
    )


def test_every_answer_forbids_framing_and_type_sniffing(tmp_path: Path, dist: Path, spa_client: TestClient) -> None:
    """No page of another site may frame the portal's (clickjacking), and no browser takes an answer for a type other
    than the one sent: the SPA, its files, the API, a problem, a refused host."""
    answers = [
        spa_client.get("/scans"),
        spa_client.get("/assets/index-abc123.js"),
        spa_client.get(f"{API_PREFIX}/openapi.json"),
        spa_client.get(f"{API_PREFIX}/nothing"),
        TestClient(spa_client.app, base_url="http://rebound.example.net").get("/scans"),
    ]
    assert [answer.status_code for answer in answers] == [200, 200, 200, 404, 400]
    assert SECURITY_HEADERS == {
        "Content-Security-Policy": "frame-ancestors 'none'",
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
    }
    for answer in answers:
        assert {name: answer.headers.get(name) for name in SECURITY_HEADERS} == SECURITY_HEADERS, answer.url


def test_large_responses_are_gzipped(spa_client: TestClient) -> None:
    big = spa_client.get("/assets/index-abc123.js", headers={"Accept-Encoding": "gzip"})
    assert big.headers["content-encoding"] == "gzip"
    small = spa_client.get(f"{API_PREFIX}/health", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in small.headers


@pytest.mark.parametrize(("size", "gzipped"), [(2000, False), (4095, False), (4096, True), (6000, True)])
def test_gzip_starts_at_4_kib(tmp_path: Path, dist: Path, size: int, gzipped: bool) -> None:
    """GZip minimum 4 KB. Starlette's default (500 bytes) would compress 2000 bytes."""
    assert GZIP_MINIMUM_SIZE == 4096
    (dist / "assets" / f"chunk-{size}.js").write_text("x" * size)
    client = TestClient(create_app(make_settings(tmp_path, app={"static_dir": dist})))

    response = client.get(f"/assets/chunk-{size}.js", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200 and response.text == "x" * size
    assert (response.headers.get("content-encoding") == "gzip") is gzipped


def test_openapi_contract_names(tmp_path: Path) -> None:
    document = TestClient(create_app(make_settings(tmp_path))).get(f"{API_PREFIX}/openapi.json").json()
    operation = document["paths"]["/api/v1/health"]["get"]
    assert operation["operationId"] == "getHealth"
    assert operation["tags"] == ["system"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Health"
    }
    schemas = document["components"]["schemas"]
    assert set(schemas) == {
        "Health",
        # problem+json, for the routes that validate parameters or a body
        "Problem",
        "ProblemItem",
        # scans
        "ScanPage",
        "ScanSummary",
        "ScanDetail",
        "ScanMetadata",
        "ScanDimension",
        "ScanPositioner",
        "ScanCatalog",
        "ScanCatalogUpdate",
        "ScanNote",
        # the scan-log import
        "Body_indexScanLog",
        "ScanLogIndex",
        "ScanLogRow",
        "ScanLogStatus",
        "ScanImportRequest",
        "ScanImportDefaults",
        "ScanImportReport",
        "ScanImportItem",
        "ScanImportOutcome",
        "ScanImportSummary",
        # system status
        "SystemStatus",
        "DatabaseStatus",
        "QueueCounts",
        # the worker, its threads and the next items
        "WorkerStatus",
        "WorkerThread",
        "QueuedItem",
        "ActiveRun",
        "RemoteRunState",
        "RunReference",
        "RunKind",
        "RunStatus",
        # the run lists
        "ReconstructionRunPage",
        "ReconstructionRunSummary",
        "IndexingRunPage",
        "IndexingRunSummary",
        # the run monitor and its controls
        "JobPage",
        "JobSummary",
        "CancelResult",
        "CancelState",
        # the job page: the job, its failed inputs and its log
        "JobDetail",
        "JobProgress",
        "JobRemoteRun",
        # a crux run's remote observation and phase
        "RemoteObservation",
        "RemotePhase",
        "RunConfiguration",
        "RunDirectory",
        "RunFile",
        "FileState",
        "SubmittedRequest",
        "RequestManifest",
        "RunExecution",
        "RunHistory",
        "MessageClass",
        "RunArtifact",
        "ArtifactViewer",
        "FailurePage",
        "FailureRow",
        "JobLog",
        "LogTail",
        # the programs a run is created with
        "ProgramList",
        "Program",
        # a new run's validation
        "ValidationReport",
        # a create form's prefill, and a created run; a key's 409 carrying the run it created
        "ProgramPrefill",
        "CreatedRun",
        "KeyConflict",
        # the Live page's API: the DAQ list, a started live session, Start's 409
        "LiveDaqList",
        "LiveDaqEntry",
        "CreatedLiveRuns",
        "LiveRunsConflict",
        "OpenLiveRun",
        # the Live page's session
        "LiveSessionState",
        "LiveSession",
        "LiveSessionRun",
        "LiveSessionScan",
        "LiveArrivalStatus",
        # a live run's block on the job page
        "JobLive",
        # the files API: the path picker's roots and folder listings
        "FileRoots",
        "FileRoot",
        # the scan-log import's limit, extensions and start folder, on the roots
        "ScanLogSource",
        "DirectoryListing",
        "FileEntry",
        "EntryKind",
        # the create forms' file helpers
        "PatternsRequest",
        "FilePatterns",
        "FilePattern",
        "IndicesRequest",
        "FileIndices",
        # the results viewer: artifact, XML chooser, conversion and summary
        "IndexingArtifact",
        "ArtifactState",
        "ConversionJob",
        "ConversionStatus",
        "XmlChoice",
        "XmlSource",
        "IndexingResultsSummary",
        "ResultsCounts",
        "AxisChoice",
        "ColorModeChoice",
        "ResultsGeometry",
        "GeometrySource",
        "OutputPath",
        "OutputPathState",
        # the viewer's run: its job and stored parameters
        "IndexingRunDetail",
        "LaueGoParameters",
        # its products: the map's and pole figure's choices, the legend, point details, selection stats
        "MapColor",
        "Palette",
        "OrientationSymmetry",
        "PoleScheme",
        "LegendColor",
        "PointDetails",
        "PatternDetails",
        "AxisCoordinate",
        "SelectionStatsRequest",
        "SelectionStats",
        # the detector view's overlays, and the peaks and patterns tables
        "DetectorOverlays",
        "DetectorSummary",
        "DetectorImageInfo",
        "DetectedPeak",
        "DetectorPattern",
        "IndexedPeak",
        "SimulatedReflection",
        "PeakPage",
        "PeakRow",
        "PatternPage",
        "PatternRow",
        # the wire viewer's run, artifact and points
        "ReconstructionRunDetail",
        "WireParameters",
        "RootRelativePath",
        "ReconstructionRelativePaths",
        "ReconstructionArtifact",
        "ReconstructionArtifactState",
        "ReconstructionFormat",
        "ReconstructionCatalogStatus",
        "ReconstructionPointList",
        "ReconstructionPoint",
        "ReconstructionPointStatus",
        # a point's images
        "ReconstructionTrace",
        "ReconstructionReferenceKind",
        "ReconstructionRoi",
        "ReconstructionRoiTracesRequest",
        "ReconstructionRoiTrace",
        "ReconstructionRoiTraces",
    }
    assert schemas["Health"]["required"] == ["db", "worker", "version"]
    assert schemas["Health"]["properties"]["db"]["enum"] == ["ok", "error"]
    assert schemas["Health"]["properties"]["worker"]["anyOf"][0]["enum"] == ["running", "late", "not_running"]


def test_route_names_must_be_camel_case_operation_ids(tmp_path: Path) -> None:
    router = APIRouter()

    @router.get("/thing")
    def get_thing() -> dict[str, str]:  # name defaults to "get_thing"
        return {}

    app = create_app(make_settings(tmp_path))
    app.include_router(router, prefix=API_PREFIX)
    with pytest.raises(ValueError, match="must be camelCase"):
        check_operation_ids(app)  # create_app runs it over its own routers


def test_operation_ids_must_be_unique(tmp_path: Path) -> None:
    router = APIRouter()

    @router.get("/one", name="getThing")
    def one() -> dict[str, str]:
        return {}

    @router.get("/two", name="getThing")
    def two() -> dict[str, str]:
        return {}

    app = create_app(make_settings(tmp_path))
    app.include_router(router, prefix=API_PREFIX)
    with pytest.raises(ValueError, match="already used"):
        check_operation_ids(app)


def test_route_operation_id_is_the_name() -> None:
    router = APIRouter(generate_unique_id_function=route_operation_id)

    @router.get("/x", name="listScans")
    def list_scans() -> list[int]:
        return []

    assert [route.unique_id for route in router.routes] == ["listScans"]  # type: ignore[attr-defined]
