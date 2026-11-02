"""Reconcile DM jobs and publish returned results into the portal database.

The portal side of a remote run after its submission (``remote.submission``):

- :func:`poll` (one pass; :func:`poll_once` for its callers outside the loop) reads every submitted remote job that is
  Queued or Running and, only when there is one, makes one DM call for all of them (``DMClient.fetch``: their records,
  the look-ups by submission ID after a lost reply, and the record of a stop's cancel job being delivered; a record DM
  could not give fails that job's reconciliation alone, logged). It then finds each job's DM job (by submission ID after
  a lost reply: never a second submission; while DM has none, it warns on every pass, and a stopped job is finalized
  Cancelled once DM still has none ``UNSUBMITTED_STOP_GRACE_SECONDS`` after the stop, unless ``dm.timeout_seconds`` is
  null), delivers a requested stop (:func:`request_cancellation`), and moves the job through the existing transitions
  (``jobs.state``): ``queued`` → ``running`` once DM leaves ``pending``; when DM is done, failed or aborted and the
  run's summary has come back, it publishes the results (:func:`publish_returned`) and finalizes the job from the
  summary's status and counters; without a summary, failed or aborted DM jobs finalize Failed (Cancelled after a stop).
  A job whose plan (``bdp.json``) cannot be read (missing, not a JSON object, or its read failing) is skipped, logged
  once per reason (:class:`MissingPlans`), left out of the pass's DM call, and finalized Failed once the plan has not
  been read for ``MISSING_PLAN_SECONDS`` with its runs root answering (never on the runs root's error, never at once). A
  job DM has several jobs for (one submission identity) is left as it is for manual reconciliation, logged once per
  change (:class:`LoggedOnce`). A ``crux`` job's record carries the launcher's remote observation
  (``remoteObservation``): kept in ``job.remote_observation`` while the job is active when ``remote.observation.accept``
  takes it, in the pass's one write of the job, else logged once (:class:`RefusedObservations`) and dropped, never
  failing the run; before it publishes a returned ``crux`` run, the poller writes the Downlink "Publishing results"
  (``remote.observation.publishing``), and keeps it until the publication is done (a retried one is no refusal). A
  refused return finalizes Failed. Refused by the validation (an artifact whose bytes HDF5 rejects, not HDF5 or
  truncated, is such a refusal), it publishes nothing. Refused while its entries are moved (a returned symlink, a
  returned directory holding one, a destination that exists and is not this publication's), it leaves the marker
  ``.bdp-publication.json``, the entries moved before the refused one (they are moved by name) in the run directory,
  the refused entry and the rest in the inbox, and no ``run.json`` (the runbook says how to recover). Any other error is logged and retried next
  time (a transient DM or filesystem failure, such as another process's lock on a returned HDF5 file, is not evidence
  that the remote work stopped), except a database error (``OperationalError``: a write lock past the busy timeout),
  which ends the pass for its caller to log; the next pass does that job again (publication is idempotent).
- :func:`publish_returned` validates the inbox and moves its files into the run directory, rewriting the input-image
  paths of ``output.h5`` and the Output XML to portal paths and keeping the remote originals in
  ``/provenance/remote_input_images``. It is idempotent: a restarted poller finishes a publication it began (also after
  a stop between publishing a file and removing its inbox copy: the copy is removed only when the destination holds
  exactly this publication's bytes), and republishing a published run changes nothing.
- :class:`RemotePoller` is the loop, a thread of the worker (``jobs.worker``), and
  :func:`run_poller` one pass of it (``laueportal remote poll --once``, the runbook's). Each holds the poller lock
  (:class:`PollerLock`, ``<submission_directory>/.poller.lock``: one poller per DM inbox, another portal's
  excluded). The listing of the DM inbox and the return inboxes, which removes what Finished, published runs
  left behind (``remote.cleanup``), comes at the thread's first pass, then every ``CLEANUP_SECONDS`` only
  while remote runs are active or one ended since the last listing.

Paths not rewritten at publication stay as the remote host wrote them; readers map them with
``remote.paths.portal_path`` and the ``path_map`` of the run's plan, ``bdp.json`` (``remote.paths.run_path_map``).
"""

from __future__ import annotations

import fcntl
import filecmp
import html
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import IO, Any

from sqlalchemy import Engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from laueportal.core.db import DatabaseErrors, begin_write
from laueportal.core.timeutil import utcnow
from laueportal.jobs import state
from laueportal.jobs.backends import REMOTE_BACKENDS
from laueportal.jobs.models import Job
from laueportal.jobs.rundir import read_manifest, write_json_file
from laueportal.jobs.state import JobStatus, RunPhase
from laueportal.remote import observation
from laueportal.remote.cleanup import PUBLICATION_MARKER, CleanupState, clean_up
from laueportal.remote.dm import CallAborted, DMClient, Records, describe_failure, launcher_command
from laueportal.remote.paths import child_path, inverse_path_map, map_path
from laueportal.remote.settings import RemoteSettings

logger = logging.getLogger(__name__)

TERMINAL_DM_STATES = frozenset({"done", "failed", "aborted"})
POLLER_LOCK_NAME = ".poller.lock"
CANCEL_SENT_MARKER = ".cancel-sent"
CANCEL_JOB_RECORD = ".cancel-job.json"
CANCEL_REQUEST_NAME = "cancel.request"
UNSUBMITTED_STOP_GRACE_SECONDS = 600.0
"""How long after a stop a job whose submission DM has no job for waits before it is finalized Cancelled: at least
twice ``dm.timeout_seconds``, which bounds every DM call, so a submission still waiting for DM's reply is not
overtaken. With ``dm.timeout_seconds: null`` (no timeout) nothing bounds that wait, and such a job is never
finalized by the poller (it warns on every pass). Should DM accept a submission after its job ended anyway (a
portal process paused for longer than the grace), ``remote.submission.submit_run`` leaves the job as it is and
names the DM job to cancel."""


