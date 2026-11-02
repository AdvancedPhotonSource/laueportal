"""The ``Idempotency-Key`` of the create endpoints: a run created once per key.

The first request with a key claims it, before anything is checked or created; once its run exists, the answer is
kept with the key for ``TTL_SECONDS`` (24 h), keyed by (key, route). The same key with the same body (its JSON,
``fingerprint``) then gets the kept answer, whatever has changed since; with another body, a 409. A request known
to have created nothing (a 422 with the issues; ``jobs.creation``) keeps nothing: it releases the key, so the
corrected form may send it again. While the first request runs, the same key is a 409 too (try again). A 409 for
another body after the run was created names that run and carries the kept answer (``KeyConflict.created``; its
type is ``KEY_CREATED``).

The keys live in the database, one row each in ``idempotency_key`` (revision 0007; ``jobs.models.
IdempotencyKey``), under ``<route>:<SHA-256 of the key>``. The claim is an insert in a short transaction of its own
that holds the write lock (``core.db.begin_write``), before anything is checked or created, so of two requests with
one key exactly one claims it; a row past its ``expires_at`` counts as absent and is replaced. The reconciler's
cycle deletes the expired rows (``purge_expired``, one indexed delete). A key claimed by a request that never
answered (it died, or its answer could not be kept) stays claimed until its 24 h are over, never released, since
its run may exist: the same key is a 409, "still being processed" while the claim is recent, and past
``STALE_SECONDS`` a 409 saying the earlier request did not finish (a run may exist; the user checks the runs and
starts a new submission, which gets a new key).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, delete
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from laueportal.core.db import begin_write
from laueportal.core.problems import ProblemError
from laueportal.core.timeutil import utcnow
from laueportal.jobs.models import IdempotencyKey
from laueportal.jobs.schemas import CreatedRun, KeyConflict

TTL_SECONDS = 24 * 3600
"""How long a key and its answer are kept."""
STALE_SECONDS = 5 * 60
"""How long after its claim a request that has not answered is taken to have died: longer than a create takes."""
PENDING = "pending"
KEPT = "kept"
KEY_CREATED = "urn:laueportal:problem:key-created"
"""The problem type of a 409 for another body after the key's run was created: ``KeyConflict.created`` is that
run's kept answer (the form shows the run). Every other 409 of a key is ``about:blank``."""


class IdempotencyConflict(ProblemError):
    """The key is in use: by another body, or by a request still running (409). Its problem is a
    ``KeyConflict``: ``created`` is the kept answer when the key's run exists, and the type then ``KEY_CREATED``."""

    def __init__(self, detail: str, *, created: CreatedRun | None = None) -> None:
        problem_type = "about:blank" if created is None else KEY_CREATED
        super().__init__(409, "Idempotency-Key conflict", detail, problem_type=problem_type)
        self.problem = KeyConflict(**self.problem.model_dump(), created=created)


def _other_body(answer: Mapping[str, Any] | None) -> IdempotencyConflict:
    """The 409 for another body: naming the key's run and carrying its kept answer when it was created (its answer
    lost, the form then edited), so the form shows that run beside a new submission."""
    if answer is None:
        return IdempotencyConflict("This Idempotency-Key was used with another body; a new submission needs a new key.")
    created = CreatedRun.model_validate(answer)
    return IdempotencyConflict(
        f"This Idempotency-Key already created {created.run.label} (job {created.job_id}) with other values: open it, "
        "or start a new submission (a new key) to create another run.",
        created=created,
    )


def fingerprint(body: Any) -> str:
    """The SHA-256 of the body's JSON, its object members sorted: the same values give the same fingerprint."""
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class Claim:
    """A request's hold on its key: ``replay`` is the kept answer when the key's run exists already (answer it),
    ``None`` when this request owns the key and creates the run."""

    name: str
    """The key's row (``idempotency_key.key``: ``<route>:<SHA-256 of the key>``)."""
    fingerprint: str
    replay: Mapping[str, Any] | None


