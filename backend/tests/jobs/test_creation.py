"""Creating and submitting one run: ``POST /api/v1/indexing-runs`` and ``POST /api/v1/reconstruction-runs``
(``jobs.creation``, ``jobs.idempotency``).

The place (``tests.validation_support``) gives a form that passes every check but the unlinked warning. The keys are in
the place's database (``idempotency_key``). A local run is queued by its committed row (the job table is the queue).
"""

from __future__ import annotations

import dataclasses
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from laueportal.app import create_app
from laueportal.core.problems import ProblemItem
from laueportal.core.settings import Settings
from laueportal.core.timeutil import utcnow
from laueportal.indexing import service as indexing_service
from laueportal.jobs import creation, programs, rundir, state
from laueportal.jobs.idempotency import KEPT, KEY_CREATED, STALE_SECONDS, TTL_SECONDS, IdempotencyStore
from laueportal.jobs.models import IdempotencyKey, Job
from laueportal.jobs.records import RunPublicationError
from laueportal.jobs.rundir import REQUEST_FILENAME
from laueportal.jobs.state import JobStatus, RunPhase
from laueportal.jobs.validation import Validated, finish
from laueportal.reconstruction import service as reconstruction_service
from laueportal.remote import submission
from tests.api_support import PORTAL_HEADERS
from tests.remote.test_submission import FakeDM
from tests.run_support import dispatch_queue
from tests.validation_support import FRAMES, Place, make_place, offering

PATHS = {"lauego": "/api/v1/indexing-runs", "wire": "/api/v1/reconstruction-runs"}
LABELS = {"lauego": "I", "wire": "R"}
KINDS = pytest.mark.parametrize("kind", ["lauego", "wire"])


@pytest.fixture
def place(tmp_path) -> Iterator[Place]:
    yield (place := make_place(tmp_path))
    place.engine.dispose()


def portal(settings: Settings, *, raise_server_exceptions: bool = True) -> TestClient:
    app: FastAPI = create_app(settings)
    return TestClient(app, headers=PORTAL_HEADERS, raise_server_exceptions=raise_server_exceptions)


@pytest.fixture
def client(place: Place) -> Iterator[TestClient]:
    with portal(place.settings) as client:
        yield client


def create(client: TestClient, kind: str, body: Any, key: str = "key-1") -> Any:
    return client.post(PATHS[kind], json=body, headers={"Idempotency-Key": key})


def keys(place: Place) -> list[IdempotencyKey]:
    """The idempotency keys in the place's database."""
    with Session(place.engine) as session:
        return list(session.scalars(select(IdempotencyKey)))


def counts(place: Place) -> tuple[int, ...]:
    with place.engine.connect() as connection:
        return tuple(
            connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            for table in ("job", "reconstruction_run", "indexing_run")
        )


def job(place: Place, job_id: int) -> Job:
    with Session(place.engine) as session:
        return session.get_one(Job, job_id)


def on_aps(place: Place, monkeypatch: pytest.MonkeyPatch) -> tuple[Settings, dict[str, Any], FakeDM]:
    """Settings offering ``aps`` (the experiment on the APS file system), a wire form for it, and the fake DM."""
    settings = offering(place, "aps")
    path = settings.remote.settings_file
    assert path is not None
    document = yaml.safe_load(path.read_text())
    document["storage"]["aps"]["filesystem_root"] = os.fspath(place.root)  # the experiment is on the APS file system
    path.write_text(yaml.safe_dump(document))
    dm = FakeDM()
    monkeypatch.setattr(submission, "DMClient", lambda _settings: dm)
    form = place.form("wire", backend="aps", output_path_template=programs.output_path_template("wire", settings))
    return settings, form, dm