def _read(path):
    path = Path(path)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError(f"Control document is too large: {path}")
    return json.loads(path.read_text())


def request_cancellation(job: Any, settings: RemoteSettings, client: Any, records: Records) -> None:
    """Deliver a stop: ``cancel.request`` in the run directory, then the launcher's cancel stage (through DM's
    cancel workflow, or directly, as a process of the client's: ``DMClient.run``); ``.cancel-sent`` once it is
    done. Repeated calls submit nothing twice.

    The cancel workflow's job is submitted once (``client.cancel``: a DM call of its own, made only while a stop is
    pending), and its state comes with the pass's batched call (``records``: :func:`poll` asks for it while
    ``.cancel-job.json`` names it and ``.cancel-sent`` is missing), so the pass that submits it reads its state at
    the next pass."""
    run = Path(job.run_directory)
    marker = run / CANCEL_SENT_MARKER
    if marker.exists():
        return
    write_json_file(run / CANCEL_REQUEST_NAME, {"requested_at": str(job.cancel_requested_at)}, overwrite=True)
    if settings.dm.cancel_workflow:
        pending = run / CANCEL_JOB_RECORD
        if not pending.exists():
            record = client.cancel(job.submission_id)
            write_json_file(pending, {"id": record["id"]})
        cancel_id = str(_read(pending)["id"])
        if cancel_id not in records:
            return  # submitted in this pass: its state comes with the next pass's call
        record = records.get(cancel_id)
        dm_state = str(record["status"]).lower()
        if dm_state in {"failed", "aborted"}:
            raise ValueError(f"DM cancellation job {record['id']} {dm_state}; inspect its stage diagnostics")
        if dm_state != "done":
            return
    else:
        client.run(launcher_command(settings.dm, "cancel", job.submission_id), timeout=60)
    marker.touch()


def _restore_input_paths(path, plan, kind):
    """Make returned image references usable at APS; retain remote provenance."""
    inverse = inverse_path_map(plan["path_map"])
    if kind == "results":
        import h5py

        with h5py.File(path, "r+") as handle:
            images: Any = handle["frames/input_images"]
            original = images.asstr()[:].tolist()
            restored = [map_path(value, inverse) for value in original]
            if restored != original:
                handle.require_group("provenance").create_dataset(
                    "remote_input_images", data=original, dtype=h5py.string_dtype()
                )
                images[:] = restored
    else:
        # The native XML writer puts each inputImage element on one line.
        # Stream it so restoring references does not load a full run's XML.
        pattern = re.compile(r"(<inputImage(?:\s[^>]*)?>)([^<]*)(</inputImage>)")

        def restore(match):
            try:
                value = map_path(html.unescape(match[2]), inverse)
            except ValueError:
                return match[0]
            return match[1] + html.escape(value, quote=False) + match[3]

        rewritten = path.with_name(path.name + ".xml")
        try:
            with path.open() as source, rewritten.open("x") as target:
                for line in source:
                    target.write(pattern.sub(restore, line))
            rewritten.chmod(0o660)
            rewritten.replace(path)
        finally:
            rewritten.unlink(missing_ok=True)


def _publish_returned_file(source, destination, transform=None):
    """Publish exclusively even when Globus files belong to another account."""
    if transform is None:
        try:
            os.link(source, destination)
        except PermissionError:
            pass
        else:
            source.unlink()
            return
    temporary = None
    try:
        # Protected hard links can reject a readable file owned by the mapped
        # Transfer user. Link an owned, complete copy instead; the destination
        # never exists partially and an existing destination is never replaced.
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".bdp-return-", delete=False) as handle:
            temporary = Path(handle.name)
            with source.open("rb") as incoming:
                shutil.copyfileobj(incoming, handle)
            os.fchmod(handle.fileno(), 0o660)
            handle.flush()
            os.fsync(handle.fileno())
        if transform:
            transform(temporary)
        os.link(temporary, destination)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    source.unlink()


def _validated(name: str, path: Path, validate: Callable[[Path], Any]) -> Any:
    """Run lauelab's validation of a returned artifact.

    HDF5's verdict on the bytes, an ``OSError`` without an ``errno`` (h5py's "file signature not found",
    "truncated file"), in a file the filesystem reads, is an invalid return (``ValueError``). An ``OSError`` that
    carries an ``errno`` comes from the filesystem and stays an ``OSError``: a missing file (``FileNotFoundError``)
    is an invalid return; anything else (another process's HDF5 file lock, ``EAGAIN``; ``ENOLCK``, ``EIO``,
    ``ESTALE``, ``EACCES``) is retried next pass."""
    try:
        return validate(path)
    except OSError as error:
        if error.errno is not None:
            raise
        try:
            with open(path, "rb") as handle:
                handle.read(1)
        except OSError:
            raise error from None
        raise ValueError(f"Returned {name} artifact is not readable HDF5: {error}") from error


