"""The files API: the allowed roots and the roots-restricted folder listing of the path picker, on temporary
directories."""

from __future__ import annotations

import os
import shutil
import socket
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from laueportal.app import create_app
from laueportal.core import api as core_api
from laueportal.core import migrations
from laueportal.core.api import FOLDER_READS_AT_ONCE, FOLDER_READS_WAITING, RETRY_AFTER_SECONDS
from laueportal.core.errors import PROBLEM_JSON
from laueportal.files.inputs import natural_key
from tests.api_support import PORTAL_HEADERS
from tests.settings_support import make_settings

ROOTS = "/api/v1/files/roots"
BROWSE = "/api/v1/files/browse"


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    return root


@pytest.fixture
def runs_root(tmp_path: Path) -> Path:
    root = tmp_path / "runs_root"
    root.mkdir()
    return root


def client_for(tmp_path: Path, **files: Any) -> TestClient:
    return TestClient(create_app(make_settings(tmp_path, files=files)))


@pytest.fixture
def client(tmp_path: Path, data_root: Path, runs_root: Path) -> Iterator[TestClient]:
    with client_for(tmp_path, roots={"data": data_root, "runs": runs_root}) as client:
        yield client


def browse(client: TestClient, **params: str) -> dict[str, Any]:
    response = client.get(BROWSE, params=params)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def names(listing: dict[str, Any]) -> list[tuple[str, str]]:
    return [(entry["name"], entry["kind"]) for entry in listing["entries"]]


def problem(client: TestClient, status: int, title: str, **params: str) -> dict[str, Any]:
    response = client.get(BROWSE, params=params)
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_JSON
    body = response.json()
    assert (body["status"], body["title"]) == (status, title)
    return body


# ---------------------------------------------------------------------------
# GET /files/roots


def test_roots_are_listed_in_the_settings_order_resolved_with_their_availability(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real)
    (tmp_path / "locked" / "data").mkdir(parents=True)
    roots = {"zeta": tmp_path / "alias", "absent": tmp_path / "net" / "storage", "alpha": real}

    with client_for(tmp_path, roots=roots | {"locked": tmp_path / "locked" / "data"}) as client:
        (tmp_path / "locked").chmod(0)  # a folder the server may not open: unavailable, not a 500
        try:
            response = client.get(ROOTS)
        finally:
            (tmp_path / "locked").chmod(0o700)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "roots": [
            {"name": "zeta", "path": str(real), "available": True},
            {"name": "absent", "path": str(tmp_path / "net" / "storage"), "available": False},
            {"name": "alpha", "path": str(real), "available": True},
            {"name": "locked", "path": str(tmp_path / "locked" / "data"), "available": False},
        ],
        # The import's defaults; without files.scan_log_dir its picker opens at the first root.
        "scan_logs": {"max_bytes": 25 * 1024 * 1024, "extensions": [".xml", ".txt"], "start_folder": str(real)},
    }


def test_without_roots_the_list_is_empty_and_nothing_can_be_browsed(tmp_path: Path, data_root: Path) -> None:
    with client_for(tmp_path) as client:
        assert client.get(ROOTS).json() == {
            "roots": [],
            "scan_logs": {"max_bytes": 25 * 1024 * 1024, "extensions": [".xml", ".txt"], "start_folder": None},
        }
        body = problem(client, 400, "Path not allowed", path=str(data_root))
        assert "No file roots are configured" in body["detail"]
        body = problem(client, 400, "Path not allowed", root="data")
        assert body["detail"] == "There is no root named 'data' (no file roots are configured)."