def fails_once(monkeypatch: pytest.MonkeyPatch, owner: object, name: str, error: BaseException) -> None:
    """``owner.name`` raises ``error`` on its first call, then works."""
    original, calls = getattr(owner, name), []

    def failing(*args: Any, **kwargs: Any) -> Any:
        calls.append(name)
        if len(calls) == 1:
            raise error
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, failing)


def raising(error: BaseException) -> Callable[..., Any]:
    def call(*args: Any, **kwargs: Any) -> Any:
        raise error

    return call


@KINDS
def test_a_form_creates_one_run_and_queues_it(place, client, kind):
    """One parameter object: validated, created with its inputs published, queued on its target (local: its row);
    a 201 with the run and its job. The output folder is the form's, inside the roots (``settings=``)."""
    before = counts(place)
    response = create(client, kind, place.form(kind))

    assert response.status_code == 201, response.text
    answer = response.json()
    run_id, job_id = answer["run"]["id"], answer["job_id"]
    assert answer == {
        "run": {"kind": "indexing" if kind == "lauego" else "reconstruction", "id": run_id,
                "label": f"{LABELS[kind]}{run_id}"},
        "job_id": job_id,
        "status": "queued",
        "target": "local",
        "queue_id": f"run_{job_id}",
        "failure": None,
    }  # fmt: skip
    assert dispatch_queue(place.engine) == [("run", job_id)]
    created = job(place, job_id)
    assert (created.phase, created.status) == (RunPhase.QUEUED, JobStatus.QUEUED)
    assert os.path.isfile(os.path.join(created.run_directory or "", REQUEST_FILENAME))
    added = {"lauego": (1, 0, 1), "wire": (1, 1, 0)}[kind]
    assert counts(place) == tuple(b + a for b, a in zip(before, added, strict=True))


@KINDS
def test_an_invalid_form_is_a_422_with_its_issues_and_its_key_can_be_sent_again(place, client, kind):
    """An error stops the creation (a 422 problem with every issue, errors first; warnings do not stop it) and
    creates nothing; the refused request keeps nothing with its key, so the corrected form sends it again."""
    before = counts(place)
    refused = create(client, kind, place.form(kind, author="", scan_points="1-3"))

    assert refused.status_code == 422 and refused.headers["content-type"] == "application/problem+json"
    issues = [(tuple(item["loc"]), item["severity"], item["code"]) for item in refused.json()["errors"]]
    assert issues == [
        (("author",), "error", "missing"),
        (("input_path",), "error", "no_inputs"),
        (("scan_number",), "warning", "unlinked"),
    ]
    assert counts(place) == before and dispatch_queue(place.engine) == []
    assert create(client, kind, place.form(kind)).status_code == 201


@pytest.mark.parametrize(
    ("found", "detail", "listed"),
    [(3, "3 problems", 3), (120, "120 problems (the first 20 of 120 are listed)", 20)],
)
def test_the_422_counts_every_issue_the_validation_found(place, client, monkeypatch, found, detail, listed):
    """A validation answers at most 50 issues (the last says how many more, ``more_issues``) and a problem lists 20:
    the ``detail`` gives the number found, not the number answered or listed."""
    issues = [ProblemItem(loc=["notes"], code="test", severity="error", message=f"issue {n}") for n in range(found)]

    def validate(*args: Any, **kwargs: Any) -> Validated:
        return Validated(None, finish(issues))

    monkeypatch.setitem(creation.PROGRAMS, "wire", dataclasses.replace(creation.PROGRAMS["wire"], validate=validate))
    refused = create(client, "wire", place.form("wire"))
    assert refused.status_code == 422
    assert (refused.json()["detail"], len(refused.json()["errors"])) == (detail, listed)


@KINDS
@pytest.mark.parametrize(
    "payload",
    [
        lambda form: [form, form],
        lambda form: {"runs": [form, form]},
        lambda form: form | {"scan_points": "1; 2"},  # pooled inputs
    ],
    ids=["list", "runs", "pooled"],
)
def test_a_multi_run_payload_is_refused_and_creates_nothing(place, client, kind, payload):
    """One run per request; a list, a batch member or ``;`` pooling is a 422, and nothing is created."""
    before = counts(place)
    response = create(client, kind, payload(place.form(kind)))
    assert response.status_code == 422, response.text
    assert counts(place) == before and dispatch_queue(place.engine) == []


