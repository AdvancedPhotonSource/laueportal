"""Run files on ``/net``: a route that looks at a recorded run directory on a hung mount answers within the probe's
bound, as a state or a 503, without waiting for the stat and without a database connection; and the routes that read
files read their rows in a session they close first. The hang is a stat that blocks on an event
(``tests.probe_support``); I1 (job 1) and R2 (job 2) keep their run directories on the mount ``net``."""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, update
from sqlalchemy.orm import Session

from laueportal import models
from laueportal.app import create_app
from laueportal.files.probe import NOT_RESPONDING, NotResponding
from laueportal.indexing import conversion
from laueportal.indexing.schemas import ConversionJob
from laueportal.indexing.viewer import mapped_path
from tests.api_support import PORTAL_HEADERS
from tests.indexing.test_viewer import FIXTURE_XML
from tests.indexing.test_viewer import add_run as add_indexing_run
from tests.probe_support import hang, install_probe
from tests.recon_support import copy_synthetic
from tests.reconstruction.conftest import Viewer as WireViewer
from tests.settings_support import make_database, make_settings
from tests.validation_support import make_place

pytestmark = pytest.mark.usefixtures("gc_disabled")  # each check starts within the probe's short timeout

TIMEOUT = 0.3
"""The probe's timeout here."""
POINT = "synthetic_wire_scan"
"""R2's point (the synthetic reconstruction)."""


@dataclass
class Net:
    client: TestClient
    engine: Engine
    net: Path
    """The mount that holds the run directories."""

    @property
    def pool(self) -> Any:
        return self.client.app.state.engine.pool  # type: ignore[attr-defined]  # the app's QueuePool

    def indexing_run(self, *xml_names: str) -> Path:
        """I1 (job 1) with its run directory ``net/index_1`` holding the fixture XML under each name."""
        directory = self.net / "index_1"
        directory.mkdir()
        for name in xml_names:
            shutil.copy(FIXTURE_XML, directory / name)
        add_indexing_run(self.engine, 1, directory)
        with Session(self.engine) as session:
            session.execute(
                update(models.Job)
                .where(models.Job.job_id == 1)
                .values(run_directory=str(directory), failure_report_path=str(directory / "failures.jsonl"))
            )
            session.commit()
        return directory

    def reconstruction_run(self) -> Path:
        """R2 (job 2) with its run directory ``net/rec_2`` holding the synthetic reconstruction."""
        directory = WireViewer(self.client, self.engine, self.net).add_run(2)
        copy_synthetic(directory)
        return directory


@pytest.fixture
def net(tmp_path: Path) -> Iterator[Net]:
    root = tmp_path / "data"
    (root / "net").mkdir(parents=True)
    settings, engine = make_database(tmp_path, files={"roots": {"data": root}})
    with TestClient(create_app(settings), headers=PORTAL_HEADERS) as client:
        yield Net(client, engine, root / "net")
    engine.dispose()


def state(body: dict[str, Any]) -> Any:
    return body["state"]


HUNG_ROUTES: dict[str, tuple[str, int, Callable[[dict[str, Any]], Any], Any]] = {
    "indexing artifact": ("/api/v1/indexing-runs/1/artifact", 200, state, "not_responding"),
    "indexing summary": ("/api/v1/indexing-runs/1/summary", 503, lambda body: body["type"], NOT_RESPONDING),
    "reconstruction artifact": ("/api/v1/reconstruction-runs/2/artifact", 200, state, "not_responding"),
    "depth frame": (
        f"/api/v1/reconstruction-runs/2/points/{POINT}/frames/0",
        503,
        lambda body: body["type"],
        NOT_RESPONDING,
    ),
    "job page": ("/api/v1/jobs/1", 200, lambda body: body["configuration"]["run_directory"]["state"], "not_responding"),
    "job log": ("/api/v1/jobs/1/log", 200, lambda body: body["run_log"]["file"]["state"], "not_responding"),
}


