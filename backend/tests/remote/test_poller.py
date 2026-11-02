"""The poller (``remote.poller``) and ``laueportal remote poll`` against a fake DM.

Submission and a lost reply are in ``test_bdp.py``, a finished publication with real outputs in its slow tests and in
``test_remote_goldens.py``. Here the returned summaries need no science validation (no artifact: Failed or Cancelled
runs), so everything is fast. Job statuses and phases move only through ``jobs.state``'s existing transitions.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from sqlalchemy.orm import Session

from laueportal.core.timeutil import utcnow
from laueportal.jobs import controls
from laueportal.jobs.models import Job
from laueportal.jobs.rundir import write_json_file
from laueportal.jobs.schemas import CancelState
from laueportal.jobs.state import JobStatus, RunPhase
from laueportal.remote import cleanup, poller, submission
from laueportal.remote.dm import CallAborted, launcher_command
from laueportal.remote.paths import map_path
from laueportal.remote.plan import submission_path
from laueportal.remote.poller import PollerBusyError, poll_once, publish_returned, run_poller
from tests.remote_support import (
    BatchedFake,
    Layout,
    bdp_settings,
    create_lauego_run,
    create_wire_run,
    output_listing,
    remote_settings,
)
from tests.run_support import load_job, make_engine
from tests.settings_support import fresh_interpreter_env, make_database


class FakeDM(BatchedFake):
    """DM's processing jobs by ID; ``cancel`` submits the cancel workflow's job ``cancel-1``, ``clean_up`` the
    clean-up workflow's ``cleanup-<n>``. ``failing`` holds an error the next call of a method raises, as the real
    client's ``_call`` does (``CalledProcessError``: e.g. a workflow DM does not know; ``TimeoutExpired``)."""

    def __init__(self):
        self.jobs: dict[str, dict[str, Any]] = {"dm-001": {"id": "dm-001", "status": "pending"}}
        self.submissions: list[dict[str, Any]] = []
        self.cancels: list[str] = []
        self.cleanups: list[str] = []
        self.gets: list[str] = []
        self.failing: dict[str, BaseException] = {}

    def _fail(self, method: str) -> None:
        error = self.failing.pop(method, None)
        if error is not None:
            raise error

    def submit(self, plan):
        self.submissions.append(plan)
        return self.jobs["dm-001"]

    def get(self, external_id):
        self.gets.append(external_id)
        self._fail("get")
        return self.jobs[external_id]

    def find(self, submission_id):
        return [self.jobs["dm-001"]]

    def cancel(self, submission_id):
        self.cancels.append(submission_id)
        self._fail("cancel")  # after DM took it: the reply is lost
        self.jobs["cancel-1"] = {"id": "cancel-1", "status": "running"}
        return self.jobs["cancel-1"]

    def clean_up(self, submission_id):
        self.cleanups.append(submission_id)
        self._fail("clean_up")
        job = {"id": f"cleanup-{len(self.cleanups)}", "status": "running"}
        self.jobs[job["id"]] = job
        return job

    def status(self, value, **record):
        self.jobs["dm-001"].update(status=value, **record)


@pytest.fixture
def remote(tmp_path):
    layout = Layout(tmp_path)
    engine = make_engine(tmp_path / "portal")
    values = bdp_settings(layout)
    yield SimpleNamespace(engine=engine, layout=layout, values=values, settings=remote_settings(values))
    engine.dispose()


def _submit(remote, *, backend="crux", settings=None, name="wire"):
    run = create_wire_run(remote.engine, remote.layout, count=2, backend=backend, name=name)
    client = FakeDM()
    submission.submit_run(run.job_id, engine=remote.engine, settings=settings or remote.settings, client=client)
    return run, client.submissions[0], client


def _return(plan, *, status="Failed", succeeded=0, failed=0, message="returned", extra=(), artifacts=None):
    """What a run that computed no artifact returns into the inbox: its summary, progress and failures (``artifacts``:
    names to paths relative to the output directory, written as given by the caller)."""
    incoming = Path(plan.get("incoming_directory", plan["output_directory"]))
    incoming.mkdir(parents=True, exist_ok=True)
    n = plan["n_inputs"]
    counters = {"n_inputs": n, "n_succeeded": succeeded, "n_failed": failed, "n_not_run": n - succeeded - failed}
    write_json_file(incoming / "progress.json", {"heartbeat_at": "2026-10-16T09:00:00+00:00", **counters})
    if failed:
        (incoming / "failures.jsonl").write_text('{"index":0,"category":"input"}\n' * failed)
    for name in extra:
        (incoming / name).write_text(name)
    summary = {
        "format": "laue-portal-run-summary",
        "version": 1,
        "job_id": plan["job_id"],
        "submission_id": plan["submission_id"],
        "kind": plan["kind"],
        "status": status,
        "phase": status.lower(),
        "message": message,
        "counters": {**counters, "n_indexed": None},
        "artifacts": {name: f"{plan['output_directory']}/{path}" for name, path in (artifacts or {}).items()},
        "provenance": {"path_map": plan["path_map"]},
    }
    write_json_file(incoming / "run.json", summary)
    (incoming / ".runner.lock").write_text("1234")
    return incoming


def test_pending_then_running_then_done_publishes_and_finalizes(remote):
    run, plan, client = _submit(remote)
    engine, settings = remote.engine, remote.settings

    assert poll_once(engine=engine, settings=settings, client=client) == [run.job_id]
    job = load_job(engine, run.job_id)
    assert (job.status, job.phase, job.external_state) == (JobStatus.QUEUED, RunPhase.QUEUED, "pending")

    client.status("running", url="https://app.globus.org/runs/flow-1")
    poll_once(engine=engine, settings=settings, client=client)
    job = load_job(engine, run.job_id)
    assert (job.status, job.phase, job.external_state) == (JobStatus.RUNNING, RunPhase.RUNNING, "running")
    assert job.external_url == "https://app.globus.org/runs/flow-1" and job.heartbeat_at is not None

    _return(plan, status="Failed", succeeded=1, failed=1, message="Not all inputs completed successfully")
    client.status("done")
    assert poll_once(engine=engine, settings=settings, client=client) == [run.job_id]
    job = load_job(engine, run.job_id)
    assert (job.status, job.phase, job.external_state) == (JobStatus.FAILED, RunPhase.FAILED, "done")
    assert (job.n_succeeded, job.n_failed, job.n_not_run) == (1, 1, 0)
    assert job.messages.splitlines()[-1] == "Not all inputs completed successfully"
    run_directory = Path(plan["run_directory"])
    assert job.failure_report_path == str(run_directory / "failures.jsonl")
    published = json.loads((run_directory / "run.json").read_text())
    assert published["provenance"]["remote_output_directory"] == plan["output_directory"]
    assert (run_directory / "failures.jsonl").is_file() and (run_directory / "progress.json").is_file()
    # A finished job is no longer read.
    assert poll_once(engine=engine, settings=settings, client=client) == []


@pytest.mark.parametrize("state", ["failed", "aborted"])
def test_a_failed_or_aborted_dm_job_without_a_return_fails_the_run(remote, state):
    run, _plan, client = _submit(remote)
    client.status("running")
    poll_once(engine=remote.engine, settings=remote.settings, client=client)
    client.status(state)
    poll_once(engine=remote.engine, settings=remote.settings, client=client)
    job = load_job(remote.engine, run.job_id)
    assert (job.status, job.phase, job.n_not_run) == (JobStatus.FAILED, RunPhase.FAILED, 2)
    assert job.messages.splitlines()[-1] == f"DM job dm-001 {state}; inspect DM stage diagnostics"


def test_a_dm_job_aborted_after_a_stop_request_is_cancelled(remote):
    run, _plan, client = _submit(remote)
    controls.cancel_run(run.job_id, engine=remote.engine)
    client.status("aborted")
    poll_once(engine=remote.engine, settings=remote.settings, client=client)
    job = load_job(remote.engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.CANCELLED, RunPhase.CANCELLED)
    assert client.cancels == []  # DM had already ended: nothing to deliver


def test_dm_done_without_a_returned_summary_fails_the_run(remote):
    run, _plan, client = _submit(remote)
    client.status("done")
    poll_once(engine=remote.engine, settings=remote.settings, client=client)
    job = load_job(remote.engine, run.job_id)
    assert job.status == JobStatus.FAILED
    assert job.messages.splitlines()[-1] == (
        "Returned output could not be validated/published: DM completed without returning a run summary"
    )