def test_the_key_is_claimed_in_a_transaction_of_its_own_before_the_form_is_checked(place, client, monkeypatch):
    """The claim is an insert in a short transaction of its own, before anything is checked or created. While the
    validation runs, the key's row is committed (another connection reads it, pending) and its transaction holds no lock
    (another connection takes the write lock at once)."""
    program = creation.PROGRAMS["wire"]
    seen: list[Any] = []

    def validate(*args: Any, **kwargs: Any) -> Validated:
        seen.extend((key.state, key.answer) for key in keys(place))
        other = sqlite3.connect(place.settings.database.path, timeout=0.1)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
        finally:
            other.close()
        return program.validate(*args, **kwargs)

    monkeypatch.setitem(creation.PROGRAMS, "wire", dataclasses.replace(program, validate=validate))
    response = create(client, "wire", place.form("wire"))

    assert response.status_code == 201, response.text
    assert seen == [("pending", None)]
    assert [(key.state, key.answer) for key in keys(place)] == [("kept", response.json())]


@KINDS
def test_the_same_key_answers_the_same_run_and_another_body_is_a_409(place, client, kind):
    """The same key with the same body answers the first 201 again and creates nothing more; with another body, or while
    the first request runs, a 409; past ``STALE_SECONDS``, a 409 saying the earlier request did not finish. Another body
    after the run was created (its answer lost, the form edited) names that run and carries the kept answer, ``created``
    (type ``KEY_CREATED``); another body on a key not yet answered carries none (``about:blank``). Keys are per
    route."""
    before = counts(place)
    first = create(client, kind, place.form(kind))
    again = create(client, kind, place.form(kind))
    other = create(client, kind, place.form(kind, notes="changed"))
    store = IdempotencyStore(place.engine, route=creation.PROGRAMS[kind].route)
    store.claim("running", place.form(kind))
    running = create(client, kind, place.form(kind), key="running")
    unanswered = create(client, kind, place.form(kind, notes="changed"), key="running")
    earlier = IdempotencyStore(
        place.engine,
        route=creation.PROGRAMS[kind].route,
        clock=lambda: time.time() - STALE_SECONDS - 60,
    )
    earlier.claim("gone", place.form(kind))  # a request that never answered
    gone = create(client, kind, place.form(kind), key="gone")
    after = counts(place)
    other_kind = "wire" if kind == "lauego" else "lauego"
    other_route = create(client, other_kind, place.form(other_kind))

    assert first.status_code == again.status_code == 201
    assert again.json() == first.json()
    assert after[0] == before[0] + 1  # one job: the first request's
    assert (other.status_code, running.status_code, gone.status_code, unanswered.status_code) == (409, 409, 409, 409)
    assert f"already created {first.json()['run']['label']} (job {first.json()['job_id']})" in other.json()["detail"]
    assert (other.json()["type"], other.json()["created"]) == (KEY_CREATED, first.json())
    assert (
        unanswered.json()["detail"]
        == "This Idempotency-Key was used with another body; a new submission needs a new key."
    )
    assert unanswered.json()["type"] == "about:blank" and "created" not in unanswered.json()
    assert "still being processed" in running.json()["detail"] and "created" not in running.json()
    assert "did not finish" in gone.json()["detail"] and "start a new submission" in gone.json()["detail"]
    assert other_route.status_code == 201 and other_route.json()["run"]["kind"] != first.json()["run"]["kind"]
    assert len(dispatch_queue(place.engine)) == 2  # the first request's run, and the other route's


