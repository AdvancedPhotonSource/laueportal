"""A point's images in the wire viewer (``reconstruction.viewer``): its depth frames, its trace, its reference images
and its ROI traces, on runs made from the synthetic reconstruction (a float64 point of 51 depths of 128 x 128) and
generated legacy per-depth files (``tests/recon_support.py``), checked against lauelab's own readers and
``plot_roi_traces``."""

from __future__ import annotations

import json
import os
import shutil
import threading
from functools import partial
from pathlib import Path
from typing import Any

import anyio
import h5py
import numpy as np
import pytest
from lauelab.reconstruct import PerDepthReader, PointReader
from lauelab.reconstruct.inspection import roi_traces, square_bounds
from lauelab.visualization import REFERENCE_OPTIONS, plot_roi_traces
from sqlalchemy import URL, create_engine

from laueportal.core.api import RETRY_AFTER_SECONDS
from laueportal.core.db import make_sessionmaker
from laueportal.indexing.products import PRODUCT_FAILED
from laueportal.jobs.state import JobStatus
from laueportal.reconstruction import viewer as wire_viewer
from laueportal.reconstruction.schemas import MAX_ROIS, ReconstructionReferenceKind
from laueportal.reconstruction.viewer import COMPUTATIONS_WAITING
from laueportal.remote.paths import PLAN_NAME
from laueportal.science import once, recon
from laueportal.science.detector import frame_bytes
from tests.api_support import PORTAL_HEADERS
from tests.recon_support import CatalogPoint, copy_synthetic, synthetic_point, write_per_depth, write_snapshot
from tests.reconstruction.conftest import REMOTE_RUN, RUNS, Viewer

POINT = "synthetic_wire_scan"
"""The synthetic reconstruction's one point."""


def point_url(reconstruction_id: int, product: str, point_id: str = POINT) -> str:
    return f"{RUNS}/{reconstruction_id}/points/{point_id}/{product}"


def write_uint16_per_depth(run_directory: Path, point_id: str) -> list[Path]:
    """The per-depth files of ``point_id`` (``write_per_depth``) with their frames stored as ``uint16``."""
    files = write_per_depth(run_directory, point_id)
    for path in files[:-1]:  # the last is the summary
        with h5py.File(path, "r+") as file:
            data = np.clip(np.rint(file["entry1/data/data"][...]), 0, None).astype(np.uint16)
            del file["entry1/data/data"]
            file.create_dataset("entry1/data/data", data=data)
    return files[:-1]


# ---------------------------------------------------------------------------
# Depth frames


@pytest.mark.parametrize("stored", ["float64", "uint16"])
def test_a_depth_frame_is_sent_as_stored_with_its_shape_type_and_depth(viewer: Viewer, stored: str) -> None:
    run_directory = viewer.add_run(1)
    if stored == "float64":  # a scan catalog's point: float frames go as float32
        copy_synthetic(run_directory)
        point_id, reader = POINT, PointReader(synthetic_point())
    else:  # per-depth files of uint16 frames: as they are
        point_id, reader = "p1", PerDepthReader(write_uint16_per_depth(run_directory, "p1"), point_id="p1")
    with reader:
        frame, depth = np.array(reader.frame(7)), float(reader.depth_um[7])

    response = viewer.client.get(point_url(1, "frames/7", point_id))

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    dtype = "float32" if stored == "float64" else "uint16"
    assert (response.headers["x-frame-shape"], response.headers["x-frame-dtype"]) == ("128,128", dtype)
    assert float(response.headers["x-depth-um"]) == depth
    assert response.content == np.ascontiguousarray(frame, dtype="<f4" if dtype == "float32" else "<u2").tobytes()
    assert response.headers["cache-control"] == "private, no-cache"


