"""The results viewer's detector image and overlays, and its peaks and patterns tables (``indexing.detector``,
``indexing.tables``, ``science.overlays``, ``science.tables``), on a run made from ``tests/fixtures/test_indexing.xml``
(``test_viewer_products.results_xml``: 12, 8, 4 and 3 peaks at steps 0 to 3, two patterns at step 0, none at step 3)
with a geometry that holds its detector, ``TEST-DET``. The expected values are lauelab's. I1 and I2:
``test_viewer_demo.py`` (slow)."""

from __future__ import annotations

import json
import os
import re
import threading
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread
import h5py
import numpy as np
import pytest
from lauelab.visualization import VisualizationDataset, indexed_peak_table, pattern_table, prepare_detector_view
from sqlalchemy import URL, create_engine

from laueportal.core.api import RETRY_AFTER_SECONDS
from laueportal.core.db import make_sessionmaker
from laueportal.indexing import detector
from laueportal.indexing import tables as grids
from laueportal.indexing.detector import SIMULATIONS_WAITING
from laueportal.indexing.products import PRODUCT_FAILED
from laueportal.indexing.schemas import PatternRow, PeakRow
from laueportal.remote.paths import PLAN_NAME
from laueportal.science import cache as cache_module
from laueportal.science import once, tables
from laueportal.science.cache import load_dataset
from laueportal.science.viewer import to_data_scope
from tests.indexing.test_viewer import GEOMETRY, REMOTE_RUN, RUNS, Viewer, converted, open_viewer
from tests.indexing.test_viewer_products import EVERY_STEP, results_xml

DETECTOR = "TEST-DET"
FRAME = np.arange(24, dtype=np.uint16).reshape(4, 6) * 1000
"""Step 0's image: ``test/image_001.h5`` beside the output."""


def detector_geometry(target: Path) -> Path:
    """The test geometry with its first detector named as the fixture's (``TEST-DET``)."""
    text = GEOMETRY.read_text()
    first = re.search(r"<ID>(.*?)</ID>", text)
    assert first is not None
    target.write_text(text.replace(first.group(0), f"<ID>{DETECTOR}</ID>", 1))
    return target


def write_frame(path: Path, data: np.ndarray, dataset: str = "entry1/data/data") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as file:
        file.create_dataset(dataset, data=data)
    return path


def with_input_images(source: Path, images: dict[int, str]) -> Path:
    tree = ET.parse(source)
    for index, value in images.items():
        element = tree.getroot().findall("step")[index].find("detector/inputImage")
        assert element is not None
        element.text = value
    tree.write(source)
    return source


@dataclass
class Run:
    viewer: Viewer
    directory: Path
    dataset: VisualizationDataset
    """The run's results as lauelab loads them, for the expected values."""

    def get(self, product: str, headers: dict[str, str] | None = None, **params: Any) -> Any:
        return self.viewer.client.get(f"{RUNS}/1/{product}", params=params, headers=headers)


def make_run(viewer: Viewer, tmp_path: Path, *, images: dict[int, str] | None = None, geometry: bool = True) -> Run:
    directory = viewer.run_directory(1)
    source = results_xml(tmp_path / "steps.xml")
    if images:
        with_input_images(source, images)
    geometry_file = str(detector_geometry(directory / "geometry.xml")) if geometry else None
    results = converted(directory, source=source, geometry_path=geometry_file)
    write_frame(directory / "test" / "image_001.h5", FRAME)
    viewer.add_run(1, directory)
    return Run(viewer, directory, load_dataset(results, geometry=geometry_file))


@pytest.fixture
def run(tmp_path: Path) -> Iterator[Run]:
    for viewer in open_viewer(tmp_path):
        yield make_run(viewer, tmp_path)


@pytest.fixture
def viewer(tmp_path: Path) -> Iterator[Viewer]:
    yield from open_viewer(tmp_path)


# ---------------------------------------------------------------------------
# The detector image


