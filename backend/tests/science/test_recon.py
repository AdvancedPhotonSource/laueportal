"""ROI depth traces of a reconstructed point (``science.recon.roi_traces``), and the product cache
(``science.recon.ProductCache``, on ``science.once``)."""

import os
import threading
from contextlib import nullcontext

import numpy as np
import pytest
from lauelab.reconstruct.inspection import ArrayPoint

from laueportal.core.problems import ProblemError
from laueportal.science import once, recon
from tests.recon_support import CatalogPoint, write_per_depth, write_snapshot


@pytest.fixture
def artifact(tmp_path, monkeypatch):
    """A per-depth artifact whose point ``p1`` is an in-memory (depth, y, x) stack."""

    images = np.arange(3 * 4 * 5, dtype=np.int64).reshape(3, 4, 5)
    point = ArrayPoint(images, depth_um=[-1.0, 0.0, 1.0], point_id="p1")
    monkeypatch.setattr(recon, "_open_point", lambda artifact, point_id, cache, admit: nullcontext(point))
    yield recon.Artifact(recon.KIND_PER_DEPTH, str(tmp_path)), images


def test_roi_traces_sum_the_bounds_in_lauelab_order_and_take_the_given_names(artifact):
    artifact, images = artifact

    bounds = (1.0, 3.0, 0, 2)  # (y0, y1, x0, x1), floats truncated
    [trace] = recon.roi_traces(artifact, "p1", [("ROI 1", bounds)], cache=recon.ProductCache())

    assert trace.label == "ROI 1"
    assert trace.bounds == (1, 3, 0, 2)
    assert trace.values.tolist() == images[:, 1:3, 0:2].sum(axis=(1, 2)).tolist()


@pytest.mark.parametrize(
    ("read_pixels", "first_reads"),
    [(2**18, [(0, 4, 0, 5)]), (0, [(0, 2, 0, 2), (2, 4, 3, 5)])],
    ids=["the box", "each its own"],
)
def test_rois_not_kept_are_summed_in_one_pass_over_the_depths_and_a_kept_one_is_not_read_again(
    artifact, monkeypatch, read_pixels, first_reads
):
    """The ROIs not kept are read in one pass over the depths: the box that holds them, or each one's own pixels when
    that reads less (far apart, against what a read costs beyond its pixels); a kept ROI, renamed, is not read."""
    artifact, images = artifact
    monkeypatch.setattr(recon, "ROI_READ_PIXELS", read_pixels)
    reads, region = [], ArrayPoint.region

    def read(point, bounds, depths=None):
        pixels = region(point, bounds, depths)
        if len(pixels):  # not lauelab's check of the bounds, which reads none
            reads.append((tuple(bounds), depths))
        return pixels

    monkeypatch.setattr(ArrayPoint, "region", read)
    cache = recon.ProductCache()

    first = recon.roi_traces(artifact, "p1", [("a", (0, 2, 0, 2)), ("b", (2, 4, 3, 5))], cache=cache)
    second = recon.roi_traces(artifact, "p1", [("b again", (2, 4, 3, 5)), ("c", (1, 2, 1, 2))], cache=cache)

    assert reads == [(bounds, slice(0, 3)) for bounds in [*first_reads, (1, 2, 1, 2)]]
    assert [trace.label for trace in second] == ["b again", "c"]
    assert second[0].values.tolist() == first[1].values.tolist()
    assert [trace.values.tolist() for trace in (*first, second[1])] == [
        images[:, y0:y1, x0:x1].sum(axis=(1, 2)).tolist()
        for y0, y1, x0, x1 in [(0, 2, 0, 2), (2, 4, 3, 5), (1, 2, 1, 2)]
    ]


@pytest.mark.parametrize("bounds", [(0, 1, 0), (0, 1, 0, 1, 2)])
def test_roi_traces_report_bounds_that_are_not_four_values_as_a_view_error(artifact, bounds):
    """lauelab rejects the bounds and the view reports it; nothing fails before (no bare ValueError)."""

    artifact, _ = artifact

    with pytest.raises(recon.ViewError, match="bounds must be four integers"):
        recon.roi_traces(artifact, "p1", [("ROI", bounds)], cache=recon.ProductCache())


def test_per_depth_point_table_has_typed_rows_and_no_store_serialization(tmp_path):
    """Per-depth rows are typed (not "" for a missing value, nor a "no frames" text), with an error text for a summary
    without frames."""

    for name in ("p1_summary.txt", "p1_0.h5", "p1_1.h5", "p2_summary.txt", "p10_0.h5", "notes.h5"):
        (tmp_path / name).write_text("")

    table = recon.point_table(recon.Artifact(recon.KIND_PER_DEPTH, str(tmp_path)))

    assert table.run_status is None
    assert table.rows == [
        recon.PointRow(
            point_id="p1",
            index=0,
            status="complete",
            source=None,
            sample_position_um=None,
            n_depths=2,
            depth_first_um=None,
            depth_last_um=None,
            frame_shape=None,
            dtype=None,
            error=None,
        ),
        recon.PointRow(
            point_id="p2",
            index=1,
            status=recon.STATUS_NO_FRAMES,
            source=None,
            sample_position_um=None,
            n_depths=None,
            depth_first_um=None,
            depth_last_um=None,
            frame_shape=None,
            dtype=None,
            error="no per-depth files next to the summary",
        ),
    ]
    assert not hasattr(recon.Artifact, "to_store") and not hasattr(recon.Artifact, "from_store")


