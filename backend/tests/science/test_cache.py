"""The results viewer's dataset cache (``science.cache``): an LRU keyed by the file's identity."""

from __future__ import annotations

import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from lauelab.visualization import VisualizationDataset, convert_xml

from laueportal.app import create_app
from laueportal.science import cache as cache_module
from laueportal.science import recon
from laueportal.science.cache import DatasetCache, load_dataset
from tests.settings_support import make_settings
from tests.viz_support import rewrite_in_place

FIXTURE_XML = Path(__file__).parents[1] / "fixtures" / "test_indexing.xml"
GEOMETRY = Path(__file__).parents[1] / "fixtures" / "lauelab" / "geoN_2022-03-29_14-15-05.xml"


@pytest.mark.parametrize(
    "identity",
    [cache_module.file_identity, cache_module.source_key, recon._file_identity],
    ids=["file_identity", "source_key", "recon"],
)
def test_a_file_rewritten_in_place_with_its_size_and_mtime_is_another_identity(tmp_path: Path, identity) -> None:
    """The identities the caches key by (the results viewer's datasets, whose key ``source_key`` copies the file's
    identity field by field, and the wire viewer's products, as the validation of a results file) hold the change time:
    a file rewritten in place with its size and modification time kept is not the file they loaded)."""
    path = convert_xml(FIXTURE_XML, tmp_path / "output.h5")
    before = identity(path)

    rewrite_in_place(path)

    assert identity(path) != before


def test_the_cache_reuses_a_dataset_reloads_a_changed_file_and_evicts_the_least_recently_used(tmp_path: Path) -> None:
    first = convert_xml(FIXTURE_XML, tmp_path / "first.h5")
    second, third = (shutil.copy(first, tmp_path / name) for name in ("second.h5", "third.h5"))
    cache = DatasetCache(capacity=2)

    dataset = cache.get(first)
    assert cache.get(first) is dataset  # the same file: reused
    cache.get(second)
    cache.get(first)  # first is now the most recently used
    cache.get(third)  # evicts second
    assert [Path(key.path).name for key in cache.keys()] == ["first.h5", "third.h5"]
    assert (cache.hits, cache.misses, cache.evictions) == (2, 3, 1)

    os.utime(first, ns=(0, os.stat(first).st_mtime_ns + 1_000_000))  # republished in place
    reloaded = cache.get(first)
    assert reloaded is not dataset and len(cache) == 2  # the stale entry was replaced, not kept beside it

    geometry = str(shutil.copy(GEOMETRY, tmp_path / "geometry.xml"))
    with_geometry = cache.get(first, geometry=geometry)
    assert cache.get(first, geometry=geometry) is with_geometry
    with open(geometry, "a") as file:  # the run's geometry file edited in place
        file.write("\n")
    assert cache.get(first, geometry=geometry) is not with_geometry
    assert create_app(make_settings(tmp_path, indexing={"dataset_cache_size": 3})).state.datasets.capacity == 3