def test_a_transient_dm_error_changes_nothing(remote):
    run, _plan, client = _submit(remote)

    def unreachable(_external_id):
        raise TimeoutError("DM did not answer")

    client.get = unreachable
    assert poll_once(engine=remote.engine, settings=remote.settings, client=client) == []
    job = load_job(remote.engine, run.job_id)
    assert (job.status, job.phase, job.external_state) == (JobStatus.QUEUED, RunPhase.QUEUED, "pending")


def test_a_stop_is_delivered_once_through_dms_cancel_workflow(remote):
    run, plan, client = _submit(remote)
    engine, settings = remote.engine, remote.settings
    client.status("running")
    poll_once(engine=engine, settings=settings, client=client)
    result = controls.cancel_run(run.job_id, engine=engine)
    assert (result.state, result.n_pending) == (CancelState.REQUESTED, 2)
    run_directory = Path(plan["run_directory"])

    poll_once(engine=engine, settings=settings, client=client)
    poll_once(engine=engine, settings=settings, client=client)
    assert client.cancels == [plan["submission_id"]]
    request = json.loads((run_directory / "cancel.request").read_text())
    assert request["requested_at"] == str(load_job(engine, run.job_id).cancel_requested_at)
    assert json.loads((run_directory / ".cancel-job.json").read_text()) == {"id": "cancel-1"}
    assert not (run_directory / ".cancel-sent").exists()

    client.jobs["cancel-1"]["status"] = "done"
    poll_once(engine=engine, settings=settings, client=client)
    assert (run_directory / ".cancel-sent").exists()
    gets = len(client.gets)
    poll_once(engine=engine, settings=settings, client=client)
    assert client.gets[gets:] == ["dm-001"]  # delivered: the cancel job is not asked again
    assert load_job(engine, run.job_id).status == JobStatus.RUNNING

    _return(plan, status="Cancelled", succeeded=1, message="Cancelled by request; completed results kept")
    client.status("done")
    poll_once(engine=engine, settings=settings, client=client)
    job = load_job(engine, run.job_id)
    assert (job.status, job.n_succeeded, job.n_not_run) == (JobStatus.CANCELLED, 1, 1)


def test_a_cancel_submission_cut_by_the_worker_s_end_is_one_line_and_the_next_pass_submits_it_again(remote, caplog):
    """The worker's end (``DMClient.abort``) cuts the cancel workflow's submission after DM took it: one warning line,
    no ``.cancel-job.json`` and nothing finalized; the next pass (the next worker's) submits the cancel again, as after
    a call's timeout (a second ``qdel`` of an ``aps`` run may then fail, reported on each pass, until the run's DM job
    ends)."""
    run, plan, client = _submit(remote)
    client.status("running")
    controls.cancel_run(run.job_id, engine=remote.engine)
    client.failing["cancel"] = CallAborted("the worker's end ended the call")
    with caplog.at_level(logging.WARNING, logger=poller.__name__):
        poll_once(engine=remote.engine, settings=remote.settings, client=client)
    [cut] = caplog.records
    assert cut.getMessage() == f"The worker's end cut the delivery of remote job {run.job_id}'s stop"
    assert cut.exc_info is None
    run_directory = Path(plan["run_directory"])
    assert not (run_directory / ".cancel-job.json").exists()
    assert load_job(remote.engine, run.job_id).status == JobStatus.QUEUED  # as submitted: the cut pass wrote nothing

    poll_once(engine=remote.engine, settings=remote.settings, client=client)
    assert client.cancels == [plan["submission_id"]] * 2
    assert load_job(remote.engine, run.job_id).status == JobStatus.RUNNING
    assert json.loads((run_directory / ".cancel-job.json").read_text()) == {"id": "cancel-1"}


def test_a_failed_cancel_workflow_is_reported_never_resubmitted_and_never_marked_delivered(remote, caplog):
    """The failed DM cancel job is reported on every pass for the operator to inspect; it is never submitted again on
    its own (the runbook: remove ``.cancel-job.json`` to have the next pass submit a new one)."""
    run, plan, client = _submit(remote)
    client.status("running")
    controls.cancel_run(run.job_id, engine=remote.engine)
    poll_once(engine=remote.engine, settings=remote.settings, client=client)
    client.jobs["cancel-1"]["status"] = "failed"
    caplog.clear()
    for _ in range(3):
        poll_once(engine=remote.engine, settings=remote.settings, client=client)
    directory = Path(plan["run_directory"])
    assert len(client.cancels) == 1 and (directory / ".cancel-job.json").exists()
    assert not (directory / ".cancel-sent").exists()
    assert load_job(remote.engine, run.job_id).status == JobStatus.RUNNING
    assert [record.exc_info[1].args[0] for record in caplog.records if record.exc_info] == [
        "DM cancellation job cancel-1 failed; inspect its stage diagnostics"
    ] * 3
    (directory / ".cancel-job.json").unlink()
    poll_once(engine=remote.engine, settings=remote.settings, client=client)
    assert len(client.cancels) == 2


def test_without_a_cancel_workflow_the_launcher_is_run_directly(remote, monkeypatch):
    values = {**remote.values, "dm": {**remote.values["dm"], "cancel_workflow": None}}
    settings = remote_settings(values)
    run, plan, client = _submit(remote, settings=settings)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs)))
    client.status("running")
    controls.cancel_run(run.job_id, engine=remote.engine)
    poll_once(engine=remote.engine, settings=settings, client=client)
    poll_once(engine=remote.engine, settings=settings, client=client)
    # A process of the client's (DMClient.run: killed whole at its timeout and at the worker's end), never a bare one.
    assert client.commands == [(launcher_command(settings.dm, "cancel", plan["submission_id"]), 60)]
    assert calls == []
    assert (Path(plan["run_directory"]) / ".cancel-sent").exists() and client.cancels == []


def test_a_restarted_poller_finishes_a_publication_and_republishing_changes_nothing(remote, monkeypatch):
    run, plan, client = _submit(remote, backend="aps")
    run_directory = Path(plan["run_directory"])
    _return(plan, status="Failed", failed=2, message="Not all inputs completed successfully", extra=("pbs-output.log",))
    client.status("done")
    publish = poller._publish_returned_file
    calls = []

    def crash_after_the_first_file(source, destination, transform=None):
        if calls:
            raise OSError("the poller host went down")
        calls.append(destination)
        return publish(source, destination, transform)

    monkeypatch.setattr(poller, "_publish_returned_file", crash_after_the_first_file)
    assert poll_once(engine=remote.engine, settings=remote.settings, client=client) == []
    assert load_job(remote.engine, run.job_id).status == JobStatus.QUEUED  # a crash is not a verdict
    marker = json.loads((run_directory / ".bdp-publication.json").read_text())
    assert sorted(marker["files"]) == ["failures.jsonl", "pbs-output.log", "progress.json"]
    assert not (run_directory / "run.json").exists()

    monkeypatch.setattr(poller, "_publish_returned_file", publish)
    assert poll_once(engine=remote.engine, settings=remote.settings, client=client) == [run.job_id]
    job = load_job(remote.engine, run.job_id)
    assert (job.status, job.n_failed) == (JobStatus.FAILED, 2)
    snapshot = {path: path.read_bytes() for path in run_directory.rglob("*") if path.is_file()}
    listing = output_listing(run_directory)
    for name in marker["files"]:
        assert (run_directory / name).is_file()

    again = publish_returned(plan)
    assert again["submission_id"] == plan["submission_id"] and again["status"] == "Failed"
    assert output_listing(run_directory) == listing
    assert {path: path.read_bytes() for path in run_directory.rglob("*") if path.is_file()} == snapshot


def _link_then_stop(source, destination, transform=None):
    """A poller that stops after publishing a file by hard link, before removing its inbox copy."""
    os.link(source, destination)
    raise OSError("the poller host went down")


def _copy_then_stop(source, destination, transform=None):
    """A poller that stops after linking an owned copy (the transfer account's file refused the link)."""
    destination.write_bytes(source.read_bytes())
    raise OSError("the poller host went down")