def test_the_scan_log_import_s_limit_extensions_and_start_folder_are_published(
    tmp_path: Path, data_root: Path, runs_root: Path
) -> None:
    logs = data_root / "logs"
    logs.mkdir()
    (tmp_path / "logs-link").symlink_to(logs)
    roots = {"runs": runs_root, "data": data_root}
    settings = {"max_scan_log_mb": 7, "scan_log_extensions": [".XML", ".log"]}

    with client_for(tmp_path, roots=roots, scan_log_dir=tmp_path / "logs-link", **settings) as client:
        published = client.get(ROOTS).json()["scan_logs"]
    assert published == {"max_bytes": 7 * 1024 * 1024, "extensions": [".xml", ".log"], "start_folder": str(logs)}

    # A folder outside the roots is not published: the picker opens at the first root, as when it is not set.
    with client_for(tmp_path, roots=roots, scan_log_dir=tmp_path) as client:
        assert client.get(ROOTS).json()["scan_logs"]["start_folder"] == str(runs_root)


# ---------------------------------------------------------------------------
# GET /files/browse: listings


def test_a_root_lists_folders_first_then_files_in_natural_order_without_hidden_entries(
    client: TestClient, data_root: Path
) -> None:
    for name in ("frame_10.h5", "frame_2.h5", "Frame_3.h5", "frame_1.h5", ".hidden.h5", "notes.txt"):
        (data_root / name).write_text("x" * 5)
    for name in ("scan_10", "scan_9", ".cache"):
        (data_root / name).mkdir()

    listing = browse(client, root="data")

    assert (listing["root"], listing["root_path"], listing["path"]) == ("data", str(data_root), str(data_root))
    assert listing["truncated"] is False
    assert names(listing) == [
        ("scan_9", "directory"),
        ("scan_10", "directory"),
        ("frame_1.h5", "file"),
        ("frame_2.h5", "file"),
        ("Frame_3.h5", "file"),
        ("frame_10.h5", "file"),
        ("notes.txt", "file"),
    ]
    folder, first = listing["entries"][0], listing["entries"][2]
    assert (folder["path"], folder["size"]) == (str(data_root / "scan_9"), None)
    assert (first["path"], first["size"]) == (str(data_root / "frame_1.h5"), 5)
    assert first["modified"].endswith("Z")


def test_a_path_inside_the_roots_is_listed_under_its_root(client: TestClient, data_root: Path) -> None:
    folder = data_root / "2026" / "scan_7"
    folder.mkdir(parents=True)
    (folder / "img_1.h5").write_text("")

    listing = browse(client, path=str(folder))
    unresolved = browse(client, path=f"{data_root}//2026/./scan_7/")

    assert (listing["root"], listing["root_path"], listing["path"]) == ("data", str(data_root), str(folder))
    assert names(listing) == [("img_1.h5", "file")]
    assert unresolved["path"] == str(folder)


def test_a_path_with_its_root_must_be_inside_that_root(client: TestClient, data_root: Path, runs_root: Path) -> None:
    folder = data_root / "scan_7"
    folder.mkdir()

    assert browse(client, root="data", path=str(folder))["path"] == str(folder)
    # A blank path is no path: the root's folder.
    assert browse(client, root="runs", path=" ")["path"] == str(runs_root)
    body = problem(client, 400, "Path not allowed", root="runs", path=str(folder))
    assert body["detail"] == f"{folder} is not inside the root 'runs' ({runs_root})."


def test_nested_roots_report_the_root_asked_for(tmp_path: Path, data_root: Path) -> None:
    inner = data_root / "beamtime"
    (inner / "scan_1").mkdir(parents=True)

    with client_for(tmp_path, roots={"data": data_root, "beamtime": inner}) as client:
        innermost = browse(client, path=str(inner / "scan_1"))
        asked = browse(client, root="data", path=str(inner / "scan_1"))

    assert (innermost["root"], innermost["root_path"]) == ("beamtime", str(inner))
    assert (asked["root"], asked["root_path"]) == ("data", str(data_root))


