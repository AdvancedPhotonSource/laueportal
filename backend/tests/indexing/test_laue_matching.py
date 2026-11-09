"""LaueMatching runs: the compute function against a stand-in for the indexer binary (the tables are written by the
test, so what each step does with them can be checked exactly), the forward-cache rules, and the form's checks."""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import h5py
import numpy as np
import pytest
from fastapi.testclient import TestClient
from lauelab.indexing import load_crystal, load_geometry
from sqlalchemy.orm import Session

pytest.importorskip("laue_index")

from laueportal.app import create_app  # noqa: E402
from laueportal.indexing.laue_matching import cache
from laueportal.indexing.laue_matching import compute as laue_compute
from laueportal.indexing.laue_matching.params import LaueMatchingParamsV1  # noqa: E402
from laueportal.indexing.models import IndexingRun  # noqa: E402
from laueportal.indexing.validation import validate_laue_matching  # noqa: E402
from laueportal.jobs import programs  # noqa: E402
from laueportal.jobs.contract import RunRequest, get_compute_function  # noqa: E402
from laueportal.jobs.creation import submit  # noqa: E402
from laueportal.jobs.models import Job  # noqa: E402
from laueportal.jobs.rundir import ManifestEntry  # noqa: E402
from laueportal.science.laue_matching import centring_letter, read_instrument  # noqa: E402
from tests.validation_support import Place, found, make_place  # noqa: E402

FIXTURES = Path(__file__).parents[1] / "fixtures" / "lauelab"
GEOMETRY = FIXTURES / "geoN_2022-03-29_14-15-05.xml"
CRYSTAL = FIXTURES / "Ni.xml"
BLOBS = ((100, 200), (800, 300), (1500, 1200), (400, 1700))
"""Spot centres (x, y) of the synthetic frames."""
NAME = "Ni_test"
# The reciprocal matrix the stand-in reports: its columns are a*, b*, c*, and no two are equal, so a transpose shows.
RECIP = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, -6.0], [7.0, -8.0, 9.0]])
ROTATION = np.eye(3)
QUALITY = 123.5


def write_frame(path: Path, *, blobs=BLOBS, shape=(2048, 2048), start=(0, 0), binning=(1, 1)) -> Path:
    image = np.zeros(shape, dtype=np.uint16)
    for x, y in blobs:
        image[y - 1 : y + 2, x - 1 : x + 2] = 1000
    with h5py.File(path, "w") as frame:
        frame["entry1/data/data"] = image
        frame["entry1/detector/startx"], frame["entry1/detector/starty"] = [start[0]], [start[1]]
        frame["entry1/detector/binx"], frame["entry1/detector/biny"] = [binning[0]], [binning[1]]
        frame["entry1/sample/sampleX"] = [1.0]
        frame["entry1/sample/sampleY"] = [2.0]
        frame["entry1/sample/sampleZ"] = [3.0]
    return path


def solution_row() -> list[float]:
    row = [0.0] * laue_compute.SOLUTION_COLUMNS
    row[0], row[1], row[2], row[4], row[5], row[6] = 1, 1, 100.0, QUALITY, 2, 2
    row[7:16] = RECIP.ravel().tolist()
    row[16:22] = [0.35, 0.35, 0.35, 90, 90, 90]
    row[22:31] = ROTATION.ravel().tolist()
    return row


def spot_row(x: float, y: float, hkl=(1, 1, 1)) -> list[float]:
    return [1, 0, *hkl, x, y, 0.0, 0.0, -1.0, 500.0]


def stand_in(tmp_path: Path, script: str = "") -> Path:
    """An executable that writes the tables of one orientation with a spot on the first blob and one far from any."""
    path = tmp_path / "LaueMatchingCPU"
    rows = {
        "solutions": ["\t".join(f"c{i}" for i in range(laue_compute.SOLUTION_COLUMNS)), _text(solution_row())],
        "spots": [
            "\t".join(f"s{i}" for i in range(laue_compute.SPOT_COLUMNS)),
            _text(spot_row(BLOBS[0][0], BLOBS[0][1])),
            _text(spot_row(20, 20, (2, 0, 0))),
        ],
    }
    body = textwrap.dedent(
        f"""\
        #!{sys.executable}
        import os, sys, json
        image = sys.argv[4]
        log = {str(tmp_path / "calls.jsonl")!r}
        with open(log, "a") as stream:
            stream.write(json.dumps(sys.argv[1:]) + "\\n")
        {script}
        rows = {json.dumps(rows)}
        for name, lines in rows.items():
            with open(image + "." + name + ".txt", "w") as stream:
                stream.write("\\n".join(lines) + "\\n")
        """
    )
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _text(row: list[float]) -> str:
    return "\t".join(repr(value) for value in row)