def test_per_depth_files_go_to_the_point_their_name_ends_in_when_one_id_prefixes_another(tmp_path):
    """``<point>_<depth index>.h5``: the index holds no ``_``, so ``a_1.h5`` is point ``a``'s depth 1 and ``a_1_2.h5``
    point ``a_1``'s depth 2."""

    names = ("a_summary.txt", "a_1_summary.txt", "a_0.h5", "a_1.h5", "a_1_0.h5", "a_1_1.h5", "a_1_2.h5", "a_x.h5")
    for name in names:
        (tmp_path / name).write_text("")

    files = recon._per_depth_files(str(tmp_path))

    assert {point: sorted(os.path.basename(path) for path in paths) for point, paths in files.items()} == {
        "a": ["a_0.h5", "a_1.h5"],
        "a_1": ["a_1_0.h5", "a_1_1.h5", "a_1_2.h5"],
    }


# ---------------------------------------------------------------------------
# The product cache: one computation per key


def _held_computation(monkeypatch, cache, key, compute):
    """Two threads ask ``cache`` for ``key`` while the first computes it with ``compute`` (held until the second
    waits for it); meanwhile another key is computed. Returns both outcomes (a value or an error) and the other
    key's value."""

    computing, waiting, outcomes = threading.Event(), threading.Event(), [None, None]
    wait = once.Pending.wait

    def watched(pending):
        waiting.set()
        return wait(pending)

    monkeypatch.setattr(once.Pending, "wait", watched)

    def held():
        computing.set()
        assert waiting.wait(10)
        return compute()

    def ask(index, make):
        try:
            outcomes[index] = cache.get(key, make)
        except Exception as error:  # the outcome under test
            outcomes[index] = error

    threads = [threading.Thread(target=ask, args=(0, held))]
    threads[0].start()
    assert computing.wait(10)
    other = cache.get("other", lambda: "not held up")
    threads.append(threading.Thread(target=ask, args=(1, lambda: pytest.fail("computed twice"))))
    threads[1].start()
    for thread in threads:
        thread.join(10)
    return outcomes, other


def test_a_catalogs_new_snapshot_replaces_its_old_one_in_the_cache(tmp_path):
    """A live run publishes a snapshot every few seconds while it is watched; the cache keeps only a catalog's latest,
    never the old ones it will not read again."""
    cache = recon.ProductCache()
    frame = cache.get("frame", lambda: np.zeros(4))  # what else the cache keeps stays
    run = tmp_path / "run"
    snapshots = [[CatalogPoint("w1", "pending", "/data/w1.h5")], [CatalogPoint("w1", "complete", "/data/w1.h5")]]
    statuses = []
    for points in snapshots:
        artifact = recon.Artifact(recon.KIND_SCAN, str(write_snapshot(run, points)))
        statuses.append([row.status for row in recon.point_table(artifact, cache=cache).rows])
        catalog = cache.get((recon.identity(artifact), "catalog"), lambda: pytest.fail("read again"))

    assert statuses == [["pending"], ["complete"]]
    assert len(cache) == 2
    assert cache.nbytes == recon._nbytes(catalog) + frame.nbytes
    assert cache.get("frame", lambda: pytest.fail("evicted")) is frame


def test_a_per_depth_folders_new_identity_leaves_one_listing_reader_and_product_of_each_point(tmp_path):
    """The same case for a per-depth run: a file added to the folder changes its identity, and every point's key with
    it; the cache then keeps one listing, one reader per point and one of each product of a point, the latest, never the
    old ones."""
    run = tmp_path / "run"
    for point_id in ("p1", "p2"):
        write_per_depth(run, point_id)
    cache = recon.ProductCache()
    artifact = recon.Artifact(recon.KIND_PER_DEPTH, str(run))
    identities, kept = [], []
    for step in range(3):
        identities.append(recon.identity(artifact))
        for point_id in ("p1", "p2"):
            recon.full_trace(artifact, point_id, cache=cache)
            recon.stored_frame(artifact, point_id, 0, cache=cache)
        kept.append(len(cache))
        (run / f"notes_{step}.txt").write_text("a file the run did not write")
        status = os.stat(run)
        os.utime(run, ns=(status.st_atime_ns, status.st_mtime_ns + 1_000_000))  # a changed folder, however quick

    assert len(set(identities)) == 3
    assert kept == [1 + 2 * 3] * 3  # the listing; each point's reader, trace and frame
    assert all(key[0] == identities[-1] or key[0][0] == identities[-1] for key in cache._entries)


