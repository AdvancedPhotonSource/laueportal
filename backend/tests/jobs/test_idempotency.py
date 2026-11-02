"""The ``Idempotency-Key`` store in SQLite (``jobs.idempotency``): the claim race, expiry and the purge. The keys'
behaviour through the create endpoints is ``test_creation.py``'s."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, event, select
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.orm import Session

from laueportal.core.settings import Settings
from laueportal.jobs import reconciler
from laueportal.jobs.idempotency import (
    KEPT,
    PENDING,
    TTL_SECONDS,
    Claim,
    IdempotencyConflict,
    IdempotencyStore,
    purge_expired,
)
from laueportal.jobs.models import IdempotencyKey
from laueportal.jobs.schemas import CreatedRun, RunKind, RunReference, RunStatus
from tests.settings_support import make_database

ROUTE = "createReconstructionRun"
BODY = {"notes": "a run"}
ANSWER = CreatedRun(
    run=RunReference(kind=RunKind.RECONSTRUCTION, id=1, label="R1"),
    job_id=1,
    status=RunStatus.QUEUED,
    target="local",
    queue_id="run_1",
    failure=None,
).model_dump(mode="json")
CLAIMS = 8


@pytest.fixture
def database(tmp_path) -> Iterator[tuple[Settings, Engine]]:
    settings, engine = make_database(tmp_path)
    yield settings, engine
    engine.dispose()


@pytest.fixture
def engine(database: tuple[Settings, Engine]) -> Engine:
    return database[1]


def store_at(engine: Engine, seconds_ago: float = 0.0) -> IdempotencyStore:
    """A store whose clock is ``seconds_ago`` behind."""
    return IdempotencyStore(engine, route=ROUTE, clock=lambda: time.time() - seconds_ago)


def rows(engine: Engine) -> dict[str, tuple[str, str]]:
    """Each row's ``(state, fingerprint)``, by its name (``Claim.name``: ``<route>:<SHA-256 of the key>``)."""
    with Session(engine) as session:
        return {row.key: (row.state, row.fingerprint) for row in session.scalars(select(IdempotencyKey))}


def kept(engine: Engine, key: str, *, seconds_ago: float = 0.0, body: Any = BODY) -> Claim:
    store = store_at(engine, seconds_ago)
    claim = store.claim(key, body)
    store.keep(claim, ANSWER)
    return claim


def test_of_claims_of_one_key_at_once_exactly_one_inserts(engine):
    """``CLAIMS`` requests claim one key at the same moment, each in its own connection: one inserts the row and
    owns the key, every other finds it claimed (the 409 of a request still running)."""
    store = store_at(engine)
    start = threading.Barrier(CLAIMS)
    outcomes: list[Claim | IdempotencyConflict] = []
    lock = threading.Lock()

    def claim() -> None:
        start.wait(timeout=30)
        try:
            outcome: Claim | IdempotencyConflict = store.claim("key", BODY)
        except IdempotencyConflict as error:
            outcome = error
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=claim) for _ in range(CLAIMS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    owners = [outcome for outcome in outcomes if isinstance(outcome, Claim)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, IdempotencyConflict)]
    assert len(outcomes) == CLAIMS
    assert [owner.replay for owner in owners] == [None]
    assert len(refused) == CLAIMS - 1
    assert all("still being processed" in (error.problem.detail or "") for error in refused)
    assert [state for state, _ in rows(engine).values()] == [PENDING]


def test_a_key_past_its_expiry_counts_as_absent_and_is_replaced(engine):
    """Within its 24 h a kept key answers its run again (and another body is the 409 with it); once past them, the
    row counts as absent: the same key is claimed anew, with any body, and the row replaced. So is a claim never
    answered."""
    kept(engine, "recent", seconds_ago=TTL_SECONDS - 60)
    kept(engine, "expired", seconds_ago=TTL_SECONDS + 1)
    store_at(engine, TTL_SECONDS + 1).claim("unanswered", BODY)
    store = store_at(engine)

    assert store.claim("recent", BODY).replay == ANSWER
    with pytest.raises(IdempotencyConflict) as other:
        store.claim("recent", {"notes": "changed"})
    assert other.value.problem.model_dump()["created"] == ANSWER
    renewed = store.claim("expired", {"notes": "changed"})
    reclaimed = store.claim("unanswered", BODY)

    assert (renewed.replay, reclaimed.replay) == (None, None)
    states = rows(engine)
    assert states[renewed.name] == (PENDING, renewed.fingerprint) and renewed.fingerprint != reclaimed.fingerprint
    assert states[reclaimed.name] == (PENDING, reclaimed.fingerprint)
    assert len(states) == 3
    with pytest.raises(IdempotencyConflict, match="still being processed"):
        store.claim("expired", {"notes": "changed"})  # its new claim holds it