def calls(tmp_path: Path) -> int:
    log = tmp_path / "calls.jsonl"
    return len(log.read_text().splitlines()) if log.exists() else 0


@dataclasses.dataclass
class Hooks:
    stop_after_calls: int | None = None
    failures: list[Any] = dataclasses.field(default_factory=list)
    progress: list[tuple[int, int]] = dataclasses.field(default_factory=list)
    messages: list[str] = dataclasses.field(default_factory=list)
    stopping: threading.Event = dataclasses.field(default_factory=threading.Event)

    def should_stop(self) -> bool:
        return self.stopping.is_set()

    def shutting_down(self) -> bool:
        return False

    def arrivals(self) -> tuple[()]:
        return ()

    def report_progress(self, *, succeeded: int, failed: int) -> None:
        self.progress.append((succeeded, failed))

    def record_failure(self, record: Any) -> None:
        self.failures.append(record)

    def report_outcome(self, outcome: Any) -> None:
        pass

    def log(self, message: str) -> None:
        self.messages.append(message)


def instrument() -> dict[str, Any]:
    return dataclasses.asdict(read_instrument(str(GEOMETRY), str(CRYSTAL)))


def parameters(data_root: Path, **changes: Any) -> dict[str, Any]:
    values = {
        **instrument(),
        "n_orientations": 2,
        "data_root": str(data_root),
        "forward_cache": NAME,
        "create_forward_cache": False,
        "geometry_file": str(GEOMETRY),
        "crystal_file": str(CRYSTAL),
        "mask_file": None,
        "background_file": None,
        "threshold_method": "fixed",
        "threshold_percentile": 99.8,
        "threshold_value": 1.0,
        "min_area": 4,
        "watershed_image": True,
        "elo": 5.0,
        "ehi": 30.0,
        "max_nr_laue_spots": 30,
        "min_nr_spots": 1,
        "min_good_spots": 1,
        "min_intensity": 1.0,
        "max_angle": 3.0,
        "robust_filter": True,
        "orientation_spacing": 0.4,
        "filter_radius": 31,
        "gauss_sigma_max": 2.5,
        "num_threads": 1,
    }
    return values | changes


def write_cache(data_root: Path, values: dict[str, Any], name: str = NAME, **record_changes: Any) -> Path:
    """The orientation database and a named cache whose record holds ``values``, with ``record_changes`` over them."""
    data_root.mkdir(parents=True, exist_ok=True)
    (data_root / cache.DATABASE_NAME).write_bytes(b"\0" * (cache.ORIENTATION_BYTES * values["n_orientations"]))
    folder = data_root / name
    folder.mkdir(exist_ok=True)
    (folder / cache.FORWARD_NAME).write_bytes(b"\0" * 16)
    (folder / cache.HKL_NAME).write_text("0 0 2\n")
    config = cache.key_from_parameters(values).expected() | record_changes
    record = {"kind": cache.FORWARD_KIND, "artifact": {"size": 16}, "config": config}
    (folder / cache.RECORD_NAME).write_text(json.dumps(record))
    return folder


@pytest.fixture
def run(tmp_path, monkeypatch):
    """A function that computes ``frames`` (paths, or manifest entries) with these parameters and returns the outcome,
    the run directory and its hooks."""
    data_root = tmp_path / "data"
    monkeypatch.setenv("LAUEMATCHING_BIN", str(stand_in(tmp_path)))

    def compute(frames, values=None, hooks=None):
        hooks = hooks or Hooks()
        directory = tmp_path / "run"
        values = parameters(data_root) if values is None else values
        entries = [
            frame
            if isinstance(frame, ManifestEntry)
            else ManifestEntry(
                index=i, input_id=f"f{i}", source=str(frame), template_index=0, scan_point=i, depth_point=None
            )
            for i, frame in enumerate(frames)
        ]
        request = RunRequest(
            job_id=1,
            kind=laue_compute.LAUE_MATCHING_KIND,
            display_id="I1",
            run_directory=str(directory),
            manifest_path=str(directory / "inputs.jsonl"),
            document={"request": values, "output": {}},
            n_inputs=len(entries),
            workers=1,
            max_in_flight=None,
        )
        return get_compute_function(request.kind)(request, entries, hooks), directory, hooks

    return compute


