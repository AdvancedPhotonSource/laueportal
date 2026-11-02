"""A ``crux`` run's remote observation: the launcher's ``remoteObservation``.

The launcher's ``02-MONITOR`` stage prints ``Observation: <json>`` each time DM runs it, the DM workflow's regex keeps
that JSON as the DM job's ``remoteObservation`` variable, and the remote poller reads it from the records of its one
batched DM call per pass (never a call of its own) into ``job.remote_observation`` (revision 0008), once :func:`accept`
takes it. The format:

- ``phase``: ``uplink``, ``queued``, ``running`` or ``downlink`` (``PHASES``; shown as ``PHASE_LABELS``), and
  ``login_required`` (the Flow waits for a Globus sign-in);
- ``detail``: what the phase is waiting for or doing (``Waiting for PBS allocation``, ``Processing``, ...);
- ``pbs``: what the launcher knows of the PBS job, its ``job_id`` among it, when it knows one;
- ``stale``: the launcher could not see the run when it observed it, so the phase may lag;
- ``observed_at``, ``phase_since``: ISO 8601 times with an offset (UTC, to the second);
- ``submission_id``, the run's; ``flow_state``, the Flow's step (``InputTransfer``, ``RunPbsJob``, ``ResultTransfer``);
- and what the portal keeps without reading it (``flow_run_id``, the transfer's counters, ``remote_progress``).

``aps`` runs have none (``OBSERVED_TARGET``). Only the standard library, so the launcher side may use it too.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

OBSERVED_TARGET = "crux"
"""The execution target whose runs carry an observation (the Globus flow to ALCF); ``aps`` has none."""
PHASE_LABELS = {
    "uplink": "Uplink",
    "queued": "Queued",
    "running": "Running",
    "downlink": "Downlink",
    "login_required": "Login needed",
}
"""The phases (the four, and ``login_required``) and how they are shown."""
PHASES = tuple(PHASE_LABELS)
MAX_LENGTH = 16384
"""The bound (16 KiB): the length of DM's text, and of the object's JSON (``json.dumps``, ASCII)."""
MAX_DEPTH = 1000
"""The deepest nesting kept, the observation itself level 1: SQLite's JSON functions read no deeper (its
``JSON_MAX_DEPTH``; ``json_valid`` is 0 from 1001), so what the database stores stays readable by them."""
STALE_AFTER_SECONDS = 90.0
"""An observation older than this is shown as delayed."""
PUBLISHING_DETAIL = "Publishing results"
"""What the poller writes, phase ``downlink``, before it publishes a returned ``crux`` run."""


class Refused(ValueError):
    """An observation the poller does not keep: its message says why, without repeating the value."""


def aware_time(value: Any) -> datetime | None:
    """``value``, an aware time (ISO 8601 with an offset), in UTC; or None, and
    None too for a time UTC cannot hold (``0001-01-01T00:00:00+01:00``), which the job page could not
    show."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _depth(value: Any) -> int:
    """How deep ``value`` nests (an object or an array is one level more than its deepest item), counted without
    recursion, and only to one past ``MAX_DEPTH``."""
    deepest, pending = 0, [(value, 1)]
    while pending and deepest <= MAX_DEPTH:
        item, level = pending.pop()
        items = item.values() if isinstance(item, dict) else item if isinstance(item, list) else None
        if items is not None:
            deepest = max(deepest, level)
            pending.extend((child, level + 1) for child in items)
    return deepest


def unreadable(value: Any) -> str | None:
    """Why SQLite's JSON functions cannot read ``value`` as the database stores it (``json.dumps``), or None when
    they can: nested deeper than ``MAX_DEPTH``, or holding a number that is not finite (NaN, Infinity; also a number
    too large for a float, which Python reads as Infinity)."""
    if _depth(value) > MAX_DEPTH:
        return f"nested deeper than {MAX_DEPTH} levels"
    try:
        json.dumps(value, allow_nan=False)
    except ValueError:
        return "a number that is not finite"
    return None


def is_publishing(value: Any) -> bool:
    """Whether ``value`` is the poller's own observation, written before it publishes a returned run (``publishing``):
    DM's record then still carries the launcher's last one, older, which is no news."""
    return isinstance(value, dict) and value.get("phase") == "downlink" and value.get("detail") == PUBLISHING_DETAIL


def accept(value: Any, *, backend: str | None, submission_id: str | None, stored: Any) -> dict[str, Any] | None:
    """The observation a DM record carries (``remoteObservation``: the JSON text, or the object) for a job of
    ``backend`` and ``submission_id`` that holds ``stored``; None when it
    carries none (absent or empty) and for a job of another target than ``OBSERVED_TARGET`` (``aps`` has none).

    Raises ``Refused`` for: a text longer than ``MAX_LENGTH``, or not JSON; a value that is not a JSON object, or whose
    JSON is longer than ``MAX_LENGTH``; one SQLite's JSON functions cannot read (``unreadable``); an unknown ``phase``;
    another ``submission_id`` than the job's; an ``observed_at`` that is not an aware time; one older than the
    ``stored`` observation's (compared as times). A ``stored`` value that is not an object, or has no aware
    ``observed_at``, is no stored one."""
    if not value or backend != OBSERVED_TARGET:
        return None
    if isinstance(value, str):
        if len(value) > MAX_LENGTH:
            raise Refused(f"longer than {MAX_LENGTH} characters")
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            raise Refused("not JSON") from None
    if not isinstance(value, dict):
        raise Refused("not a JSON object")
    try:
        length = len(json.dumps(value))
    except (TypeError, ValueError, RecursionError):
        raise Refused("not a JSON object") from None
    if length > MAX_LENGTH:
        raise Refused(f"longer than {MAX_LENGTH} characters as JSON")
    reason = unreadable(value)
    if reason is not None:
        raise Refused(reason)
    phase = value.get("phase")
    if not isinstance(phase, str) or phase not in PHASE_LABELS:
        raise Refused("an unknown phase")
    if value.get("submission_id") != submission_id:
        raise Refused("another submission's")
    observed = aware_time(value.get("observed_at"))
    if observed is None:
        raise Refused("no timezone-aware observed_at")
    previous = aware_time(stored.get("observed_at")) if isinstance(stored, dict) else None
    if previous is not None and observed < previous:
        raise Refused("older than the stored one")
    return value


def publishing(stored: Any, *, submission_id: str | None, now: datetime) -> dict[str, Any]:
    """The observation the poller writes before it publishes a returned ``crux`` run (``now`` an
    aware time): the stored one's keys, with phase ``downlink``, ``PUBLISHING_DETAIL``, not stale, observed now."""
    return {
        **(stored if isinstance(stored, dict) else {}),
        "phase": "downlink",
        "detail": PUBLISHING_DETAIL,
        "stale": False,
        "observed_at": now.isoformat(timespec="seconds"),
        "submission_id": submission_id,
    }