@pytest.mark.parametrize(
    ("reconstruction_id", "point_id", "depth_index", "status", "title", "detail"),
    [
        (1, "w9", 0, 404, "Point not found", "R1 has no point 'w9'"),
        (1, "w1", 51, 404, "Depth not found", "R1, point 'w1' has depth indices 0 to 50: no 51"),
        (
            1,
            "w2",
            0,
            409,
            "Point without images",
            "R1: Point w2 has not been reconstructed yet; press Refresh after it completes. It has no images.",
        ),  # fmt: skip
        (2, "w1", 0, 404, "Point not found", "R2 has no point 'w1': the run has no reconstruction"),
        (
            3,
            "p1",
            0,
            409,
            "Reconstruction unreadable",
            "R3, point 'p1' cannot be read: 'Unable to synchronously open file (file signature not found)'",
        ),  # fmt: skip
    ],
    ids=["unknown point", "depth past the last", "pending point", "no reconstruction", "unreadable point file"],
)
def test_a_point_or_depth_without_an_image_is_a_problem_saying_why(
    viewer: Viewer, reconstruction_id: int, point_id: str, depth_index: int, status: int, title: str, detail: str
) -> None:
    run_directory = viewer.add_run(1)
    write_snapshot(run_directory, [CatalogPoint("w1", "complete", "w1.h5"), CatalogPoint("w2", "pending", "w2.h5")])
    viewer.add_run(2)
    write_per_depth(viewer.add_run(3), "p1")[2].write_bytes(b"not HDF5")  # one of its per-depth files

    response = viewer.client.get(point_url(reconstruction_id, f"frames/{depth_index}", point_id))

    assert response.status_code == status
    assert (response.json()["title"], response.json()["detail"]) == (title, detail)


# ---------------------------------------------------------------------------
# The trace and the reference images


def test_the_trace_is_each_depths_full_frame_sum_and_opens_at_the_brightest(viewer: Viewer) -> None:
    copy_synthetic(viewer.add_run(1))
    with PointReader(synthetic_point()) as point:
        values, depths = point.depth_intensity(), point.depth_um

    body = viewer.client.get(point_url(1, "trace")).json()

    assert (body["point_id"], body["depth_um"], body["values"]) == (POINT, depths.tolist(), values.tolist())
    assert body["brightest_index"] == int(values.argmax()) == 25


@pytest.mark.parametrize(("kind", "dtype"), [("sum_reconstructed", "float32"), ("first_raw", "uint16"), ("sum_raw", "float32")])  # fmt: skip
def test_each_reference_option_is_the_points_image_sent_as_a_frame(viewer: Viewer, kind: str, dtype: str) -> None:
    copy_synthetic(viewer.add_run(1))
    with PointReader(synthetic_point()) as point:
        expected = frame_bytes(point.reference(kind))

    response = viewer.client.get(point_url(1, "reference"), params={"kind": kind})

    assert response.status_code == 200
    assert (response.content, response.headers["x-frame-dtype"]) == expected
    assert (response.headers["x-frame-shape"], expected[1]) == ("128,128", dtype)


def test_an_unknown_reference_is_a_422_and_per_depth_files_hold_no_raw_images(viewer: Viewer) -> None:
    copy_synthetic(viewer.add_run(1))
    write_per_depth(viewer.add_run(2), "p1")

    unknown = viewer.client.get(point_url(1, "reference"), params={"kind": "sum"})
    raw = viewer.client.get(point_url(2, "reference", "p1"), params={"kind": "first_raw"})
    summed = viewer.client.get(point_url(2, "reference", "p1"))  # sum_reconstructed by default

    assert [kind.value for kind in ReconstructionReferenceKind] == [option.value for option in REFERENCE_OPTIONS]
    assert (unknown.status_code, unknown.json()["errors"][0]["loc"]) == (422, ["query", "kind"])
    assert (raw.status_code, raw.json()["type"]) == (422, PRODUCT_FAILED)
    assert raw.json()["detail"] == (
        "First raw frame is unavailable: reference image 'first_raw' is not available for per-depth files"
    )
    assert (summed.status_code, summed.headers["x-frame-dtype"]) == (200, "float32")