@pytest.mark.parametrize("stop", [_link_then_stop, _copy_then_stop])
def test_a_poller_stopped_between_publishing_a_file_and_removing_its_inbox_copy_finishes(remote, monkeypatch, stop):
    run, plan, client = _submit(remote, backend="aps")
    run_directory, incoming = Path(plan["run_directory"]), Path(plan["output_directory"])  # aps: the inbox
    _return(plan, status="Failed", failed=2, message="Not all inputs completed successfully", extra=("pbs-output.log",))
    client.status("done")
    monkeypatch.setattr(poller, "_publish_returned_file", stop)
    assert poll_once(engine=remote.engine, settings=remote.settings, client=client) == []
    marker = json.loads((run_directory / ".bdp-publication.json").read_text())
    first = run_directory / marker["files"][0]
    assert first.is_file() and (incoming / first.name).is_file()  # published, and its inbox copy left behind
    assert load_job(remote.engine, run.job_id).status == JobStatus.QUEUED

    monkeypatch.undo()
    assert poll_once(engine=remote.engine, settings=remote.settings, client=client) == [run.job_id]
    job = load_job(remote.engine, run.job_id)
    assert (job.status, job.n_failed) == (JobStatus.FAILED, 2)
    assert job.messages.splitlines()[-1] == "Not all inputs completed successfully"  # the run's verdict, not ours
    assert sorted(path.name for path in incoming.iterdir()) == [".runner.lock", "run.json"]
    assert all((run_directory / name).is_file() for name in marker["files"])
    assert json.loads((run_directory / "run.json").read_text())["submission_id"] == plan["submission_id"]


def test_a_different_file_left_at_a_destination_is_never_replaced(remote, monkeypatch):
    run, plan, client = _submit(remote, backend="aps")
    run_directory, incoming = Path(plan["run_directory"]), Path(plan["output_directory"])  # aps: the inbox
    _return(plan, status="Failed", failed=2)
    client.status("done")
    monkeypatch.setattr(poller, "_publish_returned_file", _copy_then_stop)
    poll_once(engine=remote.engine, settings=remote.settings, client=client)
    monkeypatch.undo()
    name = json.loads((run_directory / ".bdp-publication.json").read_text())["files"][0]
    (run_directory / name).write_text("not this publication's file")
    poll_once(engine=remote.engine, settings=remote.settings, client=client)
    job = load_job(remote.engine, run.job_id)
    assert job.status == JobStatus.FAILED and "could not be validated/published" in job.messages
    assert (run_directory / name).read_text() == "not this publication's file"
    assert (incoming / name).is_file() and not (run_directory / "run.json").exists()


def test_a_returned_directory_is_published_by_name_whatever_the_file_systems_order(remote, monkeypatch):
    """The inbox's entries are moved by name, so that the same returned directory leaves the same marker, the same
    moved entries and the same error on every file system; here the file system lists them in reverse."""
    run, plan, client = _submit(remote, backend="aps")
    run_directory, incoming = Path(plan["run_directory"]), Path(plan["output_directory"])  # aps: the inbox
    _return(plan, status="Failed", failed=2, extra=("pbs-output.log",))
    for name in ("linked-a.log", "linked-b.log"):
        (incoming / name).symlink_to(run_directory / "inputs.jsonl")
    listing = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda directory: iter(sorted(listing(directory), reverse=True)))

    with pytest.raises(ValueError) as refused:
        publish_returned(plan)
    assert str(refused.value) == f"Returned symlink is not an output: {incoming / 'linked-a.log'}"
    marker = json.loads((run_directory / ".bdp-publication.json").read_text())
    assert marker["files"] == ["failures.jsonl", "linked-a.log", "linked-b.log", "pbs-output.log", "progress.json"]
    assert (run_directory / "failures.jsonl").is_file()
    assert sorted(path.name for path in listing(incoming)) == [
        ".runner.lock",
        "linked-a.log",
        "linked-b.log",
        "pbs-output.log",
        "progress.json",
        "run.json",
    ]
    assert not (run_directory / "pbs-output.log").exists() and not (run_directory / "run.json").exists()


def _returned_indexing_xml(remote):
    """A crux LaueGo run whose return (Failed, no artifact) holds an Output XML naming an input by its Eagle path,
    which publication rewrites to the portal's (a transformed file), and DM done."""
    run = create_lauego_run(remote.engine, remote.layout, count=2, backend="crux")
    client = FakeDM()
    submission.submit_run(run.job_id, engine=remote.engine, settings=remote.settings, client=client)
    plan = client.submissions[0]
    incoming = _return(plan, status="Failed", failed=2, message="Not all inputs completed successfully")
    portal_image = remote.layout.data / "frames" / "frame_1.h5"
    remote_image = map_path(portal_image, plan["path_map"])
    assert remote_image != str(portal_image)
    (incoming / "output.xml").write_text(f"<step>\n<inputImage>{remote_image}</inputImage>\n</step>\n")
    client.status("done")
    return run, plan, client, incoming, portal_image


_publish_returned_file = poller._publish_returned_file


def _transform_link_then_stop(source, destination, transform=None):
    """A poller that stops after linking the transformed file's owned copy, before removing its inbox copy. The files
    it does not transform, before it in the publication's order, it publishes as the poller does."""
    if transform is None:
        return _publish_returned_file(source, destination)
    copy = destination.with_name(".bdp-return-stopped")
    copy.write_bytes(source.read_bytes())
    if transform is not None:
        transform(copy)
    os.link(copy, destination)
    copy.unlink()
    raise OSError("the poller host went down")


def test_a_transformed_file_left_with_its_inbox_copy_is_recognized_by_its_bytes(remote, monkeypatch):
    run, plan, client, incoming, portal_image = _returned_indexing_xml(remote)
    run_directory = Path(plan["run_directory"])
    monkeypatch.setattr(poller, "_publish_returned_file", _transform_link_then_stop)
    assert poll_once(engine=remote.engine, settings=remote.settings, client=client) == []
    published = run_directory / "output.xml"
    assert published.read_text() == f"<step>\n<inputImage>{portal_image}</inputImage>\n</step>\n"
    assert (incoming / "output.xml").is_file()  # published, and its inbox copy left behind

    monkeypatch.undo()
    assert poll_once(engine=remote.engine, settings=remote.settings, client=client) == [run.job_id]
    job = load_job(remote.engine, run.job_id)
    assert job.status == JobStatus.FAILED
    assert job.messages.splitlines()[-1] == "Not all inputs completed successfully"  # the run's verdict, not ours
    assert sorted(path.name for path in incoming.iterdir()) == [".runner.lock", "run.json"]
    assert published.read_text() == f"<step>\n<inputImage>{portal_image}</inputImage>\n</step>\n"
    assert not list(run_directory.glob(".bdp-return-*"))


@pytest.mark.parametrize("foreign", ["the untransformed copy", "another file"])
def test_a_different_file_at_a_transformed_destination_is_refused_and_kept(remote, monkeypatch, foreign):
    """After a restart, an Output XML at its destination that is not exactly this publication's transform of the inbox
    copy (here: the inbox copy's own bytes, or another file) is refused (``FileExistsError``); both files are kept."""
    run, plan, client, incoming, _ = _returned_indexing_xml(remote)
    run_directory = Path(plan["run_directory"])
    monkeypatch.setattr(poller, "_publish_returned_file", _transform_link_then_stop)
    poll_once(engine=remote.engine, settings=remote.settings, client=client)
    monkeypatch.undo()
    returned = (incoming / "output.xml").read_bytes()
    other = returned if foreign == "the untransformed copy" else b"<step>not this publication's</step>\n"
    destination = run_directory / "output.xml"
    destination.unlink()
    destination.write_bytes(other)

    assert poll_once(engine=remote.engine, settings=remote.settings, client=client) == [run.job_id]
    job = load_job(remote.engine, run.job_id)
    assert job.status == JobStatus.FAILED
    assert job.messages.splitlines()[-1] == f"Returned output could not be validated/published: {destination}"
    assert destination.read_bytes() == other
    assert (incoming / "output.xml").read_bytes() == returned
    assert not (run_directory / "run.json").exists()
    assert not list(run_directory.glob(".bdp-return-*"))