def test_the_extension_filter_lists_matching_files_and_every_folder(client: TestClient, data_root: Path) -> None:
    for name in ("scan_b.xml", "scan_a.XML", "notes.txt", "image.h5", "README"):
        (data_root / name).write_text("")
    (data_root / "logs.xml.d").mkdir()

    xml = browse(client, root="data", extensions=".xml")
    logs = browse(client, root="data", extensions=" .XML, .txt ,")

    assert names(xml) == [("logs.xml.d", "directory"), ("scan_a.XML", "file"), ("scan_b.xml", "file")]
    assert names(logs) == names(xml)[:1] + [("notes.txt", "file"), ("scan_a.XML", "file"), ("scan_b.xml", "file")]


@pytest.mark.parametrize("extensions", ["xml", ".", ".x/y", ".xml,txt", ".a b"])
def test_a_malformed_extension_is_a_validation_problem(client: TestClient, extensions: str) -> None:
    body = problem(client, 422, "Validation failed", root="data", extensions=extensions)
    assert [item["loc"] for item in body["errors"]] == [["query", "extensions"]]


def test_the_listing_is_capped_and_says_so(tmp_path: Path, data_root: Path) -> None:
    (data_root / "folder").mkdir()
    for index in range(4):
        (data_root / f"frame_{index}.h5").write_text("")

    with client_for(tmp_path, roots={"data": data_root}, listing_cap=3) as client:
        capped = browse(client, root="data")
        filtered = browse(client, root="data", extensions=".txt")

    # Folders come first, so a capped listing keeps them.
    assert names(capped) == [("folder", "directory"), ("frame_0.h5", "file"), ("frame_1.h5", "file")]
    assert capped["truncated"] is True
    assert (names(filtered), filtered["truncated"]) == ([("folder", "directory")], False)


# ---------------------------------------------------------------------------
# GET /files/browse: the roots


