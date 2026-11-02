"""The cases of the remote-observation goldens (``goldens.json``), which pin what the launcher prints and what the
portal keeps (``remote.observation``, ``remote.stage``):

1. ``observations``: for each case of ``LAUNCHER_CASES`` (the Flow's state, the previous observation and observer
   result in the state file, the clock at ``NOW``), the JSON text of the launcher's printed line,
   ``Observation: <json>``, which DM's regex keeps as ``remoteObservation``;
2. ``intake``: for each case of ``intake_cases()``, whether the poller keeps the value a DM record carries for a job
   of the case's target holding the case's stored observation;
3. ``publishing``: the observation stored before a returned ``crux`` run is published (``before``,
   ``observations["downlink"]``) and after the pass (``after``, Downlink "Publishing results", the clock at
   ``PUBLISHED``).

The goldens are committed; a change to a case or to what the code prints changes them on purpose, reviewed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

GOLDENS = Path(__file__).parent / "goldens.json"
NOW = datetime.fromisoformat("2026-10-25T12:00:00+00:00")
"""The clock of the cases, but for ``publishing`` (``PUBLISHED``)."""
PUBLISHED = NOW + timedelta(seconds=60)
"""The clock when the poller publishes (``publishing``): later than the stored observation's time (``NOW``), so that the
golden shows ``observed_at`` refreshed and ``phase_since`` kept."""
SUBMISSION_ID = "6f1c2a9e-4b7d-4c1a-9e3f-2d8b5a7c0e14"
FLOW_ID = "0c5e7a12-1d3b-4f6a-8e2c-9b7d5f3a1e60"
PBS_ID = "123456.crux-pbs-01"


def at(seconds: float) -> str:
    """``NOW`` plus ``seconds``, in the launcher's format (UTC, to the second)."""
    return (NOW + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _flow(state: str | None, *, status: str = "ACTIVE") -> dict[str, Any]:
    actions = [] if state is None else [{"state_name": state, "status": "ACTIVE"}]
    return {"status": status, "details": {"action_statuses": actions}}


def _observer(seconds: float, status: str | None, **extra: Any) -> dict[str, Any]:
    """A result of the reference launcher's observer (``monitor.read_run_status``) made ``seconds`` from ``NOW``."""
    pbs = {"job_id": PBS_ID} if status is None else {"job_id": PBS_ID, "status": status}
    return {"submission_id": SUBMISSION_ID, "observed_at": at(seconds), "receipt_present": True, "pbs": pbs, **extra}


def _previous(phase: str, detail: str, since: float, **extra: Any) -> dict[str, Any]:
    return {"phase": phase, "detail": detail, "flow_state": "RunPbsJob", "phase_since": at(since), **extra}


CHALLENGE_TEXT = "never-printed-challenge"
"""A value inside the Globus sign-in challenge (``authorization_parameters``): no output holds it."""
CHALLENGE = {
    "session_required_single_domain": [f"{CHALLENGE_TEXT}.example.org"],
    "session_message": f"Session reauthentication required ({CHALLENGE_TEXT})",
}
"""A Flow run's sign-in challenge, as Globus Flows reports a session or consent requirement."""


def _paused(state: str | None, *, on: str, state_name: str | None = None) -> dict[str, Any]:
    """An ``INACTIVE`` Flow run waiting for a sign-in: ``CHALLENGE`` on its current action in ``state`` (``on``
    ``"action"``) or on the run's own ``details`` (``"run"``, with the run's ``state_name``)."""
    actions = [] if state is None else [{"state_name": state, "status": "INACTIVE", "details": {}}]
    details: dict[str, Any] = {"action_statuses": actions}
    if on == "action":
        actions[-1]["details"] = {"authorization_parameters": CHALLENGE}
    else:
        details.update(authorization_parameters=CHALLENGE, state_name=state_name)
    return {"status": "INACTIVE", "details": details}


LAUNCHER_CASES: dict[str, dict[str, Any]] = {
    # The Flow's step alone (the reference launcher without its observer: a launcher that does not list).
    "uplink": {"flow": _flow("InputTransfer"), "previous": None, "observer": None},
    "at-alcf": {"flow": _flow("RunPbsJob"), "previous": None, "observer": None},
    "at-alcf-late": {
        "flow": _flow("RunPbsJob"),
        "previous": _previous("queued", "Waiting for status", -120),
        "observer": None,
    },
    # The observer's PBS receipt and state (the reference launcher's observer endpoint).
    "submitting": {"flow": _flow("RunPbsJob"), "previous": None, "observer": _observer(-10, None)},
    "pbs-queued": {"flow": _flow("RunPbsJob"), "previous": None, "observer": _observer(-10, "Q")},
    "pbs-held": {"flow": _flow("RunPbsJob"), "previous": None, "observer": _observer(-10, "H")},
    "running": {
        "flow": _flow("RunPbsJob"),
        "previous": _previous("queued", "Waiting for PBS allocation", -300),
        "observer": _observer(-10, "R", progress={"heartbeat_at": at(-15), "n_succeeded": 3, "n_failed": 0}),
    },
    "finishing": {
        "flow": _flow("RunPbsJob"),
        "previous": _previous("running", "Processing", -600),
        "observer": _observer(-10, "E"),
    },
    "pbs-finished": {"flow": _flow("RunPbsJob"), "previous": None, "observer": _observer(-10, "F")},
    "observer-stale": {
        "flow": _flow("RunPbsJob"),
        "previous": _previous("running", "Processing", -600),
        "observer": _observer(-300, "R"),
    },
    "scheduler-unavailable": {
        "flow": _flow("RunPbsJob"),
        "previous": _previous("running", "Processing", -600),
        "observer": _observer(-10, None, scheduler_error="PBS status unavailable"),
    },
    "downlink": {"flow": _flow("ResultTransfer"), "previous": None, "observer": None},
    "flow-succeeded": {"flow": _flow(None, status="SUCCEEDED"), "previous": None, "observer": None},
    "workflow-unavailable": {
        "flow": None,  # Globus does not answer
        "previous": _previous("running", "Processing", -600, observed_at=at(-60), submission_id=SUBMISSION_ID),
        "observer": None,
    },
    # Login needed: the Flow waits for a Globus sign-in, the challenge on its current action ...
    "login-required": {
        "flow": _paused("RunPbsJob", on="action"),
        "previous": _previous("running", "Processing", -600),
        "observer": None,
    },
    # ... or on the run alone (no action names a step: the run's own state_name)
    "login-required-on-the-run": {
        "flow": _paused(None, on="run", state_name="InputTransfer"),
        "previous": None,
        "observer": None,
    },
    # resumed: the login pause is not inherited (with no step, the reference launcher's phase would otherwise stay the
    # last one)
    "login-resumed": {
        "flow": _flow(None),
        "previous": {
            "phase": "login_required",
            "detail": "Sign in to Globus and resume this run",
            "flow_state": None,
            "phase_since": at(-120),
        },
        "observer": None,
    },
    # INACTIVE for another reason than a sign-in: the step's phase
    "inactive-without-login": {
        "flow": {
            "status": "INACTIVE",
            "details": {
                "code": "PermissionDenied",
                "action_statuses": [{"state_name": "InputTransfer", "status": "INACTIVE", "details": {}}],
            },
        },
        "previous": None,
        "observer": None,
    },
}
"""The launcher's cases: the Flow's status (``None``: ``get_status`` fails), the observation the state file holds from
the run before, and the observer's result it holds (``None``: none). With no step in the Flow's actions, the reference
launcher asks for the run's log, which the capture's Globus stand-in does not have: it keeps the previous ``flow_state``
(``login-resumed``)."""


def nested(levels: int) -> list[Any]:
    """An array nesting ``levels`` deep (``[[]]`` is 2)."""
    value: list[Any] = []
    for _ in range(levels - 1):
        value = [value]
    return value


def intake_cases() -> dict[str, dict[str, Any]]:
    """What DM's record carries (``value``) for a job of ``backend`` holding ``stored``, one case per rule of the
    reference poller (kept, carried none, or refused), the 16 KiB bound at the boundary (``*-at-the-bound``,
    ``*-by-one``), and the portal's further refusals (``remote.observation.unreadable``; an ``observed_at`` UTC cannot
    hold)."""
    good = {"phase": "running", "detail": "Processing", "observed_at": at(0), "submission_id": SUBMISSION_ID}
    stored = {**good, "observed_at": at(-10)}

    def text(**changes: Any) -> str:
        return json.dumps({**good, **changes}, separators=(",", ":"))

    padding = "x" * 16384
    spaced = 16384 - len(json.dumps({**good, "detail": ""}))  # the detail that makes both lengths 16384
    return {
        "kept": {"value": text(), "stored": stored},
        "kept-as-an-object": {"value": dict(good), "stored": stored},
        "kept-at-the-stored-time": {"value": text(observed_at=at(-10)), "stored": stored},
        "kept-without-a-stored-one": {"value": text(), "stored": None},
        "kept-with-another-offset": {"value": text(observed_at="2026-10-25T07:00:00-05:00"), "stored": stored},
        "none-absent": {"value": None, "stored": stored},
        "none-empty": {"value": "", "stored": stored},
        "none-for-aps": {"value": text(), "stored": None, "backend": "aps"},
        "text-too-long": {"value": text(detail=padding[: 16385 - len(text(detail=""))]), "stored": stored},
        "not-json": {"value": "broken JSON", "stored": stored},
        "not-an-object": {"value": '["running"]', "stored": stored},
        "object-too-long": {"value": {**good, "detail": padding}, "stored": stored},
        "kept-at-the-bound": {"value": json.dumps({**good, "detail": padding[:spaced]}), "stored": stored},
        "object-too-long-by-one": {"value": {**good, "detail": padding[: spaced + 1]}, "stored": stored},
        "kept-at-the-depth-bound": {"value": text(nested=nested(999)), "stored": stored},
        "nested-too-deep": {"value": text(nested=nested(1000)), "stored": stored},
        "not-finite": {"value": text(progress=float("nan")), "stored": stored},
        "not-finite-too-large": {"value": text()[:-1] + ',"progress":1e999}', "stored": stored},
        "observed-at-out-of-range": {"value": text(observed_at="0001-01-01T00:00:00+01:00"), "stored": None},
        "unknown-phase": {"value": text(phase="held"), "stored": stored},
        "phase-not-text": {"value": text(phase=["running"]), "stored": stored},
        "another-submission": {"value": text(submission_id="0d4f2b8e-9c1a-4e7f-8b3d-5a6c2e9f1b70"), "stored": stored},
        "no-observed-at": {
            "value": json.dumps({k: v for k, v in good.items() if k != "observed_at"}),
            "stored": stored,
        },
        "naive-observed-at": {"value": text(observed_at="2026-10-25T12:00:00"), "stored": stored},
        "observed-at-not-a-time": {"value": text(observed_at="yesterday"), "stored": stored},
        "observed-at-not-text": {"value": text(observed_at=1761393600), "stored": stored},
        "older-than-the-stored-one": {"value": text(observed_at=at(-20)), "stored": stored},
        "kept-login-required": {"value": text(phase="login_required"), "stored": stored},
    }