def test_a_returned_symlink_whose_destination_exists_is_refused_as_an_existing_destination(remote):
    """The order of the checks: after a restart, an inbox entry that is a symlink and whose destination exists gives
    ``FileExistsError`` (the destination), not the symlink refusal, and is never taken for a published file."""
    run, plan, client = _submit(remote, backend="aps")
    run_directory, incoming = Path(plan["run_directory"]), Path(plan["output_directory"])  # aps: the inbox
    _return(plan, status="Failed", failed=2)
    (run_directory / "extra.log").write_text("the run's own file")
    (incoming / "extra.log").symlink_to(run_directory / "extra.log")  # the same file, through a symlink
    write_json_file(
        run_directory / ".bdp-publication.json", {"submission_id": plan["submission_id"], "files": ["extra.log"]}
    )
    with pytest.raises(FileExistsError) as refused:
        publish_returned(plan)
    assert str(refused.value) == str(run_directory / "extra.log")
    assert (incoming / "extra.log").is_symlink() and (run_directory / "extra.log").read_text() == "the run's own file"

    (incoming / "extra.log").unlink()
    (incoming / "extra.log").symlink_to(run_directory / "inputs.jsonl")
    with pytest.raises(FileExistsError) as refused:
        publish_returned(plan)
    assert str(refused.value) == str(run_directory / "extra.log")
    (run_directory / "extra.log").unlink()
    with pytest.raises(ValueError, match="Returned symlink is not an output"):
        publish_returned(plan)
    assert (incoming / "extra.log").is_symlink() and not (run_directory / "extra.log").exists()


def test_an_artifact_hdf5_cannot_read_is_an_invalid_return(remote):
    """lauelab raises OSError for a file that is not HDF5; the file is readable, so the return is invalid: Failed,
    nothing published, never retried forever."""
    run, plan, client = _submit(remote)
    incoming = _return(plan, status="Failed", failed=2, artifacts={"reconstruction": "reconstruction/scan.h5"})
    (incoming / "reconstruction").mkdir()
    (incoming / "reconstruction" / "scan.h5").write_bytes(b"not an HDF5 file")
    client.status("done")
    assert poll_once(engine=remote.engine, settings=remote.settings, client=client) == [run.job_id]
    job = load_job(remote.engine, run.job_id)
    assert job.status == JobStatus.FAILED
    assert "reconstruction artifact is not readable HDF5" in job.messages.splitlines()[-1]
    run_directory = Path(plan["run_directory"])
    assert not (run_directory / "run.json").exists() and not (run_directory / "reconstruction").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_artifact_the_filesystem_cannot_read_is_retried(remote):
    run, plan, client = _submit(remote)
    incoming = _return(plan, status="Failed", failed=2, artifacts={"reconstruction": "reconstruction/scan.h5"})
    (incoming / "reconstruction").mkdir()
    artifact = incoming / "reconstruction" / "scan.h5"
    artifact.write_bytes(b"not readable now")
    artifact.chmod(0)
    client.status("done")
    try:
        assert poll_once(engine=remote.engine, settings=remote.settings, client=client) == []
        assert load_job(remote.engine, run.job_id).status == JobStatus.QUEUED  # not a verdict on the return
    finally:
        artifact.chmod(0o600)


def _returned_scan(remote):
    """A crux run whose return names a reconstruction artifact, a readable HDF5 file, and DM done."""
    import h5py

    run, plan, client = _submit(remote)
    incoming = _return(plan, status="Failed", failed=2, artifacts={"reconstruction": "reconstruction/scan.h5"})
    (incoming / "reconstruction").mkdir()
    artifact = incoming / "reconstruction" / "scan.h5"
    with h5py.File(artifact, "w") as handle:
        handle["points/source_paths"] = ["a", "b"]
    client.status("done")
    return run, plan, client, artifact


def _assert_retried(remote, run, plan, client, incoming_artifact):
    assert poll_once(engine=remote.engine, settings=remote.settings, client=client) == []
    job = load_job(remote.engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.QUEUED, RunPhase.QUEUED)  # not a verdict on the return
    run_directory = Path(plan["run_directory"])
    assert not (run_directory / "run.json").exists() and not (run_directory / ".bdp-publication.json").exists()
    assert incoming_artifact.exists()


@pytest.mark.parametrize("code", [errno.EAGAIN, errno.ENOLCK, errno.EIO, errno.ESTALE], ids=errno.errorcode.get)
def test_a_filesystem_error_while_hdf5_reads_an_artifact_is_retried(remote, monkeypatch, code):
    """h5py reports the filesystem's errors with an ``errno`` (its format errors have none): transient, retried."""
    from laueportal.science import validation

    run, plan, client, artifact = _returned_scan(remote)

    def failing(path):
        raise OSError(code, f"Unable to synchronously open file ({os.strerror(code)})")

    monkeypatch.setattr(validation, "validate_scan_file", failing)
    _assert_retried(remote, run, plan, client, artifact)


def test_an_artifact_another_process_holds_an_hdf5_lock_on_is_retried(remote):
    """Another process has the returned file open for writing (HDF5's file lock): h5py fails with ``EAGAIN``."""
    run, plan, client, artifact = _returned_scan(remote)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, h5py; handle = h5py.File(sys.argv[1], 'r+'); print('locked', flush=True); sys.stdin.read()",
            str(artifact),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        import h5py

        try:
            h5py.File(artifact, "r").close()
        except OSError as error:
            assert error.errno == errno.EAGAIN
        else:
            pytest.skip("HDF5 file locking is disabled here (HDF5_USE_FILE_LOCKING)")
        _assert_retried(remote, run, plan, client, artifact)
    finally:
        holder.communicate("", timeout=30)


class _NoDMJob(FakeDM):
    """DM never acted on the submission: its reply was lost, and it has no job for the submission ID."""

    def submit(self, plan):
        self.submissions.append(plan)
        raise TimeoutError("DM's reply was lost")

    def find(self, submission_id):
        return []


def _stopped_ago(engine, job_id, seconds):
    with Session(engine) as session, session.begin():
        job = session.get(Job, job_id)
        assert job is not None and job.cancel_requested_at is not None
        job.cancel_requested_at = utcnow() - timedelta(seconds=seconds)


def test_a_submission_dm_has_no_job_for_is_reported_and_a_stop_cancels_it_after_the_grace(remote, caplog):
    run = create_wire_run(remote.engine, remote.layout, count=2, backend="crux")
    client = _NoDMJob()
    with pytest.raises(TimeoutError):
        submission.submit_run(run.job_id, engine=remote.engine, settings=remote.settings, client=client)
    job = load_job(remote.engine, run.job_id)
    assert job.external_state == "submission_uncertain"

    caplog.clear()
    for _ in range(2):
        assert poll_once(engine=remote.engine, settings=remote.settings, client=client) == []
    warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 2
    assert all(f"Remote job {run.job_id}: DM has no job for submission {job.submission_id}" in w for w in warnings)

    assert controls.cancel_run(run.job_id, engine=remote.engine).state == CancelState.REQUESTED
    _stopped_ago(remote.engine, run.job_id, poller.UNSUBMITTED_STOP_GRACE_SECONDS - 60)
    assert poll_once(engine=remote.engine, settings=remote.settings, client=client) == []
    assert load_job(remote.engine, run.job_id).status == JobStatus.QUEUED
    slow_dm = remote.settings.model_copy(
        update={"dm": remote.settings.dm.model_copy(update={"timeout_seconds": poller.UNSUBMITTED_STOP_GRACE_SECONDS})}
    )
    _stopped_ago(remote.engine, run.job_id, poller.UNSUBMITTED_STOP_GRACE_SECONDS + 60)
    assert poll_once(engine=remote.engine, settings=slow_dm, client=client) == []  # grace: twice the DM timeout

    assert poll_once(engine=remote.engine, settings=remote.settings, client=client) == [run.job_id]
    job = load_job(remote.engine, run.job_id)
    assert (job.status, job.phase, job.n_not_run) == (JobStatus.CANCELLED, RunPhase.CANCELLED, 2)
    assert job.messages.splitlines()[-1].startswith(f"DM has no job for submission {job.submission_id}")
    assert client.submissions and len(client.submissions) == 1 and client.cancels == []


def test_without_a_dm_timeout_a_submission_dm_has_no_job_for_is_never_cancelled(remote, caplog):
    """``dm.timeout_seconds: null``: nothing bounds DM's answer to a submission, so no grace period is safe."""
    run = create_wire_run(remote.engine, remote.layout, count=2, backend="crux")
    client = _NoDMJob()
    no_timeout = remote.settings.model_copy(
        update={"dm": remote.settings.dm.model_copy(update={"timeout_seconds": None})}
    )
    with pytest.raises(TimeoutError):
        submission.submit_run(run.job_id, engine=remote.engine, settings=no_timeout, client=client)
    assert controls.cancel_run(run.job_id, engine=remote.engine).state == CancelState.REQUESTED
    _stopped_ago(remote.engine, run.job_id, 30 * 24 * 3600)
    caplog.clear()
    assert poll_once(engine=remote.engine, settings=no_timeout, client=client) == []
    job = load_job(remote.engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.QUEUED, RunPhase.QUEUED)
    warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1 and "dm.timeout_seconds is null" in warnings[0] and "stopped" in warnings[0]


