"""Check actual lauelab shard outputs, not just launcher text or mocked writers.

The control directories come from ``tests.remote_support`` (the portal's run creation and plan), and the communicator is
a fake (mpi4py is installed only on the compute nodes). The four-node PBS layout (``laueportal.remote.pbs``) is checked
in ``tests/remote/test_bdp.py``. The golden tests run every rank of the MPI runner in its own fresh interpreter
(``remote_support.run_remote``) and compare with the committed goldens (``tests/fixtures/remote_goldens``).
"""

from __future__ import annotations

import copy
import json
import sys
import types
from collections import Counter
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
from xml.etree import ElementTree as ET

import h5py
import numpy as np
import pytest

from laueportal.jobs import mpi_runner, runner
from laueportal.jobs.contract import RunRequest
from laueportal.jobs.mpi_runner import distributed, partition
from laueportal.jobs.rundir import FailureRecord, ManifestEntry
from laueportal.science.merge import merge_reconstruction
from tests.remote_support import CASES, CASES_BY_NAME, Layout, assert_matches_goldens, prepare
from tests.run_support import make_engine

MPI_CASES = [case for case in CASES if case.mpi]


def _prepare(tmp_path: Path, name: str) -> dict:
    engine = make_engine(tmp_path / "portal")
    try:
        return prepare(CASES_BY_NAME[name], Layout(tmp_path), engine)
    finally:
        engine.dispose()