@pytest.mark.parametrize("route", HUNG_ROUTES)
def test_a_hung_run_directory_answers_within_the_bound_holding_no_connection(
    net: Net, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """The first stat of the run directory blocks (it would block forever): the route answers once the probe's
    timeout has passed, while that stat still blocks, and no database connection was held while it did."""
    net.indexing_run("a.xml")
    net.reconstruction_run()
    install_probe(monkeypatch, mounts=(net.net,), timeout=TIMEOUT)
    hung = hang(monkeypatch, net.net)
    held: list[int] = []
    hung.on_enter = lambda: held.append(net.pool.checkedout())
    path, status, answer, expected = HUNG_ROUTES[route]
    try:
        started = time.monotonic()
        response = net.client.get(path)
        elapsed = time.monotonic() - started
        still_blocked = not hung.released.is_set() and hung.entered.is_set()
    finally:
        hung.released.set()

    assert (response.status_code, answer(response.json())) == (status, expected), response.text
    assert (still_blocked, hung.calls, held) == (True, 1, [0])  # one stat, waited for at most the timeout
    assert TIMEOUT <= elapsed < TIMEOUT + 5


def test_a_hung_mount_holds_two_threads_and_a_healthy_mounts_pages_answer_at_once(
    net: Net, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """More checks of a hung mount at once than the probe has threads. Two run (a mount's cap) and the others answer
    once it is remembered; the third thread is free, so a healthy mount's job page and artifact answer at once, with
    their files' real states."""
    net.indexing_run("a.xml")  # I1's run directory on the healthy mount
    dead = tmp_path / "dead"
    dead.mkdir()
    timeout = 1.0
    probe = install_probe(monkeypatch, mounts=(net.net, dead), timeout=timeout, threads=3)
    hung = hang(monkeypatch, dead)
    answers: list[str] = []

    def request(index: int) -> None:
        try:
            probe.run(dead / str(index), lambda: os.stat(dead / str(index)))
        except NotResponding:
            answers.append("not responding")

    requests = [threading.Thread(target=request, args=(index,)) for index in range(4)]
    try:
        for thread in requests:
            thread.start()
        for thread in requests:
            thread.join()
        started = time.monotonic()
        job = net.client.get("/api/v1/jobs/1").json()["configuration"]
        artifact = net.client.get("/api/v1/indexing-runs/1/artifact").json()
        elapsed = time.monotonic() - started
    finally:
        hung.released.set()

    states = {file["state"] for file in job["files"]} | {job["run_directory"]["state"]}
    assert (answers, hung.calls) == (["not responding"] * 4, 2)
    assert (job["run_directory"]["state"], "not_responding" in states, artifact["state"]) == (
        "available",
        False,
        "xml_only",
    )
    assert elapsed < timeout


ROUTES = [
    ("GET", "/api/v1/indexing-runs/1/artifact"),
    ("POST", "/api/v1/indexing-runs/1/artifact/select-xml"),
    ("POST", "/api/v1/indexing-runs/1/convert"),
    ("GET", "/api/v1/jobs/1"),
    ("GET", "/api/v1/jobs/1/log"),
    ("GET", "/api/v1/jobs/1/failures"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_routes_close_their_session_before_they_touch_files(
    net: Net, monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    """Each route's file checks (``files.probe``) find no connection checked out, nor do its conversion lookups, which
    read the conversion row in a short session of their own (the select-xml route writes its choice in a short session
    after them)."""
    choosing = path.endswith("select-xml")
    net.indexing_run(*(("a.xml", "b.xml") if choosing else ("a.xml",)))
    probe = install_probe(monkeypatch, timeout=30)
    files: list[int] = []
    requested: list[bool] = []
    lookups: list[int] = []
    look = probe.run

    def run(where: Any, check: Callable[[], Any], **options: Any) -> Any:
        files.append(net.pool.checkedout())
        requested.append(options.get("requested", False))
        return look(where, check, **options)

    def find(indexing_id: int, *, sessions: Any) -> None:
        lookups.append(net.pool.checkedout())

    def enqueue(indexing_id: int, *, sessions: Any) -> ConversionJob:
        lookups.append(net.pool.checkedout())
        return ConversionJob.model_validate(
            {"id": conversion.conversion_queue_id(indexing_id), "kind": conversion.CONVERT_JOB_TYPE}
            | {"indexing_id": indexing_id, "status": "queued", "message": None}
            | {"enqueued_at": None, "started_at": None, "ended_at": None, "held": False}
        )

    monkeypatch.setattr(probe, "run", run)
    monkeypatch.setattr(conversion, "find_conversion", find)
    monkeypatch.setattr(conversion, "enqueue_conversion", enqueue)

    response = net.client.request(method, path, json={"name": "b.xml"} if choosing else None)

    assert response.is_success, response.text
    assert files and set(files) == {0}
    if "indexing" in path:
        assert lookups and set(lookups) == {0}
    assert not any(requested)  # recorded paths: the probe's threads beyond the requests' budget stay theirs


@dataclass
class Requested:
    client: TestClient
    form: Callable[..., dict[str, Any]]
    folder: Path
    """A folder of frames on the mount ``net`` (a root of its own too), with a scan log."""


@pytest.fixture
def requested(tmp_path: Path) -> Iterator[Requested]:
    root = tmp_path / "root"
    place = make_place(tmp_path, roots={"data": root, "net": root / "net"}, scan_log_dir=root / "net")
    folder = root / "net" / "scan_1"
    folder.mkdir(parents=True)
    for name in ("img_1.h5", "img_2.h5", "log.xml"):
        (folder / name).write_text("x")
    with TestClient(create_app(place.settings), headers=PORTAL_HEADERS) as client:
        yield Requested(client, lambda **values: place.form("wire", **values), folder)
    place.engine.dispose()


def _net_root(body: dict[str, Any]) -> Any:
    return next(root["available"] for root in body["roots"] if root["name"] == "net")


def _input_issue(body: dict[str, Any]) -> Any:
    return [issue["code"] for issue in body["issues"] if issue["loc"] == ["input_path"]]


REQUESTED: dict[str, tuple[Callable[[Requested], Any], int, Callable[[dict[str, Any]], Any], Any]] = {
    "file browser": (
        lambda net: net.client.get("/api/v1/files/browse", params={"path": str(net.folder)}),
        503,
        lambda body: body["type"],
        NOT_RESPONDING,
    ),
    "file roots": (lambda net: net.client.get("/api/v1/files/roots"), 200, _net_root, False),
    "find matching files": (
        lambda net: net.client.post("/api/v1/files/patterns", json={"folder": str(net.folder), "program": "wire"}),
        503,
        lambda body: body["type"],
        NOT_RESPONDING,
    ),
    "find indices": (
        lambda net: net.client.post("/api/v1/files/indices", json={"folder": str(net.folder), "program": "wire"}),
        503,
        lambda body: body["type"],
        NOT_RESPONDING,
    ),
    "form validation": (
        lambda net: net.client.post("/api/v1/reconstruction-runs/validate", json=net.form(input_path=str(net.folder))),
        200,
        _input_issue,
        ["not_responding"],
    ),
    "scan-log import": (
        lambda net: net.client.post("/api/v1/scans/import/index", data={"path": str(net.folder / "log.xml")}),
        503,
        lambda body: body["type"],
        NOT_RESPONDING,
    ),
}


@pytest.mark.parametrize("route", REQUESTED)
def test_a_requested_path_on_a_hung_mount_answers_within_the_bound(
    requested: Requested, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """The file browser, its roots, Find Matching Files, Find Indices, form validation and the scan-log import from a
    server path resolve and stat the requested path through the probe first (then list or read it in the request's
    thread), so a folder on a hung mount answers once the probe's timeout has passed, as a 503, an unavailable root (and
    the scan logs' start folder elsewhere, its own being on the mount) or a validation error, while its first stat still
    blocks. Every check is a requested one."""
    probe = install_probe(monkeypatch, mounts=(requested.folder.parent,), timeout=TIMEOUT)
    hung = hang(monkeypatch, requested.folder.parent)
    send, status, answer, expected = REQUESTED[route]
    kinds: list[bool] = []
    look = probe.run

    def run(where: Any, check: Callable[[], Any], **options: Any) -> Any:
        kinds.append(options.get("requested", False))
        return look(where, check, **options)

    monkeypatch.setattr(probe, "run", run)
    try:
        started = time.monotonic()
        response = send(requested)
        elapsed = time.monotonic() - started
        still_blocked = not hung.released.is_set() and hung.entered.is_set()
    finally:
        hung.released.set()

    assert (response.status_code, answer(response.json())) == (status, expected), response.text
    assert (still_blocked, hung.calls) == (True, 1)
    assert TIMEOUT <= elapsed < TIMEOUT + 5
    assert kinds and all(kinds)  # every check is a requested one (the requests' budget)


def _pages(net: Net) -> tuple[str, str, int]:
    """The healthy mount's pages: I1's run directory on its job page, its artifact, a browse of its folder."""
    job = net.client.get("/api/v1/jobs/1").json()["configuration"]
    artifact = net.client.get("/api/v1/indexing-runs/1/artifact").json()
    browse = net.client.get("/api/v1/files/browse", params={"path": str(net.net / "index_1")})
    return job["run_directory"]["state"], artifact["state"], browse.status_code


@pytest.mark.parametrize("how", ["dotdot", "link", "long"])
def test_a_crafted_path_never_marks_the_runs_mount(
    net: Net, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """``<dead>/../data/net/index_1`` (whose text names the runs' mount while a walk would stat ``dead``) is refused
    before any stat; a user's link inside the root to a hung mount is followed under that mount's key. An 8 KB path
    inside the runs' mount (4,000 components, which would take the probe's whole timeout to resolve) is refused by its
    length before any check of it (the roots' own walks only). Either way the runs' mount is not marked, and its pages
    still answer."""
    net.indexing_run("a.xml")
    dead = tmp_path / "dead"
    dead.mkdir()
    probe = install_probe(monkeypatch, mounts=(net.net, dead), timeout=TIMEOUT)
    before = _pages(net)
    if how == "dotdot":
        crafted = f"{dead}/../data/net/index_1"
    elif how == "link":
        (net.net / "trap").symlink_to(dead / "x")
        crafted = str(net.net / "trap" / "y")
    else:
        crafted = str(net.net) + "/a" * 4000
    hung = hang(monkeypatch, dead)
    checks: list[Any] = []
    look = probe.run

    def run(where: Any, check: Callable[[], Any], **options: Any) -> Any:
        checks.append(where)
        return look(where, check, **options)

    monkeypatch.setattr(probe, "run", run)
    try:
        status = net.client.get("/api/v1/files/browse", params={"path": crafted}).status_code
        roots_only = all(net.net.parent.is_relative_to(where) for where in checks)
        after = _pages(net)
        marked = (probe.hung(net.net / "index_1"), probe.hung(dead))
    finally:
        hung.released.set()

    assert before == ("available", "xml_only", 200)
    assert (status, after, marked) == ((503, before, (False, True)) if how == "link" else (400, before, (False, False)))
    assert roots_only == (how != "link")  # refused before any check of the path itself


@pytest.mark.parametrize("where", ["outside the roots", "links in the root"])
def test_requests_for_hung_paths_leave_the_recorded_paths_threads(
    net: Net, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """Saturation: eight browses on four slow mounts at once. Paths outside every root are refused before any stat (0
    threads stuck, no stat, listing or open of a hung path at all); links inside the root to those mounts hold at most
    the requests' budget (six of eight threads). The runs' pages then answer at once with their real states: none of
    their checks waits on the hung mounts. Counted, not timed (the pages' 20-60 ms of their own work can take 0.62 s in
    the full suite, whose full garbage collections take 0.3-0.65 s): a check that waits for a turn or a thread ends not
    responding (``FileProbe.not_responding``), and a stat, listing or open of a hung path outside the probe is counted
    by the hang (``Hung.calls``)."""
    net.indexing_run("a.xml")
    deads = [tmp_path / f"dead{index}" for index in range(4)]  # four mounts
    for index, dead in enumerate(deads):
        dead.mkdir()
        (net.net.parent / f"link{index}").symlink_to(dead)
    probe = install_probe(monkeypatch, mounts=(net.net, *deads), timeout=TIMEOUT)
    hangs = [hang(monkeypatch, dead) for dead in deads]
    paths = [
        str(dead / "x") if where == "outside the roots" else str(net.net.parent / f"link{index}" / "x")
        for index, dead in enumerate(deads)
    ]
    statuses: list[int] = []

    def browse(path: str) -> None:
        statuses.append(net.client.get("/api/v1/files/browse", params={"path": path}).status_code)

    requests = [threading.Thread(target=browse, args=(path,)) for path in paths for _ in range(2)]
    unanswered: list[Any] = []
    not_responding = probe.not_responding

    def waited(path: Any) -> NotResponding:
        unanswered.append(path)
        return not_responding(path)

    try:
        for thread in requests:
            thread.start()
        for thread in requests:
            thread.join()
        stuck = probe._stuck_total
        browsed = [hung.calls for hung in hangs]
        monkeypatch.setattr(probe, "not_responding", waited)
        job = net.client.get("/api/v1/jobs/1").json()["configuration"]
        artifact = net.client.get("/api/v1/indexing-runs/1/artifact").json()
        paged = [hung.calls - calls for hung, calls in zip(hangs, browsed, strict=True)]
        stuck_after = probe._stuck_total
    finally:
        for hung in hangs:
            hung.released.set()

    expected = (400, 0) if where == "outside the roots" else (503, 6)
    assert (sorted(set(statuses)), len(statuses), stuck) == ([expected[0]], 8, expected[1])
    assert sum(browsed) == expected[1]  # outside the roots: no stat of the path, in or out of the probe
    assert (job["run_directory"]["state"], artifact["state"]) == ("available", "xml_only")
    assert (paged, unanswered, stuck_after) == ([0] * 4, [], stuck)  # no page's check waited on the hung mounts


def test_a_folder_lists_with_its_links_and_mounts_that_do_not_answer(
    net: Net, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder holding a link to a hung mount and a hung mount point still lists: those entries are not available (a
    link's kind unknown), and the listing waits one timeout for them in all."""
    folder = net.net.parent / "folder"
    (folder / "plain").mkdir(parents=True)
    dead, other = tmp_path / "dead", tmp_path / "other"
    for target in (dead, other, folder / "mounted"):
        target.mkdir()
    (folder / "link").symlink_to(dead / "x")
    (folder / "second").symlink_to(other / "x")
    install_probe(monkeypatch, mounts=(dead, other, folder / "mounted"), timeout=TIMEOUT)
    hangs = [hang(monkeypatch, path) for path in (dead, other, folder / "mounted")]
    try:
        started = time.monotonic()
        response = net.client.get("/api/v1/files/browse", params={"path": str(folder)})
        elapsed = time.monotonic() - started
    finally:
        for hung in hangs:
            hung.released.set()

    assert response.status_code == 200, response.text
    entries = [(entry["name"], entry["kind"], entry["available"]) for entry in response.json()["entries"]]
    assert entries == [
        ("mounted", "directory", False),
        ("plain", "directory", True),
        ("link", None, False),
        ("second", None, False),
    ]
    assert sum(hung.calls for hung in hangs) == 1 and elapsed < 2 * TIMEOUT


def test_a_remote_runs_mapped_path_on_a_hung_mount_answers_within_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remote run's value under the APS data root (its plan maps the root to itself) is confined to it through the
    probe (``remote.paths.portal_path`` with ``files.roots.recorded_real_path``), so a hung data mount answers
    ``not_responding`` once the timeout has passed, while the stat still blocks, never waiting for the mount in the
    request's thread."""
    data = tmp_path / "data"
    (data / "scan").mkdir(parents=True)
    (data / "scan" / "p1.h5").write_text("x")
    run = tmp_path / "runs" / "run1"
    (run / "incoming" / "s1").mkdir(parents=True)
    install_probe(monkeypatch, mounts=(data, tmp_path / "runs"), timeout=TIMEOUT)
    settings = make_settings(tmp_path, files={"roots": {"data": data, "runs": tmp_path / "runs"}})
    mappings = {str(run): str(run / "incoming" / "s1"), str(data): str(data)}

    def state() -> str:
        return mapped_path(
            str(data / "scan" / "p1.h5"), mappings=mappings, base=str(run), settings=settings
        ).shown.state

    healthy = state()
    hung = hang(monkeypatch, data)
    try:
        started = time.monotonic()
        answered = state()
        elapsed = time.monotonic() - started
        still_blocked = not hung.released.is_set() and hung.entered.is_set()
    finally:
        hung.released.set()

    assert (healthy, answered, still_blocked) == ("available", "not_responding", True)
    assert TIMEOUT <= elapsed < TIMEOUT + 5


def test_a_prefill_from_a_reconstruction_on_a_hung_mount_answers_within_the_bound(
    net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LaueGo form's prefill from a reconstruction looks for its catalog through the probe; on a hung mount it
    answers once the timeout has passed, with the run directory as the input (as when the catalog is not there),
    while the stat still blocks."""
    directory = net.reconstruction_run()
    install_probe(monkeypatch, mounts=(net.net,), timeout=TIMEOUT)

    def input_path() -> Any:
        response = net.client.get("/api/v1/programs/lauego/prefill", params={"reconstruction": "2"})
        assert response.status_code == 200, response.text
        return response.json()["values"]["input_path"]

    healthy = input_path()
    hung = hang(monkeypatch, net.net)
    try:
        started = time.monotonic()
        answered = input_path()
        elapsed = time.monotonic() - started
        still_blocked = not hung.released.is_set() and hung.entered.is_set()
    finally:
        hung.released.set()

    assert (healthy, answered, still_blocked) == (str(directory / "reconstruction" / "scan.h5"), str(directory), True)
    assert TIMEOUT <= elapsed < TIMEOUT + 5
