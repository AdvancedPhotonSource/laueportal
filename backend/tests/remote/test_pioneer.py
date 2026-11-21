"""LaueMatching on the ``pioneer`` target (the APS cluster's GPU queue): the plan's checks and path map, the PBS
script's resources, the standalone runner with the GPU stream daemon (a stand-in matching with the CPU stand-in),
the launcher's observation, and the publication of the returned run."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

import h5py
import pytest
import yaml

pytest.importorskip("laue_index")

from laueportal.indexing import service as indexing_service  # noqa: E402
from laueportal.indexing.laue_matching import compute as laue_compute  # noqa: E402
from laueportal.indexing.laue_matching.params import LaueMatchingParamsV1  # noqa: E402
from laueportal.jobs import runner  # noqa: E402
from laueportal.jobs.backends import transport  # noqa: E402
from laueportal.remote import plan, poller, stage  # noqa: E402
from laueportal.remote.pbs import build_script  # noqa: E402
from laueportal.remote.settings import RemoteSettingsError  # noqa: E402
from tests.indexing.test_laue_matching import NAME, parameters, stand_in, write_cache, write_frame  # noqa: E402
from tests.laue_matching_stream_support import fake_daemon, on_gpu  # noqa: E402
from tests.remote_support import (  # noqa: E402
    SUBMITTED_AT,
    Layout,
    _copy_config,
    _metadata,
    bdp_settings,
    job_view,
    remote_settings,
)
from tests.run_support import make_engine  # noqa: E402
from tests.settings_support import make_settings  # noqa: E402

SUBMISSION = "00000000-0000-4000-8000-0000000000aa"


def setup(tmp_path: Path, *, count: int = 2, outside: bool = False, target: dict[str, Any] | None = None):
    """A published LaueMatching run of ``count`` frames on ``pioneer``, the portal's and the target's data folders
    holding the same cache, and the remote settings."""
    layout = Layout(tmp_path)
    document = bdp_settings(layout)
    document["targets"]["pioneer"].update(target or {})
    settings_file = tmp_path / "bdp.yaml"
    settings_file.write_text(yaml.safe_dump(document), encoding="utf-8")
    portal_root = tmp_path / "portal-data"
    target_root = Path(document["targets"]["pioneer"]["data_root"])
    for root in (portal_root, target_root):
        write_cache(root, parameters(root))
    settings = make_settings(
        tmp_path,
        files={"runs_root": layout.experiment, "roots": {"data": tmp_path}},
        remote={"enabled": True, "settings_file": settings_file},
        laue_matching={"data_root": portal_root},
    )
    engine = make_engine(tmp_path)
    _metadata(engine)
    geometry, crystal = _copy_config(layout)
    if outside:  # inside the roots, outside the experiment folder
        geometry = Path(shutil.copy(geometry, tmp_path / "geometry.xml"))
    frames = layout.data / "lm_frames"
    frames.mkdir(parents=True)
    for index in range(1, count + 1):
        write_frame(frames / f"frame_{index}.h5")
    values = {
        **{key: value for key, value in parameters(portal_root).items() if key not in _FROZEN},
        "backend": "pioneer",
        "scan_number": 1,
        "reconstruction_id": None,
        "input_path": os.fspath(frames),
        "output_path_template": os.fspath(layout.analysis / "lm_%d"),
        "filename_prefixes": ["frame_%d.h5"],
        "scan_points": f"1-{count}" if count > 1 else "1",
        "depth_range": None,
        "geometry_file": os.fspath(geometry),
        "crystal_file": os.fspath(crystal),
        "forward_cache": NAME,
        "author": "test",
        "computer_name": "test-host",
        "submitted_at": SUBMITTED_AT,
    }
    run = indexing_service.create_laue_matching(LaueMatchingParamsV1(**values), engine=engine, settings=settings)
    return layout, engine, remote_settings(document), run


_FROZEN = ("space_group", "symmetry", "lattice_parameters", "p_array", "r_array", "px_x", "px_y", "nr_px_x", "nr_px_y")
_FROZEN += ("detector_id", "n_orientations")


def test_a_gpu_plan_maps_the_data_folder_and_asks_pbs_for_one_gpu(tmp_path):
    layout, engine, settings, run = setup(tmp_path)
    view = job_view(engine, run.job_id)

    built = plan.build_plan(view, settings, SUBMISSION)

    assert built["kind"] == "laue_matching_indexing" and built["backend"] == "pioneer"
    portal_root = str(tmp_path / "portal-data")
    assert built["path_map"][portal_root] == settings.targets.pioneer.data_root
    assert built["execution"]["laue_matching_indexer"] == "stream"
    script = build_script(built)
    assert "#PBS -q gpu\n" in script
    assert "#PBS -l select=1:ncpus=4:mem=64gb:ngpus=1\n" in script
    assert "mpiexec" not in script
    assert transport("pioneer") == transport("aps")
    with pytest.raises(ValueError, match="Unknown remote execution target"):
        transport("unknown-target")


def test_a_gpu_plan_is_refused_without_the_installed_cache(tmp_path):
    layout, engine, settings, run = setup(tmp_path)
    folder = Path(settings.targets.pioneer.data_root) / NAME
    (folder / "forward.bin.meta.json").unlink()

    with pytest.raises(RemoteSettingsError, match=f"Forward cache {NAME} is not installed"):
        plan.build_plan(job_view(engine, run.job_id), settings, SUBMISSION)


def test_a_gpu_plan_is_refused_for_files_outside_the_experiment_folder(tmp_path):
    layout, engine, settings, run = setup(tmp_path, outside=True)

    with pytest.raises(RemoteSettingsError, match="geometry_file must be inside the experiment folder"):
        plan.build_plan(job_view(engine, run.job_id), settings, SUBMISSION)


def test_a_soft_deadline_must_leave_time_for_the_merge(tmp_path):
    layout, engine, settings, run = setup(
        tmp_path, target={"execution": {**_execution(), "soft_deadline_seconds": 3500}}
    )

    with pytest.raises(RemoteSettingsError, match="soft_deadline_seconds must leave 300 s"):
        plan.build_plan(job_view(engine, run.job_id), settings, SUBMISSION)


def _execution() -> dict[str, Any]:
    return dict(bdp_settings(Layout(Path("/x")))["targets"]["pioneer"]["execution"])


def test_the_runner_indexes_on_the_daemon_and_the_poller_publishes_the_run(tmp_path, monkeypatch):
    layout, engine, settings, run = setup(tmp_path)
    view = job_view(engine, run.job_id)
    built = plan.save_plan(view, settings, SUBMISSION)
    daemon = fake_daemon(tmp_path, stand_in(tmp_path))
    monkeypatch.delenv("LAUEMATCHING_BIN", raising=False)
    monkeypatch.setattr(laue_compute, "stream_binary", lambda require: daemon)
    monkeypatch.setattr(laue_compute, "gpu_evidence", on_gpu)

    summary = runner.run(built["control_directory"])

    assert summary["status"] == "Finished", summary["message"]
    assert summary["counters"]["n_succeeded"] == 2
    assert summary["provenance"]["binary"].endswith("LaueMatchingGPUStream")
    assert [segment["n_frames"] for segment in summary["provenance"]["segments"]] == [2]
    incoming = Path(built["output_directory"])
    params = (incoming / "params.txt").read_text()
    assert f"ForwardFile {settings.targets.pioneer.data_root}/{NAME}/forward.bin" in params
    assert not (incoming / "segments").exists()

    poller.publish_returned(built)

    directory = Path(built["run_directory"])
    assert json.loads((directory / "run.json").read_text())["status"] == "Finished"
    with h5py.File(directory / "output.h5") as results:
        assert results["frames/frame_ids"].shape == (2,)
    assert (directory / "laue_matching.h5").is_file()


def test_the_launcher_observes_a_gpu_run_from_pbs_and_the_runners_progress(tmp_path):
    output = tmp_path / "incoming"
    output.mkdir()
    built = {"submission_id": SUBMISSION, "output_directory": str(output), "n_inputs": 40}
    data: dict[str, Any] = {"pbs_id": "1234.server"}

    queued = stage.pbs_observation(built, data, "Q")
    assert (queued["phase"], queued["detail"], queued["pbs"]["job_id"]) == (
        "queued",
        "Waiting for PBS allocation",
        "1234.server",
    )
    data["observation"] = queued
    (output / "progress.json").write_text(
        json.dumps({"heartbeat_at": stage._time_text(stage._now()), "n_succeeded": 11, "n_failed": 1})
    )
    running = stage.pbs_observation(built, data, "R")
    assert (running["phase"], running["detail"], running["stale"]) == ("running", "Processing: 12 of 40 frames", False)
    assert running["submission_id"] == SUBMISSION


class FakePBS:
    """``subprocess.run`` for the launcher's PBS commands: ``qstat`` answers ``state``; each command is kept."""

    def __init__(self, state: str) -> None:
        self.state = state
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        import subprocess

        self.commands.append(command)
        record = {"Jobs": {"1234.server": {"job_state": self.state, "Exit_status": 0}}}
        return subprocess.CompletedProcess(command, 0, json.dumps(record), "")

    def qdels(self) -> int:
        return sum(1 for command in self.commands if command[0].endswith("qdel"))