def _published_before_restart(source: Path, destination: Path, transform: Callable[[Path], None] | None) -> bool:
    """Whether ``destination`` is this publication's copy of ``source``, left with its inbox copy by a poller that
    stopped between linking it and removing the source: the same file (a hard link), or the same bytes (an owned
    copy). A transformed file (the LaueGo results file, an Output XML) is recognized only by the bytes the transform
    makes of ``source``: it is applied again, to an owned temporary copy, and the result compared with
    ``destination`` (the transform is deterministic). Any other file is not this publication's, and the caller
    refuses it (``FileExistsError``). A directory is renamed in one step, so it never exists twice."""
    if source.is_dir() or destination.is_dir() or destination.is_symlink():
        return False
    if os.path.samefile(source, destination):
        return True
    if transform is None:
        return filecmp.cmp(source, destination, shallow=False)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".bdp-return-", delete=False) as handle:
            temporary = Path(handle.name)
            with source.open("rb") as incoming:
                shutil.copyfileobj(incoming, handle)
        transform(temporary)
        return filecmp.cmp(temporary, destination, shallow=False)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def publish_returned(plan: dict[str, Any]) -> dict[str, Any]:
    """Validate outputs before publication; repeated reconciliation is idempotent."""
    from laueportal.science.validation import validate_results_file, validate_scan_file

    run = Path(plan["run_directory"])
    incoming = child_path(run / "incoming", plan.get("incoming_directory", plan["output_directory"]))
    summary = _read(incoming / "run.json")
    if not isinstance(summary, dict):
        raise ValueError("Returned summary must be a JSON object")
    if (summary.get("job_id"), summary.get("submission_id")) != (plan["job_id"], plan["submission_id"]):
        raise ValueError("Returned results belong to a different submission")
    if summary.get("kind") != plan["kind"]:
        raise ValueError("Returned analysis kind disagrees with the submission")
    counts = summary.get("counters")
    if not isinstance(counts, dict) or any(
        type(counts.get(key)) is not int for key in ("n_inputs", "n_succeeded", "n_failed", "n_not_run")
    ):
        raise ValueError("Returned counters must be integers")
    if counts["n_inputs"] != plan["n_inputs"] or any(counts[k] < 0 for k in ("n_succeeded", "n_failed", "n_not_run")):
        raise ValueError("Returned counters disagree with the submission")
    if sum(counts[k] for k in ("n_succeeded", "n_failed", "n_not_run")) != plan["n_inputs"]:
        raise ValueError("Returned counters do not sum to the manifest size")
    indexed = counts.get("n_indexed")
    if indexed is not None and (type(indexed) is not int or not 0 <= indexed <= counts["n_succeeded"]):
        raise ValueError("Returned indexing count is invalid")
    status = summary.get("status")
    if status not in {"Finished", "Failed", "Cancelled"}:
        raise ValueError(f"Invalid returned status: {status}")
    if status == "Finished" and (counts["n_succeeded"] != plan["n_inputs"] or summary.get("validation_error")):
        raise ValueError("Returned success contradicts its counters or output validation")
    if status == "Finished" and (incoming / "pbs.json").exists():
        if _read(incoming / "pbs.json").get("exit_status") not in (None, 0):
            raise ValueError("PBS reported a nonzero exit status after the runner finished")
    artifacts = summary.get("artifacts", {})
    if not isinstance(artifacts, dict) or any(not isinstance(path, str) for path in artifacts.values()):
        raise ValueError("Returned artifacts must map names to file paths")
    existing_summary = run / "run.json"
    if existing_summary.exists() and _read(existing_summary).get("submission_id") != plan["submission_id"]:
        raise FileExistsError(existing_summary)
    mapped = {}
    for name, path in artifacts.items():
        relative = child_path(plan["output_directory"], path).relative_to(Path(plan["output_directory"]).resolve())
        returned = child_path(incoming, incoming / relative)
        mapped[name] = str(run / relative)
        check = returned if returned.exists() else run / relative
        if name == "results":
            import h5py

            result = _validated(name, check, validate_results_file)
            if result.n_frames != counts["n_succeeded"]:
                raise ValueError("Returned indexing frame count disagrees with the run summary")
            expected_ids = {entry.input_id for entry in read_manifest(run / "inputs.jsonl")}
            with h5py.File(check, "r") as handle:
                frame_ids: Any = handle["frames/frame_ids"]
                if not set(frame_ids.asstr()[...]).issubset(expected_ids):
                    raise ValueError("Returned indexing file contains frames outside this run")
        elif name == "reconstruction":
            result = _validated(name, check, validate_scan_file)
            if result.n_points != plan["n_inputs"] or result.n_complete != counts["n_succeeded"]:
                raise ValueError("Returned reconstruction counters disagree with the run summary")
    if status == "Finished":
        expected = "results" if plan["kind"] == "lauego_indexing" else "reconstruction"
        if expected not in mapped:
            raise ValueError(f"Returned success has no {expected} artifact")
    marker = run / PUBLICATION_MARKER
    if marker.exists():
        if _read(marker)["submission_id"] != plan["submission_id"]:
            raise FileExistsError("This run already contains a different publication")
    else:
        # By name, not in the file system's order (which differs between file systems), so that the same returned
        # directory gives the same result and the same error everywhere. The marker keeps this order, and a
        # restarted publication follows it.
        names = sorted(p.name for p in incoming.iterdir() if p.name not in {"run.json", ".runner.lock"})
        for name in names:
            if (run / name).exists():
                raise FileExistsError(f"Publication would overwrite {run / name}")
        write_json_file(marker, {"submission_id": plan["submission_id"], "files": names})
    for name in _read(marker)["files"]:
        if Path(name).name != name or name in {"request.json", "inputs.jsonl", "bdp.json"}:
            raise ValueError(f"Unexpected returned control file: {name}")
        source, destination = incoming / name, run / name
        if not source.exists():
            if destination.exists():
                continue  # Already moved by this publication before a poller restart.
            raise FileNotFoundError(source)
        transform = None
        if plan["kind"] == "lauego_indexing" and not source.is_dir():
            if mapped.get("results") == str(destination):
                transform = partial(_restore_input_paths, plan=plan, kind="results")
            elif source.suffix.lower() == ".xml":
                transform = partial(_restore_input_paths, plan=plan, kind="xml")
        # An existing destination first, then a returned symlink, so the same returned directory always
        # gives the same error. A symlink is never taken for this publication's file.
        if destination.exists():
            if not source.is_symlink() and _published_before_restart(source, destination, transform):
                source.unlink()  # A poller stopped between publishing this file and removing its inbox copy.
                continue
            raise FileExistsError(destination)
        if source.is_symlink():
            raise ValueError(f"Returned symlink is not an output: {source}")
        if source.is_dir():
            # No HDF5 external link outside this tree is accepted by validation.
            if any(p.is_symlink() for p in source.rglob("*")):
                raise ValueError("Returned output directory contains symlinks")
            source.rename(destination)
        else:
            _publish_returned_file(source, destination, transform)
    summary["artifacts"] = mapped
    summary.setdefault("provenance", {})["remote_output_directory"] = plan["output_directory"]
    if (run / "run.json").exists():
        if _read(run / "run.json").get("submission_id") != plan["submission_id"]:
            raise FileExistsError(run / "run.json")
    else:
        write_json_file(run / "run.json", summary)
    return summary


