"""Tests for external detector-image discovery and its missing-image handling."""

from pathlib import Path

import h5py
import numpy as np

from laueportal.core.settings import BeamlineSettings
from laueportal.science import detector as detector_image

DATASETS = BeamlineSettings().detector_datasets  # the default settings' list


def anywhere(path: Path) -> Path:
    """An ``admit`` that allows every path as it is (these tests configure no file roots)."""
    return path


def _write_image(path):
    with h5py.File(path, "w") as handle:
        handle.create_dataset("entry1/data/data", data=np.arange(16, dtype=np.uint16).reshape(4, 4))


def test_image_is_resolved_through_the_portal_search_rules(tmp_path):
    frame = tmp_path / "data" / "frame_1.h5"
    frame.parent.mkdir()
    _write_image(frame)
    xml_path = tmp_path / "run" / "output.xml"
    xml_path.parent.mkdir()

    # A relative inputImage is searched next to the XML, in the run's data folder, and under the root.
    result = detector_image.load_detector_image(
        "frame_1.h5",
        xml_path=str(xml_path),
        data_folder=str(frame.parent),
        root_path=str(tmp_path),
        dataset_paths=DATASETS,
        admit=anywhere,
    )

    assert result.image is not None
    assert result.image.path == str(frame) and result.image.dataset.endswith("entry1/data/data")
    assert result.image.data.shape == (4, 4) and result.image.vmin <= result.image.vmax
    assert result.warning is None


def test_missing_and_unreadable_locations_become_a_warning_not_an_error(tmp_path, monkeypatch):
    real_is_file = Path.is_file

    def denied(self):
        if "forbidden" in str(self):
            raise PermissionError(13, "Permission denied", str(self))
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", denied)
    result = detector_image.load_detector_image(
        "/forbidden/frame_2.h5",
        xml_path=str(tmp_path / "output.xml"),
        data_folder=str(tmp_path),
        root_path=None,
        dataset_paths=DATASETS,
        admit=anywhere,
    )

    assert result.image is None
    assert result.warning.startswith("Detector image file was not found.")
    assert "/forbidden/frame_2.h5 (not accessible: Permission denied)" in result.warning
    assert result.attempted_paths
    assert detector_image.load_detector_image(None, dataset_paths=DATASETS, admit=anywhere).warning.startswith(
        "No inputImage"
    )


def test_dataset_paths_come_from_the_settings_then_the_first_2d_dataset(tmp_path):
    frame = tmp_path / "frame.h5"
    with h5py.File(frame, "w") as handle:
        handle.create_dataset("entry1/data/data", data=np.full((2, 2), 1, dtype=np.uint16))
        handle.create_dataset("custom/image", data=np.full((3, 3), 2, dtype=np.uint16))
        handle.create_dataset("a_vector", data=np.arange(4))

    def load(datasets):
        result = detector_image.load_detector_image(str(frame), dataset_paths=datasets, admit=anywhere)
        assert result.image is not None, result.warning
        return result.image.dataset, result.image.data.shape

    assert load(DATASETS) == ("/entry1/data/data", (2, 2))  # the default list, in order
    custom = BeamlineSettings(detector_datasets=["/custom/image", "/entry1/data/data"]).detector_datasets
    assert load(custom) == ("/custom/image", (3, 3))  # the configured order wins
    assert load(["custom/image"]) == ("custom/image", (3, 3))  # the leading "/" is optional
    assert load(["/not/there"]) == ("/custom/image", (3, 3))  # fallback: the first dataset with >= 2 dimensions
    assert load([]) == ("/custom/image", (3, 3))


def test_a_stack_gives_its_first_frame_and_only_that_frame_is_read(tmp_path, monkeypatch):
    frame = tmp_path / "stack.h5"
    stack = np.arange(12, dtype=np.uint16).reshape(3, 2, 2)
    with h5py.File(frame, "w") as handle:
        handle.create_dataset("stack", data=stack)
    reads = []
    read = h5py.Dataset.__getitem__

    def recorded(self, selection, *args, **kwargs):
        reads.append(selection)
        return read(self, selection, *args, **kwargs)

    monkeypatch.setattr(h5py.Dataset, "__getitem__", recorded)
    data, dataset = detector_image.read_detector_frame(frame, dataset_paths=DATASETS)

    assert (dataset, data.tolist(), reads) == ("/stack", stack[0].tolist(), [(0,)])
