"""Estimate work from ranges/catalogs without detector reads or directory scans.

Typed inputs: each run is a ``RunSelection`` and a target's resources an ``ExecutionLayout``.
``test_the_estimates_match_the_goldens`` compares every case of ``tests/fixtures/estimate_goldens/`` with what the
estimates compute.
"""

import dataclasses
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from laueportal.remote import estimates
from laueportal.remote.estimates import ExecutionLayout, RunSelection
from laueportal.remote.settings import available_backends, parse_settings, require_backend
from tests.remote_support import Layout, bdp_settings
from tests.settings_support import make_settings

APS = ExecutionLayout(workers=32, max_in_flight=256, reconstruction_workers=4, threads_per_worker=8)
CRUX = ExecutionLayout(workers=32, max_in_flight=256, reconstruction_workers=32, threads_per_worker=4)
GOLDENS = Path(__file__).resolve().parents[1] / "fixtures" / "estimate_goldens" / "estimates.json"
PINNED_PROFILES_SHA256 = "cf4712a2f67cca0a27637bf0610b51babd107957769533ef77699284aaebecfb"


@pytest.fixture
def settings(tmp_path):
    # The selections name /not-mounted/raw (in a data root not mounted); only a catalog is read, inside the roots.
    return make_settings(tmp_path, files={"roots": {"data": "/not-mounted", "tmp": tmp_path}})


def selection(**overrides):
    values = {
        "input_path": "/not-mounted/raw",
        "filename_prefixes": ["wire_%d.h5"],
        "scan_points": "1-825",
        "depth_start": -30,
        "depth_end": 90,
        "depth_resolution": 1,
        "frame_skip": 1,
        "wire_skip": 1,
        **overrides,
    }
    return RunSelection(**values)


def estimate(kind, backend, runs, layout, settings):
    return estimates.estimate_runtime(kind, backend, runs, layout=layout, settings=settings)


def test_one_aps_option_switches_transport_only_after_dm_is_enabled():
    values = {"enabled": True, "targets": {"aps": {"enabled": False}, "crux": {"enabled": True}}}
    remote = parse_settings(values)
    assert [option.model_dump() for option in available_backends(remote)] == [
        {"label": "APS", "value": "local"},
        {"label": "ALCF", "value": "crux"},
    ]
    require_backend("local", remote)
    with pytest.raises(ValueError):
        require_backend("aps", remote)
    values["targets"]["aps"]["enabled"] = True
    assert [option.model_dump() for option in available_backends(parse_settings(values))] == [
        {"label": "APS", "value": "aps"},
        {"label": "ALCF", "value": "crux"},
    ]


def test_large_directory_selection_neither_expands_ranges_nor_scans_files(monkeypatch, settings):
    def forbidden(*args, **kwargs):
        pytest.fail("Estimate enumerated inputs instead of counting the selected ranges")

    monkeypatch.setattr(os, "listdir", forbidden)
    monkeypatch.setattr(os, "scandir", forbidden)
    monkeypatch.setattr(estimates.srange, "list", forbidden)
    count = estimates.count_selection(
        "indexing",
        selection(scan_points="1-100000000", depth_range="0-120", filename_prefixes=["wire_%d_%d.h5"]),
        settings=settings,
    )
    assert (count.points, count.frames) == (100_000_000, 12_100_000_000)


def test_reconstruction_825_point_estimates_match_recorded_single_node_profiles(settings):
    local = estimate("reconstruction", "local", [selection()], APS, settings)
    remote = estimate("reconstruction", "crux", [selection()], CRUX, settings)
    assert (local.selection.points, local.selection.frames) == (825, 99825)
    assert 146 < local.seconds / 60 < 148
    assert 32 < remote.seconds / 60 < 34
    half = estimate("reconstruction", "local", [selection(scan_points="1-400")], APS, settings)
    assert half.seconds == pytest.approx(local.seconds * 400 / 825)
    tiny = estimate("reconstruction", "crux", [selection(scan_points="1")], CRUX, settings)
    assert tiny.seconds > 60  # A single point still needs one worker wave, not 1/32 of one.


def test_indexing_uses_the_current_single_pool_and_handles_pooled_runs(settings):
    fields = selection(filename_prefixes=["wire_%d_%d.h5"], depth_range="0-120")
    result = estimate("indexing", "crux", [fields], CRUX, settings)
    assert 88 < result.seconds / 60 < 89
    assert result.profile["workers"] == 32
    two = estimate("indexing", "crux", [fields, fields], CRUX, settings)
    assert two.runs == 2 and two.selection.frames == 2 * 99825
    assert two.seconds == pytest.approx(2 * result.seconds)
    # One selection per run, so the run counts of pooled fields cannot mismatch ("SN1;SN2" with "a;b;c").
    # What remains of pooling is its limit, and a batch needs at least one run.
    assert estimate("indexing", "crux", [fields] * estimates.MAX_RUNS, CRUX, settings).runs == 100
    with pytest.raises(ValueError, match="at most 100"):
        estimate("indexing", "crux", [fields] * (estimates.MAX_RUNS + 1), CRUX, settings)
    with pytest.raises(ValueError, match="Add a run"):
        estimate("indexing", "crux", [], CRUX, settings)