def test_a_remote_job_without_a_run_directory_is_skipped_and_reported(remote, caplog):
    run, _plan, client = _submit(remote)
    with Session(remote.engine) as session, session.begin():
        job = session.get(Job, run.job_id)
        assert job is not None
        job.run_directory = None
    caplog.clear()
    assert poll_once(engine=remote.engine, settings=remote.settings, client=client) == []
    assert client.gets == []
    assert any(f"Remote job {run.job_id} has no run directory" in r.getMessage() for r in caplog.records)


class _Clock:
    """A monotonic clock the test moves."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _loop(remote, client, clock, monkeypatch) -> poller.RemotePoller:
    """The worker's poller thread's passes (``RemotePoller.cycle``) with ``client`` and ``clock``; no inbox listing."""
    monkeypatch.setattr(poller, "clean_up", lambda **_: [])
    return poller.RemotePoller(
        remote.settings, engine=remote.engine, stopped=threading.Event(), client=client, clock=clock
    )


def _warnings(caplog) -> list[str]:
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR or record.exc_info]
    return [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]


def test_a_run_whose_folder_is_gone_waits_logged_once_then_ends_failed_and_leaves_dm_s_call(
    remote, monkeypatch, caplog
):
    """A submitted run whose plan (``bdp.json``) is missing while its runs root answers (its folder deleted, moved or
    never written) is skipped, logged once, never a traceback; after ``MISSING_PLAN_SECONDS`` it ends Failed with the
    reason, its Stop with it (a Stop cannot be delivered without the folder), and DM's call no longer asks for it."""
    run, plan, client = _submit(remote)
    clock = _Clock()
    loop = _loop(remote, client, clock, monkeypatch)
    client.status("running")
    loop.cycle()
    assert load_job(remote.engine, run.job_id).status == JobStatus.RUNNING
    assert controls.cancel_run(run.job_id, engine=remote.engine).state == CancelState.REQUESTED
    shutil.rmtree(plan["run_directory"])

    caplog.clear()
    for _ in range(3):
        loop.cycle()
        clock.now += (poller.MISSING_PLAN_SECONDS - 1) / 3
    loop.cycle()
    job = load_job(remote.engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.RUNNING, RunPhase.RUNNING)
    assert client.cancels == []
    (warning,) = _warnings(caplog)
    assert warning.startswith(f"Remote job {run.job_id}: its plan {plan['run_directory']}/bdp.json is missing")

    clock.now += 1
    loop.cycle()
    job = load_job(remote.engine, run.job_id)
    assert (job.status, job.phase, job.n_not_run) == (JobStatus.FAILED, RunPhase.FAILED, 2)
    assert job.messages.splitlines()[-1] == (
        f"Run folder missing: its plan (bdp.json) was not found for {poller.MISSING_PLAN_SECONDS:.0f} s, so the run "
        "can be neither published nor stopped through it; DM job dm-001 is no longer followed (cancel it in DM if it "
        "still runs)"
    )
    assert _warnings(caplog)[1:] == [f"Remote job {run.job_id}: its plan stayed missing; ended Failed"]
    gets = len(client.gets)
    loop.cycle()
    assert client.gets[gets:] == []


def test_a_runs_root_that_does_not_answer_never_ends_a_run_and_restarts_the_count(remote, monkeypatch, caplog):
    """A plan not found while the runs root itself does not answer (an NFS mount down) is a transient error: the run
    waits, logged once, however long, and a count begun before starts again once the runs root answers; a plan read
    again ends the count."""
    run, plan, client = _submit(remote)
    clock = _Clock()
    loop = _loop(remote, client, clock, monkeypatch)
    plan_file = Path(plan["run_directory"]) / "bdp.json"
    kept = plan_file.read_bytes()
    experiment = remote.layout.experiment
    away = experiment.with_name("LauePortal.unmounted")

    caplog.clear()
    plan_file.unlink()
    loop.cycle()  # missing, the runs root answering: counted from here
    clock.now += poller.MISSING_PLAN_SECONDS / 2
    experiment.rename(away)
    for _ in range(3):
        loop.cycle()
        clock.now += poller.MISSING_PLAN_SECONDS
    assert load_job(remote.engine, run.job_id).status == JobStatus.QUEUED
    missing, unanswered = _warnings(caplog)
    assert missing.startswith(f"Remote job {run.job_id}: its plan {plan_file} is missing")
    assert unanswered.startswith(f"Remote job {run.job_id}: its plan {plan_file} cannot be read (the runs root ")
    assert "does not answer" in unanswered

    away.rename(experiment)
    loop.cycle()
    clock.now += poller.MISSING_PLAN_SECONDS - 1
    loop.cycle()
    assert load_job(remote.engine, run.job_id).status == JobStatus.QUEUED  # counted again from the runs root's return

    plan_file.write_bytes(kept)
    client.status("running")
    loop.cycle()
    assert load_job(remote.engine, run.job_id).status == JobStatus.RUNNING
    plan_file.unlink()
    loop.cycle()
    clock.now += poller.MISSING_PLAN_SECONDS - 1
    loop.cycle()
    assert load_job(remote.engine, run.job_id).status == JobStatus.RUNNING  # the plan read again ended the count
    clock.now += 1
    loop.cycle()
    assert load_job(remote.engine, run.job_id).status == JobStatus.FAILED


def _unparsable(plan_file: Path) -> None:
    plan_file.write_text("{ not json")


def _a_list(plan_file: Path) -> None:
    plan_file.write_text("[]")


def _a_folder(plan_file: Path) -> None:
    plan_file.unlink()
    plan_file.mkdir()


@pytest.mark.parametrize("damage", [_unparsable, _a_list, _a_folder])
def test_a_plan_that_is_there_but_cannot_be_read_waits_logged_once_then_ends_failed(
    remote, monkeypatch, caplog, damage
):
    """A plan (``bdp.json``) that is there but is not JSON, not a JSON object, or cannot be read (a folder in its
    place), its runs root answering, is treated as a missing one: skipped and left out of DM's call, logged once, never
    a traceback each pass, and Failed after ``MISSING_PLAN_SECONDS`` with the reason."""
    run, plan, client = _submit(remote)
    clock = _Clock()
    loop = _loop(remote, client, clock, monkeypatch)
    loop.cycle()
    plan_file = Path(plan["run_directory"]) / "bdp.json"
    damage(plan_file)

    caplog.clear()
    calls = len(client.fetches)
    for _ in range(3):
        loop.cycle()
        clock.now += (poller.MISSING_PLAN_SECONDS - 1) / 3
    loop.cycle()
    assert load_job(remote.engine, run.job_id).status == JobStatus.QUEUED
    assert len(client.fetches) == calls  # its only run skipped: no DM call
    (warning,) = _warnings(caplog)
    assert warning.startswith(f"Remote job {run.job_id}: its plan {plan_file} cannot be read (")
    assert warning.endswith(f"; it ends Failed if still unreadable in {poller.MISSING_PLAN_SECONDS:.0f} s")

    clock.now += 1
    loop.cycle()
    job = load_job(remote.engine, run.job_id)
    assert (job.status, job.phase, job.n_not_run) == (JobStatus.FAILED, RunPhase.FAILED, 2)
    message = job.messages.splitlines()[-1]
    assert message.startswith("Run plan unreadable: its plan (bdp.json) could not be read (")
    assert message.endswith(
        f") for {poller.MISSING_PLAN_SECONDS:.0f} s, so the run can be neither published nor stopped through it; DM "
        "job dm-001 is no longer followed (cancel it in DM if it still runs)"
    )
    assert _warnings(caplog)[1:] == [f"Remote job {run.job_id}: its plan stayed unreadable; ended Failed"]


class _Numbered(FakeDM):
    """DM's jobs ``dm-001``, ``dm-002``, ... one per submission, all running."""

    def submit(self, plan):
        self.submissions.append(plan)
        job = {"id": f"dm-{len(self.submissions):03d}", "status": "running"}
        self.jobs[job["id"]] = job
        return job