def _launcher(tmp_path: Path, monkeypatch, state: str):
    from types import SimpleNamespace

    control = tmp_path / "run"
    (control / "incoming").mkdir(parents=True)
    built = {
        "submission_id": SUBMISSION,
        "backend": "pioneer",
        "control_directory": str(control),
        "output_directory": str(control / "incoming"),
        "n_inputs": 4,
        "target": {"submit_host": None},
        "execution": {"segment_seconds": 300},
    }
    states = tmp_path / "state"
    states.mkdir()
    (states / f"{SUBMISSION}.json").write_text(json.dumps({"status": "RUNNING", "pbs_id": "1234.server"}))
    fake = FakePBS(state)
    monkeypatch.setattr(stage, "_pbs_run", fake)  # never the process-wide subprocess.run: other tests' threads use it
    return built, SimpleNamespace(state_directory=str(states)), fake, control


def test_a_stop_lets_a_running_gpu_job_drain_and_deletes_a_queued_one(tmp_path, monkeypatch):
    built, settings, fake, _ = _launcher(tmp_path, monkeypatch, "R")
    assert stage.cancel(built, settings)["status"] == "CANCEL_REQUESTED"
    assert fake.qdels() == 0  # the runner reads cancel.request and drains

    fake.state = "Q"
    stage.cancel(built, settings)
    assert fake.qdels() == 1