class RefusedObservations:
    """The remote observations the poller refused, across its passes: each job's refusal is logged
    once, then again only when the reason changes or after an observation of the job was kept; a job no longer
    reconciled is forgotten."""

    def __init__(self) -> None:
        self._logged: dict[int, str] = {}

    def refused(self, job_id: int, reason: str) -> None:
        if self._logged.get(job_id) != reason:
            self._logged[job_id] = reason
            logger.warning("Remote job %s: its remote observation was refused (%s) and dropped", job_id, reason)

    def accepted(self, job_id: int) -> None:
        self._logged.pop(job_id, None)

    def retain(self, job_ids: set[int]) -> None:
        self._logged = {job_id: reason for job_id, reason in self._logged.items() if job_id in job_ids}


MISSING_PLAN_SECONDS = 600.0
"""How long a submitted run's plan (``bdp.json``) may stay unread, its runs root answering, before the poller ends the
run Failed: its folder deleted, moved or never written, or the plan there but not a JSON object
(a hand edit, a damaged file system) or its read failing. Until then the run waits, logged once per reason; its Stop
cannot be delivered (``request_cancellation`` writes into the folder), so it ends Failed too. The runs root not
answering (an NFS mount down) never counts: such a run waits for it."""


class LoggedOnce:
    """Each run's reason last logged, across the poller's passes, so a reason is logged once per change; a run
    whose reason went (``forget``) or that is no longer reconciled (``retain``) is forgotten."""

    def __init__(self) -> None:
        self._logged: dict[int, str] = {}

    def log(self, job_id: int, reason: str, level: int = logging.WARNING) -> None:
        if self._logged.get(job_id) != reason:
            self._logged[job_id] = reason
            logger.log(level, "Remote job %s: %s", job_id, reason)

    def forget(self, job_id: int) -> None:
        self._logged.pop(job_id, None)

    def retain(self, job_ids: set[int]) -> None:
        self._logged = {job_id: reason for job_id, reason in self._logged.items() if job_id in job_ids}


@dataclass(frozen=True)
class Unread:
    """A run's plan not read this pass (:meth:`MissingPlans.read`)."""

    overdue: bool
    """Whether it has stayed unread, its runs root answering, for ``MISSING_PLAN_SECONDS`` (the run ends Failed)."""
    ending: str = ""
    """What the run's Failed message and the log say first: why the plan was not read."""
    state: str = ""
    """``missing`` or ``unreadable``, for the log."""


class MissingPlans:
    """The submitted runs whose plan the poller cannot read, across its passes (the loop's; a pass alone has its
    own): since when each plan has been unread (missing, not a JSON object, or its read failing) with its runs root
    answering (a pass that finds the runs root not answering starts the count again), and the reason last logged, so
    each run is logged once per reason. A run whose plan is read again, or that is no longer reconciled, is
    forgotten."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._since: dict[int, float] = {}
        self._logged = LoggedOnce()

    def read(self, job: Job, settings: RemoteSettings) -> dict[str, Any] | Unread:
        """The run's plan, or ``Unread`` while it cannot be read (logged once per reason), saying whether it has been
        unread, its runs root answering, for ``MISSING_PLAN_SECONDS`` (then the run ends Failed)."""
        path = Path(str(job.run_directory)) / "bdp.json"
        try:
            plan = _read(path)
            if not isinstance(plan, dict):
                raise ValueError(f"it holds a JSON {type(plan).__name__}, not an object")
        except (OSError, ValueError, RecursionError) as error:  # ValueError: not JSON, or over the size bound
            root = _runs_root_error(settings)
            if root is not None:
                self._since.pop(job.job_id, None)  # never counted: the count starts again once the runs root answers
                self._logged.log(job.job_id, f"its plan {path} cannot be read (the runs root {root}); it waits")
                return Unread(overdue=False)
            since = self._since.setdefault(job.job_id, self._clock())
            if isinstance(error, FileNotFoundError):
                state, ending = "missing", "Run folder missing: its plan (bdp.json) was not found"
                reason = f"its plan {path} is missing (its run folder deleted, moved or never written)"
            else:
                described = f"{type(error).__name__}: {error}"
                state, ending = (
                    "unreadable",
                    f"Run plan unreadable: its plan (bdp.json) could not be read ({described})",
                )
                reason = f"its plan {path} cannot be read ({described})"
            self._logged.log(job.job_id, f"{reason}; it ends Failed if still {state} in {MISSING_PLAN_SECONDS:.0f} s")
            return Unread(self._clock() - since >= MISSING_PLAN_SECONDS, ending, state)
        self._since.pop(job.job_id, None)
        self._logged.forget(job.job_id)
        return plan

    def retain(self, job_ids: set[int]) -> None:
        self._since = {job_id: since for job_id, since in self._since.items() if job_id in job_ids}
        self._logged.retain(job_ids)


def _runs_root_error(settings: RemoteSettings) -> str | None:
    """None when the runs root (the experiment directory, ``files.runs_root``) answers as a directory; else what is
    wrong with it, in words."""
    try:
        root = settings.experiment_path()
        mode = os.stat(root).st_mode
    except (OSError, ValueError) as error:
        return f"does not answer ({type(error).__name__}: {error})"
    return None if stat.S_ISDIR(mode) else f"{root} is not a folder"


def _end_without_plan(engine: Engine, job: Job, unread: Unread) -> bool:
    """End a run whose plan stayed unread (``MISSING_PLAN_SECONDS``) Failed, saying so; returns whether it did."""
    with Session(engine) as session, session.begin():
        begin_write(session)
        current = session.get(Job, job.job_id)
        if current is None or state.is_terminal(current):
            return False
        dm_job = f"DM job {current.external_id}" if current.external_id else f"submission {current.submission_id}"
        state.finalize(
            current,
            status=JobStatus.FAILED,
            message=(
                f"{unread.ending} for {MISSING_PLAN_SECONDS:.0f} s, so the run can be neither published nor stopped "
                f"through it; {dm_job} is no longer followed (cancel it in DM if it still runs)"
            ),
        )
    logger.warning("Remote job %s: its plan stayed %s; ended Failed", job.job_id, unread.state)
    return True


@dataclass(frozen=True)
class Pass:
    """What one pass did (:func:`poll`)."""

    updated: list[int]
    """The jobs it changed."""
    active: int
    """The submitted remote jobs it found Queued or Running."""
    called: bool
    """Whether it called DM: only while a job it can reconcile (its plan read) is active."""
    failure: Exception | None = None
    """The batched call's error, when it failed as a whole: then nothing was reconciled."""