# ---------------------------------------------------------------------------
# ROI traces


@pytest.mark.parametrize("normalize", [False, True], ids=["sum", "normalized"])
def test_roi_traces_are_lauelabs_plot_roi_traces(viewer: Viewer, normalize: bool) -> None:
    """The values lauelab plots for each ROI, the one in the zero corner too: normalized, it has none and lauelab's
    reason."""
    copy_synthetic(viewer.add_run(1))
    rois = [{"x": 64, "y": 60, "size": 9}, {"x": 20.4, "y": 100.6, "size": 4}, {"x": 1, "y": 1, "size": 3}]
    with PointReader(synthetic_point()) as point:
        placed = [square_bounds(roi["size"], roi["x"], roi["y"], point.shape[1:]) for roi in rois]
        traces = roi_traces(point, {"ROI [{}:{}, {}:{}]".format(*bounds): bounds for bounds in placed})
    by_depth = plot_roi_traces(traces, axis="depth", normalized=normalize)
    by_index = plot_roi_traces(traces, axis="index", normalized=normalize)
    plotted = {trace.name: list(trace.y) for trace in by_depth.data}
    notes = by_depth.layout.annotations[0].text if by_depth.layout.annotations else ""

    response = viewer.client.post(
        point_url(1, "roi-traces"), json={"rois": rois, "normalize": normalize}, headers=PORTAL_HEADERS
    )

    assert response.status_code == 200
    body = response.json()
    assert body["depth_um"] == list(by_depth.data[0].x)
    assert body["depth_index"] == list(by_index.data[0].x) == list(range(51))
    assert [trace["bounds"] for trace in body["traces"]] == [list(bounds) for bounds in placed]
    for trace, name in zip(body["traces"], traces, strict=True):
        assert trace["values"] == plotted.get(name)
        assert (trace["reason"] is not None) == (name not in plotted)
        assert trace["reason"] is None or trace["reason"] in notes
    assert normalize == (body["traces"][2]["values"] is None)  # normalized, the zero corner's is left out