def test_a_frame_becomes_a_validated_results_file_and_the_native_tables(tmp_path, run):
    write_cache(tmp_path / "data", parameters(tmp_path / "data"))
    frame = write_frame(tmp_path / "frame.h5")
    outcome, directory, hooks = run([frame])

    assert (outcome.n_succeeded, outcome.n_failed, outcome.n_indexed, outcome.stopped) == (1, 0, 1, False)
    assert outcome.validation_error is None and hooks.failures == []
    assert outcome.artifacts == {
        "results": str(directory / "output.h5"),
        "native_tables": str(directory / "laue_matching.h5"),
    }
    assert outcome.provenance["unmatched_spots"] == 1  # the far spot names no observed peak
    assert outcome.provenance["forward_cache"]["name"] == NAME
    with h5py.File(directory / "output.h5") as results:
        assert results["run"].attrs["program"] == "laue_matching"
        assert results["run"].attrs["forward_cache"] == NAME
        assert results["frames/n_peaks"][()].tolist() == [len(BLOBS)]
        assert results["frames/n_patterns"][()].tolist() == [1]
        assert results["frames/sample_positions"][0].tolist() == [1.0, 2.0, 3.0]
        # the indexer's columns a*, b*, c* are the format's rows
        np.testing.assert_array_equal(results["patterns/reciprocal"][0], RECIP.T)
        assert results["patterns/goodness"][()].tolist() == [QUALITY]
        assert np.isnan(results["patterns/rms_error_deg"][0])
        # only the spot on a blob is assigned, to that blob
        assert results["assignments/peak_index"][()].tolist() == [0]
        assert results["assignments/hkl"][()].tolist() == [[1, 1, 1]]
        assert np.isnan(results["assignments/error_deg"][0])
        assert results["peaks/fit_x"][0] == pytest.approx(BLOBS[0][0])
        assert results["peaks/fit_y"][0] == pytest.approx(BLOBS[0][1])
        assert np.isnan(results["peaks/chisq"][0])
    with h5py.File(directory / "laue_matching.h5") as native:
        assert native["solutions"].shape == (1, laue_compute.SOLUTION_COLUMNS)
        assert native["spots"].shape[0] == 2  # both spots, also the one with no peak
        assert native["kept"][()].tolist() == [True]
        assert native["solution_offsets"][()].tolist() == [0, 1]
        assert [name.decode() for name in native["frame_ids"][()]] == ["f0"]
    text = (directory / "params.txt").read_text()
    assert "DoFwd 0\n" in text
    assert f"ForwardFile {tmp_path}/data/{NAME}/forward.bin\n" in text
    assert not list(Path(os.environ["TMPDIR"]).glob("laue_matching_1_*"))  # the scratch folder is gone


def test_the_geometry_conversion_is_metres_and_radians():
    values = read_instrument(str(GEOMETRY), str(CRYSTAL))
    detector = load_geometry(str(GEOMETRY)).detector(0)
    assert values.p_array == pytest.approx(tuple(detector.translation * 1e-6))
    assert values.r_array == pytest.approx(tuple(detector.rotation_vector))
    assert values.px_x == pytest.approx(detector.size_x / detector.nx * 1e-6)
    assert values.nr_px_x == detector.nx
    crystal = load_crystal(str(CRYSTAL))
    assert values.space_group == crystal.space_group
    assert values.symmetry == "F"
    assert values.lattice_parameters[0] == pytest.approx(0.35238, abs=1e-3)