def test_the_monitor_deletes_a_gpu_job_still_running_long_after_its_stop(tmp_path, monkeypatch):
    built, settings, fake, control = _launcher(tmp_path, monkeypatch, "R")
    request = control / "cancel.request"
    request.write_text("{}")

    stage.monitor(built, settings)
    assert fake.qdels() == 0  # within the grace

    old = time.time() - stage.STOP_GRACE_SECONDS - 1
    os.utime(request, (old, old))
    stage.monitor(built, settings)
    stage.monitor(built, settings)
    assert fake.qdels() == 1  # once


@pytest.mark.parametrize(
    ("change", "missing"),
    [
        ({"execution": {**_execution(), "laue_matching_indexer": "cpu"}}, "laue_matching_indexer (stream)"),
        ({"pbs": {"queue": "gpu", "walltime": "01:00:00", "cpus": 4}}, "pbs.ngpus (at least 1)"),
    ],
)
def test_a_gpu_target_without_a_gpu_or_the_stream_indexer_is_refused(tmp_path, change, missing):
    layout, engine, settings, run = setup(tmp_path, target=change)

    with pytest.raises(RemoteSettingsError, match=re.escape(missing)):
        plan.build_plan(job_view(engine, run.job_id), settings, SUBMISSION)


def test_an_input_that_leads_out_of_the_experiment_folder_is_refused(tmp_path):
    layout, engine, settings, run = setup(tmp_path)
    frame = layout.data / "lm_frames" / "frame_2.h5"
    outside = Path(shutil.move(frame, tmp_path / "outside.h5"))
    frame.symlink_to(outside)

    with pytest.raises(RemoteSettingsError, match="must be inside the experiment folder"):
        plan.build_plan(job_view(engine, run.job_id), settings, SUBMISSION)


def test_a_run_larger_than_one_daemon_takes_is_refused(tmp_path, monkeypatch):
    layout, engine, settings, run = setup(tmp_path)
    monkeypatch.setattr(plan, "MAX_STREAM_IMAGES", 1)

    with pytest.raises(RemoteSettingsError, match="at most 1 inputs; this one has 2"):
        plan.build_plan(job_view(engine, run.job_id), settings, SUBMISSION)


def test_the_stop_grace_counts_from_the_stops_own_time_not_the_files(tmp_path):
    request = tmp_path / "cancel.request"
    built = {"control_directory": str(tmp_path)}
    request.write_text(json.dumps({"requested_at": "2026-11-20 10:00:00+00:00"}))  # rewritten now, requested long ago
    assert stage._stop_overdue(built)
    request.write_text(json.dumps({"requested_at": str(stage._now())}))
    assert not stage._stop_overdue(built)
