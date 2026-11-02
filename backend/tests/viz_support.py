"""Shared fixtures for visualization tests: a real native results file from the synthetic frames."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest
from lauelab.indexing import Indexer, IndexParams, PeakParams, XmlResultsWriter
from lauelab.visualization import load_results

FIXTURES = Path(__file__).parent / "fixtures" / "lauelab"


def rewrite_in_place(path: str | os.PathLike[str]) -> None:
    """Overwrite ``path`` in place with zeros, then give it back its access and modification times, as ``cp -p`` or
    ``rsync --inplace -t`` leave a file: the inode, the size and the modification time are the same, only the change
    time differs. It writes past the file system's timestamp granularity (a jiffy without multigrain timestamps), so
    that the change time differs on any local file system."""
    before = os.stat(path)
    while time.time_ns() < before.st_ctime_ns + 20_000_000:
        time.sleep(0.005)
    with open(path, "r+b") as handle:
        handle.write(bytes(before.st_size))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = os.stat(path)
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (before.st_ino, before.st_size, before.st_mtime_ns)
    assert after.st_ctime_ns != before.st_ctime_ns


GEOMETRY = FIXTURES / "geoN_2022-03-29_14-15-05.xml"
CRYSTAL = FIXTURES / "Ni.xml"
FRAMES = ("synthetic_ni_grain_a.h5", "synthetic_ni_two_grains.h5", "synthetic_ni_empty.h5", "synthetic_ni_grain_a.h5")

PEAKS = PeakParams(
    boxsize=18, max_rfactor=0.5, min_size=3, min_separation=20, threshold=None, threshold_ratio=4.0, max_peaks=200
)
INDEXING = IndexParams(
    kev_max_calc=17.2, kev_max_test=35.0, angle_tolerance_deg=0.1, cone_deg=72.0, hkl_prefer=(0, 0, 1), max_data=300
)


def write_synthetic_results(directory: Path) -> tuple[Path, Path]:
    """Index the synthetic frames natively into ``output.h5`` and ``output.xml`` under ``directory``.

    Frames get sample positions on a 2 x 2 grid so spatial maps have coordinates. The
    third frame has no pattern, so scopes with unindexed frames are exercised.
    """

    directory.mkdir(parents=True, exist_ok=True)
    frames_dir = directory / "frames"
    frames_dir.mkdir(exist_ok=True)
    indexer = Indexer(str(GEOMETRY), str(CRYSTAL), peak_params=PEAKS, index_params=INDEXING)
    results_path = directory / "output.h5"
    xml_path = directory / "output.xml"
    positions = [(0.0, 0.0, 10.0), (5.0, 0.0, 10.0), (0.0, 5.0, 10.0), (5.0, 5.0, 10.0)]
    with indexer.results_writer(results_path) as writer, XmlResultsWriter(xml_path) as xml:
        for index, (name, position) in enumerate(zip(FRAMES, positions, strict=True), start=1):
            frame = frames_dir / f"frame_{index}.h5"
            shutil.copy(FIXTURES / name, frame)
            result = indexer.index(str(frame), keep_image=False, metadata={"sample_position": position})
            writer.append(result, frame_id=f"frame_{index}")
            xml.append(result)
    return results_path, xml_path


@pytest.fixture(scope="session")
def synthetic_results(tmp_path_factory):
    results_path, xml_path = write_synthetic_results(tmp_path_factory.mktemp("viz"))
    return results_path, xml_path


@pytest.fixture(scope="session")
def synthetic_dataset(synthetic_results):
    return load_results(synthetic_results[0])