@pytest.mark.parametrize(
    ("group", "letter"), [(225, "F"), (229, "I"), (166, "R"), (15, "C"), (38, "A"), (194, "P"), (1, "P")]
)
def test_the_centring_letter_of_a_space_group(group, letter):
    assert centring_letter(group) == letter


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"Elo": 6.0}, "Elo"),
        ({"MaxNrLaueSpots": 7}, "MaxNrLaueSpots"),
        ({"n_orientations": 3}, "n_orientations"),
        ({"P_Array": [0.1, 0.2, 0.3]}, "P_Array"),
    ],
)
def test_a_cache_built_for_other_values_is_refused_before_the_indexer_starts(tmp_path, run, changes, problem):
    write_cache(tmp_path / "data", parameters(tmp_path / "data"), **changes)
    with pytest.raises(RuntimeError, match=problem):
        run([write_frame(tmp_path / "frame.h5")])
    assert calls(tmp_path) == 0


def test_a_tiny_change_in_the_geometry_is_another_cache(tmp_path):
    key = cache.key_from_parameters(parameters(tmp_path))
    moved = dataclasses.replace(key, p_array=(key.p_array[0] + 1e-12, *key.p_array[1:]))
    record = {"kind": cache.FORWARD_KIND, "config": key.expected()}
    assert cache.record_mismatch(record, key) is None
    assert "P_Array" in (cache.record_mismatch(record, moved) or "")


def test_a_forward_file_of_another_size_is_refused_so_the_indexer_cannot_rebuild_over_it(tmp_path, run):
    folder = write_cache(tmp_path / "data", parameters(tmp_path / "data"))
    (folder / cache.FORWARD_NAME).write_bytes(b"\0" * 8)
    with pytest.raises(RuntimeError, match="its record says 16"):
        run([write_frame(tmp_path / "frame.h5")])
    assert calls(tmp_path) == 0


def test_a_new_cache_needs_a_free_name(tmp_path, run):
    write_cache(tmp_path / "data", parameters(tmp_path / "data"))
    values = parameters(tmp_path / "data", create_forward_cache=True)
    with pytest.raises(RuntimeError, match="a new cache needs a new name"):
        run([write_frame(tmp_path / "frame.h5")], values)
    assert calls(tmp_path) == 0


def test_a_new_cache_writes_its_reflections_and_starts_the_build(tmp_path, run, monkeypatch):
    data_root = tmp_path / "data"
    write_cache(data_root, parameters(data_root), name="other")
    values = parameters(data_root, create_forward_cache=True, forward_cache="fresh")
    made = []
    monkeypatch.setattr(
        laue_compute,
        "_generate_hkls",
        lambda _values, files: made.append(files.hkls) or files.hkls.write_text("0 0 2\n"),
    )
    outcome, _, hooks = run([write_frame(tmp_path / "frame.h5")], values)
    assert made == [data_root / "fresh" / cache.HKL_NAME]
    assert outcome.n_succeeded == 1
    assert hooks.messages == ["Building forward cache fresh with the first frame"]
    assert outcome.provenance["forward_cache"]["created_by_this_run"] is True


@pytest.mark.parametrize(
    "frame",
    [
        {"shape": (1024, 1024)},  # another shape
        {"binning": (2, 2)},  # the full shape, binned
        {"start": (4, 0)},  # the full shape, from another origin
    ],
)
def test_a_frame_that_is_not_the_full_detector_fails_alone_and_starts_no_indexer(tmp_path, run, frame):
    write_cache(tmp_path / "data", parameters(tmp_path / "data"))
    bad = write_frame(tmp_path / "bad.h5", blobs=((10, 10),), **frame)
    good = write_frame(tmp_path / "good.h5")
    outcome, _, hooks = run([bad, good])
    assert (outcome.n_succeeded, outcome.n_failed) == (1, 1)
    assert "needs the full detector" in hooks.failures[0].message
    assert calls(tmp_path) == 1  # only the good frame


def test_a_frame_with_no_spots_is_processed_without_the_indexer(tmp_path, run):
    write_cache(tmp_path / "data", parameters(tmp_path / "data"))
    outcome, directory, _ = run([write_frame(tmp_path / "empty.h5", blobs=())])
    assert (outcome.n_succeeded, outcome.n_failed, outcome.n_indexed) == (1, 0, 0)
    assert calls(tmp_path) == 0
    with h5py.File(directory / "output.h5") as results:
        assert results["frames/n_patterns"][()].tolist() == [0]