@KINDS
@pytest.mark.parametrize(
    ("broken", "reason"),
    [
        (
            lambda monkeypatch: monkeypatch.setattr(
                "laueportal.jobs.queue.verify_manifest", raising(ValueError("the manifest changed"))
            ),
            "the manifest changed",
        ),
        (
            lambda monkeypatch: monkeypatch.setattr(
                creation, "enqueue_run", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("DM refused it"))
            ),
            "DM refused it",
        ),
    ],
    ids=["queuing-refused", "before-any-queue"],
)
def test_an_enqueue_failure_is_reported_for_that_run_and_the_run_is_failed(
    place, client, monkeypatch, kind, broken: Callable[..., None], reason
):
    """The run exists, so the answer is a 201 for it, with the reason; its job is Failed (by the creation: it never
    reached a queue, so nothing would run it). The answer is kept with its key."""
    broken(monkeypatch)
    response = create(client, kind, place.form(kind))

    assert response.status_code == 201, response.text
    answer = response.json()
    assert (answer["status"], answer["queue_id"]) == ("failed", None)
    assert answer["failure"].startswith(f"{answer['run']['label']} could not be queued") and reason in answer["failure"]
    failed = job(place, answer["job_id"])
    assert (failed.status, failed.phase) == (JobStatus.FAILED, RunPhase.FAILED)
    assert reason in (failed.messages or "")
    assert create(client, kind, place.form(kind)).json() == answer


def test_a_remote_runs_answer_is_kept_with_its_key(place, monkeypatch):
    """The keys are in the database: an ``aps`` run is created and submitted to DM, and the same key answers it
    again."""
    settings, form, dm = on_aps(place, monkeypatch)
    before = counts(place)
    with TestClient(create_app(settings), headers=PORTAL_HEADERS) as client:
        response = create(client, "wire", form)
        again = create(client, "wire", form)

    assert response.status_code == 201, response.text
    assert (response.json()["target"], response.json()["status"], response.json()["failure"]) == ("aps", "queued", None)
    assert again.json() == response.json() and len(dm.submissions) == 1
    assert counts(place)[0] == before[0] + 1


@KINDS
@pytest.mark.parametrize("step", ["publication", "recording", "lookup"])
def test_a_failure_after_the_run_was_saved_is_reported_for_that_run_which_is_failed(
    place, client, monkeypatch, kind, step
):
    """Once its rows are saved, whatever fails (writing its files, recording them, the lookup after it) is a 201 for
    that run with the reason, the run Failed (never left ``created``), and the answer kept with the key: the retry
    answers the same run and creates no second one."""
    lookup = (indexing_service, "get_indexing") if kind == "lauego" else (reconstruction_service, "get_reconstruction")
    owner, name, error = {
        "publication": (rundir, "write_run_inputs", OSError(28, "No space left on device")),
        "recording": (state, "mark_published", OperationalError("UPDATE job", {}, Exception("database is locked"))),
        "lookup": (*lookup, OperationalError("SELECT", {}, Exception("database is locked"))),
    }[step]
    fails_once(monkeypatch, owner, name, error)
    before = counts(place)
    response = create(client, kind, place.form(kind))
    again = create(client, kind, place.form(kind))

    assert response.status_code == 201, response.text
    answer = response.json()
    label = answer["run"]["label"]
    assert (answer["status"], answer["queue_id"]) == ("failed", None)
    assert answer["failure"].startswith(f"{'Indexing' if kind == 'lauego' else 'Reconstruction'} {label}: Run "
                                        "publication failed")  # fmt: skip
    failed = job(place, answer["job_id"])
    assert (failed.status, failed.phase) == (JobStatus.FAILED, RunPhase.FAILED)
    assert again.json() == answer and dispatch_queue(place.engine) == []
    assert counts(place)[0] == before[0] + 1  # one job: no second run