def poll(
    *,
    engine: Engine,
    settings: RemoteSettings | None,
    client: Any = None,
    stopped: threading.Event | None = None,
    refusals: RefusedObservations | None = None,
    missing: MissingPlans | None = None,
    ambiguous: LoggedOnce | None = None,
) -> Pass:
    """One reconciliation pass over the submitted remote jobs (the module's first item), with one DM call for all of
    them whose plan it read, made only when there is one (``client.fetch``); a job whose plan it cannot read is
    left out of the call while so (``MissingPlans``), and ended Failed once overdue. ``stopped`` (the worker's stop)
    ends the pass after the job it is on. A call that fails as a whole is the result's ``failure`` (the caller logs
    it); a record that fails is logged with its job's reconciliation; a database error (``OperationalError``) ends
    the pass at the job it is on, raised for the caller to log. ``refusals``: the refused observations already
    logged, ``missing``: the plans not read, and ``ambiguous``: the submissions DM has several jobs for already
    logged (the loop's, across its passes; new ones by default)."""
    if settings is None or not settings.enabled:
        return Pass([], 0, False)
    with Session(engine) as session:
        jobs = session.scalars(
            select(Job).where(
                Job.backend.in_(REMOTE_BACKENDS),
                Job.status.in_((int(JobStatus.QUEUED), int(JobStatus.RUNNING))),
                # A remote job that never reached submission (still ``published``) has no plan and no DM job.
                Job.submission_id.is_not(None),
            )
        ).all()
        session.expunge_all()
    reconcilable = []
    for job in jobs:
        if job.run_directory:  # submit_run records a run directory with every submission
            reconcilable.append(job)
        else:
            logger.error("Remote job %s has no run directory; it cannot be reconciled", job.job_id)
    refusals = refusals if refusals is not None else RefusedObservations()
    refusals.retain({job.job_id for job in reconcilable})
    missing = missing if missing is not None else MissingPlans()
    missing.retain({job.job_id for job in reconcilable})
    ambiguous = ambiguous if ambiguous is not None else LoggedOnce()
    ambiguous.retain({job.job_id for job in reconcilable})
    updated: list[int] = []
    planned: list[tuple[Job, dict[str, Any]]] = []
    for job in reconcilable:  # the plans first: a run without one is left out of DM's call
        if stopped is not None and stopped.is_set():
            return Pass(updated, len(jobs), False)  # the worker's stop: the pass ends after the job it was on
        plan = missing.read(job, settings)
        if isinstance(plan, dict):
            planned.append((job, plan))
        elif plan.overdue:
            try:
                if _end_without_plan(engine, job, plan):
                    updated.append(job.job_id)
            except OperationalError:
                raise  # the database: the pass ends, logged once by its caller
            except Exception:
                logger.exception("Could not end remote job %s", job.job_id)
    if not planned:
        return Pass(updated, len(jobs), False)
    client = client or DMClient(settings.dm)
    ids, submissions = _asked([job for job, _plan in planned], settings)
    try:
        records = client.fetch(ids, submissions)
    except Exception as error:  # the call as a whole (no answer, a refused login, a malformed answer)
        return Pass(updated, len(jobs), True, error)
    for job, plan in planned:
        if stopped is not None and stopped.is_set():
            break  # the worker's stop: the pass ends after the job it was on
        if _reconcile(engine, job, plan, settings, client, records, refusals, ambiguous):
            updated.append(job.job_id)
    return Pass(updated, len(jobs), True)


def poll_once(*, engine: Engine, settings: RemoteSettings | None, client: Any = None) -> list[int]:
    """One reconciliation pass (:func:`poll`); returns the IDs of the jobs it updated. A DM call that failed as a
    whole is logged, and nothing changes."""
    result = poll(engine=engine, settings=settings, client=client)
    if result.failure is not None:
        logger.error("The DM call failed (%s); no remote job was reconciled", describe_failure(result.failure))
    return result.updated


def _asked(jobs: list[Job], settings: RemoteSettings) -> tuple[list[str], list[str]]:
    """What the pass's call asks DM for: each job's record, by its DM job ID or, after a lost reply, by its
    submission ID; and the record of a stop's cancel job while it is being delivered."""
    ids: list[str] = []
    submissions: list[str] = []
    for job in jobs:
        if job.external_id:
            ids.append(str(job.external_id))
        else:
            submissions.append(str(job.submission_id))
        if job.cancel_requested_at and settings.dm.cancel_workflow:
            cancel_id = _cancel_job(Path(str(job.run_directory)))
            if cancel_id is not None:
                ids.append(cancel_id)
    return ids, submissions