def test_an_roi_outside_the_image_and_too_many_rois_are_refused_before_anything_is_read(
    viewer: Viewer, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy_synthetic(viewer.add_run(1))
    traced = []
    monkeypatch.setattr(recon, "_roi_sums", lambda *args, **kwargs: traced.append(args))
    inside = {"x": 64, "y": 64, "size": 9}

    outside = viewer.client.post(
        point_url(1, "roi-traces"), json={"rois": [inside, {"x": 126, "y": 3, "size": 6}]}, headers=PORTAL_HEADERS
    )
    many = viewer.client.post(
        point_url(1, "roi-traces"), json={"rois": [inside] * (MAX_ROIS + 1)}, headers=PORTAL_HEADERS
    )

    assert outside.status_code == 422
    assert outside.json()["errors"] == [
        {
            "loc": ["rois", 1],
            "message": "A 6 x 6 ROI at (126, 3) would extend outside the 128 x 128 image",
            "code": "roi_outside",
            "severity": "error",
        }
    ]
    assert (many.status_code, many.json()["errors"][0]["loc"]) == (422, ["rois"])
    assert traced == []


# ---------------------------------------------------------------------------
# Revalidation, waiting, and a remote run's point files


def test_a_points_images_revalidate_until_the_running_run_rewrites_its_file(viewer: Viewer) -> None:
    run_directory = viewer.add_run(1, status=JobStatus.RUNNING)
    write_snapshot(run_directory, [CatalogPoint("w1", "complete", "w1.h5")])
    urls = [point_url(1, product, "w1") for product in ("frames/3", "trace", "reference?kind=sum_raw")]
    tags = [viewer.client.get(url).headers["etag"] for url in urls]

    unchanged = [
        viewer.client.get(url, headers={"If-None-Match": tag}).status_code for url, tag in zip(urls, tags, strict=True)
    ]
    point_file = run_directory / "reconstruction" / "points" / "w1.h5"
    shutil.copyfile(point_file, point_file.with_suffix(".partial"))
    os.replace(point_file.with_suffix(".partial"), point_file)  # lauelab replaces a point file whole
    rewritten = [viewer.client.get(url, headers={"If-None-Match": tag}) for url, tag in zip(urls, tags, strict=True)]

    assert unchanged == [304, 304, 304]
    assert [response.status_code for response in rewritten] == [200, 200, 200]
    assert all(response.headers["etag"] not in tags for response in rewritten)


async def asgi_call(
    app: Any, method: str, path: str, *, body: Any = None, client_gone: bool = False
) -> tuple[int, bytes, dict[str, str]]:
    """``method`` ``path`` (with a JSON ``body``) as uvicorn would call the app (``asgi_get``, for a POST and the
    headers too): the status, the body and the headers. With ``client_gone`` the client disconnects once its
    request is sent (uvicorn's ``receive`` then answers ``http.disconnect``, which the TestClient cannot)."""
    content = b"" if body is None else json.dumps(body).encode()
    status, chunks, headers, sent = 0, [], {}, False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if client_gone and sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": content, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
            headers.update((name.decode(), value.decode()) for name, value in message["headers"])
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    request_headers = [(b"host", b"testserver"), *((k.lower().encode(), v.encode()) for k, v in PORTAL_HEADERS.items())]
    if body is not None:
        request_headers.append((b"content-type", b"application/json"))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": request_headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "state": {},
    }
    await app(scope, receive, send)
    return status, b"".join(chunks), headers


async def until(condition: Any) -> None:
    """Wait (at most 10 s) until ``condition()`` holds: a thread's event, or the limiter, which has no event."""
    with anyio.fail_after(10):
        while not condition():  # noqa: ASYNC110
            await anyio.sleep(0.01)


@pytest.mark.parametrize(
    ("product", "method", "body", "computation"),
    [
        ("trace", "GET", None, "depth_trace"),
        ("reference", "GET", None, "reference_image"),
        ("roi-traces", "POST", {"rois": [{"x": 64, "y": 60, "size": 9}]}, "_roi_sums"),
    ],
    ids=["trace", "reference", "roi-traces"],
)
def test_a_points_computed_images_wait_their_turn_and_a_kept_one_takes_none(
    viewer: Viewer, monkeypatch: pytest.MonkeyPatch, product: str, method: str, body: Any, computation: str
) -> None:
    """While every turn is taken, a request for a point's trace, reference or ROI traces waits for one outside the
    thread pool: one whose client has gone is dropped unrun (499), at most ``COMPUTATIONS_WAITING`` wait, and the next
    is a 503 with ``Retry-After``. When turns come, the product is computed once; once kept, it takes no turn."""
    copy_synthetic(viewer.add_run(1))
    app = viewer.client.app
    limiter = app.state.point_computations
    computed: list[object] = []
    compute = getattr(recon, computation)
    monkeypatch.setattr(recon, computation, lambda *args, **kwargs: computed.append(1) or compute(*args, **kwargs))
    url = point_url(1, product)

    async def while_every_turn_is_taken() -> tuple[list[int], int, tuple[int, bytes, dict[str, str]], int]:
        statuses = [0] * (COMPUTATIONS_WAITING + 1)

        async def call(index: int, *, client_gone: bool = False) -> None:
            statuses[index] = (await asgi_call(app, method, url, body=body, client_gone=client_gone))[0]

        others = [object() for _ in range(int(limiter.total_tokens))]  # others' computations, holding every turn
        async with anyio.create_task_group() as group:
            for other in others:
                await limiter.acquire_on_behalf_of(other)
            group.start_soon(partial(call, 0, client_gone=True))
            await until(lambda: statuses[0] != 0)  # it waited, then left when its client went
            for index in range(1, COMPUTATIONS_WAITING + 1):
                group.start_soon(call, index)
            await until(lambda: limiter.statistics().tasks_waiting == COMPUTATIONS_WAITING)
            refused = await asgi_call(app, method, url, body=body)
            for other in others:
                limiter.release_on_behalf_of(other)
        for other in others:  # every turn taken again: a kept product needs none
            await limiter.acquire_on_behalf_of(other)
        with anyio.fail_after(5):
            kept = (await asgi_call(app, method, url, body=body))[0]
        for other in others:
            limiter.release_on_behalf_of(other)
        return statuses, len(computed), refused, kept

    statuses, computations, refused, kept = viewer.client.portal.call(while_every_turn_is_taken)

    assert statuses == [499] + [200] * COMPUTATIONS_WAITING
    assert computations == 1
    assert (refused[0], refused[2]["retry-after"], json.loads(refused[1])["title"]) == (
        503,
        str(RETRY_AFTER_SECONDS),
        "Too many point computations waiting",
    )
    assert kept == 200
    assert limiter.statistics().borrowed_tokens == 0