@KINDS
@pytest.mark.parametrize(
    "error",
    [RunPublicationError("lost", job_id=999), OperationalError("COMMIT", {}, Exception("disk I/O error"))],
    ids=["no-run-for-the-job", "unknown"],
)
def test_a_creation_failure_that_may_have_saved_the_run_keeps_the_key(place, monkeypatch, kind, error):
    """Any failure of run creation but its own refusal (a 500) keeps the key claimed: the run may be saved, so the
    retry is a 409, never a second run."""
    program = creation.PROGRAMS[kind]
    monkeypatch.setitem(creation.PROGRAMS, kind, dataclasses.replace(program, create=raising(error)))
    with portal(place.settings, raise_server_exceptions=False) as client:
        failed = create(client, kind, place.form(kind))
        monkeypatch.setitem(creation.PROGRAMS, kind, program)
        retried = create(client, kind, place.form(kind))

    assert failed.status_code == 500
    assert retried.status_code == 409 and "still being processed" in retried.json()["detail"]
    assert dispatch_queue(place.engine) == []


@KINDS
def test_run_creations_own_refusal_is_a_422_creates_nothing_and_releases_the_key(place, client, monkeypatch, kind):
    """Run creation refuses parameters on its own (``jobs.validation.check_creatable``, input resolution) before
    it saves anything: the input files went after the validation. A 422 ``not_creatable``; nothing is created and
    the key is released, so the same key creates the run once the files are back."""
    program = creation.PROGRAMS[kind]

    def validate_then_remove_the_inputs(*args: Any, **kwargs: Any) -> Validated:
        validated = program.validate(*args, **kwargs)
        for frame in FRAMES:
            (place.data / frame).unlink()
        return validated

    monkeypatch.setitem(creation.PROGRAMS, kind, dataclasses.replace(program, validate=validate_then_remove_the_inputs))
    before = counts(place)
    refused = create(client, kind, place.form(kind))
    monkeypatch.setitem(creation.PROGRAMS, kind, program)
    for frame in FRAMES:
        (place.data / frame).write_text("frame")

    assert refused.status_code == 422, refused.text
    assert [error["code"] for error in refused.json()["errors"]] == ["not_creatable"]
    assert counts(place) == before and dispatch_queue(place.engine) == []
    assert create(client, kind, place.form(kind)).status_code == 201


def test_a_remote_run_is_submitted_through_dm(place, monkeypatch):
    """``aps`` and ``crux`` runs go to DM (M0b; ``remote.submission``), never to the worker; the answer names DM's
    job."""
    settings, form, dm = on_aps(place, monkeypatch)
    with portal(settings) as client:
        response = create(client, "wire", form)

    assert response.status_code == 201, response.text
    answer = response.json()
    assert (answer["target"], answer["status"], answer["queue_id"], answer["failure"]) == (
        "aps",
        "queued",
        "dm-001",
        None,
    )
    assert len(dm.submissions) == 1 and dispatch_queue(place.engine) == []


def test_the_key_is_a_database_row_kept_with_its_answer(tmp_path: Path):
    """A repeated request answers the kept 201 with one run queued, on the job table; another body is a 409; the
    key is a row of the database, kept with its answer for 24 hours."""
    place = make_place(tmp_path)
    try:
        with TestClient(create_app(place.settings), headers=PORTAL_HEADERS) as client:
            first = create(client, "wire", place.form("wire"))
            again = create(client, "wire", place.form("wire"))
            other = create(client, "wire", place.form("wire", notes="changed"))
        assert (first.status_code, again.status_code, other.status_code) == (201, 201, 409)
        assert again.json() == first.json()
        assert len(dispatch_queue(place.engine)) == 1
        [key] = keys(place)
        assert (key.key.split(":")[0], key.state, key.answer) == ("createReconstructionRun", KEPT, first.json())
        assert 0 < (key.expires_at - utcnow()).total_seconds() <= TTL_SECONDS
    finally:
        place.engine.dispose()