def test_the_image_is_the_frame_in_uint16_or_float32_for_signed_values(run: Run) -> None:
    signed = np.array([[-3, 0, 7], [1, 2, 70_000]], dtype=np.int32)  # a reconstructed frame's values
    write_frame(run.directory / "test" / "image_002.h5", signed, "/custom/image")

    counts = run.get("detector/0/image")
    floats = run.get("detector/1/image")

    assert counts.status_code == 200, counts.text
    assert counts.headers["content-type"] == "application/octet-stream"
    assert (counts.headers["x-frame-shape"], counts.headers["x-frame-dtype"]) == ("4,6", "uint16")
    assert counts.content == FRAME.astype("<u2").tobytes()
    assert floats.status_code == 200, floats.text
    assert (floats.headers["x-frame-shape"], floats.headers["x-frame-dtype"]) == ("2,3", "float32")
    assert np.array_equal(np.frombuffer(floats.content, "<f4").reshape(2, 3), signed)  # the first 2-D dataset


@pytest.mark.parametrize(
    ("remote", "value", "why"),
    [
        pytest.param(False, "test/image_001.h5", None, id="relative-beside-the-output"),
        pytest.param(True, f"{REMOTE_RUN}/test/image_001.h5", None, id="remote-mapped"),
        pytest.param(
            False,
            "{outside}/image_001.h5",
            ("{outside}/image_001.h5 (not opened: {outside}/image_001.h5 is outside the allowed roots (data).)",),
            id="outside-the-roots",
        ),
        pytest.param(
            True,
            "{outside}/image_001.h5",
            (
                "{outside}/image_001.h5 (not opened: not available for this run: its plan maps no such path, or it "
                "leads out of the mapped folder)",
            ),
            id="remote-unmapped",
        ),
        pytest.param(False, "test/missing.h5", ("{run}/test/missing.h5 (no such file)",), id="missing"),
        pytest.param(
            True,
            f"{REMOTE_RUN}/test/missing.h5",
            (f"{REMOTE_RUN}/test/missing.h5 → {{run}}/test/missing.h5 (no such file)",),
            id="remote-mapped-missing",
        ),
    ],
)
def test_an_image_is_read_where_its_input_image_maps_and_otherwise_is_a_404_with_the_reason(
    viewer: Viewer, tmp_path: Path, remote: bool, value: str, why: tuple[str, ...] | None
) -> None:
    outside = tmp_path / "outside"  # outside the file roots: a frame that must never be served
    write_frame(outside / "image_001.h5", FRAME)
    run = make_run(viewer, tmp_path, images={0: value.format(outside=outside)})
    if remote:
        plan = {"version": 1, "submission_id": "laue-1-0001", "path_map": {str(run.directory): REMOTE_RUN}}
        (run.directory / PLAN_NAME).write_text(json.dumps(plan))

    response = run.get("detector/0/image")
    image = run.get("detector/0/overlays").json()["summary"]["image"]

    if why is None:
        assert response.status_code == 200, response.text
        assert response.content == FRAME.astype("<u2").tobytes()
        assert image == {
            "available": True,
            "path": str(run.directory / "test" / "image_001.h5"),
            "dataset": "/entry1/data/data",
            "reason": None,
        }
        return
    assert response.status_code == 404, response.text
    problem = response.json()
    assert problem["title"] == "Detector image not found"
    assert problem["detail"].startswith("I1, step 0: Detector image file was not found. Tried: ")
    assert all(part.format(outside=outside, run=run.directory) in problem["detail"] for part in why)
    assert image["available"] is False and image["reason"] in problem["detail"]


