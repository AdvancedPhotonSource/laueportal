"""Run a frozen request on a compute node without portal config or a database.

The standalone runner, run as ``python -m laueportal.jobs.runner <control directory>``. It reads ``request.json``,
``inputs.jsonl`` and the plan ``bdp.json`` from the control directory and writes ``.runner.lock``, ``progress.json``,
``failures.jsonl``, the outputs and the ``run.json`` summary into the plan's output directory, in the run directory's
format byte for byte. It imports neither the database nor the settings (import-linter rule 3).

A run continued over PBS allocations (a ``pioneer`` plan's ``continuation``) runs one allocation per call,
``... <control directory> --allocation <N>`` (``remote.pbs.build_script``): its lock is
``allocations/<NNNNNN>/runner.lock``, the launcher's ``allocation.json`` there says whether it only ends a stopped run
(``finalize``), and ``progress.json`` counts the whole run's committed inputs. An allocation that hands the run on
(``RunOutcome.handoff``), or that a signal ends (``interrupted``), writes ``allocations/<NNNNNN>/result.json``
(:func:`handoff_result`) and no ``run.json``: only the allocation that ends the run writes its summary, with the run's
first allocation's start time and every allocation in its provenance. An allocation that fails writes the run's summary
Failed, its committed segments kept unmerged with ``checkpoint.json`` and its counts those last recorded, never zeros.

An allocation holds the run's lock (``allocations/run.lock``, a POSIX record lock, which the shared storage's lock
manager keeps across hosts) for as long as it runs, and refuses to start (:func:`start_allocation`) while another
holds it, once the run has a summary or its launcher's ledger says it ended, or after a later allocation started: one
writer at a time, never one after the run ended. The committed segments are removed only once the summary is written
(``RunOutcome.remove_after_summary``).
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import platform
import shutil
import signal
import sys
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from laueportal.jobs.contract import RunOutcome, RunRequest, get_compute_function
from laueportal.jobs.processes import terminate_descendants
from laueportal.jobs.rundir import (
    ALLOCATION_LEDGER_FILENAME,
    ALLOCATION_LOCK_FILENAME,
    ALLOCATION_RECORD_FILENAME,
    ALLOCATION_RESULT_FILENAME,
    ALLOCATION_RESULT_FORMAT,
    ALLOCATIONS_DIRECTORY,
    FailureReportWriter,
    allocation_directory,
    read_manifest,
    read_request,
    verify_manifest,
    write_json_file,
)
from laueportal.remote.paths import map_path


def timestamp():
    """The run-file time: aware UTC to the second with ``+00:00`` (not ``core.timestamps``' ``Z``)."""
    return datetime.now(UTC).isoformat(timespec="seconds")


class FileHooks:
    """Publish a heartbeat even during a long native call; cancellation drains active work."""

    def __init__(
        self,
        directory,
        cancel_file,
        *,
        interval=5,
        baseline=(0, 0),
        overwrite_report=False,
        best_effort_progress=False,
    ):
        """``baseline``: a continued run's counts as last recorded (:func:`recorded_counts`), until compute reports the
        checkpoint's; reports never go back. ``overwrite_report``: a continued run's failure report
        may replace one that a killed end of the run published. ``best_effort_progress``: a heartbeat that cannot be
        written is printed and tried again, never stopping the run."""
        self.directory = Path(directory)
        self.cancel_file = Path(cancel_file)
        self.interval = interval
        self.succeeded, self.failed = (int(baseline[0]), int(baseline[1]))
        self.messages = []
        self.outcome = None
        self.signal_received = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._error = None
        self._progress_error = None
        self._best_effort_progress = best_effort_progress
        self.failures = FailureReportWriter(self.directory / "failures.jsonl", overwrite=overwrite_report)

    def should_stop(self):
        return self.signal_received is not None or self.cancel_file.exists() or self._error is not None

    def shutting_down(self):
        return self.signal_received is not None or self._error is not None

    def arrivals(self):
        return ()  # a remote run is never live

    def report_progress(self, *, succeeded, failed):
        with self._lock:
            if succeeded < self.succeeded or failed < self.failed:
                raise ValueError("Progress cannot go backwards")
            self.succeeded, self.failed = succeeded, failed

    def record_failure(self, record):
        self.failures.append(record)

    def report_outcome(self, outcome):
        self.outcome = outcome

    def log(self, message):
        self.messages.append(str(message))

    def publish(self):
        try:
            with self._lock:
                write_json_file(
                    self.directory / "progress.json",
                    {
                        "heartbeat_at": timestamp(),
                        "n_succeeded": self.succeeded,
                        "n_failed": self.failed,
                    },
                    overwrite=True,
                )
        except OSError as error:
            if not self._best_effort_progress:
                raise
            problem = str(error)
            if problem != self._progress_error:
                print(f"Progress heartbeat could not be written ({problem}); will retry", file=sys.stderr)
            self._progress_error = problem
        else:
            self._progress_error = None

    def _heartbeat(self):
        while not self._stop.wait(self.interval):
            try:
                self.publish()
            except Exception as error:
                self._error = error
                return

    def start(self):
        self.publish()
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join()
        report = self.failures.close()
        self.publish()
        if self._error:
            raise self._error
        return report


MAPPED_KEYS = ("geometry_file", "crystal_file", "mask_file", "input_path", "background_file", "data_root")
"""The request's paths the plan's ``path_map`` translates (the last two are LaueMatching's), or, for a file the plan
staged (``staged_inputs``), replaces with its copy."""


def staged_path(key, value, spec):
    """The copy a ``pioneer`` plan staged of the request's ``key`` (``remote.plan.stage_input``), checked: it is the
    copy of this value, and holds the recorded bytes. ``ValueError`` otherwise: a run never reads a changed copy."""
    if spec.get("source") != value:
        raise ValueError(f"The plan staged another {key} than the request names")
    digest = hashlib.sha256()
    with open(spec["path"], "rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    if digest.hexdigest() != spec["sha256"]:
        raise ValueError(f"The staged copy of {key} changed after the plan recorded it")
    return spec["path"]


def load_request(directory, plan, *, started=None):
    """The run's request and manifest entries, its paths mapped. ``started``: the ``time.monotonic()`` the
    execution policy's ``soft_deadline_seconds`` is counted from (now when not given)."""
    directory = Path(directory)
    path = directory / "request.json"
    if hashlib.sha256(path.read_bytes()).hexdigest() != plan["request_sha256"]:
        raise ValueError("Frozen request checksum does not match the submission plan")
    document = read_request(path)
    verify_manifest(
        directory / "inputs.jsonl", expected_digest=plan["manifest_sha256"], expected_count=plan["n_inputs"]
    )
    mappings = plan["path_map"]
    staged = plan.get("staged_inputs", {})
    document = json.loads(json.dumps(document))
    for key in MAPPED_KEYS:
        if key in staged:
            document["request"][key] = staged_path(key, document["request"].get(key), staged[key])
        elif document["request"].get(key):
            document["request"][key] = map_path(document["request"][key], mappings)
    if os.path.isabs(document["output"].get("xml", "")):
        document["output"]["xml"] = map_path(document["output"]["xml"], mappings)
    policy = plan["execution"]
    document["request"]["num_threads"] = (
        policy.get("indexing_threads_per_worker", policy["threads_per_worker"])
        if document["kind"] == "lauego_indexing"
        else policy["threads_per_worker"]
    )
    if policy.get("memory_limit_mb") is not None:
        document["request"]["memory_limit_mb"] = policy["memory_limit_mb"]
    soft_deadline = policy.get("soft_deadline_seconds")
    deadline = None
    if soft_deadline is not None:
        deadline = (time.monotonic() if started is None else started) + float(soft_deadline)
    entries = [
        replace(entry, source=map_path(entry.source, mappings)) for entry in read_manifest(directory / "inputs.jsonl")
    ]
    run = document["run"]
    display_id = (
        f"Indexing I{run['indexing_id']}" if "indexing_id" in run else f"Reconstruction R{run['reconstruction_id']}"
    )
    return RunRequest(
        job_id=plan["job_id"],
        kind=document["kind"],
        display_id=display_id,
        run_directory=plan["output_directory"],
        manifest_path=str(directory / "inputs.jsonl"),
        document=document,
        n_inputs=plan["n_inputs"],
        workers=policy["workers"],
        max_in_flight=policy.get("max_in_flight"),
        reconstruction_workers=policy["reconstruction_workers"],
        execution=policy,
        deadline=deadline,
    ), entries


RUN_LOCK_FILENAME = "run.lock"
"""``allocations/run.lock``: held (``fcntl.lockf``, exclusive) by the one allocation of a continued run that runs."""


def take_run_lock(output):
    """Hold a continued run's lock (``allocations/run.lock``) for this process; returns its descriptor (closed when
    the allocation ends, which releases it). ``RuntimeError`` when another process holds it: an allocation of the run
    is running."""
    folder = Path(output) / ALLOCATIONS_DIRECTORY
    folder.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(folder / RUN_LOCK_FILENAME, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(descriptor)
        if error.errno in (errno.EACCES, errno.EAGAIN):
            raise RuntimeError("Another allocation of this run is running: it holds the run's lock") from None
        raise
    return descriptor


def start_allocation(output, plan, sequence, started_at):
    """Take the run's lock (:func:`take_run_lock`) and allocation ``sequence``'s record of its start
    (``allocations/<NNNNNN>/runner.lock``: its process, host, PBS job and start); return whether the launcher started
    it only to end a stopped run (its ``allocation.json``'s ``finalize``) and the run lock's descriptor. Refused
    (``RuntimeError``, nothing written) when another allocation of the run is running, when the run has ended (its
    ``run.json``, or a launcher's ledger with an ended status: a job PBS accepted after the launcher failed the run),
    when the launcher's record names another submission or allocation, when an allocation after the first has no
    record, and when this or a later allocation has started already."""
    lock = take_run_lock(output)
    try:
        if (Path(output) / "run.json").exists():
            raise RuntimeError("The run has ended already: it has a run summary")
        try:
            ledger = json.loads((Path(output) / ALLOCATIONS_DIRECTORY / ALLOCATION_LEDGER_FILENAME).read_text())
        except FileNotFoundError:
            ledger = {}
        if ledger.get("status") in ("SUCCEEDED", "FAILED", "CANCELLED"):
            raise RuntimeError(f"The launcher ended this run ({ledger['status']}): {ledger.get('message')}")
        folder = allocation_directory(output, sequence)
        folder.mkdir(parents=True, exist_ok=True)
        finalize = False
        try:
            record = json.loads((folder / ALLOCATION_RECORD_FILENAME).read_text())
        except FileNotFoundError:
            if sequence != 1:
                raise RuntimeError(f"Allocation {sequence} has no launcher record") from None
        else:
            if record.get("submission_id") != plan["submission_id"] or record.get("sequence") != sequence:
                raise RuntimeError(f"Allocation {sequence}'s launcher record is another allocation's")
            finalize = bool(record.get("finalize"))
        for other in (Path(output) / ALLOCATIONS_DIRECTORY).iterdir():
            if other.name.isdigit() and int(other.name) > sequence and (other / ALLOCATION_LOCK_FILENAME).exists():
                raise RuntimeError(f"Allocation {int(other.name)} of this run has started after this one")
        started = {
            "pid": os.getpid(),
            "host": platform.node(),
            "pbs_job_id": os.environ.get("PBS_JOBID"),
            "started_at": started_at,
        }
        with open(folder / ALLOCATION_LOCK_FILENAME, "x", encoding="utf-8") as handle:
            json.dump(started, handle)
    except BaseException:
        os.close(lock)
        raise
    return finalize, lock


def recorded_counts(output, *, n_inputs, submission_id):
    """The counts of the latest hand-off of this submission (its ``result.json``), until compute reports the
    checkpoint's (the same, once checked). A heartbeat (``progress.json``) is not a checkpoint and is never read."""
    candidates = [(0, 0)]
    try:
        folders = sorted((Path(output) / ALLOCATIONS_DIRECTORY).iterdir())
    except OSError:
        folders = []
    for folder in folders:
        try:
            result = json.loads((folder / ALLOCATION_RESULT_FILENAME).read_text())
            if result.get("submission_id") != submission_id or result.get("format") != ALLOCATION_RESULT_FORMAT:
                continue
            committed = result["committed"]
            succeeded, failed = committed["n_succeeded"], committed["n_failed"]
            if type(succeeded) is int and type(failed) is int and min(succeeded, failed) >= 0:
                if succeeded + failed <= n_inputs and succeeded + failed == committed.get("n_attempted"):
                    candidates.append((succeeded, failed))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return max(candidates, key=lambda pair: (pair[0] + pair[1], pair))


def _allocations(output, sequence, status):
    """Every allocation that started, for a continued run's summary: its number, PBS job, host and start, and how it
    ended (its ``result.json``'s status; this one's ``status``)."""
    rows = []
    for folder in sorted((Path(output) / ALLOCATIONS_DIRECTORY).iterdir()):
        if not folder.name.isdigit():
            continue
        try:
            row = json.loads((folder / ALLOCATION_LOCK_FILENAME).read_text())
        except (OSError, ValueError):
            continue
        number = int(folder.name)
        try:
            ended = json.loads((folder / ALLOCATION_RESULT_FILENAME).read_text()).get("status")
        except (OSError, ValueError):
            ended = status if number == sequence else "ended without a result"
        rows.append(
            {
                "allocation": number,
                "pbs_job_id": row.get("pbs_job_id"),
                "host": row.get("host"),
                "started_at": row.get("started_at"),
                "status": ended,
            }
        )
    return rows


def handoff_result(request, plan, outcome, hooks, *, started_at):
    """``allocations/<NNNNNN>/result.json`` of an allocation that did not end the run: ``status`` ``handoff`` (its
    ``reason``: ``time_limit``, ``frame_limit``) or ``interrupted`` (a signal), and the run's committed work
    (``committed``: inputs attempted, succeeded and failed), which the launcher checks before the next allocation."""
    return {
        "format": ALLOCATION_RESULT_FORMAT,
        "version": 1,
        "job_id": request.job_id,
        "submission_id": plan["submission_id"],
        "allocation": request.allocation,
        "status": "interrupted" if outcome.interrupted else "handoff",
        "reason": f"signal {hooks.signal_received}" if outcome.interrupted else outcome.handoff,
        "message": outcome.summary,
        "started_at": started_at,
        "finished_at": timestamp(),
        "host": platform.node(),
        "pbs_job_id": os.environ.get("PBS_JOBID"),
        "committed": outcome.checkpoint,
        "warnings": outcome.warnings,
        "log": hooks.messages,
        "gpu": outcome.provenance.get("gpu"),
        "daemon_start_seconds": outcome.provenance.get("daemon_start_seconds"),
    }


def run(directory, *, compute=None, request=None, entries=None, allocation=None):
    """Run once. A durable exclusive lock prevents accidental duplicate execution. ``allocation``: a continued run's
    allocation number (1 when not given); an error for any other run."""
    directory = Path(directory)
    started = time.monotonic()
    plan = json.loads((directory / "bdp.json").read_text())
    continuation = plan.get("continuation")
    if continuation is None and allocation is not None:
        raise ValueError("Only a run its plan continues over allocations has allocations")
    sequence = int(allocation or 1) if continuation is not None else None
    if request is None:
        request, entries = load_request(directory, plan, started=started)
    output = Path(request.run_directory)
    output.mkdir(parents=True, exist_ok=True)
    started_at = timestamp()
    run_lock = None
    baseline = (0, 0)
    if sequence is None:
        with open(output / ".runner.lock", "x", encoding="utf-8") as lock:
            lock.write(str(os.getpid()))
    else:
        finalize, run_lock = start_allocation(output, plan, sequence, started_at)
        request = replace(request, continuation=continuation, allocation=sequence, finalize=finalize)
        baseline = recorded_counts(output, n_inputs=request.n_inputs, submission_id=plan["submission_id"])
    try:
        return _run(directory, plan, request, entries, compute, output, sequence, started_at, baseline)
    finally:
        if run_lock is not None:
            os.close(run_lock)  # the run's lock, held until the summary is durable and the segments removed


def _run(directory, plan, request, entries, compute, output, sequence, started_at, baseline):
    hooks = FileHooks(
        output,
        directory / "cancel.request",
        baseline=baseline,
        overwrite_report=sequence is not None,
        best_effort_progress=sequence is not None,
    )
    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, lambda number, frame: setattr(hooks, "signal_received", number))
    outcome = None
    error = None
    try:
        hooks.start()
        if not hooks.should_stop() or sequence is not None:
            # A continued run's allocation computes after a stop too: it ends the run with its committed work.
            # entries is None only when a caller passes a request without its entries.
            outcome = (compute or get_compute_function(request.kind))(request, entries, hooks)  # pyright: ignore[reportArgumentType]
        else:
            outcome = RunOutcome(0, 0, stopped=True)
        if not isinstance(outcome, RunOutcome):
            raise TypeError("Compute function did not return a RunOutcome")
        if sequence is None or not (outcome.handoff or outcome.interrupted):
            hooks.report_progress(succeeded=outcome.n_succeeded, failed=outcome.n_failed)
    except BaseException as exception:
        error = exception
        outcome = hooks.outcome
    finally:
        terminate_descendants()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        try:
            report = hooks.close()
        except Exception as exception:
            report = None
            error = error or exception
    published = (
        error is None
        and outcome is not None
        and not outcome.validation_error
        and bool(outcome.artifacts or outcome.remove_after_summary)
    )
    # A signal after a continued run's end published its files leaves that end's outcome as it is.
    signal_failure = hooks.signal_received is not None and not (sequence is not None and published)
    succeeded, failed = hooks.succeeded, hooks.failed
    if succeeded + failed > request.n_inputs:
        raise ValueError("Compute counters exceed the manifest")
    if sequence is not None and error is None and outcome is not None and (outcome.handoff or outcome.interrupted):
        result = handoff_result(request, plan, outcome, hooks, started_at=started_at)
        write_json_file(allocation_directory(output, sequence) / ALLOCATION_RESULT_FILENAME, result)
        return result
    status = "Finished"
    message = outcome.summary if outcome else "Run failed"
    checkpoint = None
    if error or signal_failure or (outcome and outcome.validation_error):
        status = "Failed"
        message = str(
            error or (outcome and outcome.validation_error) or f"Interrupted by signal {hooks.signal_received}"
        )
        if sequence is not None and not published and (succeeded or failed) and (output / "segments").is_dir():
            # The run's committed work as last recorded: kept, not merged, never presented as not run.
            message += (
                f"; the {succeeded + failed} inputs committed before ({succeeded} succeeded, {failed} failed) are kept"
                f" unmerged in {output / 'segments'} with checkpoint.json"
            )
            checkpoint = {"kept": "segments", "n_succeeded": succeeded, "n_failed": failed, "merged": False}
    elif outcome and outcome.stopped:
        status, message = "Cancelled", "Cancelled by request; completed results kept"
    elif failed or succeeded != request.n_inputs:
        status, message = "Failed", "Not all inputs completed successfully"
    summary = {
        "format": "laue-portal-run-summary",
        "version": 1,
        "job_id": request.job_id,
        "submission_id": plan["submission_id"],
        "display_id": request.display_id,
        "kind": request.kind,
        "status": status,
        "phase": status.lower(),
        "message": message,
        "started_at": _first_start(output) if sequence is not None else started_at,
        "finished_at": timestamp(),
        "host": platform.node(),
        "counters": {
            "n_inputs": request.n_inputs,
            "n_succeeded": succeeded,
            "n_failed": failed,
            "n_not_run": request.n_inputs - succeeded - failed,
            "n_indexed": outcome.n_indexed if outcome else None,
        },
        "artifacts": dict(outcome.artifacts) if outcome else {},
        "provenance": {
            **(outcome.provenance if outcome else {}),
            "path_map": plan["path_map"],
            **({"allocations": _allocations(output, sequence, status)} if sequence is not None else {}),
            **({"checkpoint": checkpoint} if checkpoint is not None else {}),
        },
        "warnings": outcome.warnings if outcome else [],
        "log": hooks.messages,
        "failure_report": os.path.basename(report) if report else None,
        "policy": plan["execution"],
    }
    write_json_file(output / "run.json", summary)
    for folder in outcome.remove_after_summary if outcome and not error else []:
        try:
            shutil.rmtree(folder)
        except OSError as problem:  # the summary is durable; the merged files hold every committed frame
            print(f"Could not remove {folder} after the summary: {problem}", file=sys.stderr)
    return summary


def _first_start(output):
    """A continued run's start: its first allocation's (the earliest allocation lock's ``started_at``)."""
    starts = [row["started_at"] for row in _allocations(output, 0, None) if row.get("started_at")]
    return starts[0] if starts else timestamp()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", help="Directory containing request.json, inputs.jsonl, and bdp.json")
    parser.add_argument("--allocation", type=int, help="A continued run's allocation number (remote.stage)")
    args = parser.parse_args()
    summary = run(args.directory, allocation=args.allocation)
    if summary.get("format") == ALLOCATION_RESULT_FORMAT:
        print(json.dumps({"status": summary["status"], "committed": summary["committed"]}))
        return 0
    print(json.dumps({"status": summary["status"], "counters": summary["counters"]}))
    return 0 if summary["status"] == "Finished" else 1


if __name__ == "__main__":
    raise SystemExit(main())