@pytest.mark.parametrize(
    ("product", "method", "body", "computation", "kept"),
    [
        ("trace", "GET", None, "depth_trace", "trace_kept"),
        ("reference", "GET", None, "reference_image", "reference_kept"),
        ("roi-traces", "POST", {"rois": [{"x": 64, "y": 60, "size": 9}]}, "_roi_sums", "rois_kept"),
    ],
    ids=["trace", "reference", "roi-traces"],
)
def test_a_product_found_kept_but_evicted_before_its_read_takes_a_turn_like_any_other(
    viewer: Viewer, monkeypatch: pytest.MonkeyPatch, product: str, method: str, body: Any, computation: str, kept: str
) -> None:
    """A product the route found kept, but which the cache evicted before its read started, is computed in a turn, never
    at once beside the capped computations."""
    copy_synthetic(viewer.add_run(1))
    app = viewer.client.app
    limiter, cache = app.state.point_computations, app.state.reconstruction_products
    url = point_url(1, product)
    computed: list[object] = []
    compute = getattr(recon, computation)
    monkeypatch.setattr(recon, computation, lambda *args, **kwargs: computed.append(1) or compute(*args, **kwargs))
    found = getattr(wire_viewer, kept)

    def found_then_evicted(*args: Any, **kwargs: Any) -> bool:
        was_kept = found(*args, **kwargs)
        cache.clear()  # the frames of a quick depth stepping push it out before the read starts
        return was_kept

    async def while_every_turn_is_taken() -> tuple[int, int, int]:
        assert (await asgi_call(app, method, url, body=body))[0] == 200  # computed, and kept
        monkeypatch.setattr(wire_viewer, kept, found_then_evicted)
        status = [0]

        async def call() -> None:
            status[0] = (await asgi_call(app, method, url, body=body))[0]

        others = [object() for _ in range(int(limiter.total_tokens))]  # others' computations, holding every turn
        async with anyio.create_task_group() as group:
            for other in others:
                await limiter.acquire_on_behalf_of(other)
            group.start_soon(call)
            await until(lambda: limiter.statistics().tasks_waiting == 1)
            computed_while_waiting = len(computed)
            for other in others:
                limiter.release_on_behalf_of(other)
        return status[0], computed_while_waiting, len(computed)

    status, computed_while_waiting, computations = viewer.client.portal.call(while_every_turn_is_taken)

    assert (status, computed_while_waiting, computations) == (200, 1, 2)
    assert limiter.statistics().borrowed_tokens == 0