def test_a_mask_drops_the_spots_it_covers(tmp_path, run):
    data_root = tmp_path / "data"
    write_cache(data_root, parameters(data_root))
    mask = np.zeros((2048, 2048), dtype=np.uint8)
    x, y = BLOBS[1]
    mask[y - 3 : y + 4, x - 3 : x + 4] = 1
    with h5py.File(tmp_path / "mask.h5", "w") as stream:
        stream["entry1/data/data"] = mask
    values = parameters(data_root, mask_file=str(tmp_path / "mask.h5"))
    _, directory, _ = run([write_frame(tmp_path / "frame.h5")], values)
    with h5py.File(directory / "output.h5") as results:
        assert results["frames/n_peaks"][()].tolist() == [len(BLOBS) - 1]


def test_a_failing_indexer_fails_that_frame_with_its_output(tmp_path, monkeypatch, run):
    data_root = tmp_path / "data"
    write_cache(data_root, parameters(data_root))
    monkeypatch.setenv("LAUEMATCHING_BIN", str(stand_in(tmp_path, 'print("FATAL: no"); sys.exit(3)')))
    outcome, directory, hooks = run([write_frame(tmp_path / "frame.h5")])
    assert (outcome.n_succeeded, outcome.n_failed) == (0, 1)
    assert "exited with code 3" in hooks.failures[0].message and "FATAL: no" in hooks.failures[0].message
    assert not (directory / "output.h5").exists()  # no scientific file without a processed frame


def test_stop_ends_a_running_indexer_keeps_finished_frames_and_cancels_the_rest(tmp_path, monkeypatch, run):
    data_root = tmp_path / "data"
    write_cache(data_root, parameters(data_root))
    script = """
    import time
    if sum(1 for _ in open(log)) == 2:
        time.sleep(30)
    """
    monkeypatch.setenv("LAUEMATCHING_BIN", str(stand_in(tmp_path, textwrap.dedent(script).replace("\n", "\n        "))))
    frames = [write_frame(tmp_path / f"frame{index}.h5") for index in range(3)]
    hooks = Hooks()

    def stop_when_the_second_frame_runs() -> None:
        while calls(tmp_path) < 2:
            time.sleep(0.02)
        hooks.stopping.set()

    threading.Thread(target=stop_when_the_second_frame_runs, daemon=True).start()
    started = time.monotonic()
    outcome, directory, hooks = run(frames, hooks=hooks)

    assert time.monotonic() - started < 15  # not the indexer's 30 s
    assert (outcome.n_succeeded, outcome.stopped) == (1, True)
    assert [failure.category for failure in hooks.failures] == ["cancelled", "cancelled"]
    with h5py.File(directory / "output.h5") as results:  # the finished frame is kept
        assert len(results["frames/frame_ids"]) == 1


def test_a_reconstructed_frame_gets_no_background_and_a_raw_one_the_first_frames(tmp_path):
    background = laue_compute._Background(parameters(tmp_path, filter_radius=3), str(tmp_path))
    point = ManifestEntry(
        index=0, input_id="p", source="p.h5", template_index=0, scan_point=0, depth_point=0, point_id="1", depth_index=0
    )
    raw = np.ones((2048, 2048))
    assert background.for_frame(point, raw) is None
    entry = ManifestEntry(index=0, input_id="f", source="first.h5", template_index=0, scan_point=0, depth_point=None)
    first = background.for_frame(entry, raw)
    assert first is not None and (tmp_path / laue_compute.BACKGROUND_FILENAME).is_file()
    assert background.for_frame(entry, raw * 5) is first  # computed once, from the run's first frame
    assert background.source == "first.h5"


def test_a_deep_frame_warns_in_the_outcome(tmp_path, run):
    write_cache(tmp_path / "data", parameters(tmp_path / "data"))
    deep = ManifestEntry(
        index=0,
        input_id="d",
        source=str(write_frame(tmp_path / "f.h5")),
        template_index=0,
        scan_point=0,
        depth_point=0,
        depth=150.0,
    )
    outcome, _, _ = run([deep])
    assert len(outcome.warnings) == 1 and "100 um" in outcome.warnings[0]


def test_compute_refuses_to_start_without_the_optional_packages(tmp_path, run, monkeypatch):
    write_cache(tmp_path / "data", parameters(tmp_path / "data"))
    monkeypatch.setattr(laue_compute, "HAS_DIPLIB", False)
    with pytest.raises(RuntimeError, match="diplib"):
        run([write_frame(tmp_path / "frame.h5")])


