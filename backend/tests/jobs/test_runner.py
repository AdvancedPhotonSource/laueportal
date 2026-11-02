"""The standalone runner (``jobs.runner``) on the compute side.

Slow tests run it in a fresh interpreter, as the PBS script does, on control directories built the way the deployed
format has them (the portal's run creation and plan, the launcher's staging), with an import hook that fails on the
database, the settings, SQLAlchemy, Redis or RQ. Every file it writes must match the committed goldens on the same
control directories (``tests/fixtures/remote_goldens/``). Fast tests cover the request loading and the runner's control
flow with fake compute functions, in process.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from laueportal.jobs import runner
from laueportal.jobs.contract import RunOutcome
from laueportal.jobs.rundir import FailureRecord, ManifestError, read_failure_report, read_manifest, read_request
from tests.remote_support import (
    CASES,
    CASES_BY_NAME,
    SUBMISSION_ID,
    V2_BLOCKED,
    Layout,
    assert_matches_goldens,
    case_settings,
    prepare,
    save_plan,
)
from tests.run_support import make_engine
from tests.settings_support import fresh_interpreter_env

SERIAL_CASES = [case for case in CASES if not case.mpi]


def _prepare(tmp_path: Path, name: str, **changes) -> dict:
    """``name``'s control directory under ``tmp_path``, with ``changes`` to the target's execution policy."""

    case = CASES_BY_NAME[name]
    layout = Layout(tmp_path)
    engine = make_engine(tmp_path / "portal")
    try:
        if not changes:
            return prepare(case, layout, engine)

        def writer(job, settings, submission_id):
            settings = case_settings(case, layout)
            settings["targets"][case.backend]["execution"].update(changes)
            return save_plan(job, settings, submission_id)

        return prepare(case, layout, engine, plan_writer=writer)
    finally:
        engine.dispose()


def _summary(plan: dict) -> dict:
    return json.loads((Path(plan["output_directory"]) / "run.json").read_text())


# --- the deployed format, against the goldens -----------------------------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize("case", SERIAL_CASES, ids=[case.name for case in SERIAL_CASES])
def test_the_runner_writes_the_goldens_without_the_database_or_the_settings(case, tmp_path):
    plan = assert_matches_goldens(case, tmp_path)

    summary = _summary(plan)
    assert summary["submission_id"] == SUBMISSION_ID
    if case.cancel_before_start:
        assert (summary["status"], summary["counters"]["n_not_run"]) == ("Cancelled", case.count)
    elif case.corrupt:
        assert (summary["status"], summary["counters"]["n_failed"]) == ("Failed", 1)
    else:
        assert summary["status"] == "Finished"


RUN_WITH_HOOK = textwrap.dedent(
    """
    import json
    import os
    import signal
    import sys
    from pathlib import Path

    from laueportal.jobs import runner
    from laueportal.jobs.contract import get_compute_function


    def main():
        control, action = sys.argv[1], sys.argv[2]

        def compute(request, entries, hooks):
            class Hooks:
                # The real hooks, plus an event once the first input completed.
                def __getattr__(self, name):
                    return getattr(hooks, name)

                def report_progress(self, *, succeeded, failed):
                    hooks.report_progress(succeeded=succeeded, failed=failed)
                    if succeeded + failed >= 1:
                        if action == "cancel":
                            Path(control, "cancel.request").touch()
                        else:
                            os.kill(os.getpid(), signal.SIGTERM)

            return get_compute_function(request.kind)(request, entries, Hooks())

        summary = runner.run(control, compute=compute)
        print(json.dumps({"status": summary["status"], "modules": sorted(sys.modules)}))


    if __name__ == "__main__":
        main()
    """
)


def _run_with_hook(tmp_path: Path, plan: dict, action: str) -> dict:
    script = tmp_path / "run_with_hook.py"
    script.write_text(RUN_WITH_HOOK, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, os.fspath(script), plan["control_directory"], action],
        env=fresh_interpreter_env(),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    blocked = [name for name in report["modules"] for root in V2_BLOCKED if name == root or name.startswith(root + ".")]
    assert not blocked, blocked
    return report


@pytest.mark.slow
def test_a_cancel_request_during_the_run_stops_admission_and_keeps_the_completed_frames(tmp_path):
    from lauelab.indexing import validate_results_file

    plan = _prepare(tmp_path, "aps-lauego", max_in_flight=1)  # one frame in flight at a time

    report = _run_with_hook(tmp_path, plan, "cancel")

    summary = _summary(plan)
    assert report["status"] == summary["status"] == "Cancelled"
    assert summary["message"] == "Cancelled by request; completed results kept"
    counters = summary["counters"]
    # lauelab refills its in-flight window before it hands over an outcome, so the second frame may have
    # been admitted (and finished) before the cancel; nothing after it was.
    assert counters["n_failed"] == 0 and counters["n_succeeded"] in (1, 2)
    assert counters["n_not_run"] == 3 - counters["n_succeeded"]
    output = Path(plan["output_directory"])
    records, _ = read_failure_report(output / "failures.jsonl")
    assert [record.index for record in records] == list(range(counters["n_succeeded"], 3))
    assert {record.category for record in records} == {"cancelled"}
    assert summary["failure_report"] == "failures.jsonl"
    frame_ids = [f"frame_{index}" for index in range(1, counters["n_succeeded"] + 1)]
    assert validate_results_file(output / "output.h5", frame_ids=frame_ids).n_frames == counters["n_succeeded"]
    progress = json.loads((output / "progress.json").read_text())
    assert (progress["n_succeeded"], progress["n_failed"]) == (counters["n_succeeded"], 0)


@pytest.mark.slow
def test_a_signal_during_the_run_drains_and_fails_it(tmp_path):
    plan = _prepare(tmp_path, "aps-lauego", max_in_flight=1)

    report = _run_with_hook(tmp_path, plan, "signal")

    summary = _summary(plan)
    assert report["status"] == summary["status"] == "Failed"
    assert summary["message"] == f"Interrupted by signal {int(signal.SIGTERM)}"
    assert summary["counters"]["n_succeeded"] in (1, 2)


# --- before compute ------------------------------------------------------------------------------------


def _never(*_):
    pytest.fail("compute ran")


@pytest.mark.parametrize("name", ["crux-wire", "aps-wire"])
def test_a_tampered_manifest_is_refused_before_compute(tmp_path, name):
    plan = _prepare(tmp_path, name)
    with open(Path(plan["control_directory"]) / "inputs.jsonl", "a", encoding="utf-8") as handle:
        handle.write("{}\n")

    with pytest.raises(ManifestError, match="digest"):
        runner.run(plan["control_directory"], compute=_never)

    output = Path(plan["output_directory"])
    assert not output.exists() or not any(output.iterdir())  # no lock, no progress, no output


def test_a_tampered_request_is_refused_before_compute(tmp_path):
    plan = _prepare(tmp_path, "crux-wire")
    request = Path(plan["control_directory"]) / "request.json"
    request.write_text(request.read_text().replace('"verbose": 0', '"verbose": 1'))

    with pytest.raises(ValueError, match="checksum does not match the submission plan"):
        runner.run(plan["control_directory"], compute=_never)
    assert not Path(plan["output_directory"]).exists()


def test_a_cancel_request_before_the_start_runs_nothing(tmp_path):
    plan = _prepare(tmp_path, "crux-wire")
    (Path(plan["control_directory"]) / "cancel.request").touch()

    summary = runner.run(plan["control_directory"], compute=_never)

    assert summary["status"] == "Cancelled" and summary["counters"]["n_not_run"] == 2
    assert summary["message"] == "Cancelled by request; completed results kept"
    assert _summary(plan) == summary


def test_a_run_starts_at_most_once(tmp_path):
    plan = _prepare(tmp_path, "crux-wire")
    first = runner.run(plan["control_directory"], compute=lambda request, entries, hooks: RunOutcome(2, 0))
    output = Path(plan["output_directory"])
    before = {path.name: path.read_bytes() for path in output.iterdir()}

    with pytest.raises(FileExistsError, match=r"\.runner\.lock"):
        runner.run(plan["control_directory"], compute=_never)

    assert first["status"] == "Finished"
    assert {path.name: path.read_bytes() for path in output.iterdir()} == before


# --- the request as the compute function sees it ----------------------------------------------------------


@pytest.mark.parametrize("name", ["crux-lauego", "aps-lauego"])
def test_load_request_remaps_paths_in_memory_and_takes_resources_from_the_plan(tmp_path, name):
    plan = _prepare(tmp_path, name, workers=3, max_in_flight=7, reconstruction_workers=2, threads_per_worker=5)
    control = Path(plan["control_directory"])
    original = read_request(control / "request.json")
    original_entries = list(read_manifest(control / "inputs.jsonl"))

    request, entries = runner.load_request(control, plan)

    remote = {source: target for source, target in plan["path_map"].items()}
    aps = os.fspath(tmp_path / "aps")
    assert request.run_directory == plan["output_directory"]
    assert request.manifest_path == os.fspath(control / "inputs.jsonl")
    assert (request.job_id, request.n_inputs, request.display_id) == (plan["job_id"], 3, "Indexing I1")
    assert (request.workers, request.max_in_flight, request.reconstruction_workers) == (3, 7, 2)
    for key in ("geometry_file", "crystal_file", "input_path"):
        assert request.parameters[key] == original["request"][key].replace(aps, remote[aps], 1)
    assert [entry.source for entry in entries] == [
        entry.source.replace(aps, remote[aps], 1) for entry in original_entries
    ]
    # LaueGo threads come from indexing_threads_per_worker when the target sets it (crux), else threads_per_worker.
    assert request.parameters["num_threads"] == (1 if name.startswith("crux") else 5)
    assert request.parameters.get("memory_limit_mb") == (256 if name.startswith("crux") else None)
    assert "num_threads" not in original["request"]  # in memory only: the file is never rewritten
    assert read_request(control / "request.json") == original


def test_load_request_sets_the_wire_threads_and_memory_from_the_plan(tmp_path):
    plan = _prepare(tmp_path, "crux-wire", threads_per_worker=6, memory_limit_mb=4096)

    request, _ = runner.load_request(plan["control_directory"], plan)

    assert (request.parameters["num_threads"], request.parameters["memory_limit_mb"]) == (6, 4096)
    assert request.display_id == "Reconstruction R1"
    assert read_request(Path(plan["control_directory"]) / "request.json")["request"]["num_threads"] == 1


def test_a_path_outside_every_mapping_is_refused(tmp_path):
    plan = _prepare(tmp_path, "crux-wire")
    plan["path_map"] = {os.fspath(tmp_path / "elsewhere"): "/remote"}

    with pytest.raises(ValueError, match="No storage mapping"):
        runner.load_request(plan["control_directory"], plan)


# --- the runner's decisions, with fake compute functions -------------------------------------------------


def test_a_compute_error_fails_the_run_and_keeps_the_reported_outcome(tmp_path):
    plan = _prepare(tmp_path, "crux-wire")

    def failing(request, entries, hooks):
        entries = list(entries)
        hooks.report_progress(succeeded=1, failed=0)
        hooks.record_failure(FailureRecord.not_run(entries[1], message="never started"))
        hooks.report_outcome(RunOutcome(1, 0, artifacts={"reconstruction": "partial"}, provenance={"engine": "x"}))
        hooks.log("about to fail")
        raise RuntimeError("native crash")

    summary = runner.run(plan["control_directory"], compute=failing)

    assert (summary["status"], summary["phase"], summary["message"]) == ("Failed", "failed", "native crash")
    assert summary["counters"] == {"n_inputs": 2, "n_succeeded": 1, "n_failed": 0, "n_not_run": 1, "n_indexed": None}
    assert summary["artifacts"] == {"reconstruction": "partial"}
    assert summary["provenance"] == {"engine": "x", "path_map": plan["path_map"]}
    assert summary["log"] == ["about to fail"]
    assert summary["failure_report"] == "failures.jsonl"
    records, _ = read_failure_report(Path(plan["output_directory"]) / "failures.jsonl")
    assert [(record.index, record.message) for record in records] == [(1, "never started")]


@pytest.mark.parametrize(
    ("outcome", "status", "message"),
    [
        (RunOutcome(1, 1), "Failed", "Not all inputs completed successfully"),
        (RunOutcome(1, 0), "Failed", "Not all inputs completed successfully"),
        (RunOutcome(2, 0, validation_error="scan.h5: broken"), "Failed", "scan.h5: broken"),
        (RunOutcome(1, 0, stopped=True), "Cancelled", "Cancelled by request; completed results kept"),
        (RunOutcome(2, 0, summary="2 of 2 point(s)"), "Finished", "2 of 2 point(s)"),
        ("not an outcome", "Failed", "Compute function did not return a RunOutcome"),
    ],
)
def test_the_status_follows_the_outcome(tmp_path, outcome, status, message):
    plan = _prepare(tmp_path, "crux-wire")

    summary = runner.run(plan["control_directory"], compute=lambda request, entries, hooks: outcome)

    assert (summary["status"], summary["message"]) == (status, message)
    assert summary["phase"] == status.lower()


def test_counts_beyond_the_manifest_are_refused_without_a_summary(tmp_path):
    plan = _prepare(tmp_path, "crux-wire")

    with pytest.raises(ValueError, match="exceed the manifest"):
        runner.run(plan["control_directory"], compute=lambda request, entries, hooks: RunOutcome(2, 1))
    assert not (Path(plan["output_directory"]) / "run.json").exists()


def test_progress_never_goes_backwards(tmp_path):
    hooks = runner.FileHooks(tmp_path, tmp_path / "cancel.request")
    hooks.report_progress(succeeded=2, failed=1)
    with pytest.raises(ValueError, match="backwards"):
        hooks.report_progress(succeeded=1, failed=1)
    hooks.start()
    hooks.close()
    assert json.loads((tmp_path / "progress.json").read_text())["n_succeeded"] == 2


def test_the_heartbeat_rewrites_progress_during_a_long_input(tmp_path):
    hooks = runner.FileHooks(tmp_path, tmp_path / "cancel.request", interval=0.02)
    hooks.start()
    first = (tmp_path / "progress.json").stat().st_mtime_ns
    hooks.report_progress(succeeded=1, failed=0)
    deadline = 200
    while json.loads((tmp_path / "progress.json").read_text())["n_succeeded"] != 1 and deadline:
        deadline -= 1
        time.sleep(0.01)
    hooks.close()
    assert deadline, "the heartbeat thread never republished progress.json"
    assert (tmp_path / "progress.json").stat().st_mtime_ns >= first
    assert sorted(path.name for path in tmp_path.iterdir()) == ["progress.json"]  # no partial left behind