def test_a_run_whose_plan_is_not_read_is_left_out_of_dm_s_call_until_it_is_read_again(remote, monkeypatch):
    """The pass's one DM call asks only for the runs whose plan it read; with none, it makes no call; a run whose plan
    comes back is in the next call again."""
    client = _Numbered()
    first, second = (
        create_wire_run(remote.engine, remote.layout, count=2, backend="crux", name=name) for name in ("one", "two")
    )
    submission.submit_run(first.job_id, engine=remote.engine, settings=remote.settings, client=client)
    loop = _loop(remote, client, _Clock(), monkeypatch)
    plan_file = Path(str(load_job(remote.engine, first.job_id).run_directory)) / "bdp.json"
    kept = plan_file.read_bytes()

    plan_file.unlink()
    loop.cycle()
    assert getattr(client, "fetches", []) == []

    submission.submit_run(second.job_id, engine=remote.engine, settings=remote.settings, client=client)
    loop.cycle()
    assert client.fetches == [(["dm-002"], [])]

    plan_file.write_bytes(kept)
    loop.cycle()
    assert client.fetches[1:] == [(["dm-001", "dm-002"], [])]
    assert load_job(remote.engine, first.job_id).status == JobStatus.RUNNING


class _SeveralDMJobs(_NoDMJob):
    """DM has several jobs for the submission (``found``)."""

    def __init__(self):
        super().__init__()
        self.found = [{"id": "dm-a", "status": "running"}, {"id": "dm-b", "status": "running"}]

    def find(self, submission_id):
        return list(self.found)


def test_a_submission_dm_has_several_jobs_for_is_logged_once_per_change_and_left_as_it_is(remote, monkeypatch, caplog):
    """The problem "Multiple DM jobs have the same submission identity" leaves the run as it is for manual
    reconciliation, logged once per change of those jobs, never a traceback each pass."""
    run = create_wire_run(remote.engine, remote.layout, count=2, backend="crux")
    client = _SeveralDMJobs()
    with pytest.raises(TimeoutError):
        submission.submit_run(run.job_id, engine=remote.engine, settings=remote.settings, client=client)
    loop = _loop(remote, client, _Clock(), monkeypatch)

    caplog.clear()
    for _ in range(3):
        loop.cycle()
    client.found.append({"id": "dm-c", "status": "running"})
    for _ in range(2):
        loop.cycle()

    job = load_job(remote.engine, run.job_id)
    assert (job.status, job.external_id, job.external_state) == (JobStatus.QUEUED, None, "submission_uncertain")
    assert not [record for record in caplog.records if record.exc_info]
    assert [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR] == [
        f"Remote job {run.job_id}: Multiple DM jobs have the same submission identity ({ids}); manual "
        "reconciliation required"
        for ids in ("dm-a, dm-b", "dm-a, dm-b, dm-c")
    ]


def test_a_publication_of_another_submission_is_refused(remote):
    run, plan, client = _submit(remote)
    _return(plan, status="Cancelled")
    run_directory = Path(plan["run_directory"])
    write_json_file(run_directory / ".bdp-publication.json", {"submission_id": "another", "files": []})
    client.status("done")
    poll_once(engine=remote.engine, settings=remote.settings, client=client)
    job = load_job(remote.engine, run.job_id)
    assert job.status == JobStatus.FAILED
    assert "a different publication" in job.messages
    assert not (run_directory / "run.json").exists() and not (run_directory / "progress.json").exists()


def test_disabled_remote_execution_polls_nothing(remote):
    _run, _plan, client = _submit(remote)
    client.status("done")
    assert poll_once(engine=remote.engine, settings=None, client=client) == []
    disabled = remote.settings.model_copy(update={"enabled": False})
    assert poll_once(engine=remote.engine, settings=disabled, client=client) == []


def test_only_one_poller_holds_the_lock(remote):
    """``run_poller`` (``remote poll --once``) makes its one pass under the lock, refused while another poller holds it
    (the worker's thread, or another ``remote poll --once``), and lets it go after the pass."""
    lock_path = Path(remote.settings.submission_directory) / ".poller.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(PollerBusyError, match="another remote poller holds"):
            run_poller(remote.settings, engine=remote.engine, client=FakeDM())
    locked = []
    run_poller(remote.settings, engine=remote.engine, client=FakeDM(), on_locked=locked.append)
    assert locked == [lock_path]
    with open(lock_path, "a") as again:
        fcntl.flock(again, fcntl.LOCK_EX | fcntl.LOCK_NB)  # let go after the pass


def test_the_loop_polls_until_it_is_stopped(remote, monkeypatch):
    """The loop is the worker's thread (``RemotePoller.run_forever``): a pass every ``poll_seconds`` until the stop; its
    back-offs, lock and stop: ``test_poller_thread.py``."""
    passes = []
    monkeypatch.setattr(poller, "poll", lambda **kwargs: passes.append(kwargs) or poller.Pass([], 0, False))
    stop = threading.Event()
    settings = remote.settings.model_copy(update={"poll_seconds": 0.01})
    loop = poller.RemotePoller(settings, engine=remote.engine, stopped=stop)
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    deadline = time.monotonic() + 10
    while len(passes) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=10)
    loop.close()
    assert not thread.is_alive() and len(passes) >= 3


# --- what a published run leaves behind (remote.cleanup) ------------------------------------------------


def _cleanup_settings(remote, workflow="laue-portal-bdp-cleanup"):
    return remote_settings({**remote.values, "dm": {**remote.values["dm"], "cleanup_workflow": workflow}})


def _published(remote, settings, *, backend="crux", status="Finished", name="wire"):
    """A remote run returned, published and finalized by the poller with ``status`` (Finished: its reconstruction
    published, lauelab's validation of the catalog stubbed; Failed and Cancelled: the MPI runner's ``ranks.json``
    with them); what it left: the return inbox's remnants, the DM inbox copy."""
    from laueportal.science import validation

    run, plan, client = _submit(remote, backend=backend, settings=settings, name=name)
    if status == "Finished":
        incoming = _return(plan, status=status, succeeded=2, artifacts={"reconstruction": "reconstruction/scan.h5"})
        (incoming / "reconstruction").mkdir()
        (incoming / "reconstruction" / "scan.h5").write_bytes(b"catalog")
    else:
        failed = 2 if status == "Failed" else 0
        _return(plan, status=status, failed=failed, message="At least one MPI rank failed", extra=("ranks.json",))
    client.status("done")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(validation, "validate_scan_file", lambda _path: SimpleNamespace(n_points=2, n_complete=2))
        assert poll_once(engine=remote.engine, settings=settings, client=client) == [run.job_id]
    assert load_job(remote.engine, run.job_id).status == JobStatus[status.upper()]
    incoming = Path(plan["run_directory"]) / "incoming" / plan["submission_id"]
    assert sorted(path.name for path in incoming.iterdir()) == [".runner.lock", "run.json"]
    assert submission_path(settings, plan["submission_id"]).is_file()
    return run, plan, client


@pytest.mark.parametrize("backend", ["crux", "aps"])
def test_a_published_runs_leftovers_go_once_the_launchers_cleanup_is_done(remote, backend):
    settings = _cleanup_settings(remote)
    _run, plan, client = _published(remote, settings, backend=backend)
    run_directory, submission_id = Path(plan["run_directory"]), plan["submission_id"]
    inbox_copy = submission_path(settings, submission_id)
    published = output_listing(run_directory)

    assert cleanup.clean_up(engine=remote.engine, settings=settings, client=client) == []
    assert not (run_directory / "incoming").exists()  # the remnants, and incoming/ with them
    assert client.cleanups == [submission_id]  # the launcher's stage: the Eagle folder and its state file
    assert json.loads((run_directory / ".cleanup-job.json").read_text()) == {"id": "cleanup-1"}
    assert inbox_copy.is_file()  # the launcher reads it

    assert cleanup.clean_up(engine=remote.engine, settings=settings, client=client) == []  # DM job running
    client.jobs["cleanup-1"]["status"] = "done"
    assert cleanup.clean_up(engine=remote.engine, settings=settings, client=client) == [submission_id]
    assert not inbox_copy.exists() and client.cleanups == [submission_id]

    gets = len(client.gets)
    assert cleanup.clean_up(engine=remote.engine, settings=settings, client=client) == []
    assert len(client.gets) == gets
    assert [name for name in output_listing(run_directory) if not name.startswith(".cleanup-job")] == [
        name for name in published if not name.startswith("incoming")
    ]