# ---------------------------------------------------------------------------
# the parameter model and the form's checks


@pytest.mark.parametrize("name", ["", "../up", ".hidden", "a b", "a/b", "x" * 129])
def test_a_cache_name_that_is_not_a_plain_folder_name_is_refused(name):
    assert cache.name_problem(name) is not None


@pytest.mark.parametrize("name", ["Ni", "Ni_geoN_5-30", "a.b-c_1"])
def test_a_plain_cache_name_is_accepted(name):
    assert cache.name_problem(name) is None


@pytest.fixture
def place(tmp_path) -> Iterator[Place]:
    made = make_place(tmp_path)
    made.geometry.write_bytes(GEOMETRY.read_bytes())
    made.crystal.write_bytes(CRYSTAL.read_bytes())
    yield made
    made.engine.dispose()


def matching(place: Place, tmp_path: Path) -> tuple[Any, dict[str, Any], Path]:
    root = tmp_path / "shared"
    settings = place.settings.model_copy(
        update={"laue_matching": place.settings.laue_matching.model_copy(update={"data_root": root})}
    )
    defaults = programs.form_defaults("laue_matching", settings)
    values = {
        **instrument(),
        "n_orientations": 2,
        "elo": defaults["elo"],
        "ehi": defaults["ehi"],
        "max_nr_laue_spots": defaults["max_nr_laue_spots"],
    }
    return settings, values, root


def checked(place: Place, settings: Any, **values: Any):
    return validate_laue_matching(place.form("laue_matching", **values), settings=settings, engine=place.engine)


def cache_issues(validated: Any) -> set[tuple[Any, str, str]]:
    return {issue for issue in found(validated) if issue[2] != "unlinked"}


def test_the_form_has_no_target_and_refuses_the_portals_values(place):
    model = LaueMatchingParamsV1
    assert "backend" not in model.fields_from("form") and "backend" in model.fields_from("derived")
    for name in ("p_array", "data_root", "num_threads"):
        with pytest.raises(ValueError, match="set by the portal"):
            programs.build_params("laue_matching", place.form("laue_matching", **{name: 1}), place.settings)


def test_an_unset_data_folder_is_one_error(place):
    validated = checked(place, place.settings)
    assert cache_issues(validated) == {(("forward_cache",), "error", "not_configured")}
    assert not validated.ok


def test_a_cache_that_fits_passes_and_one_that_does_not_is_an_error(place, tmp_path):
    settings, values, root = matching(place, tmp_path)
    write_cache(root, values, name="cache")
    assert cache_issues(checked(place, settings)) == set()

    write_cache(root, values, name="other", Ehi=99.0)
    validated = checked(place, settings, forward_cache="other")
    assert cache_issues(validated) == {(("forward_cache",), "error", "cache_mismatch")}
    assert "Ehi" in next(issue.message for issue in validated.issues if issue.code == "cache_mismatch")


def test_a_missing_cache_and_a_taken_new_name_are_errors_and_a_new_name_warns(place, tmp_path):
    settings, values, root = matching(place, tmp_path)
    write_cache(root, values, name="cache")
    assert cache_issues(checked(place, settings, forward_cache="absent")) == {
        (("forward_cache",), "error", "cache_not_found")
    }
    assert cache_issues(checked(place, settings, create_forward_cache=True)) == {
        (("forward_cache",), "error", "cache_exists")
    }
    assert cache_issues(checked(place, settings, forward_cache="fresh", create_forward_cache=True)) == {
        (("forward_cache",), "warning", "cache_build")
    }


def test_the_energy_range_must_be_ascending(place, tmp_path):
    settings, values, root = matching(place, tmp_path)
    write_cache(root, values, name="cache")
    validated = checked(place, settings, elo=40.0, ehi=30.0)
    assert (("ehi",), "error", "energy_range") in found(validated)


def test_an_unreadable_geometry_is_reported_on_its_field(place, tmp_path):
    settings, values, root = matching(place, tmp_path)
    write_cache(root, values, name="cache")
    place.geometry.write_text("not xml")
    assert (("geometry_file",), "error", "unreadable") in found(checked(place, settings))


