"""DM submission (``remote.submission.submit_run``) against a stop that arrives meanwhile, and its refusals.

The tests of no repeat and of a lost reply are in ``test_bdp.py``. A stop and a submission never interleave
(``core.db.begin_write``): the stop either comes first, and the run is Cancelled with DM never asked, or it finds the
recorded submission and the poller delivers it. A run never ends Cancelled with a live DM job.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session

from laueportal.core.timeutil import utcnow
from laueportal.jobs import controls, reconciler
from laueportal.jobs.models import Job
from laueportal.jobs.schemas import CancelState
from laueportal.jobs.state import ExecutionStateError, JobStatus, RunPhase
from laueportal.remote import plan as remote_plan
from laueportal.remote import poller, submission
from tests.remote_support import BatchedFake, Layout, bdp_settings, create_wire_run, remote_settings
from tests.run_support import load_job, make_engine


class FakeDM(BatchedFake):
    def __init__(self):
        self.jobs: dict[str, dict[str, Any]] = {}
        self.submissions: list[dict[str, Any]] = []
        self.cancels: list[str] = []
        self.on_submit = None
        self.reply_lost = False

    def submit(self, plan):
        if self.on_submit is not None:
            self.on_submit()
        self.submissions.append(plan)
        if self.reply_lost:
            raise TimeoutError("DM's reply was lost")
        return self.jobs.setdefault("dm-001", {"id": "dm-001", "status": "pending"})

    def get(self, external_id):
        return self.jobs[external_id]

    def find(self, submission_id):
        return [self.jobs["dm-001"]] if "dm-001" in self.jobs else []

    def cancel(self, submission_id):
        self.cancels.append(submission_id)
        self.jobs["cancel-1"] = {"id": "cancel-1", "status": "done"}
        return self.jobs["cancel-1"]


@pytest.fixture
def remote(tmp_path):
    layout = Layout(tmp_path)
    engine = make_engine(tmp_path / "portal")
    run = create_wire_run(engine, layout, count=2, backend="crux")
    settings = remote_settings(bdp_settings(layout))
    yield SimpleNamespace(engine=engine, run=run, settings=settings, client=FakeDM(), layout=layout)
    engine.dispose()


def test_a_plan_built_slowly_records_its_progress_so_the_creation_timeout_counts_from_it(remote, monkeypatch):
    """A large ``crux`` run's plan checks every input (slow on a slow mount) while the run is ``published`` with no
    submission. Its progress moves the run's last change, so the reconciler does not fail it past
    ``creation_timeout_seconds`` after the publication; a run failed meanwhile (a build stuck on a hung mount) stops the
    build at its next progress, before anything is written."""
    engine, run, client = remote.engine, remote.run, remote.client
    monkeypatch.setattr(submission, "PLAN_PROGRESS_SECONDS", 0)
    with Session(engine) as session, session.begin():
        session.get(Job, run.job_id).updated_at = utcnow() - timedelta(minutes=11)  # published 11 minutes ago
    build_plan, checks, verdicts = remote_plan.build_plan, [], []

    def reconciled_meanwhile(job, settings, submission_id, *, progress):
        def counted():
            progress()
            checks.append(1)

        written = build_plan(job, settings, submission_id, progress=counted)
        verdicts.append(reconciler.reconcile_runs(engine, stale_after_seconds=300, creation_timeout_seconds=600))
        return written

    monkeypatch.setattr(remote_plan, "build_plan", reconciled_meanwhile)
    submission.submit_run(run.job_id, engine=engine, settings=remote.settings, client=client)

    inputs = len(client.submissions[0]["input_transfer"]["transfer_items"]) - 3  # less the three control files
    assert len(checks) == 1 + inputs and verdicts[0].abandoned == []  # the manifest, then each input's check
    job = load_job(engine, run.job_id)
    assert (job.status, job.phase, job.external_id) == (JobStatus.QUEUED, RunPhase.QUEUED, "dm-001")

    stuck = create_wire_run(engine, remote.layout, count=2, backend="crux", name="stuck")
    progress = submission.plan_progress(stuck.job_id, engine=engine)
    with Session(engine) as session, session.begin():
        session.get(Job, stuck.job_id).updated_at = utcnow() - timedelta(minutes=11)
    assert reconciler.reconcile_runs(engine, stale_after_seconds=300, creation_timeout_seconds=600).abandoned == [
        stuck.job_id
    ]
    with pytest.raises(ExecutionStateError):
        progress()


def test_a_stop_while_the_plan_is_written_cancels_the_run_and_dm_is_never_asked(remote, monkeypatch):
    engine, run, client = remote.engine, remote.run, remote.client
    save_plan, stops = remote_plan.save_plan, []

    def stopped_meanwhile(job, settings, submission_id, **kwargs):
        written = save_plan(job, settings, submission_id, **kwargs)
        stops.append(controls.cancel_run(run.job_id, engine=engine))
        return written

    monkeypatch.setattr(submission, "save_plan", stopped_meanwhile)
    with pytest.raises(ExecutionStateError, match="changed while its DM submission was prepared"):
        submission.submit_run(run.job_id, engine=engine, settings=remote.settings, client=client)

    assert [stop.state for stop in stops] == [CancelState.CANCELLED]
    assert client.submissions == []
    job = load_job(engine, run.job_id)
    assert (job.status, job.phase, job.submission_id, job.external_state) == (
        JobStatus.CANCELLED,
        RunPhase.CANCELLED,
        None,
        None,
    )
    assert list(Path(remote.settings.submissions()).glob("*.json")) == []  # no inbox copy is left for DM
    assert poller.poll_once(engine=engine, settings=remote.settings, client=client) == []
    assert client.submissions == [] and client.cancels == []


def test_a_stop_while_dm_is_asked_is_recorded_and_delivered_by_the_poller(remote):
    engine, run, client = remote.engine, remote.run, remote.client
    stops = []
    client.on_submit = lambda: stops.append(controls.cancel_run(run.job_id, engine=engine))

    assert submission.submit_run(run.job_id, engine=engine, settings=remote.settings, client=client) == "dm-001"

    assert [stop.state for stop in stops] == [CancelState.REQUESTED]
    job = load_job(engine, run.job_id)
    assert (job.status, job.phase, job.external_id) == (JobStatus.QUEUED, RunPhase.QUEUED, "dm-001")
    assert job.cancel_requested_at is not None
    plan = client.submissions[0]
    poller.poll_once(engine=engine, settings=remote.settings, client=client)
    assert client.cancels == [plan["submission_id"]]
    assert not (Path(plan["run_directory"]) / ".cancel-sent").exists()  # the cancel job's state: the next pass's
    poller.poll_once(engine=engine, settings=remote.settings, client=client)
    assert client.cancels == [plan["submission_id"]]
    assert (Path(plan["run_directory"]) / ".cancel-sent").exists()
    client.jobs["dm-001"]["status"] = "aborted"
    poller.poll_once(engine=engine, settings=remote.settings, client=client)
    job = load_job(engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.CANCELLED, RunPhase.CANCELLED)


def test_without_enabled_remote_settings_a_run_is_refused_saying_why(remote):
    engine, run, client = remote.engine, remote.run, remote.client
    with pytest.raises(ValueError, match="no remote settings were given"):
        submission.submit_run(run.job_id, engine=engine, settings=None, client=client)
    disabled = remote.settings.model_copy(update={"enabled": False})
    with pytest.raises(ValueError, match="the remote settings given are disabled"):
        submission.submit_run(run.job_id, engine=engine, settings=disabled, client=client)
    job = load_job(engine, run.job_id)
    assert (job.phase, job.submission_id, client.submissions) == (RunPhase.PUBLISHED, None, [])


def _stop_and_poll_past_the_grace(remote, settings):
    """While DM is asked: a stop, then a poller pass after the grace period, which finds no DM job for the
    submission yet (DM has not answered)."""
    engine, run, client = remote.engine, remote.run, remote.client
    assert controls.cancel_run(run.job_id, engine=engine).state == CancelState.REQUESTED
    with Session(engine) as session, session.begin():
        job = session.get(Job, run.job_id)
        assert job is not None
        job.cancel_requested_at = utcnow() - timedelta(seconds=poller.UNSUBMITTED_STOP_GRACE_SECONDS + 60)
    return poller.poll_once(engine=engine, settings=settings, client=client)


def test_dm_accepting_a_submission_after_its_job_ended_leaves_the_job_and_names_the_dm_job(remote, caplog):
    """A portal process paused while DM was asked, for longer than the grace: the poller cancelled the stopped job.
    DM's late answer changes nothing in the ended job; its message and the log name the DM job to cancel."""
    engine, run, client = remote.engine, remote.run, remote.client
    passes = []
    client.on_submit = lambda: passes.append(_stop_and_poll_past_the_grace(remote, remote.settings))
    assert submission.submit_run(run.job_id, engine=engine, settings=remote.settings, client=client) == "dm-001"
    assert passes == [[run.job_id]]
    job = load_job(engine, run.job_id)
    assert (job.status, job.phase, job.external_id, job.external_state) == (
        JobStatus.CANCELLED,
        RunPhase.CANCELLED,
        None,
        "submitting",
    )
    message = f"DM accepted submission {job.submission_id} as processing job dm-001 after this run ended"
    assert job.messages.splitlines()[-1].startswith(message)
    assert any(r.levelname == "ERROR" and message in r.getMessage() for r in caplog.records)