def test_four_node_estimates_use_the_measured_full_scan_layout(settings):
    layout = dataclasses.replace(CRUX, nodes=4, indexing_ranks_per_node=4)
    recon = estimate("reconstruction", "crux", [selection()], layout, settings)
    index = estimate(
        "indexing", "crux", [selection(filename_prefixes=["wire_%d_%d.h5"], depth_range="0-120")], layout, settings
    )
    assert recon.seconds == pytest.approx(469.987, abs=0.001)
    assert index.seconds == pytest.approx(301.769, abs=0.001)
    assert recon.profile["nodes"] == index.profile["nodes"] == 4
    assert index.profile["ranks_per_node"] == 4


def test_bidirectional_transfer_projection_uses_the_confirmed_rate(settings):
    reconstruction = estimate("reconstruction", "crux", [selection()], CRUX, settings)
    transfer = estimates.estimate_transfers("reconstruction", reconstruction.selection)
    assert transfer.input_bytes == pytest.approx(3.53e12)
    assert transfer.output_bytes == pytest.approx(1e12)
    assert transfer.input_seconds == pytest.approx(771)
    assert transfer.seconds / 60 == pytest.approx(16.4902266)
    indexing = estimate(
        "indexing", "crux", [selection(filename_prefixes=["wire_%d_%d.h5"], depth_range="0-120")], CRUX, settings
    )
    transfer = estimates.estimate_transfers("indexing", indexing.selection)
    assert transfer.input_bytes == pytest.approx(1e12)
    assert 0 < transfer.output_bytes < 2e8
    assert transfer.output_seconds < 1


@pytest.mark.slow  # native lauelab reconstruction (the catalog)
def test_catalog_counts_depths_without_opening_point_files_and_reuses_snapshot(tmp_path, monkeypatch, settings):
    from lauelab.reconstruct import ScanReader, reconstruct_scan

    from tests.wire_support import GEOMETRY, write_wire_scan

    source = write_wire_scan(tmp_path / "wire_1.h5")
    output = tmp_path / "reconstruction"
    reconstruct_scan(
        [source],
        output,
        geometry=GEOMETRY,
        detector=0,
        depth_range=(-25, 25),
        resolution=5,
        percent_brightest=100,
        threads_per_worker=1,
        workers=1,
    )
    fields = selection(input_path=str(output / "scan.h5"), scan_points="1", depth_range=None)
    snapshots = []
    original = ScanReader.__init__

    def init(reader, path):
        snapshots.append(path)
        original(reader, path)

    monkeypatch.setattr(ScanReader, "__init__", init)

    def no_point_reads(*args, **kwargs):
        pytest.fail("Counting must not open a reconstructed point's pixels")

    monkeypatch.setattr(ScanReader, "point", no_point_reads)
    estimates._catalog_points.cache_clear()
    monkeypatch.setattr(estimates.time, "monotonic", lambda: 100)
    first = estimates.count_selection("indexing", fields, settings=settings)
    second = estimates.count_selection("indexing", dataclasses.replace(fields, depth_range="0,3,10"), settings=settings)
    assert first.points == 1 and first.frames == 11
    assert second.frames == 3
    assert first.staged_frames == second.staged_frames == 11
    first_transfer = estimates.estimate_transfers("indexing", first)
    subset_transfer = estimates.estimate_transfers("indexing", second)
    assert subset_transfer.input_bytes == first_transfer.input_bytes
    assert subset_transfer.output_bytes == pytest.approx(first_transfer.output_bytes * 3 / 11)
    assert len(snapshots) == 1
    with pytest.raises(ValueError, match="exceed"):
        estimates.count_selection("indexing", dataclasses.replace(fields, depth_range="0-11"), settings=settings)


@pytest.mark.parametrize(
    "values",
    [
        {"scan_points": "1-5:0"},
        {"depth_resolution": 0},
        {"depth_start": float("nan")},
        {"filename_prefixes": ["*.h5"]},
    ],
)
def test_invalid_or_unknown_selections_do_not_invent_a_count(values, settings):
    with pytest.raises(ValueError):
        estimates.count_selection("reconstruction", selection(**values), settings=settings)


# ---------------------------------------------------------------------------
# The goldens, the profiles file, the allowed roots, the layouts and the per-target estimates.