def test_a_load_holds_up_only_the_requests_for_its_own_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-key loading: while one file loads, a cached file is served and another file loads; a second request for
    the loading file waits for that load rather than loading it again; what is derived from a dataset is made once
    and goes with it."""
    slow = convert_xml(FIXTURE_XML, tmp_path / "slow.h5")
    cached, other = (shutil.copy(slow, tmp_path / name) for name in ("cached.h5", "other.h5"))
    cache = DatasetCache(capacity=2)
    hot = cache.get(cached)
    started, release = threading.Event(), threading.Event()
    loads: list[str] = []

    def load(path: str, *, geometry: str | None = None) -> VisualizationDataset:
        loads.append(Path(path).name)
        if Path(path).name == "slow.h5":
            started.set()
            assert release.wait(10)
        return load_dataset(path, geometry=geometry)

    monkeypatch.setattr(cache_module, "load_dataset", load)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(cache.get, slow)
        assert started.wait(10)
        hits = cache.hits
        second = pool.submit(cache.get, slow)
        while cache.hits == hits and not second.done():  # the second request is waiting for the first's load
            time.sleep(0.01)
        assert cache.get(cached) is hot  # a hit is served during the load ...
        assert cache.get(other) is not None  # ... and another file loads
        release.set()
        assert first.result(10) is second.result(10)
    assert loads == ["slow.h5", "other.h5"]

    made: list[int] = []
    dataset = first.result()
    tables = [cache.derived(dataset, "table", lambda value: made.append(1) or value.n_frames) for _ in range(2)]
    assert (tables, made) == ([dataset.n_frames] * 2, [1])
    cache.get(cached), cache.get(other)  # evict slow.h5's dataset, and what was derived from it
    assert cache.derived(dataset, "table", lambda value: made.append(1) or 0) == 0 and made == [1, 1]


def test_a_failed_load_fails_every_request_waiting_for_it_and_the_next_request_loads_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = convert_xml(FIXTURE_XML, tmp_path / "a.h5")
    cache = DatasetCache()
    started, release = threading.Event(), threading.Event()
    loads: list[str] = []

    def failing(path: str, *, geometry: str | None = None) -> VisualizationDataset:
        loads.append(Path(path).name)
        started.set()
        assert release.wait(10)
        raise OSError("unreadable")

    monkeypatch.setattr(cache_module, "load_dataset", failing)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(cache.get, path)
        assert started.wait(10)
        hits = cache.hits
        second = pool.submit(cache.get, path)
        while cache.hits == hits and not second.done():  # the second request is waiting for the first's load
            time.sleep(0.01)
        release.set()
        for request in (first, second):  # the error the viewer answers as its results-unreadable problem
            with pytest.raises(OSError, match="unreadable"):
                request.result(10)
    assert loads == ["a.h5"]
    monkeypatch.setattr(cache_module, "load_dataset", load_dataset)
    assert cache.get(path).n_frames > 0 and cache.misses == 2  # the key is not poisoned


def test_at_most_max_loads_files_load_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = convert_xml(FIXTURE_XML, tmp_path / "a.h5")
    paths = [first, *(shutil.copy(first, tmp_path / name) for name in ("b.h5", "c.h5"))]
    cache = DatasetCache(capacity=3)
    lock, two, release = threading.Lock(), threading.Event(), threading.Event()
    loading: list[str] = []
    most = 0

    def load(path: str, *, geometry: str | None = None) -> VisualizationDataset:
        nonlocal most
        with lock:
            loading.append(path)
            most = max(most, len(loading))
            if len(loading) == cache_module.MAX_LOADS:
                two.set()
        try:
            assert release.wait(10)
            return load_dataset(path, geometry=geometry)
        finally:
            with lock:
                loading.remove(path)

    monkeypatch.setattr(cache_module, "load_dataset", load)
    with ThreadPoolExecutor(3) as pool:
        requests = [pool.submit(cache.get, path) for path in paths]
        assert two.wait(10)
        time.sleep(0.2)  # time for the third file's load to start, were it not waiting for a turn
        assert (most, cache.misses) == (2, 3)
        release.set()
        assert len({id(request.result(10)) for request in requests}) == 3
    assert most == 2


def test_one_output_xml_loads_at_a_time_and_a_results_file_loads_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An Output XML's parse peaks at about 7.5 times its size, so two never load at once; an ``output.h5`` takes the
    other turn."""

    xmls = [shutil.copy(FIXTURE_XML, tmp_path / name) for name in ("a.xml", "b.xml")]
    paths = [*xmls, convert_xml(FIXTURE_XML, tmp_path / "c.h5")]
    cache = DatasetCache(capacity=3)
    lock, two, release = threading.Lock(), threading.Event(), threading.Event()
    loading: list[str] = []
    most_xml = 0

    def load(path: str, *, geometry: str | None = None) -> VisualizationDataset:
        nonlocal most_xml
        with lock:
            loading.append(path)
            most_xml = max(most_xml, sum(name.endswith(".xml") for name in loading))
            if len(loading) == 2:
                two.set()
        try:
            assert release.wait(10)
            return load_dataset(path, geometry=geometry)
        finally:
            with lock:
                loading.remove(path)

    monkeypatch.setattr(cache_module, "load_dataset", load)
    with ThreadPoolExecutor(3) as pool:
        requests = [pool.submit(cache.get, path) for path in paths]
        assert two.wait(10)
        time.sleep(0.2)  # time for the second XML's load to start, were it not waiting for its turn
        with lock:
            assert sorted(Path(name).suffix for name in loading) == [".h5", ".xml"]
        release.set()
        assert len({id(request.result(10)) for request in requests}) == 3
    assert (most_xml, cache.misses) == (1, 3)