def test_without_a_cleanup_workflow_only_the_return_inbox_remnants_go(remote, caplog):
    """Without ``dm.cleanup_workflow`` (a launcher with no ``cleanup`` stage), the inbox copy stays for a later clean-up
    to read. A run whose remnants are gone or kept is not looked at again by the process, and its warning is logged
    once."""
    _run, plan, client = _published(remote, remote.settings)
    _kept_run, kept, _ = _published(remote, remote.settings, name="kept")
    (Path(kept["incoming_directory"]) / "late.log").write_text("written after the publication")
    calls = (len(client.gets), len(client.submissions))
    state = cleanup.CleanupState()
    for _ in range(3):
        assert cleanup.clean_up(engine=remote.engine, settings=remote.settings, client=client, state=state) == []
    assert not (Path(plan["run_directory"]) / "incoming").exists()
    assert submission_path(remote.settings, plan["submission_id"]).is_file()
    assert (len(client.gets), len(client.submissions), client.cleanups) == (*calls, [])
    assert state.settled == {plan["submission_id"], kept["submission_id"]}
    assert caplog.text.count("still holds ['late.log']; kept") == 1
    assert caplog.text.count("dm.cleanup_workflow is not set") == 1


@pytest.mark.parametrize("status", ["Failed", "Cancelled"])
def test_a_published_run_that_did_not_finish_keeps_everything(remote, status):
    """A Failed or Cancelled run (an MPI run's ranks' results exist only in its Eagle folder's ``shards/``) keeps its
    return inbox's remnants, its inbox copy, and so its Eagle folder and state file: no DM call."""
    settings = _cleanup_settings(remote)
    _run, plan, client = _published(remote, settings, status=status)
    run_directory, submission_id = Path(plan["run_directory"]), plan["submission_id"]
    assert (run_directory / "ranks.json").is_file()
    calls = (len(client.gets), len(client.submissions))

    assert cleanup.clean_up(engine=remote.engine, settings=settings, client=client) == []
    incoming = run_directory / "incoming" / submission_id
    assert sorted(path.name for path in incoming.iterdir()) == [".runner.lock", "run.json"]
    assert submission_path(settings, submission_id).is_file() and not (run_directory / ".cleanup-job.json").exists()
    assert (len(client.gets), len(client.submissions), client.cleanups) == (*calls, [])


def test_a_run_that_was_not_published_and_a_submission_of_another_portal_keep_everything(remote):
    settings = _cleanup_settings(remote)
    run, plan, client = _submit(remote, settings=settings)
    incoming = Path(plan["incoming_directory"])
    incoming.mkdir(parents=True)
    (incoming / "partial.h5").write_text("partial")
    client.status("failed")  # no return: Failed, kept for diagnosis
    assert poll_once(engine=remote.engine, settings=settings, client=client) == [run.job_id]
    foreign = submission_path(settings, "0b5c6a2e-1d3f-4a5b-8c7d-9e0f1a2b3c4d")  # another portal's, beside this one
    foreign.write_text("{}")

    assert cleanup.clean_up(engine=remote.engine, settings=settings, client=client) == []
    assert (incoming / "partial.h5").is_file() and foreign.is_file()
    assert submission_path(settings, plan["submission_id"]).is_file() and client.cleanups == []


def test_a_return_inbox_holding_more_than_publication_leaves_is_kept(remote, caplog):
    settings = _cleanup_settings(remote)
    _run, plan, client = _published(remote, settings)
    incoming = Path(plan["incoming_directory"])
    (incoming / "late.log").write_text("written after the publication")

    assert cleanup.clean_up(engine=remote.engine, settings=settings, client=client) == []
    assert (incoming / "late.log").is_file() and (incoming / "run.json").is_file()
    assert "still holds ['late.log']; kept" in caplog.text
    assert client.cleanups == [plan["submission_id"]]  # the run is published: the launcher's part goes on


def test_the_return_inbox_removal_never_leaves_the_run_directory(tmp_path):
    """A symbolic ``incoming/`` or ``<submission>`` leading elsewhere is kept, and so is everything where it leads; a
    link among the remnants is removed, not what it names."""
    submission_id = "6f1c2a4e-3b5d-4e7f-8a9b-0c1d2e3f4a5b"
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / submission_id).mkdir(parents=True)
    (elsewhere / submission_id / "run.json").write_text("{}")
    (elsewhere / "sibling.h5").write_text("data")
    linked_incoming = tmp_path / "run1"
    linked_incoming.mkdir()
    (linked_incoming / "incoming").symlink_to(elsewhere)
    linked_inbox = tmp_path / "run2"
    (linked_inbox / "incoming").mkdir(parents=True)
    (linked_inbox / "incoming" / submission_id).symlink_to(elsewhere / submission_id)
    assert (
        cleanup.remove_inbox_remnants(linked_incoming, submission_id)
        == f"{linked_incoming}/incoming is a symbolic link"
    )
    assert cleanup.remove_inbox_remnants(linked_inbox, submission_id) == (
        f"{linked_inbox}/incoming/{submission_id} is not a directory"
    )
    assert (elsewhere / submission_id / "run.json").is_file() and (elsewhere / "sibling.h5").is_file()

    run = tmp_path / "run3"
    (run / "incoming" / submission_id).mkdir(parents=True)
    (run / "incoming" / submission_id / "run.json").symlink_to(elsewhere / submission_id / "run.json")
    assert cleanup.remove_inbox_remnants(run, submission_id) is None
    assert not (run / "incoming").exists() and (elsewhere / submission_id / "run.json").is_file()


def test_a_failed_cleanup_job_is_reported_never_resubmitted_until_its_record_goes(remote, caplog):
    """DM's verdict is kept in the record: the failed job is reported once per poller process and never looked up
    again."""
    settings = _cleanup_settings(remote)
    _run, plan, client = _published(remote, settings)
    record = Path(plan["run_directory"]) / ".cleanup-job.json"
    state = cleanup.CleanupState()
    cleanup.clean_up(engine=remote.engine, settings=settings, client=client, state=state)
    client.jobs["cleanup-1"]["status"] = "failed"
    caplog.clear()
    client.gets.clear()
    for _ in range(3):
        assert cleanup.clean_up(engine=remote.engine, settings=settings, client=client, state=state) == []
    assert client.cleanups == [plan["submission_id"]] and client.gets == ["cleanup-1"]
    assert json.loads(record.read_text()) == {"id": "cleanup-1", "status": "failed"}
    assert caplog.text.count("clean-up DM job cleanup-1 failed") == 1
    assert submission_path(settings, plan["submission_id"]).is_file()

    record.unlink()
    cleanup.clean_up(engine=remote.engine, settings=settings, client=client, state=state)
    assert len(client.cleanups) == 2


def _submission(plan) -> str:
    return plan["submission_id"]


def test_failed_cleanups_cannot_use_up_a_pass_s_dm_calls(remote, caplog):
    """A run whose DM calls fail is retried at the next pass, then backs off (1, 3, 7, ... passes skipped), and one
    whose clean-up job failed makes no DM call: the other runs get the pass's calls (here one a pass)."""
    settings = _cleanup_settings(remote)
    runs = sorted((_published(remote, settings, name=name)[1] for name in ("one", "two")), key=_submission)
    first, second = (plan["submission_id"] for plan in runs)

    class FirstFails(FakeDM):
        def clean_up(self, submission_id):
            if submission_id == first:  # e.g. DM refusing the job (the real client's CalledProcessError)
                self.cleanups.append(submission_id)
                raise subprocess.CalledProcessError(1, ["dm-submit-job"], "", "refused")
            return super().clean_up(submission_id)

    client, state = FirstFails(), cleanup.CleanupState()
    pass_ = partial(cleanup.clean_up, engine=remote.engine, settings=settings, client=client, calls=1, state=state)
    for _ in range(5):
        pass_()
    # The first fails at passes 1 and 2, is skipped at 3, fails at 4 and is skipped at 5 (and 6 and 7).
    job = json.loads((Path(runs[1]["run_directory"]) / ".cleanup-job.json").read_text())["id"]
    assert (client.cleanups, client.gets) == ([first, first, second, first], [job])
    assert not (Path(runs[0]["run_directory"]) / ".cleanup-job.json").exists()

    client.jobs[job]["status"] = "failed"
    for _ in range(2):
        pass_()
    assert (client.cleanups, client.gets) == ([first, first, second, first], [job, job])
    assert caplog.text.count("Could not clean up after remote run") == 3
    assert caplog.text.count(f"clean-up DM job {job} failed") == 1