def _cancel_job(run: Path) -> str | None:
    """The cancel workflow's job a stop of the run waits for: ``.cancel-job.json`` without ``.cancel-sent``."""
    try:
        if (run / CANCEL_SENT_MARKER).exists() or not (run / CANCEL_JOB_RECORD).exists():
            return None
        return str(_read(run / CANCEL_JOB_RECORD)["id"])
    except (OSError, ValueError, KeyError, TypeError):
        return None  # request_cancellation reads it again and reports it


def _reconcile(
    engine: Engine,
    job: Job,
    plan: dict[str, Any],
    settings: RemoteSettings,
    client: Any,
    records: Records,
    refusals: RefusedObservations,
    ambiguous: LoggedOnce,
) -> bool:
    """One job's reconciliation, with its plan, from the pass's answer (:func:`poll`); returns whether it changed the
    job. A submission DM has several jobs for is left as it is (manual reconciliation), logged once per change of
    those jobs (``ambiguous``), never a traceback each pass."""
    run_directory = Path(str(job.run_directory))
    dm_state = None
    try:
        external_id = job.external_id
        if not external_id:
            found = records.find(str(job.submission_id))
            if len(found) > 1:
                ambiguous.log(
                    job.job_id,
                    f"Multiple DM jobs have the same submission identity ({', '.join(str(r.get('id')) for r in found)}); "
                    "manual reconciliation required",
                    logging.ERROR,
                )
                return False
            ambiguous.forget(job.job_id)
            if not found:
                return _without_dm_job(engine, job, settings)
            external_id = found[0]["id"]
        record = records.get(str(external_id))
        dm_state = str(record["status"]).lower()
        observed = _observe(job, record, refusals)
        if (
            job.cancel_requested_at
            and dm_state not in TERMINAL_DM_STATES
            and not (run_directory / CANCEL_SENT_MARKER).exists()
        ):
            request_cancellation(job, settings, client, records)
        summary = None
        if dm_state in TERMINAL_DM_STATES:
            incoming = Path(plan.get("incoming_directory", plan["output_directory"]))
            if (incoming / "run.json").exists():
                if job.backend == observation.OBSERVED_TARGET:
                    _publishing(engine, job, observed)
                    observed = None  # merged into "Publishing results"
                summary = publish_returned(plan)
            elif dm_state == "done":
                raise ValueError("DM completed without returning a run summary")
        with Session(engine) as session, session.begin():
            begin_write(session)  # against a concurrent stop or submit_run's record of DM's job
            current = session.get(Job, job.job_id)
            if current is None or state.is_terminal(current):
                return False
            current.external_id = external_id
            current.external_state = dm_state
            current.external_url = record.get("url") or current.external_url
            if observed is not None:
                current.remote_observation = observed
            if current.phase == RunPhase.QUEUED and dm_state != "pending":
                state.mark_running(current)
            if current.phase == RunPhase.RUNNING:
                state.heartbeat(current)
            if summary:
                counts = summary["counters"]
                state.record_progress(current, succeeded=counts["n_succeeded"], failed=counts["n_failed"])
                if current.indexing_run:
                    current.indexing_run.results_path = summary["artifacts"].get("results")
                    current.indexing_run.n_frames_indexed = counts.get("n_indexed")
                state.finalize(
                    current,
                    status=JobStatus[summary["status"].upper()],
                    message=summary.get("message"),
                    failure_report_path=str(run_directory / "failures.jsonl"),
                )
            elif dm_state in {"failed", "aborted"}:
                state.finalize(
                    current,
                    status=JobStatus.CANCELLED if current.cancel_requested_at else JobStatus.FAILED,
                    message=f"DM job {external_id} {dm_state}; inspect DM stage diagnostics",
                )
        return True
    except Exception as error:
        changed = False
        if dm_state in TERMINAL_DM_STATES and isinstance(error, (ValueError, FileExistsError, FileNotFoundError)):
            with Session(engine) as session, session.begin():
                begin_write(session)
                current = session.get(Job, job.job_id)
                if current is not None and not state.is_terminal(current):
                    state.finalize(
                        current,
                        status=JobStatus.FAILED,
                        message=f"Returned output could not be validated/published: {error}",
                    )
                    changed = True
        if isinstance(error, OperationalError):
            raise  # the database (a write lock past the busy timeout): the pass ends, logged once by its caller
        if isinstance(error, CallAborted):  # the worker's end cut the stop's delivery: the next poller does it
            logger.warning("The worker's end cut the delivery of remote job %s's stop", job.job_id)
            return changed
        # A transient DM/filesystem failure is not evidence that remote work stopped.
        logger.exception("Could not reconcile remote job %s", job.job_id)
        return changed


def _observe(job: Job, record: dict[str, Any], refusals: RefusedObservations) -> dict[str, Any] | None:
    """The remote observation ``record`` carries (``remoteObservation``; ``crux`` jobs only) when
    ``remote.observation.accept`` takes it and it is new, for the pass's one write of the job (``_reconcile``, which
    keeps it while the job is active); else None. A refused one is logged once (``refusals``) and dropped. Once the
    stored one is the poller's own "Publishing results" (a publication to retry), the record's is not looked at: it
    is the launcher's last, older, and no refusal."""
    if observation.is_publishing(job.remote_observation):
        return None
    try:
        accepted = observation.accept(
            record.get("remoteObservation"),
            backend=job.backend,
            submission_id=job.submission_id,
            stored=job.remote_observation,
        )
    except observation.Refused as refusal:
        refusals.refused(job.job_id, str(refusal))
        return None
    if accepted is None:
        return None
    refusals.accepted(job.job_id)
    return None if accepted == job.remote_observation else accepted