def test_a_lost_reply_after_the_job_ended_leaves_the_job_and_says_what_to_look_up(remote):
    engine, run, client = remote.engine, remote.run, remote.client
    client.reply_lost = True
    client.on_submit = lambda: _stop_and_poll_past_the_grace(remote, remote.settings)
    with pytest.raises(TimeoutError):
        submission.submit_run(run.job_id, engine=engine, settings=remote.settings, client=client)
    job = load_job(engine, run.job_id)
    assert (job.status, job.external_state) == (JobStatus.CANCELLED, "submitting")
    assert job.messages.splitlines()[-1].startswith(f"DM's reply to submission {job.submission_id} was lost after")


def test_without_a_dm_timeout_a_stop_never_overtakes_a_submission_waiting_for_dm(remote, caplog):
    """``dm.timeout_seconds: null``: nothing bounds DM's answer, so the poller never cancels a submission DM has no
    job for; DM's late answer is recorded and the stop is delivered."""
    engine, run, client = remote.engine, remote.run, remote.client
    no_timeout = remote.settings.model_copy(
        update={"dm": remote.settings.dm.model_copy(update={"timeout_seconds": None})}
    )
    passes = []
    client.on_submit = lambda: passes.append(_stop_and_poll_past_the_grace(remote, no_timeout))
    assert submission.submit_run(run.job_id, engine=engine, settings=no_timeout, client=client) == "dm-001"
    assert passes == [[]]
    assert any("dm.timeout_seconds is null" in r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    job = load_job(engine, run.job_id)
    assert (job.status, job.external_id, job.external_state) == (JobStatus.QUEUED, "dm-001", "pending")
    poller.poll_once(engine=engine, settings=no_timeout, client=client)
    assert client.cancels == [job.submission_id]


def test_dm_answering_after_the_poller_found_its_job_keeps_the_pollers_record(remote):
    """The poller found DM's job by submission ID (already running) before DM's answer reached ``submit_run``."""
    engine, run, client = remote.engine, remote.run, remote.client

    def found_meanwhile():
        client.jobs["dm-001"] = {"id": "dm-001", "status": "running"}
        assert poller.poll_once(engine=engine, settings=remote.settings, client=client) == [run.job_id]

    client.on_submit = found_meanwhile
    assert submission.submit_run(run.job_id, engine=engine, settings=remote.settings, client=client) == "dm-001"
    job = load_job(engine, run.job_id)
    assert (job.status, job.phase, job.external_id, job.external_state) == (
        JobStatus.RUNNING,
        RunPhase.RUNNING,
        "dm-001",
        "running",
    )