def test_symlinks_out_of_the_roots_are_neither_listed_nor_browsed(
    tmp_path: Path, client: TestClient, data_root: Path, runs_root: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.h5").write_text("")
    (data_root / "escape").symlink_to(outside)
    (data_root / "secret.h5").symlink_to(outside / "secret.h5")
    (data_root / "runs").symlink_to(runs_root)
    (data_root / "kept.h5").write_text("")

    listing = browse(client, root="data")

    # A link into another root is listed at its target, under that root.
    assert [(entry["name"], entry["path"]) for entry in listing["entries"]] == [
        ("runs", str(runs_root)),
        ("kept.h5", str(data_root / "kept.h5")),
    ]
    assert browse(client, path=str(data_root / "runs"))["root"] == "runs"
    for path in (data_root / "escape", data_root / "escape" / "deeper"):
        body = problem(client, 400, "Path not allowed", path=str(path))
        assert "outside the allowed roots" in body["detail"]


@pytest.mark.parametrize(
    "path", ["{tmp}", "{tmp}/outside", "{data}/..", "{data}/../outside", "/", "~", "~/data", "data", "./data"]
)
def test_a_path_outside_the_roots_is_a_400(tmp_path: Path, client: TestClient, data_root: Path, path: str) -> None:
    (tmp_path / "outside").mkdir()

    body = problem(client, 400, "Path not allowed", path=path.format(tmp=tmp_path, data=data_root))

    assert body["detail"]


def test_an_unknown_root_is_a_400(client: TestClient, data_root: Path) -> None:
    body = problem(client, 400, "Path not allowed", root="storage")
    assert body["detail"] == "There is no root named 'storage' (the allowed roots are data, runs)."
    problem(client, 400, "Path not allowed", root="storage", path=str(data_root))


def test_a_missing_folder_is_a_404_with_the_reason(tmp_path: Path, client: TestClient, data_root: Path) -> None:
    body = problem(client, 404, "Path not found", path=str(data_root / "scan_404"))
    assert body["detail"] == f"{data_root / 'scan_404'} does not exist or is not available on this host."

    # A root whose folder is not on this host (a /net root of another host, say).
    with client_for(tmp_path, roots={"storage": tmp_path / "net" / "storage"}) as other:
        body = problem(other, 404, "Path not found", root="storage")
    assert "not available on this host" in body["detail"]


def test_a_file_is_not_a_folder(client: TestClient, data_root: Path) -> None:
    scan_log = data_root / "scan.xml"
    scan_log.write_text("<scan/>")

    body = problem(client, 422, "Invalid selection", path=str(scan_log))

    assert body["detail"] == f"{scan_log} is not a directory."


def test_neither_a_root_nor_a_path_is_a_validation_problem(client: TestClient) -> None:
    for params in ({}, {"root": "", "path": " "}):
        body = problem(client, 422, "Validation failed", **params)
        assert [item["loc"] for item in body["errors"]] == [["query", "root"]]
    body = problem(client, 422, "Validation failed", root="data", depth="2")
    assert [item["loc"] for item in body["errors"]] == [["query", "depth"]]


def _make_files(folder: Path, count: int, name: str) -> None:
    """``count`` empty files ``name.format(index)`` in ``folder`` (quickly: one open each, relative to the folder)."""
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        for index in range(count):
            os.close(os.open(name.format(index), os.O_CREAT | os.O_WRONLY, 0o644, dir_fd=descriptor))
    finally:
        os.close(descriptor)


def test_a_folder_of_15000_files_lists_every_subfolder(tmp_path: Path, data_root: Path) -> None:
    """A data folder of 15,000 frames and subfolders (real data folders reach 15,089 frames). Every subfolder is listed
    first, whatever its place in the file system's order."""
    frames = data_root / "frames"
    frames.mkdir()
    folders = [f"sub_{index:02d}" for index in range(30)]
    # Half the folders before the frames and half after: one half lands past the frames whether the file
    # system lists the oldest entries first or the newest (tmpfs).
    for name in folders[:15]:
        (frames / name).mkdir()
    _make_files(frames, 15_000, "frame_{}.h5")
    for name in folders[15:]:
        (frames / name).mkdir()
    order = [entry.name for entry in os.scandir(frames)]
    if not any(order.index(name) >= 10_000 for name in folders):
        pytest.skip("this file system lists every folder within its first 10,000 entries")

    with client_for(tmp_path, roots={"data": data_root}) as client:
        listing = browse(client, path=str(frames))

    assert names(listing)[:30] == [(name, "directory") for name in folders]
    files = [name for name, kind in names(listing)[30:] if kind == "file"]
    assert len(files) == 970 and all(name.startswith("frame_") for name in files)
    assert files == sorted(files, key=natural_key)
    assert listing["truncated"] is True


@pytest.mark.slow
def test_a_folder_of_200000_files_answers_well_under_a_second(tmp_path: Path, data_root: Path) -> None:
    """200,000 entries take 2 s a request when each is typed and sorted. A wall-clock check, so it runs in make
    test-slow; the bound itself is counted in tests/files/test_browser.py."""
    big = data_root / "frames"
    big.mkdir()
    try:
        _make_files(big, 200_000, "frame_{:06d}.h5")
        with client_for(tmp_path, roots={"data": data_root}) as client:
            browse(client, root="data")  # the app's first request
            started = time.perf_counter()
            listing = browse(client, path=str(big))
            elapsed = time.perf_counter() - started
    finally:
        shutil.rmtree(big)  # not left in pytest's kept temporary folders
    assert (len(listing["entries"]), listing["truncated"]) == (1000, True)
    assert elapsed < 1.0, f"{elapsed:.2f} s"


# ---------------------------------------------------------------------------
# GET /files/browse: names the API cannot carry, special files, unreadable folders


def utf8_invalid(folder: Path, name: bytes) -> bytes:
    """``folder/name`` as bytes, for a name that is not valid UTF-8 (e.g. Latin-1 ``M\xfcller``)."""
    return os.fsencode(folder) + b"/" + name


def test_names_that_are_not_utf8_are_left_out_and_counted(client: TestClient, data_root: Path) -> None:
    # Latin-1 names, plausible on a long-lived share: os keeps their bytes as surrogate escapes, which UTF-8 (so
    # the API's JSON answer) cannot encode, so a listing that held them could not be sent at all.
    with open(utf8_invalid(data_root, b"M\xfcller.h5"), "wb") as handle:
        handle.write(b"x")
    os.mkdir(utf8_invalid(data_root, b"D\xfcrer"))
    (data_root / "Mueller.h5").write_text("x")
    (data_root / "scans").mkdir()

    listing = browse(client, root="data")
    filtered = browse(client, root="data", extensions=".txt")

    assert names(listing) == [("scans", "directory"), ("Mueller.h5", "file")]
    assert listing["invalid_names"] == 2
    # Files the extension filter leaves out are not counted.
    assert (names(filtered), filtered["invalid_names"]) == ([("scans", "directory")], 1)


def test_a_link_whose_target_is_not_utf8_is_left_out_and_cannot_be_browsed(client: TestClient, data_root: Path) -> None:
    target = utf8_invalid(data_root, b"D\xfcrer")
    os.mkdir(target)
    (data_root / "durer").symlink_to(os.fsdecode(target))
    (data_root / "kept").mkdir()

    listing = browse(client, root="data")
    body = problem(client, 422, "Invalid selection", path=str(data_root / "durer"))

    assert (names(listing), listing["invalid_names"]) == ([("kept", "directory")], 2)
    # The reason names the folder, its bad bytes shown as U+FFFD.
    assert body["detail"] == f"{data_root}/D\ufffdrer cannot be listed: its path is not valid UTF-8."


def test_a_root_whose_folder_is_not_utf8_is_unavailable(tmp_path: Path, data_root: Path) -> None:
    target = utf8_invalid(tmp_path, b"Str\xf6me")
    os.mkdir(target)
    (tmp_path / "stroeme").symlink_to(os.fsdecode(target))

    with client_for(tmp_path, roots={"data": data_root, "stroeme": tmp_path / "stroeme"}) as client:
        roots = client.get(ROOTS).json()["roots"]
        body = problem(client, 422, "Invalid selection", root="stroeme")

    assert roots[1] == {"name": "stroeme", "path": f"{tmp_path}/Str\ufffdme", "available": False}
    assert "not valid UTF-8" in body["detail"]


def test_fifos_sockets_and_broken_links_are_left_out(
    client: TestClient, data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    os.mkfifo(data_root / "a_fifo")
    # A socket bound by a relative name: a temporary directory's full path can exceed AF_UNIX's 107 bytes.
    monkeypatch.chdir(data_root)
    unix = socket.socket(socket.AF_UNIX)
    unix.bind("a_socket")
    (data_root / "loop_a").symlink_to(data_root / "loop_b")
    (data_root / "loop_b").symlink_to(data_root / "loop_a")
    (data_root / "dangling").symlink_to(data_root / "missing")
    (data_root / "kept.h5").write_text("x")
    (data_root / "folder").mkdir()
    try:
        listing = browse(client, root="data")
    finally:
        unix.close()

    assert names(listing) == [("folder", "directory"), ("kept.h5", "file")]
    assert listing["invalid_names"] == 0


def test_an_unreadable_folder_is_a_422(client: TestClient, data_root: Path) -> None:
    locked = data_root / "locked"
    locked.mkdir()
    (locked / "inside.h5").write_text("x")
    locked.chmod(0)
    try:
        assert not os.access(locked, os.R_OK), "the folder is still readable (are the tests running as root?)"
        body = problem(client, 422, "Invalid selection", path=str(locked))
        # Its parent lists it: it is a folder, only its contents cannot be read.
        assert ("locked", "directory") in names(browse(client, root="data"))
    finally:
        locked.chmod(0o755)

    assert body["detail"] == f"{locked} cannot be read: Permission denied."


FOLDER_READS: dict[str, tuple[str, str, dict[str, Any], int]] = {
    "browse": ("GET", BROWSE, {"params": {"root": "data"}}, 200),
    "patterns": ("POST", "/api/v1/files/patterns", {"json": {"folder": "{data}", "program": "lauego"}}, 200),
    "indices": ("POST", "/api/v1/files/indices", {"json": {"folder": "{data}", "program": "wire"}}, 200),
    "validate-lauego": ("POST", "/api/v1/indexing-runs/validate", {"json": {}}, 200),
    "validate-wire": ("POST", "/api/v1/reconstruction-runs/validate", {"json": {}}, 200),
    "create-lauego": ("POST", "/api/v1/indexing-runs", {"json": {}, "headers": {"Idempotency-Key": "k"}}, 422),
    "create-wire": ("POST", "/api/v1/reconstruction-runs", {"json": {}, "headers": {"Idempotency-Key": "k"}}, 422),
}
"""Each kind of folder read (``core.api.read_folders``): its request (``{data}``: the data root) and its status once
it has its turn (an empty form is a run with issues: a validation's 200, a creation's 422)."""


@pytest.mark.parametrize("route", FOLDER_READS)
def test_folder_reads_take_a_shared_turn_and_a_few_wait(
    route: str, client: TestClient, data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file browser's listing, Find Matching Files, Find Indices and a run form's validation and creation (which
    list the input folder) are folder reads: at most ``FOLDER_READS_AT_ONCE`` at once (each one's work has a bound of
    its own); the others wait for a turn outside the thread pool, at most ``FOLDER_READS_WAITING`` of them in all, and
    the next is a 503 with ``Retry-After``, a creation's before its key is claimed."""
    import anyio

    assert (FOLDER_READS_AT_ONCE, FOLDER_READS_WAITING) == (4, 16)
    monkeypatch.setattr(core_api, "FOLDER_READS_WAITING", 1)
    migrations.upgrade(client.app.state.settings.database)  # type: ignore[attr-defined]  (a creation's key)
    limiter = client.app.state.folder_reads  # type: ignore[attr-defined]

    def read(name: str) -> Any:
        method, url, options, _ = FOLDER_READS[name]
        json = options.get("json")
        if json is not None:
            json = {key: str(data_root) if value == "{data}" else value for key, value in json.items()}
        headers = PORTAL_HEADERS | options.get("headers", {})
        return client.request(method, url, params=options.get("params"), json=json, headers=headers)

    async def while_every_turn_is_taken() -> tuple[list[int], list[Any], int]:
        statuses: list[int] = []

        async def waiting() -> None:
            statuses.append((await anyio.to_thread.run_sync(read, route)).status_code)

        others = [object() for _ in range(int(limiter.total_tokens))]  # other folder reads, holding every turn
        async with anyio.create_task_group() as group:
            for other in others:
                await limiter.acquire_on_behalf_of(other)
            group.start_soon(waiting)
            with anyio.fail_after(10):  # the limiter has no event for its waiters
                while limiter.statistics().tasks_waiting < 1:  # noqa: ASYNC110
                    await anyio.sleep(0.01)
            refused = [await anyio.to_thread.run_sync(read, name) for name in (route, "browse")]
            read_while_waiting = len(statuses)
            for other in others:
                limiter.release_on_behalf_of(other)
        return statuses, refused, read_while_waiting

    statuses, refused, read_while_waiting = client.portal.call(while_every_turn_is_taken)  # type: ignore[union-attr]

    assert limiter.total_tokens == FOLDER_READS_AT_ONCE
    assert (read_while_waiting, statuses) == (0, [FOLDER_READS[route][3]])
    for answer in refused:  # the route's own, and the file browser's: one queue
        assert (answer.status_code, answer.headers["retry-after"], answer.json()["title"]) == (
            503,
            str(RETRY_AFTER_SECONDS),
            "Too many folder reads waiting",
        )
    assert limiter.statistics().borrowed_tokens == 0
    with client.app.state.engine.connect() as connection:  # type: ignore[attr-defined]
        assert connection.exec_driver_sql("SELECT count(*) FROM idempotency_key").scalar_one() == 0  # none claimed