def test_full_point_local_index_partition_covers_each_depth_once():
    entries = [
        ManifestEntry(p * 121 + d, f"{p}_{d}", f"/points/{p}.h5", 0, p, d, point_id=str(p), depth_index=d)
        for p in range(825)
        for d in range(121)
    ]
    assignments = [
        partition(entries, kind="lauego_indexing", rank=rank, size=16, ranks_per_node=4) for rank in range(16)
    ]
    assert Counter(entry.index for subset in assignments for entry in subset) == Counter(range(825 * 121))
    for rank, subset in enumerate(assignments):
        assert all(int(entry.point_id) % 4 == rank // 4 and entry.depth_index % 4 == rank % 4 for entry in subset)
    assert max(map(len, assignments)) - min(map(len, assignments)) <= 238


def _run_shards(document, entries, request, size):
    summaries = []
    for rank in range(size):
        subset = entries[rank::size]
        shard_document = copy.deepcopy(request.document)
        shard_document["output"]["xml"] = "output.xml"
        shard = replace(
            request,
            run_directory=str(Path(request.run_directory).parent / "shards" / str(rank)),
            n_inputs=len(subset),
            document=shard_document,
        )
        summary = runner.run(document["control_directory"], request=shard, entries=subset)
        assert summary["status"] == "Finished", summary
        summaries.append(summary)
    return summaries


@pytest.mark.slow
def test_reconstruction_merge_keeps_global_point_order_links_and_indices(tmp_path):
    from lauelab.reconstruct import validate_scan_file

    document = _prepare(tmp_path, "crux-wire")
    request, entries = runner.load_request(document["control_directory"], document)
    # Reverse the shard completion order; the final catalog must use manifest order.
    summaries = list(reversed(_run_shards(document, entries, request, 2)))
    destination = Path(request.run_directory) / "reconstruction/scan.h5"
    merged = merge_reconstruction([s["artifacts"]["reconstruction"] for s in summaries], destination, entries)
    assert (merged.n_points, merged.n_complete) == (2, 2)
    with h5py.File(destination, "r") as catalog:
        assert list(catalog["points/point_ids"].asstr()[:]) == [entry.input_id for entry in entries]
        for entry, name in zip(entries, catalog["points/paths"].asstr()[:], strict=True):
            with h5py.File(destination.parent / name, "r") as point:
                assert point["entry1/reconstruction/manifest_index"][()] == entry.index
                assert np.prod(point["entry1/data/data"].shape) > 0
    # The complete directory is portable, with no links back into rank-private output.
    for path in destination.parent.rglob("*.h5"):
        assert not path.is_symlink()
    assert validate_scan_file(destination).n_complete == 2


@pytest.mark.slow
def test_indexing_merge_matches_single_writer_values_and_rebases_offsets(tmp_path, monkeypatch):
    from lauelab.indexing import InvalidResultsFile, merge_results_files, validate_results_file

    from tests.indexing.test_lauego_compute import CRYSTAL, GEOMETRY, _frames_dir, _parameters

    document = _prepare(tmp_path, "crux-wire")
    frames = _frames_dir(tmp_path)
    entries = [ManifestEntry(i, f"frame_{i + 1}", str(frames / f"frame_{i + 1}.h5"), 0, i + 1, None) for i in range(3)]
    request = RunRequest(
        document["job_id"],
        "lauego_indexing",
        "Indexing I1",
        str(tmp_path / "merged"),
        "unused",
        {
            "request": _parameters(geometry_file=str(GEOMETRY), crystal_file=str(CRYSTAL)),
            "output": {"xml": "output.xml"},
        },
        3,
        1,
        2,
    )
    summaries = _run_shards(document, entries, request, 2)
    assert [s["counters"]["n_succeeded"] for s in summaries] == [2, 1]
    destination = Path(request.run_directory) / "output.h5"
    destination.parent.mkdir()
    for rank, summary in enumerate(summaries):
        summary["host"] = f"compute-{rank}"
    comm = Mock()
    comm.Get_rank.return_value = 0
    comm.Get_size.return_value = 2
    comm.gather.return_value = summaries
    document["pbs"]["nodes"] = 2
    document["execution"]["indexing_ranks_per_node"] = 1
    with monkeypatch.context() as patch:
        # Replace transport/execution only: the gathered outputs are real
        # lauelab results and the MPI coordinator performs the actual merge.
        patch.setattr(runner, "run", lambda *args, **kwargs: summaries[0])
        outcome = distributed(document["control_directory"], request, entries, document, comm, Mock())
    assert outcome.n_succeeded == 3
    assert outcome.provenance["indexing_hdf5_merge_seconds"] >= 0
    assert outcome.provenance["indexing_xml_merge_seconds"] >= 0
    assert validate_results_file(destination).n_frames == 3
    assert len(ET.parse(destination.with_suffix(".xml")).getroot()) == 3
    serial = runner.run(
        document["control_directory"], request=replace(request, run_directory=str(tmp_path / "serial")), entries=entries
    )
    assert serial["status"] == "Finished"
    with h5py.File(destination, "r") as merged, h5py.File(serial["artifacts"]["results"], "r") as baseline:
        ids = list(merged["frames/frame_ids"].asstr()[:])
        assert ids == [entries[i].input_id for i in [0, 2, 1]]
        assert list(merged.attrs["merged_sources"]) == [s["artifacts"]["results"] for s in summaries]
        for i, frame_id in enumerate(baseline["frames/frame_ids"].asstr()[:]):
            j = ids.index(frame_id)
            for group, offsets in (("peaks", "frames/peak_offsets"), ("patterns", "frames/pattern_offsets")):
                a, b = baseline[offsets][i : i + 2]
                c, d = merged[offsets][j : j + 2]
                for column in baseline[group]:
                    if column.endswith("_offsets"):
                        continue
                    np.testing.assert_equal(baseline[group][column][a:b], merged[group][column][c:d])
            # Assignment rows reference frame-local peaks, so their indices must not be rebased.
            a, b = baseline["frames/pattern_offsets"][i : i + 2]
            c, d = merged["frames/pattern_offsets"][j : j + 2]
            x, y = baseline["patterns/assignment_offsets"][[a, b]] if a != b else (0, 0)
            u, v = merged["patterns/assignment_offsets"][[c, d]] if c != d else (0, 0)
            for column in baseline["assignments"]:
                np.testing.assert_equal(baseline["assignments"][column][x:y], merged["assignments"][column][u:v])
    with pytest.raises(InvalidResultsFile, match="identities"):
        merge_results_files(
            [summaries[0]["artifacts"]["results"]], tmp_path / "bad.h5", frame_ids=[e.input_id for e in entries]
        )
    assert not (tmp_path / "bad.h5").exists()


# --- Every rank in a fresh interpreter, against the goldens ----------------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize("case", MPI_CASES, ids=[case.name for case in MPI_CASES])
def test_the_mpi_runner_writes_the_goldens_without_the_database_or_the_settings(case, tmp_path):
    from lauelab.indexing import validate_results_file
    from lauelab.reconstruct import validate_scan_file

    plan = assert_matches_goldens(case, tmp_path)

    output = Path(plan["output_directory"])
    summary = json.loads((output / "run.json").read_text())
    assert summary["status"] == "Finished"
    assert summary["message"] == f"{case.count} inputs completed across {case.nodes} nodes ({case.size} MPI ranks)"
    provenance = summary["provenance"]
    assert (provenance["mpi_nodes"], provenance["mpi_ranks"]) == (case.nodes, case.size)
    ranks = json.loads((output / "ranks.json").read_text())
    assert [rank["host"] for rank in ranks] == provenance["rank_hosts"]
    assert sum(rank["counters"]["n_succeeded"] for rank in ranks) == case.count
    if case.kind == "lauego_indexing":
        assert validate_results_file(output / "output.h5").n_frames == case.count
    else:
        assert validate_scan_file(output / "reconstruction" / "scan.h5").n_complete == case.count


# --- the coordinator's decisions, with the shards' runs replaced --------------------------------------


def _shard(status="Finished", *, host="node-0", n_inputs=1, succeeded=1, failed=0, report=None):
    return {
        "status": status,
        "host": host,
        "counters": {"n_inputs": n_inputs, "n_succeeded": succeeded, "n_failed": failed, "n_indexed": None},
        "provenance": {"engine": "fake"},
        "artifacts": {},
        "warnings": [f"{host} warning"],
        "failure_report": report,
    }


def _comm(rank, size, gathered=None):
    comm = Mock()
    comm.Get_rank.return_value = rank
    comm.Get_size.return_value = size
    comm.gather.return_value = gathered
    return comm


def _wire_request(tmp_path):
    document = _prepare(tmp_path, "crux-wire")
    request, entries = runner.load_request(document["control_directory"], document)
    return document, request, entries


def test_partition_refuses_an_impossible_rank_layout():
    entries = [ManifestEntry(i, f"f{i}", f"/f{i}.h5", 0, i, None) for i in range(4)]
    for rank, size, per_node in ((0, 0, 1), (0, 3, 2), (2, 2, 1), (-1, 2, 1), (0, 2, 0)):
        with pytest.raises(ValueError, match="Invalid MPI rank layout"):
            partition(entries, kind="lauego_indexing", rank=rank, size=size, ranks_per_node=per_node)
    with pytest.raises(ValueError, match="one coordinator per node"):
        partition(entries, kind="wire_reconstruction", rank=0, size=2, ranks_per_node=2)
    assert partition(entries, kind="wire_reconstruction", rank=1, size=2, ranks_per_node=1) == entries[1::2]


def test_a_failed_rank_fails_the_run_and_its_records_reach_the_failure_report(tmp_path, monkeypatch):
    document, request, entries = _wire_request(tmp_path)
    document["pbs"]["nodes"] = 2
    shard_root = Path(request.run_directory).parent / "shards"
    (shard_root / "rank-0001").mkdir(parents=True)
    record = FailureRecord.not_run(entries[1], message="rank 1 lost its input")
    (shard_root / "rank-0001" / "failures.jsonl").write_text(record.to_json() + "\n")
    shards = [_shard(host="node-0"), _shard("Failed", host="node-1", succeeded=0, failed=1, report="failures.jsonl")]
    Path(request.run_directory).mkdir(parents=True)
    hooks = Mock()
    monkeypatch.setattr(runner, "run", lambda *args, **kwargs: shards[0])

    outcome = distributed(document["control_directory"], request, entries, document, _comm(0, 2, shards), hooks)

    assert (outcome.n_succeeded, outcome.n_failed, outcome.stopped) == (1, 1, False)
    assert outcome.validation_error == "At least one MPI rank failed; inspect ranks.json"
    assert outcome.warnings == ["node-0 warning", "node-1 warning"]
    assert outcome.provenance["rank_hosts"] == ["node-0", "node-1"]
    (relayed,) = [call.args[0] for call in hooks.record_failure.call_args_list]
    assert (relayed.index, relayed.message) == (1, "rank 1 lost its input")
    hooks.report_progress.assert_called_with(succeeded=1, failed=1)
    assert json.loads((Path(request.run_directory) / "ranks.json").read_text()) == shards
    assert not (Path(request.run_directory) / "reconstruction").exists()  # nothing merged


def test_a_cancelled_rank_cancels_the_run_without_merging(tmp_path, monkeypatch):
    document, request, entries = _wire_request(tmp_path)
    document["pbs"]["nodes"] = 2
    shards = [_shard(host="node-0"), _shard("Cancelled", host="node-1", succeeded=0)]
    Path(request.run_directory).mkdir(parents=True)
    monkeypatch.setattr(runner, "run", lambda *args, **kwargs: shards[0])

    outcome = distributed(document["control_directory"], request, entries, document, _comm(0, 2, shards), Mock())

    assert (outcome.stopped, outcome.validation_error, outcome.artifacts) == (True, None, {})


def test_the_ranks_must_match_the_frozen_pbs_request(tmp_path):
    document, request, entries = _wire_request(tmp_path)

    with pytest.raises(ValueError, match="MPI size disagrees"):
        distributed(document["control_directory"], request, entries, document, _comm(0, 2), Mock())


def test_coordinators_on_too_few_hosts_are_refused(tmp_path, monkeypatch):
    document, request, entries = _wire_request(tmp_path)
    document["pbs"]["nodes"] = 2
    shards = [_shard(host="node-0"), _shard(host="node-0")]
    Path(request.run_directory).mkdir(parents=True)
    monkeypatch.setattr(runner, "run", lambda *args, **kwargs: shards[0])

    with pytest.raises(ValueError, match="did not run on the requested number of nodes"):
        distributed(document["control_directory"], request, entries, document, _comm(0, 2, shards), Mock())


def test_a_rank_other_than_zero_only_contributes_its_shard(tmp_path, monkeypatch):
    document, request, entries = _wire_request(tmp_path)
    document["pbs"]["nodes"] = 2
    calls = []

    def shard_run(directory, *, request, entries, compute):
        calls.append((request.run_directory, [entry.index for entry in entries], request.document["output"]))
        return _shard(host="node-1")

    monkeypatch.setattr(runner, "run", shard_run)
    comm = _comm(1, 2)

    assert distributed(document["control_directory"], request, entries, document, comm, None) is None
    shard_output = {**request.output, "xml": "output.xml"}  # every rank has a private XML writer
    assert calls == [(str(Path(request.run_directory).parent / "shards" / "rank-0001"), [1], shard_output)]
    comm.gather.assert_called_once_with(_shard(host="node-1"), root=0)


def test_a_rank_whose_runner_raises_still_joins_the_gather(tmp_path, monkeypatch):
    document, request, entries = _wire_request(tmp_path)
    document["pbs"]["nodes"] = 2

    def refused(*args, **kwargs):
        raise FileExistsError("rank directory already used")

    monkeypatch.setattr(runner, "run", refused)
    comm = _comm(1, 2)

    distributed(document["control_directory"], request, entries, document, comm, None)

    (shard,), _ = comm.gather.call_args
    assert (shard["status"], shard["message"], shard["counters"]["n_inputs"]) == (
        "Failed",
        "rank directory already used",
        1,
    )


def test_main_refuses_to_run_outside_a_pbs_allocation(tmp_path, monkeypatch, capsys):
    document = _prepare(tmp_path, "crux-wire")
    comm = _comm(0, 1)

    class Aborted(Exception):
        pass

    comm.Abort.side_effect = Aborted
    monkeypatch.setitem(sys.modules, "mpi4py", types.SimpleNamespace(MPI=types.SimpleNamespace(COMM_WORLD=comm)))
    monkeypatch.delenv("PBS_JOBID", raising=False)
    monkeypatch.setattr("sys.argv", ["mpi_runner", document["control_directory"]])

    with pytest.raises(Aborted):
        mpi_runner.main()

    comm.Abort.assert_called_once_with(1)
    assert "The MPI runner requires a PBS allocation" in capsys.readouterr().err
    assert not Path(document["output_directory"]).exists()