def test_depth_frames_take_a_turn_of_their_own_even_when_kept(viewer: Viewer, monkeypatch: pytest.MonkeyPatch) -> None:
    """At most ``FRAMES_AT_ONCE`` frames are read and copied at once, a kept one too (each read holds its frame and a
    copy). The others wait outside the thread pool: one whose client has gone is dropped unread (499), at most
    ``FRAMES_WAITING`` wait, and the next is a 503 with ``Retry-After``."""
    copy_synthetic(viewer.add_run(1))
    app = viewer.client.app
    limiter = app.state.frame_reads
    monkeypatch.setattr(wire_viewer, "FRAMES_WAITING", 2)
    reads: list[int] = []
    depth_frame = wire_viewer.depth_frame
    monkeypatch.setattr(
        wire_viewer,
        "depth_frame",
        lambda images, index, **kwargs: reads.append(index) or depth_frame(images, index, **kwargs),
    )
    assert viewer.client.get(point_url(1, "frames/0")).status_code == 200  # frame 0 kept

    async def while_every_turn_is_taken() -> tuple[list[int], list[int], tuple[int, bytes, dict[str, str]]]:
        statuses = [0, 0, 0]

        async def get(index: int, depth: int, *, client_gone: bool = False) -> None:
            url = point_url(1, f"frames/{depth}")
            statuses[index] = (await asgi_call(app, "GET", url, client_gone=client_gone))[0]

        others = [object() for _ in range(int(limiter.total_tokens))]  # other frames, holding every turn
        async with anyio.create_task_group() as group:
            for other in others:
                await limiter.acquire_on_behalf_of(other)
            group.start_soon(partial(get, 0, 1, client_gone=True))
            await until(lambda: statuses[0] != 0)  # it waited, then left when its client went
            group.start_soon(get, 1, 0)  # kept
            group.start_soon(get, 2, 2)
            await until(lambda: limiter.statistics().tasks_waiting == 2)
            refused = await asgi_call(app, "GET", point_url(1, "frames/3"))
            read_while_waiting = list(reads)
            for other in others:
                limiter.release_on_behalf_of(other)
        return statuses, read_while_waiting, refused

    statuses, read_while_waiting, refused = viewer.client.portal.call(while_every_turn_is_taken)

    assert limiter.total_tokens == wire_viewer.FRAMES_AT_ONCE == 6
    assert statuses == [499, 200, 200]
    assert (read_while_waiting, sorted(reads)) == ([0], [0, 0, 2])
    assert (refused[0], refused[2]["retry-after"], json.loads(refused[1])["title"]) == (
        503,
        str(RETRY_AFTER_SECONDS),
        "Too many frame reads waiting",
    )
    assert limiter.statistics().borrowed_tokens == 0


