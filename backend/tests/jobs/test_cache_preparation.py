"""One Pioneer run: local cache preparation, then one guarded handoff to DM."""

import json
import shutil
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

pytest.importorskip("laue_index")

from laueportal.core.timeutil import utcnow
from laueportal.indexing.laue_matching import compute
from laueportal.indexing.laue_matching.params import LaueMatchingParamsV1
from laueportal.indexing.validation import validate_laue_matching
from laueportal.jobs import controls, dispatch, executor, reconciler
from laueportal.jobs.contract import RunOutcome
from laueportal.jobs.models import Job
from laueportal.jobs.order import queued_runs
from laueportal.jobs.queue import RunPolicy, enqueue_run
from laueportal.jobs.state import JobStatus, RunPhase
from laueportal.remote import submission
from tests.remote.test_pioneer import check, setup
from tests.settings_support import make_settings


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    layout, engine, remote, run = setup(tmp_path, target={"data_root": str(tmp_path / "portal-data")})
    with Session(engine) as session, session.begin():
        job = session.get(Job, run.job_id)
        path = Path(job.request_path)
        document = json.loads(path.read_text())
        document["request"]["create_forward_cache"] = True
        path.write_text(json.dumps(document))
        job.indexing_run.laue_matching_parameters.create_forward_cache = True
    settings = make_settings(
        tmp_path,
        files={"runs_root": layout.experiment, "roots": {"data": tmp_path}},
        remote={"enabled": True, "settings_file": tmp_path / "bdp.yaml"},
        laue_matching={"data_root": tmp_path / "portal-data"},
    )
    events = []

    class DM:
        fail = False

        def submit(self, plan):
            events.append("submit")
            if self.fail:
                raise TimeoutError("reply lost")
            return {"id": "dm-cache-test"}

    dm = DM()
    monkeypatch.setattr(submission, "DMClient", lambda *a: dm)
    monkeypatch.setattr(executor, "get_settings", lambda: settings)

    def build(request, hooks):
        events.append("build")
        return RunOutcome(0, 0)

    monkeypatch.setattr(compute, "build_forward_cache", build)
    enqueue_run(run.job_id, engine=engine, remote=remote, sources=check(layout))
    result = SimpleNamespace(engine=engine, run=run, events=events, dm=dm, settings=settings, request=path)
    yield result
    engine.dispose()


def execute(prepared):
    assert dispatch.claim_run(prepared.engine, prepared.run.job_id)
    return executor.execute_run(prepared.run.job_id, engine=prepared.engine, policy=RunPolicy())


def load(prepared):
    with Session(prepared.engine) as session:
        job = session.get(Job, prepared.run.job_id)
        session.expunge(job)
        return job


def test_cache_preparation_uses_the_local_queue_then_submits_the_same_run_once(prepared):
    frozen = prepared.request.read_bytes()
    with Session(prepared.engine) as session:
        assert list(session.scalars(queued_runs())) == [prepared.run.job_id]
    assert prepared.events == []
    result = execute(prepared)
    job = load(prepared)
    assert prepared.events == ["build", "submit"]
    assert result["status"] == "Queued"
    assert (job.status, job.phase, job.external_id) == (JobStatus.QUEUED, RunPhase.QUEUED, "dm-cache-test")
    assert job.submission_id and job.process_pid is None
    assert job.n_succeeded == job.n_failed == 0
    assert prepared.request.read_bytes() == frozen
    assert not (prepared.request.parent / "run.json").exists()
    assert not dispatch.claim_run(prepared.engine, job.job_id)
    assert prepared.events == ["build", "submit"]


@pytest.mark.parametrize("same_folder", [True, False])
def test_new_gpu_cache_requires_the_same_shared_folder(prepared, same_folder):
    document = json.loads(prepared.request.read_text())["request"]
    shutil.rmtree(Path(document["data_root"]) / document["forward_cache"])
    form = {name: document[name] for name in LaueMatchingParamsV1.fields_from("form") if name in document}
    settings = prepared.settings
    if not same_folder:
        settings = settings.model_copy(
            update={"laue_matching": settings.laue_matching.model_copy(update={"data_root": prepared.request.parent})}
        )
    checked = validate_laue_matching(form, settings=settings, engine=prepared.engine)
    if same_folder:
        assert checked.ok
        assert any(i.code == "cache_build" and "locally" in i.message for i in checked.issues)
    else:
        assert not checked.ok
        assert any(i.code == "cache_folders_differ" for i in checked.issues)


def test_stop_while_waiting_for_the_worker_submits_nothing(prepared):
    controls.cancel_run(prepared.run.job_id, engine=prepared.engine)
    assert not dispatch.claim_run(prepared.engine, prepared.run.job_id)
    assert load(prepared).status == JobStatus.CANCELLED
    assert prepared.events == []


def test_stop_during_cache_build_submits_nothing(prepared, monkeypatch):
    def stop(request, hooks):
        controls.cancel_run(request.job_id, engine=prepared.engine)
        return RunOutcome(0, 0)

    monkeypatch.setattr(compute, "build_forward_cache", stop)
    execute(prepared)
    job = load(prepared)
    assert job.status == JobStatus.CANCELLED and job.n_not_run == job.n_inputs
    assert job.submission_id is None and prepared.events == []


def test_stop_at_the_handoff_prevents_a_dm_submission(prepared, monkeypatch):
    save = submission.save_plan

    def stop(*args, **kwargs):
        plan = save(*args, **kwargs)
        controls.cancel_run(prepared.run.job_id, engine=prepared.engine)
        return plan

    monkeypatch.setattr(submission, "save_plan", stop)
    execute(prepared)
    assert load(prepared).status == JobStatus.CANCELLED
    assert prepared.events == ["build"]


def test_a_failed_cache_build_fails_cleanly_without_submitting(prepared, monkeypatch):
    def fail(*args):
        raise OSError("cache disk failed")

    monkeypatch.setattr(compute, "build_forward_cache", fail)
    with pytest.raises(OSError, match="cache disk failed"):
        execute(prepared)
    job = load(prepared)
    assert job.status == JobStatus.FAILED and job.n_not_run == job.n_inputs
    assert job.submission_id is None and prepared.events == []


def test_a_lost_dm_reply_keeps_the_submission_with_the_remote_poller(prepared):
    prepared.dm.fail = True
    execute(prepared)
    job = load(prepared)
    assert job.status == JobStatus.QUEUED and job.submission_id is not None
    assert job.external_state == submission.SUBMISSION_UNCERTAIN
    assert prepared.events == ["build", "submit"]
    assert not (prepared.request.parent / "run.json").exists()
    assert not dispatch.claim_run(prepared.engine, job.job_id)


def test_a_dead_cache_builder_is_reconciled_as_a_local_process(prepared):
    assert dispatch.claim_run(prepared.engine, prepared.run.job_id)
    with Session(prepared.engine) as session, session.begin():
        job = session.get(Job, prepared.run.job_id)
        job.heartbeat_at = utcnow() - timedelta(hours=1)
    reconciler.reconcile_runs(prepared.engine, stale_after_seconds=300)
    job = load(prepared)
    assert job.status == JobStatus.FAILED and job.phase == RunPhase.INTERRUPTED
    assert prepared.events == []