def _publishing(engine: Engine, job: Job, observed: dict[str, Any] | None) -> None:
    """Before a returned ``crux`` run is published: the Downlink "Publishing results", merged into the pass's new
    observation (``observed``) when it has one, else into the stored one."""
    with Session(engine) as session, session.begin():
        begin_write(session)
        current = session.get(Job, job.job_id)
        if current is not None and not state.is_terminal(current):
            current.remote_observation = observation.publishing(
                current.remote_observation if observed is None else observed,
                submission_id=current.submission_id,
                now=utcnow(),
            )


def _without_dm_job(engine: Engine, job: Job, settings: RemoteSettings) -> bool:
    """A submission DM has no job for: never submitted (a portal that stopped between recording it and calling DM),
    a lost reply DM did not act on, or a submission still waiting for DM's reply. Warn on every pass; once a stop is
    ``UNSUBMITTED_STOP_GRACE_SECONDS`` old (at least twice ``dm.timeout_seconds``; never without a DM timeout) and
    DM still has no job, finalize the job Cancelled. Returns whether it was finalized."""
    timeout = settings.dm.timeout_seconds
    requested = job.cancel_requested_at
    if timeout is None:
        logger.warning(
            "Remote job %s: DM has no job for submission %s (external state %s)%s; dm.timeout_seconds is null (no "
            "timeout), so its submission may still be waiting for DM and the poller never cancels it: look the "
            "submission up in DM (docs/runbooks/remote-execution.md)",
            job.job_id,
            job.submission_id,
            job.external_state,
            "" if requested is None else ", stopped",
        )
        return False
    grace = max(UNSUBMITTED_STOP_GRACE_SECONDS, 2 * float(timeout))
    if requested is None or (utcnow() - requested).total_seconds() < grace:
        logger.warning(
            "Remote job %s: DM has no job for submission %s (external state %s); %s",
            job.job_id,
            job.submission_id,
            job.external_state,
            "a stop finalizes it Cancelled if DM still has none after the grace period"
            if requested is None
            else f"stopped: finalized Cancelled {grace:.0f} s after the stop if DM still has none",
        )
        return False
    with Session(engine) as session, session.begin():
        begin_write(session)  # submit_run records DM's job under the same lock: never both
        current = session.get(Job, job.job_id)
        if current is None or state.is_terminal(current) or current.external_id:
            return False
        state.finalize(
            current,
            status=JobStatus.CANCELLED,
            message=f"DM has no job for submission {job.submission_id} {grace:.0f} s after the stop; cancelled",
        )
    logger.warning("Remote job %s: DM has no job for submission %s; cancelled", job.job_id, job.submission_id)
    return True


CLEANUP_SECONDS = 600.0
"""How often the loop may list the DM inbox and the return inboxes (``remote.cleanup``): at its first pass, then at
most every ten minutes, and only while remote runs are active or one ended since the last listing."""
MAX_WAIT_SECONDS = 60.0
"""The longest wait of the loop's back-offs: between passes while DM calls fail, and between tries of
a poller lock another process holds; each failure doubles the wait from ``poll_seconds``."""


class PollerBusyError(RuntimeError):
    """Another poller holds the lock; only one runs at a time."""