def _goldens() -> dict[str, Any]:
    return json.loads(GOLDENS.read_text(encoding="utf-8"))


def _document(value: Any) -> Any:
    """A result as the goldens hold it (JSON: tuples become lists; floats keep every bit)."""
    return json.loads(json.dumps(dataclasses.asdict(value) if dataclasses.is_dataclass(value) else value))


def _outcome(call) -> Any:
    try:
        return call()
    except ValueError as error:
        return {"error": str(error), "type": type(error).__name__}


@pytest.mark.parametrize("case", _goldens()["cases"], ids=lambda case: case["name"])
def test_the_estimates_match_the_goldens(case, settings):
    runs = [RunSelection(**row) for row in case["rows"]] * case["repeat"]
    layout = ExecutionLayout(**case["layout"])
    if case["repeat"] == 1:
        counts = [
            _outcome(lambda run=run: _document(estimates.count_selection(case["kind"], run, settings=settings)))
            for run in runs
        ]
        assert counts == case["counts"]
    result = _outcome(lambda: estimate(case["kind"], case["backend"], runs, layout, settings))
    if case["transfer"] is None:
        assert result == case["estimate"]
        return
    assert isinstance(result, estimates.RuntimeEstimate)
    assert {
        "seconds": result.seconds,
        "runs": result.runs,
        "profile": result.profile,
        "notes": list(result.notes),
        "selection": _document(result.selection),
    } == case["estimate"]
    transfer = estimates.estimate_transfers(case["kind"], result.selection)
    assert {
        **_document(transfer),
        "input_seconds": transfer.input_seconds,
        "output_seconds": transfer.output_seconds,
        "seconds": transfer.seconds,
    } == case["transfer"]
    assert estimates.format_duration(result.seconds) == case["duration"]


def test_durations_read_as_the_goldens_say():
    for seconds, text in _goldens()["durations"]:
        assert estimates.format_duration(seconds) == text


def test_the_measured_profiles_are_the_pinned_file():
    path = Path(estimates.__file__).with_name("runtime_profiles.json")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == PINNED_PROFILES_SHA256
    assert estimates.measured_profiles() == json.loads(path.read_text(encoding="utf-8"))


def test_a_catalog_outside_the_allowed_roots_is_never_read(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, files={"roots": {"data": tmp_path / "data"}})
    outside = tmp_path / "elsewhere" / "scan.h5"
    outside.parent.mkdir()
    outside.write_bytes(b"not read")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "scan.h5").symlink_to(outside)
    reads = []
    monkeypatch.setattr(estimates, "_catalog_points", lambda path, window: reads.append(path))
    for path in (outside, tmp_path / "data" / "scan.h5"):  # outside, and a link from inside to outside
        with pytest.raises(ValueError, match="outside the allowed roots"):
            estimates.count_selection("indexing", selection(input_path=str(path)), settings=settings)
    with pytest.raises(ValueError, match="not an absolute path"):
        estimates.count_selection("indexing", selection(input_path="data/scan.h5"), settings=settings)
    assert reads == []
    # A folder is never read, so it is counted wherever it is (validation checks the folder itself).
    count = estimates.count_selection(
        "indexing", selection(input_path="/elsewhere", filename_prefixes=["a_%d.h5"]), settings=settings
    )
    assert (count.points, count.frames) == (825, 825)


def test_a_catalog_inside_the_roots_is_read_at_its_resolved_path(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, files={"roots": {"data": tmp_path / "data"}})
    (tmp_path / "data" / "real").mkdir(parents=True)
    (tmp_path / "data" / "link").symlink_to(tmp_path / "data" / "real")
    reads = []
    monkeypatch.setattr(estimates, "_catalog_points", lambda path, window: reads.append(path))
    count = estimates.count_selection(
        "indexing", selection(input_path=str(tmp_path / "data" / "link" / "points.h5")), settings=settings
    )
    # Not a catalog (None): counted as a folder.
    assert (count.points, count.frames, count.basis) == (825, 825, "Selected indices")
    assert reads == [os.path.realpath(tmp_path / "data" / "real" / "points.h5")]


def _remote(**targets: dict[str, Any]):
    values = bdp_settings(Layout(Path("/base")))
    for name, overrides in targets.items():
        target = values["targets"][name]
        for key, value in overrides.items():
            if isinstance(value, dict):
                target[key] = {**target.get(key, {}), **value}
            else:
                target[key] = value
    return parse_settings(values)


def test_the_local_layout_is_the_portals_execution_settings(tmp_path):
    settings = make_settings(
        tmp_path,
        execution={"workers": 4, "reconstruction_workers": 2, "num_threads": 35, "max_in_flight": 9},
    )
    layout = estimates.execution_layout("local", settings, None)
    assert layout == ExecutionLayout(workers=4, reconstruction_workers=2, threads_per_worker=35, max_in_flight=9)
    assert estimates.execution_layout("local", make_settings(tmp_path), None) == ExecutionLayout()