class IdempotencyStore:
    """The keys of one route (``route``, its create endpoint's operation ID) in ``engine``'s database. ``other_body``
    makes the 409 for another body from the key's kept answer (``None`` while it has none): a run's
    ``IdempotencyConflict`` by default; the live create's carries its session (``jobs.live_session``)."""

    def __init__(
        self,
        engine: Engine,
        *,
        route: str,
        clock: Callable[[], float] = time.time,
        other_body: Callable[[Mapping[str, Any] | None], ProblemError] | None = None,
    ) -> None:
        self.engine = engine
        self.prefix = f"{route}:"
        self.clock = clock
        """The time in seconds since the epoch (``time.time``): when a key is claimed, and how long ago."""
        self.other_body = other_body or _other_body

    def _name(self, key: str) -> str:
        return self.prefix + hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), UTC)

    def claim(self, key: str, body: Any) -> Claim:
        """Claim ``key`` for ``body``, or find its kept answer (``Claim.replay``). A 409 when the key is in use with
        another body or by a request that has not answered (still running, or past ``STALE_SECONDS`` gone)."""

        name, digest, now = self._name(key), fingerprint(body), self._now()
        with Session(self.engine) as session, session.begin():
            begin_write(session)  # the read and the insert are one step: of two claims, one inserts
            row = session.get(IdempotencyKey, name)
            if row is None or row.expires_at <= now:  # absent, or expired (counts as absent): claim it
                if row is None:
                    row = IdempotencyKey(key=name)
                    session.add(row)
                row.fingerprint, row.state, row.claimed_at = digest, PENDING, now
                row.answer, row.expires_at = None, now + timedelta(seconds=TTL_SECONDS)
                return Claim(name, digest, None)
            found, state, claimed_at, answer = row.fingerprint, row.state, row.claimed_at, row.answer
        if found != digest:
            raise self.other_body(answer if state == KEPT else None)
        if state != KEPT or answer is None:
            raise IdempotencyConflict(_unanswered(claimed_at, now))
        return Claim(name, digest, answer)

    def keep(self, claim: Claim, answer: Mapping[str, Any]) -> None:
        """Keep the answer of the claimed key's created run for ``TTL_SECONDS``."""
        now = self._now()
        with Session(self.engine) as session, session.begin():
            begin_write(session)
            row = session.get(IdempotencyKey, claim.name)
            if row is None:  # never gone before its 24 h; written again all the same
                row = IdempotencyKey(key=claim.name, claimed_at=now)
                session.add(row)
            row.fingerprint, row.state, row.answer = claim.fingerprint, KEPT, dict(answer)
            row.expires_at = now + timedelta(seconds=TTL_SECONDS)

    def release(self, claim: Claim) -> None:
        """Give the key up: the request created nothing."""
        with Session(self.engine) as session, session.begin():
            session.execute(
                delete(IdempotencyKey).where(
                    IdempotencyKey.key == claim.name,
                    IdempotencyKey.state == PENDING,
                    IdempotencyKey.fingerprint == claim.fingerprint,
                )
            )


def _unanswered(claimed_at: datetime, now: datetime) -> str:
    """The 409's detail for a key whose request has not answered: still running, or gone."""
    if (now - claimed_at).total_seconds() > STALE_SECONDS:
        return (
            "An earlier request with this Idempotency-Key did not finish, and its run may exist: check the runs, "
            "then start a new submission (a new key)."
        )
    return "A request with this Idempotency-Key is still being processed; try again in a moment."


def purge_expired(engine: Engine, *, now: datetime | None = None) -> int:
    """Delete the keys past their ``expires_at`` (one indexed delete; the reconciler's cycle): how many went."""
    with Session(engine) as session, session.begin():
        result = session.execute(
            delete(IdempotencyKey).where(IdempotencyKey.expires_at <= (now or utcnow())),
            execution_options={"synchronize_session": False},
        )
    return int(result.rowcount) if isinstance(result, CursorResult) else 0