def test_a_path_in_the_reason_with_a_byte_that_is_not_utf8_shows_as_a_replacement_character(
    viewer: Viewer, tmp_path: Path
) -> None:
    run = make_run(viewer, tmp_path, images={0: "odd/missing.h5"})
    target = run.directory / os.fsdecode(b"t\xfc")  # a folder name that is not UTF-8
    target.mkdir()
    (run.directory / "odd").symlink_to(target)  # where odd/missing.h5 is looked for, mapped and confined

    overlays = run.get("detector/0/overlays")
    response = run.get("detector/0/image")

    assert overlays.status_code == 200, overlays.text
    reason = overlays.json()["summary"]["image"]["reason"]
    assert f"{run.directory}/odd/missing.h5 → {run.directory}/t\ufffd/missing.h5 (no such file)" in reason
    assert response.status_code == 404 and reason in response.json()["detail"]


# ---------------------------------------------------------------------------
# The overlays


def test_the_overlays_are_lauelabs_detector_view_with_its_summary(run: Run) -> None:
    view = prepare_detector_view(run.dataset, frame_id=run.dataset.frame_ids[0], patterns="all")

    overlays = run.get("detector/0/overlays")
    pattern0_only = run.get("detector/0/overlays", **{**EVERY_STEP, "pattern0_only": "true"}).json()

    assert overlays.status_code == 200, overlays.text
    body = overlays.json()
    assert (body["width"], body["height"]) == (2048, 2048)
    assert [(peak["peak_index"], peak["x"], peak["y"], peak["indexed"]) for peak in body["detected"]] == [
        (int(index), float(xy[0]), float(xy[1]), bool(indexed))
        for index, xy, indexed in zip(view.measured_peak_indices, view.measured_xy, view.measured_indexed, strict=True)
    ]
    for pattern, expected in zip(body["patterns"], view.patterns, strict=True):
        assert pattern["pattern_index"] == expected.pattern_index
        assert [[peak[name] for name in ("peak_index", "h", "k", "l")] for peak in pattern["peaks"]] == [
            [int(index), *map(int, hkl)]
            for index, hkl in zip(expected.measured_peak_indices, expected.hkl, strict=True)
        ]
        assert [[peak["x"], peak["y"]] for peak in pattern["peaks"]] == [
            [float(value) if np.isfinite(value) else None for value in xy] for xy in expected.predicted_xy
        ]
        assert pattern["simulated"] is None
    assert [pattern["in_scope"] for pattern in pattern0_only["patterns"]] == [True, False]
    summary = body["summary"]
    assert {key: summary[key] for key in ("step", "frame_id", "in_scope", "detector_id", "input_image")} == {
        "step": 0,
        "frame_id": 0,
        "in_scope": True,
        "detector_id": DETECTOR,
        "input_image": "test/image_001.h5",
    }
    assert (summary["n_detected"], summary["n_indexed"]) == (12, int(view.measured_indexed.sum()))
    assert summary["indexed_fraction"] == summary["n_indexed"] / 12
    assert (body["energy_range_kev"], body["simulation_error"]) == (None, None)