def test_a_remote_layout_is_the_targets_execution_and_pbs_nodes(tmp_path):
    settings = make_settings(tmp_path)
    remote = _remote(
        crux={
            "pbs": {"nodes": "4"},
            "execution": {
                "workers": 32,
                "max_in_flight": 256,
                "reconstruction_workers": 32,
                "threads_per_worker": 4.0,
                "indexing_ranks_per_node": "4",
            },
        },
        aps={
            "execution": {"workers": None, "max_in_flight": None, "reconstruction_workers": 4, "threads_per_worker": 8}
        },
    )
    assert estimates.execution_layout("crux", settings, remote) == ExecutionLayout(
        workers=32,
        max_in_flight=256,
        reconstruction_workers=32,
        threads_per_worker=4,
        nodes=4,
        indexing_ranks_per_node=4,
    )
    # No workers: one (int(workers or 1)); max_in_flight left unset (twice the workers).
    assert estimates.execution_layout("aps", settings, remote) == ExecutionLayout(
        workers=1, reconstruction_workers=4, threads_per_worker=8
    )
    with pytest.raises(ValueError, match="not enabled"):
        estimates.execution_layout("crux", settings, None)
    with pytest.raises(ValueError, match="nodes"):
        estimates.execution_layout("crux", settings, _remote(crux={"pbs": {"nodes": 0}}))
    with pytest.raises(ValueError, match="indexing_ranks_per_node"):
        ExecutionLayout(indexing_ranks_per_node=0)


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("01:30:00", 5400), ("00:10:00", 600), ("10:00", 600), ("3600", 3600), (7200, 7200), (None, None), ("1h", None)],
)
def test_walltimes_are_read_in_pbs_form(value, seconds):
    assert estimates.walltime_seconds(value) == seconds


def test_without_remote_execution_the_one_target_is_local(tmp_path):
    settings = make_settings(tmp_path, execution={"workers": 4, "reconstruction_workers": 1, "num_threads": 8})
    (result,) = estimates.estimate_targets("reconstruction", [selection()], settings=settings, remote=None)
    assert (result.option.label, result.option.value) == ("APS", "local")
    assert result.layout == ExecutionLayout(workers=4, reconstruction_workers=1, threads_per_worker=8)
    expected = estimate("reconstruction", "local", [selection()], result.layout, settings)
    assert result.estimate == expected and result.seconds == expected.seconds
    assert result.transfer is None and result.walltime_seconds is None and not result.exceeds_walltime


def test_each_enabled_target_is_estimated_and_alcf_includes_the_round_trip(tmp_path):
    settings = make_settings(tmp_path)
    remote = _remote(
        crux={"pbs": {"walltime": "00:40:00"}, "execution": {"reconstruction_workers": 32, "threads_per_worker": 4}},
        aps={"pbs": {"walltime": "00:10:00"}, "execution": {"reconstruction_workers": 4, "threads_per_worker": 8}},
    )
    aps, crux = estimates.estimate_targets("reconstruction", [selection()], settings=settings, remote=remote)
    assert [(target.option.label, target.option.value) for target in (aps, crux)] == [("APS", "aps"), ("ALCF", "crux")]
    assert aps.transfer is None and aps.seconds == aps.estimate.seconds
    assert crux.estimate == estimate("reconstruction", "crux", [selection()], crux.layout, settings)
    assert crux.transfer == estimates.estimate_transfers("reconstruction", crux.estimate.selection)
    assert crux.seconds == crux.estimate.seconds + crux.transfer.seconds
    # The walltime is compared with the processing time alone.
    assert (aps.walltime_seconds, crux.walltime_seconds) == (600, 2400)
    assert aps.estimate.seconds > 600 and aps.exceeds_walltime
    assert crux.estimate.seconds < 2400 < crux.seconds and not crux.exceeds_walltime


def test_a_target_that_cannot_be_estimated_says_why(tmp_path):
    settings = make_settings(tmp_path)
    runs = [selection(depth_range="0-9")]
    aps, crux = estimates.estimate_targets("indexing", runs, settings=settings, remote=_remote())
    assert aps.error == crux.error == "For directory inputs, depth indices need a second filename placeholder."
    assert aps.estimate is None and aps.seconds is None and not aps.exceeds_walltime
    assert crux.walltime_seconds == 600  # the walltime is known even when the estimate is not
    aps, crux = estimates.estimate_targets(
        "indexing", [selection()], settings=settings, remote=_remote(crux={"pbs": {"nodes": 0}})
    )
    assert aps.error is None and aps.estimate is not None
    assert crux.error == "The execution layout needs at least one of nodes." and crux.layout is None