def test_the_purge_deletes_the_expired_keys_with_one_indexed_delete(engine):
    """``purge_expired`` deletes every row past its ``expires_at``, kept or not, and nothing else, in one
    ``DELETE`` that searches the ``expires_at`` index."""
    fresh = [kept(engine, "kept"), store_at(engine).claim("pending", BODY)]
    kept(engine, "old-kept", seconds_ago=TTL_SECONDS + 1)
    store_at(engine, TTL_SECONDS + 1).claim("old-pending", BODY)
    statements: list[tuple[str, Any]] = []

    def record(_connection: Any, _cursor: Any, statement: str, parameters: Any, *_: Any) -> None:
        statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", record)
    try:
        purged = purge_expired(engine)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert purged == 2
    assert set(rows(engine)) == {claim.name for claim in fresh}
    assert {state for state, _ in rows(engine).values()} == {PENDING, KEPT}
    assert purge_expired(engine) == 0
    [(sql, parameters)] = [item for item in statements if item[0].lstrip().upper().startswith("DELETE")]
    with engine.connect() as connection:
        plan = connection.exec_driver_sql(f"EXPLAIN QUERY PLAN {sql}", parameters).fetchall()
    assert any("USING INDEX ix_idempotency_key_expires_at" in str(row[-1]) for row in plan), plan


def test_a_reconciler_cycle_purges_the_expired_keys(database):
    """The reconciler's cycle purges the expired keys, on the database alone."""
    _, engine = database
    kept(engine, "old", seconds_ago=TTL_SECONDS + 1)
    current = kept(engine, "current")

    result = reconciler.reconcile_once(engine, reconciler.ReconcilePolicy())

    assert result.keys_purged == 1 and not result
    assert set(rows(engine)) == {current.name}


def test_a_failed_purge_is_logged_and_never_the_cycles(database, monkeypatch, caplog):
    """The purge's failure is logged with its traceback and never the cycle's: ``reconcile_once`` returns with
    ``keys_purged == 0``, the expired key is left, and the next cycle purges it. A database error (``OperationalError``:
    a write lock past the busy timeout) ends the cycle instead, as anywhere in the cycle, for the worker's reconciler
    thread to log once per streak with no mark (``tests/jobs/test_worker.py``)."""
    _, engine = database
    old = kept(engine, "old", seconds_ago=TTL_SECONDS + 1)
    failure: list[Exception] = [
        DatabaseError("DELETE FROM idempotency_key", {}, Exception("database disk image is malformed"))
    ]

    def fails(_engine: Engine) -> int:
        raise failure[0]

    monkeypatch.setattr(reconciler, "purge_expired", fails)
    with caplog.at_level(logging.WARNING, logger=reconciler.__name__):
        result = reconciler.reconcile_once(engine, reconciler.ReconcilePolicy())

    assert result.keys_purged == 0 and not result
    [record] = [r for r in caplog.records if r.getMessage() == "Could not purge the expired Idempotency-Keys"]
    assert record.levelno == logging.WARNING and record.exc_info is not None
    assert set(rows(engine)) == {old.name}
    failure[0] = OperationalError("DELETE FROM idempotency_key", {}, Exception("database is locked"))
    with pytest.raises(OperationalError):
        reconciler.reconcile_once(engine, reconciler.ReconcilePolicy())
    assert set(rows(engine)) == {old.name}
    monkeypatch.undo()
    assert reconciler.reconcile_once(engine, reconciler.ReconcilePolicy()).keys_purged == 1