def test_the_list_offers_only_caches_that_fit_and_a_free_name(place, tmp_path):
    settings, values, root = matching(place, tmp_path)
    write_cache(root, values, name="fits")
    write_cache(root, values, name="other", Ehi=99.0)
    (root / "half-built").mkdir()  # no forward file: not offered
    expected = f"crystal_geometry_{values['elo']:g}-{values['ehi']:g}"
    write_cache(root, values, name=expected, Ehi=98.0)  # the suggestion's own name is taken
    client = TestClient(create_app(settings))
    query = {
        "geometry_file": str(place.geometry),
        "crystal_file": str(place.crystal),
        "elo": values["elo"],
        "ehi": values["ehi"],
        "max_nr_laue_spots": values["max_nr_laue_spots"],
    }
    answer = client.get("/api/v1/laue-matching/forward-caches", params=query).json()
    assert answer == {"caches": ["fits"], "suggested_name": f"{expected}_2", "problem": None}

    query["geometry_file"] = "/etc/hostname"
    refused = client.get("/api/v1/laue-matching/forward-caches", params=query).json()
    assert refused["caches"] == [] and refused["problem"]


def test_an_unset_data_folder_is_the_lists_problem_not_an_error(place):
    answer = (
        TestClient(create_app(place.settings))
        .get(
            "/api/v1/laue-matching/forward-caches",
            params={
                "geometry_file": str(place.geometry),
                "crystal_file": str(place.crystal),
                "elo": 5,
                "ehi": 30,
                "max_nr_laue_spots": 30,
            },
        )
        .json()
    )
    assert answer["caches"] == [] and "data_root" in answer["problem"]


# ---------------------------------------------------------------------------
# creating a run


def created(place: Place, tmp_path: Path) -> tuple[Any, int, dict[str, Any]]:
    settings, values, root = matching(place, tmp_path)
    write_cache(root, values, name="cache")
    form = place.form("laue_matching", scan_number=1, author="me")
    answer = submit("laue_matching", form, key="k", settings=settings, engine=place.engine)
    return settings, answer.run.id, values


def test_a_new_run_stores_its_parameters_and_freezes_the_instrument(place, tmp_path):
    settings, run_id, values = created(place, tmp_path)
    with Session(place.engine) as session:
        run = session.get(IndexingRun, run_id)
        assert run is not None and run.method == "laue_matching"
        assert run.lauego_parameters is None and run.laue_matching_parameters is not None
        assert run.laue_matching_parameters.forward_cache == "cache"
        job = session.get(Job, run.job_id)
        assert job is not None and job.backend == "local"
        document = json.loads(Path(job.request_path).read_text())
    assert document["kind"] == "laue_matching_indexing"
    assert document["run"]["method"] == "laue_matching"
    assert "xml" not in document["output"]
    request = document["request"]
    assert request["schema_id"] == "laue-matching/v1"
    assert request["p_array"] == list(values["p_array"])
    assert request["symmetry"] == "F"
    assert request["n_orientations"] == 2
    assert request["data_root"] == str(settings.laue_matching.data_root)


def test_the_list_the_detail_and_a_clone_show_a_laue_matching_run(place, tmp_path):
    settings, run_id, _ = created(place, tmp_path)
    client = TestClient(create_app(settings))
    rows = client.get("/api/v1/indexing-runs").json()["rows"]
    row = next(item for item in rows if item["id"] == run_id)
    assert row["method"] == "laue_matching"
    assert (row["n_points"], row["box_size"]) == (2, None)
    assert row["crystal_file"] == str(place.crystal)

    detail = client.get(f"/api/v1/indexing-runs/{run_id}").json()
    assert detail["parameters"] is None
    assert detail["laue_matching_parameters"]["forward_cache"] == "cache"

    clone = client.get("/api/v1/programs/laue_matching/prefill", params={"indexing": run_id}).json()
    assert clone["clone_of"]["id"] == run_id
    assert clone["values"]["forward_cache"] == "cache"
    assert clone["values"]["create_forward_cache"] is False
    assert "backend" not in clone["values"]
    # the LaueGo form does not clone it
    assert client.get("/api/v1/programs/lauego/prefill", params={"indexing": run_id}).status_code == 404
