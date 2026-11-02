"""Remote observations: a run's ``remoteObservation``, which the remote poller reads from the records of its one batched
DM call per pass and keeps in ``job.remote_observation`` (``remote.observation``), against the goldens
(``tests/fixtures/observation_goldens/``, whose ``cases.py`` holds the cases). The job page and the runs
grid show them: ``tests/jobs/test_job_page_api.py``, ``test_jobs_api.py``.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import pytest

from laueportal.jobs.state import JobStatus
from laueportal.remote import observation, poller, submission
from laueportal.remote.observation import Refused, accept
from laueportal.remote.poller import RefusedObservations, poll, poll_once
from tests.fixtures.observation_goldens import cases
from tests.remote.test_poller import FakeDM, _return
from tests.remote_support import Layout, bdp_settings, create_wire_run, remote_settings
from tests.run_support import load_job, make_engine

GOLDENS: dict[str, Any] = json.loads(cases.GOLDENS.read_text(encoding="utf-8"))
KEPT = "kept"
EXPECTED = {
    "kept": KEPT,
    "kept-as-an-object": KEPT,
    "kept-at-the-stored-time": KEPT,
    "kept-without-a-stored-one": KEPT,
    "kept-with-another-offset": KEPT,
    "none-absent": None,
    "none-empty": None,
    "none-for-aps": None,
    "text-too-long": "longer than 16384 characters",
    "not-json": "not JSON",
    "not-an-object": "not a JSON object",
    "object-too-long": "longer than 16384 characters as JSON",
    "unknown-phase": "an unknown phase",
    "phase-not-text": "an unknown phase",
    "another-submission": "another submission's",
    "no-observed-at": "no timezone-aware observed_at",
    "naive-observed-at": "no timezone-aware observed_at",
    "observed-at-not-a-time": "no timezone-aware observed_at",
    "observed-at-not-text": "no timezone-aware observed_at",
    "older-than-the-stored-one": "older than the stored one",
    "kept-at-the-bound": KEPT,
    "object-too-long-by-one": "longer than 16384 characters as JSON",
    "kept-at-the-depth-bound": KEPT,
    "nested-too-deep": "nested deeper than 1000 levels",
    "not-finite": "a number that is not finite",
    "not-finite-too-large": "a number that is not finite",
    "observed-at-out-of-range": "no timezone-aware observed_at",
    "kept-login-required": KEPT,  # the Login needed phase
}
"""The outcome for each intake case (``cases.intake_cases``): kept, none carried (None), or the refusal's reason."""
KNOWN_DIFFERENCES = {
    "kept-with-another-offset",
    "nested-too-deep",
    "not-finite",
    "not-finite-too-large",
    "observed-at-out-of-range",
}
"""Where the portal's verdicts and the goldens' differ. The portal keeps one the goldens refuse: the captured code
compares ``observed_at`` with the stored one as text, the portal as times (the same for the launcher's format and the
poller's, UTC to the second; this one is 12:00 UTC written at -05:00). The portal refuses what the goldens keep and it
could not show alike on the job page and in the runs grid: what SQLite's JSON functions cannot read
(``observation.unreadable``), and an ``observed_at`` UTC cannot hold."""


def _outcome(case: dict[str, Any]) -> str | None:
    try:
        kept = accept(
            case["value"],
            backend=case.get("backend", observation.OBSERVED_TARGET),
            submission_id=cases.SUBMISSION_ID,
            stored=case["stored"],
        )
    except Refused as refusal:
        return str(refusal)
    if kept is None:
        return None
    value = case["value"]
    assert kept == (json.loads(value) if isinstance(value, str) else value)
    return KEPT