def test_a_product_two_requests_miss_at_once_is_computed_once_and_other_keys_go_ahead(monkeypatch):
    cache = recon.ProductCache()
    value = np.arange(4)

    outcomes, other = _held_computation(monkeypatch, cache, "frame", lambda: value)

    assert outcomes[0] is value and outcomes[1] is value
    assert other == "not held up"
    assert cache.get("frame", lambda: pytest.fail("computed again")) is value


@pytest.mark.parametrize(
    ("error", "where"),
    [
        (OSError(2, "the mount went away", "/data/w1.h5"), "compute"),
        (ProblemError(404, "Point file not available", "R1, point 'w1': w1.h5 is outside the file roots"), "compute"),
        (MemoryError("sizing"), "bookkeeping"),
    ],
    ids=["read", "problem", "bookkeeping"],
)
def test_a_failed_product_fails_the_requests_waiting_for_it_and_the_next_computes_it_again(monkeypatch, error, where):
    """Whether the computation fails or the cache's bookkeeping after it does, the key is released: the computing
    request raises the error, a waiting one its own copy of it (raised from it, so its traceback is its own), and the
    next request computes again."""
    cache = recon.ProductCache()

    def failing():
        raise error

    if where == "bookkeeping":
        monkeypatch.setattr(recon, "_nbytes", lambda value: failing() if value == "computed" else 64)
    outcomes, _ = _held_computation(monkeypatch, cache, "frame", failing if where == "compute" else lambda: "computed")

    assert outcomes[0] is error
    waiter = outcomes[1]
    assert type(waiter) is type(error) and waiter is not error and waiter.__cause__ is error
    assert (str(waiter), vars(waiter)) == (str(error), vars(error))
    assert cache.get("frame", lambda: "read again") == "read again"


# ---------------------------------------------------------------------------
# A point reconstructed elsewhere, published as a one-point scan (as ``seed demo`` publishes R2)


@pytest.fixture(scope="module")
def standalone_point(tmp_path_factory):
    """A standalone point file (no scan, no manifest index), as a point reconstructed outside a scan is: the tiny
    wire scan of ``tests.wire_support`` reconstructed natively."""
    from lauelab.reconstruct import reconstruct_point

    from tests.wire_support import GEOMETRY, write_wire_scan

    directory = tmp_path_factory.mktemp("standalone")
    raw = write_wire_scan(directory / "wire_7.h5", scan_number=42)
    outcome = reconstruct_point(
        raw, directory / "point" / "wire_7.h5", geometry=str(GEOMETRY), detector=0, depth_range=(-25.0, 25.0),
        resolution=5.0, wire_edge="leading", percent_brightest=100.0, memory_limit_mb=256, num_threads=1,
    )  # fmt: skip
    assert outcome.status == "complete", outcome.error
    return directory / "point" / "wire_7.h5"


def _file_state(path):
    status = path.stat()
    return path.read_bytes(), status.st_mtime_ns


@pytest.mark.slow
def test_publish_point_scan_catalogs_a_copy_of_a_standalone_point(standalone_point, tmp_path):
    from lauelab.reconstruct import PointReader, ScanReader

    before = _file_state(standalone_point)
    scan_directory = tmp_path / "rec_2" / "reconstruction"

    record = recon.publish_point_scan(standalone_point, scan_directory)

    assert _file_state(standalone_point) == before  # the source is only read
    assert sorted(p.relative_to(scan_directory).as_posix() for p in scan_directory.rglob("*")) == [
        "points", "points/wire_7.h5", "scan.h5",
    ]  # fmt: skip
    assert (record.point_id, record.scan_number, record.num_threads) == ("wire_7", 42, 1)
    assert record.shape == (11, 32, 32) and record.depth_bounds_um == (-25.0, 25.0)
    assert record.source_path == str(standalone_point.parent.parent / "wire_7.h5")
    assert record.settings["depth_range"] == (-25.0, 25.0) and record.settings["wire_edge"] == "leading"
    assert record.created is not None and record.created.tzinfo is not None

    artifact = recon.Artifact(recon.KIND_SCAN, str(scan_directory / "scan.h5"))
    table = recon.point_table(artifact)
    assert table.run_status == "finished"
    assert [(row.point_id, row.status) for row in table.rows] == [("wire_7", "complete")]
    with ScanReader(scan_directory / "scan.h5") as scan, scan.point("wire_7") as copy:
        assert scan.points[0].source_path == record.source_path
        with PointReader(standalone_point) as source:
            assert source.manifest_index is None and copy.manifest_index == 0
            assert copy.settings == source.settings
            for depth in range(copy.shape[0]):
                np.testing.assert_array_equal(copy.frame(depth), source.frame(depth))


def test_publish_point_scan_refuses_a_directory_that_is_not_empty(tmp_path):
    (tmp_path / "reconstruction").mkdir()
    (tmp_path / "reconstruction" / "scan.h5").write_text("")
    with pytest.raises(FileExistsError, match="not empty"):
        recon.publish_point_scan(tmp_path / "no-such-point.h5", tmp_path / "reconstruction")
