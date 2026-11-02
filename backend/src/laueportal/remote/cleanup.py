"""Remove what a Finished, published remote run left behind, from the remote poller's listing
(``remote.poller.RemotePoller``, the worker's thread: at its start, then at most every ten minutes while remote runs
are active or one ended since; ``remote.poller.run_poller``, ``laueportal remote poll --once``).

A remote run leaves, besides its run directory: the return inbox's remnants (``<run>/incoming/<submission>/``,
where publication leaves the runner's ``run.json`` and ``.runner.lock``), the plan's copy in the DM inbox
(``<submission_directory>/<submission>.json``), the launcher's state file (``<state_directory>/<submission>.json``)
and, for ``crux``, its folder on Eagle (``<workspace>/runs/<submission>``: control files, input copies, results,
and the MPI runner's ``shards/``). :func:`clean_up` goes through the DM inbox's plan copies; for each whose job is
Finished in this portal's database and whose results are published as Finished (``run.json`` names its
submission with the status ``Finished``, and the publication marker names it too, both written last),
it:

1. removes the return inbox's remnants, when nothing else is left there (anything else is kept and logged, as is
   a removal the file system refuses or an ``incoming/`` that leads outside the run directory; neither stops
   step 2);
2. with ``dm.cleanup_workflow`` set, submits that DM workflow once, whose stage is the launcher's ``cleanup``
   (``remote.stage.cleanup``: the Eagle folder, then the state file; it too refuses a run whose ``run.json`` is not
   Finished); once DM reports it done, removes the inbox copy, which the launcher read. The run directory's
   ``.cleanup-job.json`` keeps the job: ``{"status": "submitting", "attempts": n}`` written before DM is asked (a
   record that cannot be written submits nothing; one left ``submitting``, its job's ID not recorded or DM's
   answer not received in time, is submitted again, at most ``SUBMISSION_ATTEMPTS`` times in all: a second
   clean-up job is harmless; only DM's refusal removes it), then ``{"id": ...}``, and
   ``{"id": ..., "status": "failed"}`` (or ``"aborted"``) once DM says so. A failed clean-up job is reported once
   per poller process, never looked up again and never submitted again on its own (remove ``.cleanup-job.json``
   to retry, as for a stop's ``.cancel-job.json``).

Without ``dm.cleanup_workflow``, only step 1 runs, and the inbox copy stays for the launcher's clean-up to read later. A
Failed or Cancelled run (an MPI run's ranks' results and diagnostics exist only in its Eagle folder's ``shards/``) and a
run that was not published keep everything, for diagnosis: nothing of theirs is removed. Inbox copies of submissions
this database does not know (another portal's, beside it) are never touched. A pass submits or looks up at most
``CLEANUP_CALLS_PER_PASS`` DM jobs, so a backlog drains over several passes; a run whose DM call or file operation
failed is retried at the next pass, then backs off (skipped for 1, 3, 7, ... passes, at most ``BACKOFF_PASSES``), so it
cannot use up a pass's calls. The poller keeps that, and the warnings already logged (each once per process: a kept
return inbox, a failed clean-up job), in a :class:`CleanupState` across its passes; without ``dm.cleanup_workflow``, a
run whose remnants are gone or kept is not looked at again by that process.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from laueportal.jobs.backends import REMOTE_BACKENDS
from laueportal.jobs.models import Job
from laueportal.jobs.rundir import (
    CLEANUP_JOB_FILENAME,
    INCOMING_DIRECTORY,
    RUN_SUMMARY_FILENAME,
    RUNNER_LOCK_FILENAME,
    write_json_file,
)
from laueportal.jobs.state import JobStatus
from laueportal.remote.dm import DMClient
from laueportal.remote.paths import child_path
from laueportal.remote.plan import submission_path
from laueportal.remote.settings import RemoteSettings

logger = logging.getLogger(__name__)

CLEANUP_JOB_RECORD = CLEANUP_JOB_FILENAME
"""In the run directory: the DM clean-up job submitted for it (a reserved name, ``rundir.RESERVED_RUN_FILENAMES``)."""
PUBLICATION_MARKER = ".bdp-publication.json"
"""``remote.poller.PUBLICATION_MARKER``."""
REMNANTS = frozenset({RUN_SUMMARY_FILENAME, RUNNER_LOCK_FILENAME})
"""What publication leaves in the return inbox (the runbook's live smoke test, step 4)."""
CLEANUP_CALLS_PER_PASS = 10
"""The most DM calls (submissions and look-ups of clean-up jobs) one pass makes."""
SUBMISSION_ATTEMPTS = 3
"""How often a clean-up job whose record still says ``submitting`` (DM was asked, then its ID could not be
recorded, or DM did not answer in time) is submitted before the poller stops and asks for a look in DM."""
BACKOFF_PASSES = 32
"""A run whose DM call or file operation failed is skipped for 2**(failures - 1) - 1 passes, at most this less
one (about five hours at ten minutes a pass)."""
_IN_CHUNK = 500
"""Submission IDs per database query (SQLite's variable limit is far above)."""


def _uuid(name: str) -> str | None:
    try:
        return name if str(UUID(name)) == name else None
    except ValueError:
        return None


def _read(path: Path) -> Any:
    if path.stat().st_size > 1024 * 1024:
        raise ValueError(f"Control document is too large: {path}")
    return json.loads(path.read_text())


FINISHED = "Finished"
"""``run.json``'s status of a run whose every input succeeded (``JobStatus.FINISHED``)."""


def published_finished(run_directory: Path, submission_id: str) -> bool:
    """Whether the run directory holds this submission's whole publication (its ``run.json`` is written last) of a
    Finished run."""
    try:
        summary, marker = _read(run_directory / RUN_SUMMARY_FILENAME), _read(run_directory / PUBLICATION_MARKER)
    except (OSError, ValueError):
        return False
    return (
        isinstance(summary, dict)
        and isinstance(marker, dict)
        and summary.get("submission_id") == submission_id
        and summary.get("status") == FINISHED
        and marker.get("submission_id") == submission_id
    )


def remove_inbox_remnants(run_directory: Path, submission_id: str) -> str | None:
    """Remove ``<run>/incoming/<submission>/`` when it holds only what publication leaves (and ``incoming/`` when
    that leaves it empty). Returns ``None`` once it is gone, else why it was kept: anything else there, or an inbox
    a link leads elsewhere (a symbolic ``incoming/`` or ``<submission>``; ``remote.paths.child_path``)."""
    incoming = run_directory / INCOMING_DIRECTORY
    inbox = incoming / submission_id
    if incoming.is_symlink():
        return f"{incoming} is a symbolic link"
    if not inbox.exists() and not inbox.is_symlink():
        return None
    if inbox.is_symlink() or not inbox.is_dir():
        return f"{inbox} is not a directory"
    try:
        inbox = child_path(run_directory, inbox)
    except ValueError:
        return f"{inbox} leads outside its run directory"
    entries = list(inbox.iterdir())
    others = sorted(entry.name for entry in entries if entry.name not in REMNANTS or not entry.is_file())
    if others:
        return f"its return inbox still holds {others}"
    shutil.rmtree(inbox)
    try:
        incoming.rmdir()  # only when no other submission's inbox is there
    except OSError:
        pass
    return None


@dataclass
class CleanupState:
    """What the poller's clean-up keeps across its passes, in one process (``remote.poller.RemotePoller``)."""

    warned: set[tuple[str, str]] = field(default_factory=set)
    """(submission, warning) already logged: each once."""
    settled: set[str] = field(default_factory=set)
    """Submissions with nothing left to do here (without ``dm.cleanup_workflow``): not looked at again."""
    failures: dict[str, int] = field(default_factory=dict)
    """Consecutive failed passes of a submission."""
    skipping: dict[str, int] = field(default_factory=dict)
    """Passes a submission is still skipped for (its back-off)."""
    announced: bool = False
    """The missing ``dm.cleanup_workflow`` was logged."""

    def warn(self, submission_id: str, warning: str) -> None:
        if (submission_id, warning) not in self.warned:
            self.warned.add((submission_id, warning))
            logger.warning("Remote run %s: %s", submission_id, warning)

    def backing_off(self, submission_id: str) -> bool:
        left = self.skipping.get(submission_id, 0)
        if left:
            self.skipping[submission_id] = left - 1
        return left > 0

    def failed(self, submission_id: str) -> None:
        failures = self.failures[submission_id] = self.failures.get(submission_id, 0) + 1
        self.skipping[submission_id] = min(2 ** (failures - 1), BACKOFF_PASSES) - 1

    def succeeded(self, submission_id: str) -> None:
        self.failures.pop(submission_id, None)
        self.skipping.pop(submission_id, None)


def _finished_submissions(engine: Engine, submission_ids: list[str]) -> list[tuple[str, str]]:
    """(submission ID, run directory) of this database's Finished remote jobs among ``submission_ids``."""
    found: list[tuple[str, str]] = []
    with Session(engine) as session:
        for start in range(0, len(submission_ids), _IN_CHUNK):
            rows = session.execute(
                select(Job.submission_id, Job.run_directory).where(
                    Job.submission_id.in_(submission_ids[start : start + _IN_CHUNK]),
                    Job.backend.in_(REMOTE_BACKENDS),
                    Job.status == int(JobStatus.FINISHED),
                    Job.run_directory.is_not(None),
                )
            ).all()
            found.extend((str(submission_id), str(run_directory)) for submission_id, run_directory in rows)
    return sorted(found)


def clean_up(
    *,
    engine: Engine,
    settings: RemoteSettings,
    client: Any = None,
    calls: int = CLEANUP_CALLS_PER_PASS,
    state: CleanupState | None = None,
) -> list[str]:
    """One pass over the DM inbox (the module's steps), with ``state`` kept across the poller's passes; returns the
    submissions whose inbox copy it removed."""
    state = state if state is not None else CleanupState()
    inbox = settings.submissions()
    try:
        names = sorted(name for path in inbox.glob("*.json") if (name := _uuid(path.stem)) is not None)
    except OSError as error:
        logger.warning("Could not list the DM inbox %s: %s", inbox, error)
        return []
    if not names:
        return []
    workflow = settings.dm.cleanup_workflow
    client = client or (DMClient(settings.dm) if workflow else None)
    budget = _Budget(calls)
    cleaned: list[str] = []
    for submission_id, run_directory in _finished_submissions(engine, names):
        if submission_id in state.settled or state.backing_off(submission_id):
            continue
        run = Path(run_directory)
        try:
            if not published_finished(run, submission_id):
                continue
            try:
                kept = remove_inbox_remnants(run, submission_id)
            except OSError as error:  # e.g. a directory the transfer account owns: logged, and the rest goes on
                kept = None
                state.warn(submission_id, f"could not remove its return inbox: {error}")
            else:
                if kept is not None:
                    state.warn(submission_id, f"{kept}; kept")
            if not workflow:
                if not state.announced:
                    state.announced = True
                    logger.warning(
                        "dm.cleanup_workflow is not set: the Finished runs' inbox copies, launcher state files and "
                        "Eagle folders stay (only their return inboxes' remnants are removed)"
                    )
                if kept is not None or not (run / INCOMING_DIRECTORY / submission_id).exists():
                    state.settled.add(submission_id)
                continue
            _launcher_cleanup(client, settings, run, submission_id, state, cleaned, budget)
            state.succeeded(submission_id)
        except Exception:  # a transient DM or filesystem failure: retried at the next pass, then backing off
            state.failed(submission_id)
            logger.exception("Could not clean up after remote run %s", submission_id)
    return cleaned


@dataclass
class _Budget:
    """The DM calls a pass may still make; a call that fails counts too."""

    left: int

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True


def _launcher_cleanup(
    client: Any,
    settings: RemoteSettings,
    run: Path,
    submission_id: str,
    state: CleanupState,
    cleaned: list[str],
    budget: _Budget,
) -> None:
    """Step 2 for one run (the module's record); no DM call once the pass's ``budget`` is spent."""
    record = run / CLEANUP_JOB_RECORD
    try:
        data = _read(record)
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"Unreadable clean-up record: {record}")
    status = data.get("status")
    if status in {"failed", "aborted"}:
        state.warn(
            submission_id,
            f"clean-up DM job {data.get('id')} {status}; inspect its stage diagnostics, then remove {record} to retry",
        )
        return
    if "id" not in data:  # never submitted, or DM was asked and its job's ID not recorded
        attempts = int(data.get("attempts", 0))
        if attempts >= SUBMISSION_ATTEMPTS:
            state.warn(
                submission_id,
                f"its clean-up was submitted {attempts} times without its DM job ID being recorded; look the jobs "
                f"up in DM, then remove {record} to retry",
            )
            return
        if not budget.take():
            return
        # Before DM is asked: no record, no job.
        write_json_file(record, {"status": "submitting", "attempts": attempts + 1}, overwrite=attempts > 0)
        try:
            job = client.clean_up(submission_id)
        except subprocess.CalledProcessError:
            record.unlink(missing_ok=True)  # DM refused it: submitted again later, afresh
            raise
        # Any other failure (DM did not answer in time: it may have accepted the job) keeps the record, so this
        # attempt counts towards SUBMISSION_ATTEMPTS.
        write_json_file(record, {"id": job["id"]}, overwrite=True)
        logger.info("Remote run %s: clean-up DM job %s submitted", submission_id, job["id"])
        return
    if not budget.take():
        return
    dm_id = data["id"]
    dm_state = str(client.get(dm_id)["status"]).lower()
    if dm_state in {"failed", "aborted"}:
        write_json_file(record, {"id": dm_id, "status": dm_state}, overwrite=True)  # never looked up again
        state.warn(
            submission_id,
            f"clean-up DM job {dm_id} {dm_state}; inspect its stage diagnostics, then remove {record} to retry",
        )
    elif dm_state == "done":
        submission_path(settings, submission_id).unlink(missing_ok=True)
        cleaned.append(submission_id)
        logger.info("Remote run %s: leftovers removed", submission_id)