def test_a_cleanup_record_that_cannot_be_written_submits_no_job_every_pass(remote, monkeypatch, caplog):
    """The record says ``submitting`` before DM is asked, so a record that cannot be written submits nothing; one whose
    job ID could not be recorded is submitted again at most ``SUBMISSION_ATTEMPTS`` times."""
    settings = _cleanup_settings(remote)
    _run, plan, client = _published(remote, settings)
    record = Path(plan["run_directory"]) / ".cleanup-job.json"
    state = cleanup.CleanupState()
    write_json_file_ = cleanup.write_json_file

    def unwritable(path, payload, **kwargs):
        raise OSError(errno.EROFS, "Read-only file system", str(path))

    monkeypatch.setattr(cleanup, "write_json_file", unwritable)
    for _ in range(4):
        cleanup.clean_up(engine=remote.engine, settings=settings, client=client, state=state)
        state.skipping.clear()  # no back-off here: every pass tries
    assert client.cleanups == [] and not record.exists()

    def only_submitting(path, payload, **kwargs):  # DM is asked, then its job's ID cannot be recorded
        if payload.get("status") != "submitting":
            raise OSError(errno.EIO, "Input/output error", str(path))
        return write_json_file_(path, payload, **kwargs)

    monkeypatch.setattr(cleanup, "write_json_file", only_submitting)
    for _ in range(6):
        cleanup.clean_up(engine=remote.engine, settings=settings, client=client, state=state)
        state.skipping.clear()
    assert len(client.cleanups) == cleanup.SUBMISSION_ATTEMPTS == 3
    assert json.loads(record.read_text()) == {"status": "submitting", "attempts": 3}
    assert caplog.text.count("submitted 3 times without its DM job ID being recorded") == 1


def test_a_dm_call_that_fails_is_retried_at_the_next_pass(remote):
    """The real client's ``_call`` raises ``CalledProcessError`` (e.g. a workflow DM does not know) and
    ``TimeoutExpired``: a submission DM refused leaves no record; one DM did not answer in time (it may have accepted
    the job) keeps its ``submitting`` record, so it counts towards ``SUBMISSION_ATTEMPTS``; a look-up that failed leaves
    the record as it was."""
    settings = _cleanup_settings(remote)
    _run, plan, client = _published(remote, settings)
    record = Path(plan["run_directory"]) / ".cleanup-job.json"
    state = cleanup.CleanupState()
    client.failing["clean_up"] = subprocess.CalledProcessError(1, ["dm-submit-job"], "", "unknown workflow")
    assert cleanup.clean_up(engine=remote.engine, settings=settings, client=client, state=state) == []
    assert not record.exists() and client.cleanups == [plan["submission_id"]]
    client.failing["clean_up"] = subprocess.TimeoutExpired(["dm-submit-job"], 30)
    cleanup.clean_up(engine=remote.engine, settings=settings, client=client, state=state)
    assert json.loads(record.read_text()) == {"status": "submitting", "attempts": 1}
    state.skipping.clear()  # past its back-off
    cleanup.clean_up(engine=remote.engine, settings=settings, client=client, state=state)
    assert json.loads(record.read_text()) == {"id": "cleanup-3"}
    client.jobs["cleanup-3"]["status"] = "done"
    client.failing["get"] = subprocess.TimeoutExpired(["dm-get-processing-job"], 30)
    assert cleanup.clean_up(engine=remote.engine, settings=settings, client=client, state=state) == []
    assert json.loads(record.read_text()) == {"id": "cleanup-3"}
    assert submission_path(settings, plan["submission_id"]).is_file()
    assert cleanup.clean_up(engine=remote.engine, settings=settings, client=client, state=state) == [
        plan["submission_id"]
    ]


def test_a_dm_that_never_answers_a_submission_in_time_gets_at_most_three(remote, caplog):
    """DM accepts each clean-up job but answers too late (``TimeoutExpired``). Each attempt keeps its record, so the run
    gets ``SUBMISSION_ATTEMPTS`` jobs, then a warning, not one more at every pass."""
    settings = _cleanup_settings(remote)
    _run, plan, client = _published(remote, settings)
    record = Path(plan["run_directory"]) / ".cleanup-job.json"
    state = cleanup.CleanupState()
    for _ in range(6):
        client.failing["clean_up"] = subprocess.TimeoutExpired(["dm-submit-job"], 30)
        cleanup.clean_up(engine=remote.engine, settings=settings, client=client, state=state)
        state.skipping.clear()  # no back-off here: every pass tries
    assert len(client.cleanups) == cleanup.SUBMISSION_ATTEMPTS == 3
    assert json.loads(record.read_text()) == {"status": "submitting", "attempts": 3}
    assert caplog.text.count("submitted 3 times without its DM job ID being recorded") == 1


def test_a_pass_makes_at_most_its_share_of_dm_calls_and_the_loop_cleans_up(remote):
    settings = _cleanup_settings(remote)
    _, plan, client = _published(remote, settings)
    assert cleanup.clean_up(engine=remote.engine, settings=settings, client=client, calls=0) == []
    assert client.cleanups == [] and not (Path(plan["run_directory"]) / "incoming").exists()
    # A pass of `remote poll --once` cleans up, as the loop's first pass does (then every CLEANUP_SECONDS).
    run_poller(settings, engine=remote.engine, client=client)
    assert client.cleanups == [plan["submission_id"]]


# --- laueportal remote poll ---------------------------------------------------------------------------


def _portal(tmp_path, *, remote_enabled: bool, runs_root: Path | None = None) -> tuple[dict[str, str], Any]:
    """A settings file (with a database at head) for ``laueportal remote poll`` in a fresh interpreter."""
    layout = Layout(tmp_path)
    settings, engine = make_database(tmp_path, files={"runs_root": layout.experiment})
    engine.dispose()
    bdp = tmp_path / "bdp.yaml"
    bdp.write_text(yaml.safe_dump(bdp_settings(layout)), encoding="utf-8")
    config = tmp_path / "portal.toml"
    config.write_text(
        f'[database]\npath = "{settings.database.path}"\n'
        f'[files]\nruns_root = "{runs_root or layout.experiment}"\n'
        f'[remote]\nenabled = {"true" if remote_enabled else "false"}\nsettings_file = "{bdp}"\n',
        encoding="utf-8",
    )
    return fresh_interpreter_env(config), layout


def _poll(env, *arguments, cwd):
    return subprocess.run(
        [sys.executable, "-m", "laueportal", "remote", "poll", *arguments],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_the_command_does_nothing_when_remote_execution_is_disabled(tmp_path):
    env, _ = _portal(tmp_path, remote_enabled=False)
    result = _poll(env, "--once", cwd=tmp_path)
    assert result.returncode == 0 and "remote execution is disabled" in result.stderr


def test_the_command_makes_one_pass_only_the_worker_s_thread_is_the_loop(tmp_path):
    """The loop is the worker's poller thread; ``remote poll`` without ``--once`` (a looping form, which would be a
    second poller beside the worker) is a usage error naming ``--once``, whatever the settings."""
    for enabled in (False, True):
        env, _ = _portal(tmp_path / str(enabled), remote_enabled=enabled)
        result = _poll(env, cwd=tmp_path)
        assert result.returncode == 2 and "--once" in result.stderr, result.stderr


def test_the_command_refuses_an_experiment_other_than_the_runs_root(tmp_path):
    env, _ = _portal(tmp_path, remote_enabled=True, runs_root=tmp_path / "other-runs")
    result = _poll(env, "--once", cwd=tmp_path)
    assert result.returncode == 2 and "files.runs_root" in result.stderr


def test_the_command_never_creates_a_missing_database(tmp_path):
    env, _ = _portal(tmp_path, remote_enabled=True)
    database = tmp_path / "laueportal.db"
    database.unlink()
    result = _poll(env, "--once", cwd=tmp_path)
    assert result.returncode == 2 and "does not exist" in result.stderr
    assert not database.exists()


def test_the_command_polls_once_and_refuses_a_second_poller(tmp_path):
    env, layout = _portal(tmp_path, remote_enabled=True)
    result = _poll(env, "--once", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    lock_path = layout.submissions / ".poller.lock"
    with open(lock_path, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _poll(env, "--once", cwd=tmp_path)
    assert result.returncode == 1 and "another remote poller holds" in result.stderr
