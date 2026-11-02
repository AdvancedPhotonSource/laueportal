"""Stop against the worker's dispatcher's claim (``jobs.controls``, ``jobs.dispatch``).

The job table is the queue: each control is one write transaction (``core.db.begin_write``), and so is the dispatcher's
claim, so they never interleave: a Stop that waits for a claim in progress finds the run claimed (Running) and asks it
to stop. Stop of a Queued run and Move to Front racing a claim are fault-injection tests (``test_faults.py``, with this
module's ``_during_a_claim``); ``test_queue.py`` covers the controls' answers; ``test_jobs_api.py`` the API's.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from laueportal.jobs import controls, dispatch, state
from laueportal.jobs.schemas import CancelResult, CancelState
from laueportal.jobs.state import JobStatus
from tests.run_support import enqueue_with, load_job, publish_wire_run
from tests.settings_support import make_database


@pytest.fixture
def setup(tmp_path):
    settings, engine = make_database(tmp_path)
    yield settings, engine
    engine.dispose()


@pytest.fixture
def engine(setup):
    return setup[1]


def _queued(engine, tmp_path, name: str) -> int:
    run = publish_wire_run(engine, tmp_path, name=name)
    enqueue_with(engine, run.job_id)
    return run.job_id


def _during_a_claim(
    monkeypatch: pytest.MonkeyPatch, engine, job_id: int, control: Callable[[], Any]
) -> tuple[bool, Any]:
    """Run ``control`` in a thread while the dispatcher's claim of ``job_id`` holds its write transaction (held just
    after it marked the run Running); returns the claim's result and the control's."""
    in_claim, release = threading.Event(), threading.Event()
    mark_running = state.mark_running

    def held(job: Any, **options: Any) -> None:
        mark_running(job, **options)
        in_claim.set()
        assert release.wait(30)

    monkeypatch.setattr(state, "mark_running", held)
    claimed: list[bool] = []
    answered: list[Any] = []
    claim = threading.Thread(target=lambda: claimed.append(dispatch.claim_run(engine, job_id)))
    claim.start()
    assert in_claim.wait(30)
    waiting = threading.Thread(target=lambda: answered.append(control()))
    waiting.start()
    time.sleep(0.3)
    early = list(answered)
    release.set()
    claim.join(30)
    waiting.join(60)
    monkeypatch.undo()
    assert early == [], "the control did not wait for the claim's transaction"
    return claimed[0], answered[0]


def test_a_stop_that_waits_for_a_claim_asks_the_claimed_run_to_stop(engine, tmp_path, monkeypatch):
    job_id = _queued(engine, tmp_path, "claimed")

    claimed, result = _during_a_claim(monkeypatch, engine, job_id, lambda: controls.cancel_run(job_id, engine=engine))

    assert claimed is True
    assert result == CancelResult(job_id=job_id, state=CancelState.REQUESTED, n_pending=3)
    job = load_job(engine, job_id)  # its executor polls the flag, drains and finalizes it Cancelled
    assert (job.status, job.cancel_requested_at is not None) == (JobStatus.RUNNING, True)