def test_the_goldens_cover_the_capture_s_cases():
    """The committed goldens hold exactly ``cases.py``'s cases."""
    assert set(GOLDENS["observations"]) == set(cases.LAUNCHER_CASES)
    assert set(GOLDENS["intake"]) == set(cases.intake_cases()) == set(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_the_poller_keeps_or_refuses_each_intake_case_by_its_rules(name):
    """Each rule (at most 16 KiB, a JSON object, a known phase, the job's submission, an aware ``observed_at`` never
    older than the stored one; ``aps`` runs have none) keeps or refuses as the goldens say, but for
    ``KNOWN_DIFFERENCES``."""
    outcome = _outcome(cases.intake_cases()[name])
    assert outcome == EXPECTED[name]
    v1_kept = GOLDENS["intake"][name]
    assert (outcome == KEPT) == (not v1_kept if name in KNOWN_DIFFERENCES else v1_kept)


def test_the_16_kib_bound_is_pinned_at_its_boundary():
    """Both lengths (DM's text, and ``json.dumps`` of the object): 16384 kept, 16385 refused (the verdicts:
    ``test_the_poller_keeps_or_refuses_each_intake_case_by_its_rules``)."""
    intake = cases.intake_cases()

    def lengths(name: str) -> tuple[int | None, int]:
        value = intake[name]["value"]
        text = value if isinstance(value, str) else None
        return (None if text is None else len(text), len(json.dumps(json.loads(text) if text else value)))

    assert lengths("kept-at-the-bound") == (16384, 16384)
    assert lengths("text-too-long")[0] == 16385
    assert lengths("object-too-long-by-one") == (None, 16385)


@pytest.mark.parametrize(
    "stored", [["running"], "running", 7, {"observed_at": 7}], ids=["list", "text", "number", "no-time"]
)
def test_a_stored_value_that_is_not_an_observation_is_no_stored_one(stored):
    """A stored value that is not an object (or has no aware ``observed_at``) does not hold back a new observation."""
    value = cases.intake_cases()["kept"]["value"]
    assert accept(value, backend="crux", submission_id=cases.SUBMISSION_ID, stored=stored) == json.loads(value)


def test_every_golden_launcher_observation_is_kept():
    for name, text in GOLDENS["observations"].items():
        kept = accept(text, backend="crux", submission_id=cases.SUBMISSION_ID, stored=None)
        assert kept == json.loads(text), name


def test_before_publication_the_observation_is_downlink_publishing_results():
    before, after = GOLDENS["publishing"]["before"], GOLDENS["publishing"]["after"]
    assert observation.publishing(before, submission_id=cases.SUBMISSION_ID, now=cases.PUBLISHED) == after
    assert (after["observed_at"], after["phase_since"]) == (cases.at(60), before["phase_since"])  # refreshed, kept
    assert before["observed_at"] == cases.at(0)


# ---------------------------------------------------------------------------
# The poller


@pytest.fixture
def remote(tmp_path):
    layout = Layout(tmp_path)
    engine = make_engine(tmp_path / "portal")
    yield engine, layout, remote_settings(bdp_settings(layout))
    engine.dispose()


def _submitted(remote, backend="crux"):
    engine, layout, settings = remote
    run = create_wire_run(engine, layout, count=2, backend=backend)
    client = FakeDM()
    submission.submit_run(run.job_id, engine=engine, settings=settings, client=client)
    return run, client.submissions[0], client


def _golden(name: str, submission_id: str, **changes: Any) -> dict[str, Any]:
    """``name``'s golden observation for the run of ``submission_id``."""
    return {**json.loads(GOLDENS["observations"][name]), "submission_id": submission_id, **changes}


def test_the_poller_keeps_an_observation_from_its_one_dm_call_per_pass(remote, monkeypatch):
    """The observation rides in the record of the pass's one DM call (no call of its own), and a newer one
    replaces it, written in the pass's one write of the job (with its DM state and heartbeat)."""
    engine, _layout, settings = remote
    run, plan, client = _submitted(remote)
    writes: list[None] = []
    begin_write = poller.begin_write

    def counted(session: Any) -> None:
        writes.append(None)
        begin_write(session)

    monkeypatch.setattr(poller, "begin_write", counted)
    queued = _golden("pbs-queued", plan["submission_id"])
    client.status("running", remoteObservation=json.dumps(queued))
    poll_once(engine=engine, settings=settings, client=client)
    assert load_job(engine, run.job_id).remote_observation == queued
    assert len(writes) == 1

    running = _golden("running", plan["submission_id"], observed_at=cases.at(10))
    client.status("running", remoteObservation=json.dumps(running))
    poll_once(engine=engine, settings=settings, client=client)
    assert load_job(engine, run.job_id).remote_observation == running
    assert len(client.fetches) == 2  # one call per pass
    assert len(writes) == 2  # one write per pass


def test_a_refused_observation_is_logged_once_and_dropped_and_the_run_carries_on(remote, caplog):
    engine, _layout, settings = remote
    run, plan, client = _submitted(remote)
    kept = _golden("pbs-queued", plan["submission_id"])
    client.status("pending", remoteObservation=json.dumps(kept))
    refusals = RefusedObservations()
    poll(engine=engine, settings=settings, client=client, refusals=refusals)

    def refused_lines() -> list[str]:
        lines = [record.getMessage() for record in caplog.records]
        return [line for line in lines if "remote observation was refused" in line]

    caplog.set_level(logging.WARNING, logger="laueportal.remote.poller")
    client.status("running", remoteObservation="broken JSON")
    for _ in range(3):
        poll(engine=engine, settings=settings, client=client, refusals=refusals)
    job = load_job(engine, run.job_id)
    assert (job.external_state, job.remote_observation) == ("running", kept)  # the run carried on
    assert refused_lines() == [f"Remote job {run.job_id}: its remote observation was refused (not JSON) and dropped"]

    client.status("running", remoteObservation=json.dumps({**kept, "submission_id": "another"}))
    poll(engine=engine, settings=settings, client=client, refusals=refusals)
    assert len(refused_lines()) == 2  # another reason

    client.status("running", remoteObservation=json.dumps(kept))
    poll(engine=engine, settings=settings, client=client, refusals=refusals)
    client.status("running", remoteObservation="broken JSON")
    poll(engine=engine, settings=settings, client=client, refusals=refusals)
    assert len(refused_lines()) == 3  # again after one was kept
    assert load_job(engine, run.job_id).status == JobStatus.RUNNING


@pytest.mark.parametrize("backend", ["crux", "aps"])
def test_before_publication_the_poller_writes_publishing_results_for_crux_only(remote, monkeypatch, backend):
    """The Downlink "Publishing results" observation, written before the returned run is published; an ``aps`` run has
    no observation."""
    engine, _layout, settings = remote
    run, plan, client = _submitted(remote, backend)
    returning = _golden("downlink", plan["submission_id"])
    client.status("running", remoteObservation=json.dumps(returning))
    poll_once(engine=engine, settings=settings, client=client)

    seen = []
    publish = poller.publish_returned

    def publish_returned(plan):
        seen.append(load_job(engine, run.job_id).remote_observation)
        return publish(plan)

    monkeypatch.setattr(poller, "publish_returned", publish_returned)
    monkeypatch.setattr(poller, "utcnow", lambda: cases.PUBLISHED)
    _return(plan)
    client.status("done")
    poll_once(engine=engine, settings=settings, client=client)
    job = load_job(engine, run.job_id)
    assert job.status == JobStatus.FAILED  # the returned summary's status
    if backend == "aps":
        assert seen == [None] and job.remote_observation is None
        return
    expected = {**GOLDENS["publishing"]["after"], "submission_id": plan["submission_id"]}
    assert seen == [expected] and job.remote_observation == expected


def test_a_publication_retried_after_a_transient_error_logs_no_refusal_and_writes_publishing_results_again(
    remote, monkeypatch, caplog
):
    """Once the stored observation is the poller's own "Publishing results", the record's (the launcher's last, older)
    is not looked at: a publication retried after a transient error logs no refusal, and "Publishing results" is
    written again at the retry's time."""
    engine, _layout, settings = remote
    run, plan, client = _submitted(remote)
    client.status("running", remoteObservation=json.dumps(_golden("downlink", plan["submission_id"])))
    poll_once(engine=engine, settings=settings, client=client)

    clock = [cases.PUBLISHED]
    monkeypatch.setattr(poller, "utcnow", lambda: clock[0])
    attempts: list[str] = []
    publish = poller.publish_returned

    def publish_returned(plan):
        attempts.append(load_job(engine, run.job_id).remote_observation["observed_at"])
        if len(attempts) == 1:
            raise PermissionError(13, "Permission denied")  # transient: the next pass retries
        return publish(plan)

    monkeypatch.setattr(poller, "publish_returned", publish_returned)
    _return(plan)
    client.status("done")
    caplog.set_level(logging.WARNING, logger="laueportal.remote.poller")
    refusals = RefusedObservations()
    poll(engine=engine, settings=settings, client=client, refusals=refusals)
    assert load_job(engine, run.job_id).status == JobStatus.RUNNING
    clock[0] = cases.PUBLISHED + timedelta(seconds=10)
    poll(engine=engine, settings=settings, client=client, refusals=refusals)

    job = load_job(engine, run.job_id)
    assert job.status == JobStatus.FAILED  # the returned summary's status
    assert attempts == [cases.at(60), cases.at(70)]
    assert job.remote_observation["detail"] == observation.PUBLISHING_DETAIL
    assert not [record for record in caplog.records if "remote observation was refused" in record.getMessage()]