def test_a_failed_simulation_keeps_the_other_layers(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    import lauelab.visualization.preparation as preparation

    calls: list[int] = []

    def failing(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        raise RuntimeError("Failed to execute the reflection simulation backend")

    monkeypatch.setattr(preparation, "simulate_reflections", failing)
    plain = run.get("detector/0/overlays").json()

    simulated = run.get("detector/0/overlays", simulate="true", kev="8,25")

    assert simulated.status_code == 200, simulated.text
    body = simulated.json()
    assert body["simulation_error"] == "Failed to execute the reflection simulation backend"
    assert body["energy_range_kev"] == [8.0, 25.0]
    assert (body["detected"], body["summary"]) == (plain["detected"], plain["summary"])
    assert [pattern["peaks"] for pattern in body["patterns"]] == [pattern["peaks"] for pattern in plain["patterns"]]
    assert all(pattern["simulated"] is None for pattern in body["patterns"])
    tried = len(calls)
    again = run.get("detector/0/overlays", simulate="true", kev="8,25")  # a failed simulation is not kept
    assert (again.json()["simulation_error"], len(calls)) == (body["simulation_error"], 2 * tried)


async def asgi_get(app: Any, path: str, query: str, *, client_gone: bool = False) -> tuple[int, bytes]:
    """GET ``path`` from the app as uvicorn would call it; with ``client_gone`` the client has disconnected (uvicorn's
    ``receive`` then answers ``http.disconnect``, which the TestClient cannot)."""

    status, body = 0, b""

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"} if client_gone else {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status, body
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            body += message.get("body", b"")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": query.encode(),
        "headers": [(b"host", b"testserver")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "state": {},
    }
    await app(scope, receive, send)
    return status, body


def test_simulations_run_one_at_a_time_are_kept_and_one_whose_client_has_gone_is_not_run(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lauelab.visualization.preparation as preparation

    from laueportal.science import overlays

    simulated: list[str] = []
    prepare = overlays.prepare_detector_view

    class Reflections:
        """A pattern's simulated reflections, one on the frame (the fixture's crystal has no simulation)."""

        hkl, detector_xy = np.array([[1, 1, 1]]), np.array([[1.0, 2.0]])
        energy_kev, relative_intensity = np.array([10.0]), np.array([1.0])

        def missing_from(self, hkl: Any) -> Reflections:
            return self

    def counting(dataset: Any, *, frame_id: Any, simulation_energy_range_kev: Any = None, **kwargs: Any) -> Any:
        if simulation_energy_range_kev is not None:
            simulated.append(str(frame_id))
        return prepare(dataset, frame_id=frame_id, simulation_energy_range_kev=simulation_energy_range_kev, **kwargs)

    monkeypatch.setattr(overlays, "prepare_detector_view", counting)
    monkeypatch.setattr(preparation, "simulate_reflections", lambda *args, **kwargs: Reflections())
    app, query = run.viewer.client.app, "simulate=true&kev=8,25"
    limiter = app.state.simulations

    async def while_another_simulation_runs() -> list[tuple[int, bytes]]:
        results: list[tuple[int, bytes]] = [(0, b"")] * 3

        async def get(index: int, step: int, client_gone: bool) -> None:
            results[index] = await asgi_get(app, f"{RUNS}/1/detector/{step}/overlays", query, client_gone=client_gone)

        async with anyio.create_task_group() as group:
            await limiter.acquire()  # another simulation's turn
            for index, (step, client_gone) in enumerate([(1, False), (2, True), (1, False)]):
                group.start_soon(get, index, step, client_gone)
            with anyio.fail_after(10):  # the limiter has no event for its waiters
                while limiter.statistics().tasks_waiting < 2 or results[1][0] == 0:  # noqa: ASYNC110
                    await anyio.sleep(0.01)
            limiter.release()
        return results

    first, gone, second = run.viewer.client.portal.call(while_another_simulation_runs)
    another_scope = run.get("detector/1/overlays", simulate="true", kev="8,25", min_peaks=1)

    # The step's simulation ran once: the second request waited its turn and found it kept, as a request in another
    # scope did without waiting; the request whose client had gone was dropped while it waited, unrun.
    assert simulated == [str(run.dataset.frame_ids[1])]
    assert (first[0], second[0], gone[0], another_scope.status_code) == (200, 200, 499, 200)
    assert first[1] == second[1]
    patterns = json.loads(first[1])["patterns"]
    assert patterns and all(pattern["simulated"] for pattern in patterns)
    assert [pattern["simulated"] for pattern in another_scope.json()["patterns"]] == [
        pattern["simulated"] for pattern in patterns
    ]
    assert limiter.statistics().borrowed_tokens == 0


def test_requests_waiting_for_a_simulation_or_a_dataset_load_hold_no_database_connection(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a one-connection pool, simulations waiting for their turn and requests waiting for a slow dataset load leave
    an unrelated route answering at once. Each request is seen waiting (the limiter's count, the load started,
    ``science.once.Pending.wait_async``) before the pool is checked: no sleeps."""

    database = run.viewer.settings.database.path
    pool = create_engine(URL.create("sqlite", database=str(database)), pool_size=1, max_overflow=0, pool_timeout=2)
    app = run.viewer.client.app
    app.state.sessions = make_sessionmaker(pool)
    run.viewer.add_run(2, run.viewer.run_directory(2, "a.xml"))  # an Output XML, whose load is held below
    slow_xml, loading, loaded = str(run.viewer.root / "index_2" / "a.xml"), threading.Event(), threading.Event()
    load, wait, waiting = cache_module.load_dataset, once.Pending.wait_async, threading.Event()

    def held(path: str, *, geometry: str | None = None) -> VisualizationDataset:
        if os.path.realpath(path) == os.path.realpath(slow_xml):
            loading.set()
            assert loaded.wait(10)
        return load(path, geometry=geometry)

    async def watched(pending: once.Pending) -> Any:
        waiting.set()
        return await wait(pending)

    monkeypatch.setattr(cache_module, "load_dataset", held)
    monkeypatch.setattr(once.Pending, "wait_async", watched)
    limiter = app.state.simulations

    async def until(condition: Any) -> None:
        with anyio.fail_after(10):  # the limiter has no event for its waiters
            while not condition():  # noqa: ASYNC110
                await anyio.sleep(0.01)

    async def while_requests_wait() -> tuple[list[int], int, int]:
        statuses = [0] * 5
        overlays, summary = f"{RUNS}/1/detector/{{}}/overlays", f"{RUNS}/2/summary"

        async def get(index: int, path: str, query: str) -> None:
            statuses[index] = (await asgi_get(app, path, query))[0]

        async with anyio.create_task_group() as group:
            await limiter.acquire()  # another simulation's turn
            for index, step in enumerate((1, 2, 3)):
                group.start_soon(get, index, overlays.format(step), "simulate=true&kev=8,25")
            await until(lambda: limiter.statistics().tasks_waiting == 3)  # each waits for its turn, I1 loaded
            group.start_soon(get, 3, summary, "")
            await until(loading.is_set)  # one summary loads I2
            waiting.clear()  # nothing else waits for a computation now
            group.start_soon(get, 4, summary, "")
            await until(waiting.is_set)  # the other waits for that load
            held_connections = pool.pool.checkedout()  # type: ignore[attr-defined]  # a QueuePool
            with anyio.fail_after(5):
                unrelated = (await asgi_get(app, f"{RUNS}/1", ""))[0]
            loaded.set()
            limiter.release()
        return statuses, held_connections, unrelated

    statuses, held_connections, unrelated = run.viewer.client.portal.call(while_requests_wait)
    pool.dispose()

    assert (held_connections, unrelated) == (0, 200)
    assert statuses == [200] * 5


def test_requests_waiting_for_a_dataset_load_hold_no_thread(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    """While one request loads a run's dataset, the other product requests for it wait in the event loop, so the thread
    pool lends one thread, the load's. Each waiter is seen waiting (``science.once.Pending.wait_async``) before the pool
    is counted: no sleeps."""

    app = run.viewer.client.app
    run.viewer.add_run(2, run.viewer.run_directory(2, "a.xml"))  # an Output XML, whose load is held below
    slow_xml, loading, loaded = str(run.viewer.root / "index_2" / "a.xml"), threading.Event(), threading.Event()
    load, wait, waiters = cache_module.load_dataset, once.Pending.wait_async, []

    def held(path: str, *, geometry: str | None = None) -> VisualizationDataset:
        if os.path.realpath(path) == os.path.realpath(slow_xml):
            loading.set()
            assert loaded.wait(10)
        return load(path, geometry=geometry)

    async def watched(pending: once.Pending) -> Any:
        waiters.append(pending)
        return await wait(pending)

    monkeypatch.setattr(cache_module, "load_dataset", held)
    monkeypatch.setattr(once.Pending, "wait_async", watched)

    async def until(condition: Any) -> None:
        with anyio.fail_after(10):
            while not condition():  # noqa: ASYNC110
                await anyio.sleep(0.01)

    async def while_requests_wait() -> tuple[list[int], int]:
        threads = anyio.to_thread.current_default_thread_limiter()
        statuses = [0] * 4

        async def get(index: int, product: str) -> None:
            statuses[index] = (await asgi_get(app, f"{RUNS}/2/{product}", ""))[0]

        async with anyio.create_task_group() as group:
            group.start_soon(get, 0, "summary")
            await until(loading.is_set)  # it loads I2, in a worker thread
            for index, product in enumerate(("summary", "peaks", "patterns"), start=1):
                group.start_soon(get, index, product)
            await until(lambda: len(waiters) == 3)  # each waits for that load
            borrowed = threads.borrowed_tokens
            loaded.set()
        return statuses, borrowed

    statuses, borrowed = run.viewer.client.portal.call(while_requests_wait)

    assert (borrowed, statuses) == (1, [200] * 4)


def test_at_most_four_simulations_wait_and_the_next_is_a_503_the_page_can_show(run: Run) -> None:
    app = run.viewer.client.app
    limiter = app.state.simulations

    async def with_four_waiting() -> tuple[list[int], int, dict[str, Any], str | None]:
        statuses = [0] * SIMULATIONS_WAITING

        async def get(index: int, step: int) -> None:
            statuses[index] = (await asgi_get(app, f"{RUNS}/1/detector/{step}/overlays", "simulate=true"))[0]

        async with anyio.create_task_group() as group:
            await limiter.acquire()  # another simulation's turn
            for index in range(SIMULATIONS_WAITING):
                group.start_soon(get, index, index % 4)
            with anyio.fail_after(10):
                while limiter.statistics().tasks_waiting < SIMULATIONS_WAITING:  # noqa: ASYNC110
                    await anyio.sleep(0.01)
            refused = await anyio.to_thread.run_sync(lambda: run.get("detector/1/overlays", simulate="true"))
            limiter.release()
        return statuses, refused.status_code, refused.json(), refused.headers.get("retry-after")

    statuses, status, problem, retry_after = run.viewer.client.portal.call(with_four_waiting)

    assert (status, problem["title"], retry_after) == (503, "Too many detector simulations waiting", "10")
    assert problem["detail"].startswith(f"{SIMULATIONS_WAITING} detector simulations are already waiting")
    assert statuses == [200] * SIMULATIONS_WAITING
    assert run.get("detector/1/overlays").status_code == 200  # without a simulation, no turn is taken


def test_detector_images_take_a_turn_and_wait_for_it_holding_no_database_connection(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The depth frames' turn: at most ``IMAGES_AT_ONCE`` images are read and copied at once. The others wait outside
    the thread pool, holding no database connection (a one-connection pool): one whose client has gone is dropped unread
    (499), at most ``IMAGES_WAITING`` wait, and the next is a 503 with ``Retry-After``."""
    database = run.viewer.settings.database.path
    pool = create_engine(URL.create("sqlite", database=str(database)), pool_size=1, max_overflow=0, pool_timeout=2)
    app = run.viewer.client.app
    app.state.sessions = make_sessionmaker(pool)
    limiter = app.state.detector_images
    monkeypatch.setattr(detector, "IMAGES_WAITING", 2)
    reads: list[int] = []
    image_product = detector.image_product
    monkeypatch.setattr(
        detector,
        "image_product",
        lambda loaded, image, **kwargs: reads.append(image.step) or image_product(loaded, image, **kwargs),
    )
    path = f"{RUNS}/1/detector/0/image"

    async def while_every_turn_is_taken() -> tuple[list[int], list[int], int, Any]:
        statuses = [0, 0, 0]

        async def get(index: int, *, client_gone: bool = False) -> None:
            statuses[index] = (await asgi_get(app, path, "", client_gone=client_gone))[0]

        others = [object() for _ in range(int(limiter.total_tokens))]  # other images, holding every turn
        async with anyio.create_task_group() as group:
            for other in others:
                await limiter.acquire_on_behalf_of(other)
            group.start_soon(partial(get, 0, client_gone=True))
            with anyio.fail_after(10):  # the limiter has no event for its waiters
                while statuses[0] == 0:  # noqa: ASYNC110  # it waited, then left when its client went
                    await anyio.sleep(0.01)
                group.start_soon(get, 1)
                group.start_soon(get, 2)
                while limiter.statistics().tasks_waiting < 2:  # noqa: ASYNC110
                    await anyio.sleep(0.01)
            held_connections = pool.pool.checkedout()  # type: ignore[attr-defined]  # a QueuePool
            refused = await anyio.to_thread.run_sync(lambda: run.get("detector/0/image"))
            read_while_waiting = list(reads)
            for other in others:
                limiter.release_on_behalf_of(other)
        return statuses, read_while_waiting, held_connections, refused

    statuses, read_while_waiting, held_connections, refused = run.viewer.client.portal.call(while_every_turn_is_taken)
    pool.dispose()

    assert limiter.total_tokens == detector.IMAGES_AT_ONCE == 6
    assert (statuses, read_while_waiting, reads, held_connections) == ([499, 200, 200], [], [0, 0], 0)
    assert (refused.status_code, refused.headers["retry-after"], refused.json()["title"]) == (
        503,
        str(RETRY_AFTER_SECONDS),
        "Too many detector images waiting",
    )
    assert limiter.statistics().borrowed_tokens == 0


@pytest.mark.parametrize(
    ("path", "params", "status", "expected"),
    [
        pytest.param("detector/0/overlays", {}, 422, PRODUCT_FAILED, id="no-geometry"),
        pytest.param("detector/0/overlays", {"kev": "30,6"}, 422, "kev", id="kev-decreasing"),
        pytest.param("detector/0/overlays", {"kev": "6"}, 422, "kev", id="kev-one-value"),
        pytest.param("detector/0/overlays", {"kev": "6,60"}, 422, "kev", id="kev-wider-than-the-beamlines-window"),
        pytest.param("detector/4/overlays", {}, 404, "Step not found", id="unknown-step-overlays"),
        pytest.param("detector/4/image", {}, 404, "Step not found", id="unknown-step-image"),
    ],
)
def test_overlays_lauelab_cannot_prepare_and_malformed_requests_are_problems(
    viewer: Viewer, tmp_path: Path, path: str, params: dict[str, str], status: int, expected: str
) -> None:
    run = make_run(viewer, tmp_path, geometry=expected != PRODUCT_FAILED)

    response = run.get(path, **params)

    assert response.status_code == status, response.text
    problem = response.json()
    if expected == PRODUCT_FAILED:
        assert (problem["type"], problem["detail"]) == (
            PRODUCT_FAILED,
            "No detector overlays: detector geometry is required to prepare a detector view",
        )
    elif expected == "kev":
        assert [item["loc"] for item in problem["errors"]] == [["query", "kev"]]
    else:
        assert problem["title"] == expected


def test_the_image_and_overlays_tags_follow_the_image_file(run: Run) -> None:
    image = run.get("detector/0/image")
    overlays = run.get("detector/0/overlays")
    tags = {"image": image.headers["etag"], "overlays": overlays.headers["etag"]}

    unchanged = {name: run.get(f"detector/0/{name}", headers={"If-None-Match": tag}) for name, tag in tags.items()}
    frame = run.directory / "test" / "image_001.h5"
    os.utime(frame, ns=(0, os.stat(frame).st_mtime_ns + 1_000_000))  # the image rewritten in place
    changed = {name: run.get(f"detector/0/{name}", headers={"If-None-Match": tag}) for name, tag in tags.items()}

    assert image.headers["cache-control"] == "private, no-cache"
    assert {name: response.status_code for name, response in unchanged.items()} == {"image": 304, "overlays": 304}
    assert {name: response.status_code for name, response in changed.items()} == {"image": 200, "overlays": 200}
    assert all(changed[name].headers["etag"] != tag for name, tag in tags.items())


# ---------------------------------------------------------------------------
# The peaks and patterns tables


def rows(run: Run, table: str, **params: Any) -> dict[str, Any]:
    if "filter" in params:
        params["filter"] = json.dumps(params["filter"])
    response = run.get(table, **{"limit": 1000, **params})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("scope", [pytest.param({}, id="default"), pytest.param(EVERY_STEP, id="every-step")])
def test_the_tables_are_lauelabs_within_the_scope(run: Run, scope: dict[str, Any]) -> None:
    data_scope = to_data_scope(
        **{key: value == "true" if key == "pattern0_only" else value for key, value in scope.items()}
    )
    peaks = indexed_peak_table(run.dataset, scope=data_scope)
    patterns = pattern_table(run.dataset, scope=data_scope)

    peak_page = rows(run, "peaks", **scope)
    pattern_page = rows(run, "patterns", **scope)

    assert peak_page["total"] == len(peaks) == len(peak_page["rows"])
    assert [[row[name] for name in ("pattern_index", "peak_index", "h", "k", "l")] for row in peak_page["rows"]] == [
        [int(peaks[name][index]) for name in ("pattern_index", "peak_index", "h", "k", "l")]
        for index in range(len(peaks))
    ]
    assert [row["x_pixel"] for row in peak_page["rows"]] == pytest.approx(list(peaks["fit_x"]))
    assert [row["frame_id"] for row in peak_page["rows"]] == [str(value) for value in peaks["frame_id"]]
    assert pattern_page["total"] == len(patterns) == len(pattern_page["rows"])
    assert [(row["step"], row["pattern_index"], row["n_indexed"]) for row in pattern_page["rows"]] == [
        (int(frame), int(rank), int(count))
        for frame, rank, count in zip(
            patterns["frame_id"], patterns["pattern_index"], patterns["n_indexed"], strict=True
        )
    ]
    assert [row["goodness"] for row in pattern_page["rows"]] == pytest.approx(list(patterns["goodness"]))


def test_a_filtered_sorted_page_counts_the_rows_within_the_scope(run: Run) -> None:
    every = rows(run, "peaks", **EVERY_STEP)["rows"]
    threshold = sorted(row["intensity"] for row in every)[len(every) // 2]
    expected = sorted(
        (row for row in every if row["intensity"] > threshold and row["pattern_index"] == 0),
        key=lambda row: (-row["intensity"], row["id"]),
    )
    query = {
        **EVERY_STEP,
        "pattern0_only": "true",
        "filter": {"intensity": {"filterType": "number", "type": "greaterThan", "filter": threshold}},
        "sort": "-intensity",
    }

    pages = [rows(run, "peaks", **query, offset=offset, limit=3) for offset in (0, 3, 6)]

    assert {page["total"] for page in pages} == {len(expected)}
    assert [row["id"] for page in pages for row in page["rows"]] == [row["id"] for row in expected][:9]
    assert [row["step"] for row in rows(run, "patterns", **EVERY_STEP, sort="-frame_id")["rows"]] == [2, 1, 0, 0]


def test_the_rows_the_tables_and_the_grids_name_the_same_fields(run: Run) -> None:
    peaks = tables.peak_table(run.dataset).table
    patterns = tables.pattern_table_of(run.dataset).table
    assert peaks.column_names == list(PeakRow.model_fields) and set(grids.PEAK_GRID.fields) == set(PeakRow.model_fields)
    assert patterns.column_names == list(PatternRow.model_fields)
    assert set(grids.PATTERN_GRID.fields) == set(PatternRow.model_fields)