def test_a_points_images_hold_no_database_connection_while_they_read_or_wait(
    viewer: Viewer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a one-connection pool, a request summing a per-depth point's frames, one waiting for that sum, one
    waiting for a turn and a depth frame waiting for one leave an unrelated route answering at once; the sum is made
    once."""
    write_per_depth(viewer.add_run(1), "p1")
    app = viewer.client.app
    pool = create_engine(URL.create("sqlite", database=str(app.state.settings.database.path)), pool_size=1,
                         max_overflow=0, pool_timeout=2)  # fmt: skip
    app.state.sessions = make_sessionmaker(pool)
    limiter, frame_reads = app.state.point_computations, app.state.frame_reads
    summing, summed, waiting, calls = threading.Event(), threading.Event(), threading.Event(), []
    depth_trace, wait = recon.depth_trace, once.Pending.wait

    def held(point: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        summing.set()
        assert summed.wait(10)
        return depth_trace(point, *args, **kwargs)

    def watched(pending: once.Pending) -> Any:
        waiting.set()
        return wait(pending)

    monkeypatch.setattr(recon, "depth_trace", held)
    monkeypatch.setattr(once.Pending, "wait", watched)

    async def while_requests_wait() -> tuple[list[int], list[int], int, int]:
        statuses = [0, 0, 0, 0]

        async def get(index: int, product: str) -> None:
            statuses[index] = (await asgi_call(app, "GET", point_url(1, product, "p1")))[0]

        others = [object() for _ in range(int(limiter.total_tokens) - 2)]  # others' computations: two turns left
        frames = [object() for _ in range(int(frame_reads.total_tokens))]  # other frames, holding every turn
        async with anyio.create_task_group() as group:
            for other in others:
                await limiter.acquire_on_behalf_of(other)
            for frame in frames:
                await frame_reads.acquire_on_behalf_of(frame)
            group.start_soon(get, 0, "trace")
            await until(summing.is_set)  # the first sums the frames in its turn
            group.start_soon(get, 1, "trace")
            await until(waiting.is_set)  # the second waits for that sum in its turn
            group.start_soon(get, 2, "reference")
            await until(lambda: limiter.statistics().tasks_waiting == 1)  # the third waits for a turn
            group.start_soon(get, 3, "frames/0")
            await until(lambda: frame_reads.statistics().tasks_waiting == 1)  # the frame waits for one too
            in_flight = list(statuses)
            held_connections = pool.pool.checkedout()  # type: ignore[attr-defined]  # a QueuePool
            with anyio.fail_after(5):
                unrelated = (await asgi_call(app, "GET", f"{RUNS}/1"))[0]
            summed.set()
            for other in others:
                limiter.release_on_behalf_of(other)
            for frame in frames:
                frame_reads.release_on_behalf_of(frame)
        return statuses, in_flight, held_connections, unrelated

    statuses, in_flight, held_connections, unrelated = viewer.client.portal.call(while_requests_wait)
    pool.dispose()

    assert (in_flight, held_connections, unrelated) == ([0, 0, 0, 0], 0, 200)
    assert statuses == [200, 200, 200, 200]
    assert calls == [()]


@pytest.mark.parametrize("remote", [True, False], ids=["remote", "local"])
@pytest.mark.parametrize("stored", ["scan", "per_depth"])
def test_a_remote_runs_point_file_leading_out_of_the_run_directory_is_a_404_and_never_opened(
    viewer: Viewer, monkeypatch: pytest.MonkeyPatch, remote: bool, stored: str
) -> None:
    """A point file links outside the run directory (still inside the roots): a scan point's file, or one of a
    per-depth point's files (admitted when the point's reader is made). A remote run's plan maps no path there, so
    none of the point's files is opened; a local run's paths are the portal's, so the link is read."""
    run_directory = viewer.add_run(1)
    if stored == "scan":
        copy_synthetic(run_directory)
        point_id, linked = POINT, run_directory / "reconstruction" / "points" / f"{POINT}.h5"
    else:
        point_id, linked = "p1", write_per_depth(run_directory, "p1")[3]
    elsewhere = viewer.root / "elsewhere" / linked.name
    elsewhere.parent.mkdir()
    os.replace(linked, elsewhere)
    linked.symlink_to(elsewhere)
    if remote:
        plan = {"version": 1, "submission_id": "laue-1-0001", "path_map": {str(run_directory): REMOTE_RUN}}
        (run_directory / PLAN_NAME).write_text(json.dumps(plan))
    opened, open_file = [], h5py.File

    def spied(name: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(Path(os.path.realpath(name)))
        return open_file(name, *args, **kwargs)

    monkeypatch.setattr(h5py, "File", spied)

    response = viewer.client.get(point_url(1, "frames/0", point_id))

    if remote:
        assert (response.status_code, response.json()["title"]) == (404, "Point file not available")
        assert response.json()["detail"] == (
            f"R1, point '{point_id}': {linked} is not available for this run: its plan maps no such path, or it "
            "leads out of the run directory"
        )
        assert [path for path in opened if path.name.startswith(point_id)] == []
    else:
        assert response.status_code == 200
        assert elsewhere in opened