class PollerLock:
    """The poller lock: an exclusive ``flock`` on ``<submission_directory>/.poller.lock``, so one poller runs
    per DM inbox (the worker's thread, ``laueportal remote poll --once``, another portal's), held while its file
    stays open (``take`` until ``close``)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: IO[str] | None = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def take(self) -> bool:
        """Take the lock unless another process holds it; whether this one holds it."""
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a", encoding="utf-8")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        self._handle = handle
        return True

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def run_poller(
    settings: RemoteSettings,
    *,
    engine: Engine,
    client: Any = None,
    on_locked: Callable[[Path], None] | None = None,
) -> list[int]:
    """One pass under the poller lock, then the listing (``laueportal remote poll --once``, the runbook's check of
    the settings); returns the IDs of the jobs it updated.

    Raises ``PollerBusyError`` when another process holds ``<submission_directory>/.poller.lock`` (the worker's
    poller thread, or another portal's).
    """
    lock = PollerLock(settings.submissions() / POLLER_LOCK_NAME)
    if not lock.take():
        raise PollerBusyError(
            f"another remote poller holds {lock.path} (the worker's poller thread, or another portal's)"
        )
    try:
        if on_locked is not None:
            on_locked(lock.path)
        updated = poll_once(engine=engine, settings=settings, client=client)
        try:
            clean_up(engine=engine, settings=settings, client=client)
        except Exception:  # never the end of the pass
            logger.exception("Could not clean up after published remote runs")
        return updated
    finally:
        lock.close()


class RemotePoller:
    """The worker's remote poller thread: ``run_forever`` is its body, under the worker's
    ``jobs.worker.Supervised`` restarts, with its own engine (``engine``; the worker makes one for it).

    - **The lock.** It takes :class:`PollerLock` before its first pass and holds it for its lifetime, across its
      restarts, until ``close`` (the worker's end). While another process holds it, the thread waits, trying again
      after ``poll_seconds``, twice as long each time up to ``MAX_WAIT_SECONDS``, logged once when the lock is found
      held and once when it is taken; it makes no pass and writes no mark meanwhile, so its mark goes late (the
      status page). The worker's dispatcher and reconciler carry on.
    - **A pass** every ``poll_seconds`` (:func:`poll`): one DM call for every active remote run, none while no run
      is active. A call that fails doubles the wait, from ``poll_seconds`` up to ``MAX_WAIT_SECONDS``; logged when the
      calls start failing, when the failure changes, and when DM answers again. Then the listing, when it is due
      (``CLEANUP_SECONDS``). A database error (``OperationalError``: the first read or write the database refuses)
      ends the pass, logged once until a pass gets through again, with no mark; the next pass tries again.
    - **The mark** (``mark``: the worker row's ``poller_at``, at most every ``heartbeat_seconds``) follows a pass made
      under the lock that needed no DM call or had DM's answer: while the lock is held elsewhere, DM calls fail or the
      database errs, it goes late.
    - **The stop** (``stopped``: the worker's service stop) ends it after its current pass: the pass stops after the
      job it is on and lists nothing. A DM call or launcher command still running at the worker's end (after its
      bounded join) is killed with everything it started before the lock goes (``close``).
    """

    def __init__(
        self,
        settings: RemoteSettings,
        *,
        engine: Engine,
        stopped: threading.Event,
        mark: Callable[[datetime], None] | None = None,
        heartbeat_seconds: float = 15.0,
        client: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.client = client or DMClient(settings.dm, own_session=True)  # close() ends a call with all it started
        self.lock = PollerLock(settings.submissions() / POLLER_LOCK_NAME)
        self.poll_seconds = float(settings.poll_seconds)
        self.wait = self.poll_seconds
        """The wait before the next pass (``poll_seconds``, or the back-off after failed DM calls)."""
        self.lock_wait = self.poll_seconds
        """The wait before the next try of a lock another process holds."""
        self._stopped = stopped
        self._mark = mark
        self._heartbeat_seconds = heartbeat_seconds
        self._clock = clock
        self._failure: str | None = None
        self._busy = False
        self._database = DatabaseErrors(logger, "remote poller")
        self._listed: tuple[float, datetime] | None = None
        """The last listing: ``clock()`` and ``utcnow()`` as it began."""
        self._cleanup = CleanupState()  # the clean-up's warnings and back-offs, across the thread's passes
        self._refusals = RefusedObservations()  # the refused observations logged, across the thread's passes
        self._missing = MissingPlans(clock)  # the plans not read, across the thread's passes
        self._ambiguous = LoggedOnce()  # the submissions DM has several jobs for, across the thread's passes
        self._last_mark = float("-inf")

    def run_forever(self) -> None:
        """Pass after pass under the lock until the worker's stop (the class's steps)."""
        while not self._stopped.is_set():
            wait = self.cycle() if self.take_lock() else self.lock_wait
            if self._stopped.wait(wait):
                return

    def take_lock(self) -> bool:
        """Whether the thread holds the lock, taking it if no other process holds it (logged once per change)."""
        if self.lock.held:
            return True
        if self.lock.take():
            logger.info("The remote poller holds %s; it polls every %g s", self.lock.path, self.poll_seconds)
            self._busy, self.lock_wait = False, self.poll_seconds
            return True
        if not self._busy:
            self._busy, self.lock_wait = True, self.poll_seconds
            logger.warning(
                "Another process holds %s (`laueportal remote poll --once`, or another portal's poller or "
                "worker): the remote poller waits for it, trying again after %g s, twice as long each time up to %g s",
                self.lock.path,
                self.lock_wait,
                MAX_WAIT_SECONDS,
            )
        else:
            self.lock_wait = max(self.poll_seconds, min(2 * self.lock_wait, MAX_WAIT_SECONDS))
        return False

    def cycle(self) -> float:
        """One pass under the lock, the listing when it is due and the mark; returns the wait before the next pass."""
        try:
            result = poll(
                engine=self.engine,
                settings=self.settings,
                client=self.client,
                stopped=self._stopped,
                refusals=self._refusals,
                missing=self._missing,
                ambiguous=self._ambiguous,
            )
            self.wait = self._after(result)
            if not self._stopped.is_set():
                self._list_when_due(result.active)
            if result.failure is None:
                self._beat()
        except OperationalError as error:
            self._database.failed(error)
            return self.wait
        self._database.succeeded()
        return self.wait

    def close(self) -> None:
        """The worker's end: end the DM call or launcher command a pass still runs, with everything it started
        (``DMClient.abort``; the pass may outlast the worker's bounded join), then let the lock go."""
        self.client.abort()
        self.lock.close()

    def _after(self, result: Pass) -> float:
        """The wait after a pass: ``poll_seconds``; after a failed DM call twice the last wait, up to
        ``MAX_WAIT_SECONDS``, logged once per change (the calls fail, the failure changes, DM answers again)."""
        if not result.called:
            return self.poll_seconds
        if isinstance(result.failure, CallAborted):  # the worker's end, not DM's failure
            return self.wait
        if result.failure is None:
            if self._failure is not None:
                self._failure = None
                logger.info("DM answers the remote poller again; it polls every %g s", self.poll_seconds)
            return self.poll_seconds
        failure = describe_failure(result.failure)
        wait = max(self.poll_seconds, min(2 * self.wait, MAX_WAIT_SECONDS))
        if failure != self._failure:
            self._failure = failure
            logger.warning(
                "The remote poller's DM call failed (%s); it calls DM again in %g s, twice as long after each failure "
                "up to %g s",
                failure,
                wait,
                MAX_WAIT_SECONDS,
            )
        return wait

    def _list_when_due(self, active: int) -> None:
        """The listing of the DM inbox and the return inboxes (``remote.cleanup``): at the first pass, then at most
        every ``CLEANUP_SECONDS`` while remote runs are active or one ended since the last listing."""
        now = self._clock()
        if self._listed is not None:
            listed_at, listed_since = self._listed
            if now - listed_at < CLEANUP_SECONDS:
                return
            if not active and not _ended_since(self.engine, listed_since):
                return
        self._listed = (now, utcnow())
        try:
            clean_up(engine=self.engine, settings=self.settings, client=self.client, state=self._cleanup)
        except Exception:  # never the end of the poller: the next listing tries again
            logger.exception("Could not clean up after published remote runs")

    def _beat(self) -> None:
        now = self._clock()
        if self._mark is not None and now - self._last_mark >= self._heartbeat_seconds:
            self._last_mark = now
            self._mark(utcnow())


def _ended_since(engine: Engine, since: datetime) -> bool:
    """Whether a remote run ended (``finish_time``) at or after ``since``."""
    with Session(engine) as session:
        ended = select(Job.job_id).where(Job.backend.in_(REMOTE_BACKENDS), Job.finish_time >= since).limit(1)
        return session.scalar(ended) is not None
