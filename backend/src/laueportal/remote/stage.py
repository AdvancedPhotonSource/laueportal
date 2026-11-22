"""DM stage commands. Submission IDs locate plans; form values never enter shell commands.

The launcher on the DM host:
``python -m laueportal.remote.stage --config <bdp.yaml> submit|monitor|permissions|check|cancel|cleanup|auth
[submission ID]`` (``deploy/remote/dm-stage.sh`` runs it; the DM workflows of
``deploy/remote/workflow-bdp*.json.template`` call that). It reads the plan from the DM inbox by submission ID and
keeps each submission's remote IDs in ``<state_directory>/<submission>.json``. ``permissions``
(``03-PERMISSIONS``, which the workflow runs only for a ``crux`` run whose Flow succeeded) has DM restore its
permissions on the run's returned files (:func:`restore_permissions`). ``cleanup`` (the DM
workflow ``dm.cleanup_workflow``) removes what a Finished, published run left: a ``crux`` run's Eagle folder and the
state file.
``monitor`` on ``crux`` also prints the run's remote observation (``remoteObservation``, the
format of ``remote.observation``), which ``02-MONITOR``'s regex keeps as a DM workflow variable:
from the Flow's step, and while the Flow is in ``RunPbsJob`` from a listing of the run's ``results/`` on Eagle
(:func:`observe`). While its Flow status call fails, it prints the kept observation, stale, and calls again after a
back-off; only failures lasting longer than ``targets.crux.status_failure_budget_seconds`` fail it (:func:`flow_status`).
On ``pioneer`` (the GPU queue), ``monitor`` prints an observation from the PBS job's state and the runner's
``progress.json`` (:func:`pbs_observation`), and a stop leaves a running job to drain (:func:`drains`).
A ``pioneer`` plan with a ``continuation`` is one run over sequential PBS allocations, which the launcher chains
(:func:`monitor_continued`; the module's section "Continued runs"). It reads only the YAML settings: no database, no
portal settings (import-linter rule 3). gladier and globus-sdk (``requirements/launcher.in``) are imported where needed.

Continued runs
--------------

The state file keeps the run's allocations (``allocations``: each one's number, PBS job name, mode, state, ``qsub``
outcome, PBS job ID and how it ended); the same record, with the run's status and the reason it failed, is written to
``allocations/ledger.json`` in the run's output folder. A run ends in one of three ways: the allocation that attempts
the last input (or that drains after a stop) writes the run's summary; a stop is finalized with the committed work;
anything unexpected fails the run with its files kept (:func:`_fail`). Nothing is retried or rescued. The rules:

- **One allocation at a time.** The next one is submitted only once PBS reports the previous one finished (``F``) and
  its hand-off checks (:func:`read_result`: the runner's ``result.json`` for this submission and allocation, with more
  inputs committed than before). The run ends when an allocation writes the run's summary (``run.json``): Finished,
  Failed or Cancelled as it says. After ``F`` the launcher waits up to ``VISIBILITY_GRACE_SECONDS`` for either file to
  be visible on the shared storage before it takes the allocation to have ended without them.
- **Recorded before PBS is asked, submitted once.** A new allocation's number, unique job name
  (``remote.pbs.allocation_name``), ``allocation.json`` and PBS script are written, and its ``qsub`` is recorded,
  before ``qsub`` runs. PBS's history is asked for the name first (``qselect -x -N``): a job found is adopted, so a
  lost reply never leads to a second job. An allocation is never submitted twice: when no job of its name appears
  within ``RECONCILE_SECONDS`` of a lost reply, the run fails and says so (the runner of a job that PBS shows later
  refuses to start, the ledger saying the run ended). A refused ``qsub`` fails the run.
- **Re-entrant and serialized.** Each pass first saves what PBS said, then decides what follows
  (:func:`_after_allocation`); until that decision has added the next allocation or ended the run, the next pass
  makes it again. ``submit``, ``monitor`` and ``cancel`` of one submission hold ``<state_directory>/<submission>.lock``
  (``fcntl.flock``) while they read, decide and write; a monitor that cannot take it within ``LOCK_WAIT_SECONDS``
  prints the kept observation.
- **A stop** (``cancel.request``): no processing allocation is submitted after it; a queued processing allocation is
  deleted at once; a running one drains, commits, merges and writes the run's summary (Cancelled), and is deleted
  ``STOP_GRACE_SECONDS`` after the stop (or after it started running, when that is later) if it still runs. When the
  allocation in PBS at the stop ended without the summary, one *finalizing* allocation (``allocation.json``'s
  ``finalize``) merges the committed work and ends the run Cancelled: it asks for the target's ``finalize_pbs`` (a CPU
  request, no GPU), processes no input and starts no daemon. With nothing committed the run ends ``CANCELLED`` at
  once.
- **Anything unexpected** (an allocation that ended without the summary or a hand-off that checks, a hand-off that
  committed nothing, a finalizing allocation without the summary, a refused or never confirmed ``qsub``, an error of
  the launcher itself): the run is ``FAILED`` with the reason, no allocation is submitted after it, an allocation
  still in PBS is deleted (the message says when that could not be done), and the committed segments,
  ``checkpoint.json`` and every allocation's PBS output stay in the output folder for the operator (the runbook's
  salvage).
- **PBS or the shared storage that does not answer** (``qstat``, ``qselect``, ``qdel`` failing or timing out; an
  ``OSError`` on the run's output folder): the stage prints the kept observation, stale, keeps the reason in the state
  file (``unavailable``) and tries again on the next pass, until DM's monitor stage ends (``dm.monitor_seconds``). An
  allocation PBS cannot be asked about is never taken to have ended.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import UUID

from laueportal.jobs.backends import GLOBUS, transport
from laueportal.jobs.rundir import (
    ALLOCATION_LEDGER_FILENAME,
    ALLOCATION_RECORD_FILENAME,
    ALLOCATION_RESULT_FILENAME,
    ALLOCATION_RESULT_FORMAT,
    ALLOCATIONS_DIRECTORY,
    PBS_OUTPUT_FILENAME,
    allocation_directory,
    write_json_file,
)
from laueportal.remote.dm import DMClient
from laueportal.remote.observation import OBSERVED_TARGETS, aware_time
from laueportal.remote.paths import child_path
from laueportal.remote.pbs import allocation_name, build_script
from laueportal.remote.plan import submission_path
from laueportal.remote.settings import STATUS_FAILURE_BUDGET_SECONDS, ListingSettings, collection_path, load_settings

PBS_JOB_ID = r"[0-9]+(?:\.[A-Za-z0-9_.-]+)?"
"""A PBS job ID as ``qsub`` prints it (``1234.server``): the pattern the ``crux`` PBS task checks
(``remote.pbs.run_pbs_job``). The ``aps`` launcher checks it too before keeping the ID for ``qstat`` and ``qdel``."""


def configure_auth(settings):
    """Read existing credentials into this process only; never modify or print them."""
    if settings.auth_profile == "personal":
        os.environ.pop("GLADIER_CLIENT_ID", None)
        os.environ.pop("GLADIER_CLIENT_SECRET", None)
        return
    path = settings.credentials_file
    if not path:
        raise ValueError("credentials_file is required by the beamline authentication profile")
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            'source "$1"; printf "%s\\0%s" "$GLADIER_CLIENT_ID" "$GLADIER_CLIENT_SECRET"',
            "bash",
            path,
        ],
        check=True,
        capture_output=True,
    )
    values = result.stdout.split(b"\0")
    if len(values) != 2 or not all(values):
        raise ValueError("The existing credentials file did not provide both Gladier credentials")
    os.environ["GLADIER_CLIENT_ID"], os.environ["GLADIER_CLIENT_SECRET"] = (v.decode() for v in values)


def _client(settings):
    from laueportal.remote.flow import client_class

    configure_auth(settings)
    _state_directory(settings).mkdir(parents=True, exist_ok=True, mode=0o700)
    return client_class(settings)()


def _state_directory(settings):
    if not settings.state_directory:
        raise ValueError("state_directory is required by the launcher")
    return Path(settings.state_directory)


def submit(plan, settings):
    if continued(plan):
        return submit_continued(plan, settings)
    state = _state_directory(settings) / f"{plan['submission_id']}.json"
    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state.exists():
        raise FileExistsError("This submission was already attempted; inspect its saved state before retrying")
    # Also retain an uncertain state when a network request succeeds but its reply is lost.
    write_json_file(state, {"status": "SUBMISSION_UNCERTAIN"})
    if transport(plan["backend"]) == GLOBUS:
        from laueportal.remote.flow import flow_input

        client = _client(settings)
        record = client.run_flow(
            flow_input(plan),
            label=f"Laue {plan['submission_id'][:8]}",
            tags=["aps", "34IDE", "LauePortal", plan["submission_id"]],
        )
        run_id = record["action_id"]
        data = {"status": "RUNNING", "flow_id": run_id, "url": f"https://app.globus.org/runs/{run_id}"}
    else:
        import shlex

        from laueportal.remote.pbs import build_script

        path = Path(plan["control_directory"]) / "job.pbs"
        Path(plan["output_directory"]).mkdir(parents=True, exist_ok=False)
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(build_script(plan))
        command = ["/opt/pbs/bin/qsub", "-o", str(Path(plan["output_directory"]) / "pbs-output.log"), str(path)]
        host = plan["target"].get("submit_host")
        if host:
            command = ["ssh", "-T", "-o", "BatchMode=yes", host, shlex.join(command)]
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
        pbs_id = result.stdout.strip()
        if not re.fullmatch(PBS_JOB_ID, pbs_id):
            # The state stays SUBMISSION_UNCERTAIN: no retry submits again, and monitor asks for reconciliation.
            raise ValueError("PBS returned an invalid job ID")
        data = {"status": "RUNNING", "pbs_id": pbs_id}
    write_json_file(state, data, overwrite=True)
    return data


def monitor(plan, settings):
    if continued(plan):
        return monitor_continued(plan, settings)
    state = _state_directory(settings) / f"{plan['submission_id']}.json"
    data = json.loads(state.read_text())
    if data.get("flow_id"):
        record = flow_status(settings, data)
        if record is None:  # no answer from Flows: the last status, and the kept observation printed stale
            write_json_file(state, data, overwrite=True)
            return {**data, "observation": unanswered_observation(plan, data)}
        data["status"] = record["status"]
        observe(plan, settings, data, record, save=lambda: write_json_file(state, data, overwrite=True))
    elif data.get("pbs_id"):
        import shlex

        command = ["/opt/pbs/bin/qstat", "-xf", "-F", "json", data["pbs_id"]]
        if plan["target"].get("submit_host"):
            command = ["ssh", "-T", "-o", "BatchMode=yes", plan["target"]["submit_host"], shlex.join(command)]
        result = _pbs_run(command, check=True, capture_output=True, text=True, timeout=30)
        record = next(iter(json.loads(result.stdout)["Jobs"].values()))
        if record["job_state"] == "F":
            data["status"] = "SUCCEEDED" if record.get("Exit_status") == 0 else "FAILED"
        elif drains(plan) and not data.get("qdel_sent") and _stop_overdue(plan):
            _qdel(plan, data["pbs_id"])  # the runner did not drain within STOP_GRACE_SECONDS of the stop
            data["qdel_sent"] = True
        if plan["backend"] in OBSERVED_TARGETS:
            data["observation"] = pbs_observation(plan, data, record["job_state"])
    else:
        raise ValueError("Submission has no returned remote ID; manual reconciliation is required")
    write_json_file(state, data, overwrite=True)
    return data


PBS_QUEUED = ("queued", "Waiting for PBS allocation")
PBS_STALE_SECONDS = 90.0
"""A running job's ``progress.json`` older than this makes its observation stale (the runner rewrites it every 5 s)."""


def pbs_observation(plan, data: Mapping[str, Any], job_state: str) -> dict[str, Any]:
    """A shared-storage run's observation (the format of ``remote.observation``) from its PBS job's state and the
    runner's ``progress.json`` in the run's output folder, which the launcher reads directly: queued while PBS holds
    the job, then running with the frames processed so far. Committed frames only, so the count never goes back."""
    now = _now()
    previous = data.get("observation") if isinstance(data.get("observation"), dict) else None
    stale = None
    if job_state in ("R", "E", "F"):
        phase, detail = "running", "Processing"
        try:
            progress = json.loads((Path(plan["output_directory"]) / "progress.json").read_text())
            done = int(progress["n_succeeded"]) + int(progress["n_failed"])
            detail = f"Processing: {done} of {plan['n_inputs']} frames"
            heartbeat = aware_time(progress.get("heartbeat_at"))
            stale = heartbeat is not None and (now - heartbeat).total_seconds() > PBS_STALE_SECONDS and job_state == "R"
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if job_state == "F":
            detail, stale = "Finishing processing", None
    else:
        phase, detail = PBS_QUEUED
    observed = _time_text(now)
    observation: dict[str, Any] = {"phase": phase, "detail": detail, "observed_at": observed}
    if stale is not None:
        observation["stale"] = stale
    since = previous.get("phase_since") if previous is not None and previous.get("phase") == phase else None
    observation["phase_since"] = since or observed
    observation["submission_id"] = plan["submission_id"]
    observation["pbs"] = {"job_id": data.get("pbs_id")}  # where a crux observation keeps it
    return observation


FLOW_STEPS = {
    **dict.fromkeys(
        ("InitInputTransfer", "InputTransfer", "AdvanceInputTransfer", "MoreInputTransfers", "InputsReady"),
        "InputTransfer",
    ),
    "RunPbsJob": "RunPbsJob",
    "ResultTransfer": "ResultTransfer",
}
"""The ``crux`` Flow's states (``remote.flow``) by the step the observation names (``flow_state``): the
batched input transfer's (``BatchedInputTransfer``) are ``InputTransfer``."""
RUN_PBS_JOB = "RunPbsJob"
LISTING_FLOW = ("ACTIVE", "INACTIVE")
"""The Flow's statuses (Globus Flows) in which the run's ``results/`` is listed while it is in ``RunPbsJob``; a
Flow that ended (``SUCCEEDED``, ``FAILED``) is not."""
STEP_PHASES = {"InputTransfer": ("uplink", "Transferring inputs"), "ResultTransfer": ("downlink", "Returning results")}
"""The observation of a Flow step that is not listed."""
LOGIN_REQUIRED = ("login_required", "Sign in to Globus and resume this run")
"""The observation of a Flow that waits for a Globus sign-in (:func:`login_required`): its phase and
detail, which take precedence over the step's (applied last), with no ``stale`` key and no listing."""
AWAITING_WORKFLOW = "Awaiting workflow"
"""The detail when the Flow's step is unknown (the phase stays the last one, else ``queued``)."""
LISTED_FILES = (
    ("run.json", "running", "Finishing processing"),
    ("progress.json", "running", "Processing"),
    ("pbs.json", "queued", "Waiting for PBS allocation"),
)
"""What the run's ``results/`` on Eagle shows, the first file present deciding: the runner's
``run.json`` (written at its end), its ``progress.json`` (from its start, every 5 s), the PBS task's ``pbs.json``
(after ``qsub``, then on every 5 s ``qstat`` poll)."""
WAITING_FOR_ENDPOINT = ("queued", "Waiting for endpoint")
"""No ``results/`` yet, or none of ``LISTED_FILES`` in it: the PBS task has not submitted the job."""
HEARTBEAT_FILES = ("pbs.json", "progress.json")
"""The files rewritten every 5 s while the run is in PBS: the newer of them older than ``stale_after_seconds`` makes
the observation stale (the last phase kept), unless ``FINISHED_FILE`` is listed."""
FINISHED_FILE = "run.json"
"""The runner's last file: once it is listed the runner has ended and nothing is left to rewrite the heartbeats, so a
listing that shows it is never stale by their age; a failed or slow listing still is."""
MAX_LISTING_WAIT_SECONDS = 60.0
"""The longest wait between listings while they fail: each failed listing doubles the wait from
``targets.crux.listing.interval_seconds`` up to this (an interval over it is kept as it is)."""
STATUS_INTERVAL_SECONDS = 10.0
"""The stage's interval: DM runs ``02-MONITOR`` every 10 s (``repeatPeriod`` in
``deploy/remote/workflow-bdp.json.template``). After a failed Flow status call the next is made no sooner than this,
twice as long after each further failure (:func:`flow_status`)."""
MAX_STATUS_WAIT_SECONDS = 60.0
"""The longest wait between Flow status calls while they fail."""
STATUS_TIMEOUT_SECONDS = 10.0
"""The bound on one Flow status call, the whole step (the launcher's client, its authorization's renewal and one
Flows request, with no retry): a call with no answer by then has failed (:func:`flow_status`)."""
REQUEST_TIMEOUT_MARGIN_SECONDS = 5.0
"""How far a bounded step's HTTP timeout lies past the step's own bound (:func:`request_timeout`), so that with no
answer the bound always fires first and names the error (``TimeoutError``). With the two equal, the waiting thread's
check (its ``join`` waking, then the GIL) and the request's own timeout race, and a garbage collection or a loaded
host let the request's win (the Flow status call's ``GlobusTimeoutError`` in the full test suite, whose full
collections take 0.3 s at the median and up to 0.65 s, measured on a development machine). 5 s is several times any such
delay, as ``remote.dm.SELF_BOUND_MARGIN_SECONDS`` is for a DM call, and costs nothing: the stage prints and exits at
the bound, ending the request left behind with its process (0.1 s later, measured)."""
STATUS_UNAVAILABLE = ("queued", "Workflow status unavailable")
"""The observation when ``get_status`` fails and no observation is kept: its phase (``queued``) and
detail, which the first stage runs of a submission print, stale, while the call fails."""
STATUS_FAILURE_KEYS = ("status_failure", "status_wait", "status_failed_at", "status_failing_since")
"""The state file's keys while Flow status calls fail (:func:`_after_status`): why the last one failed, the wait
before the next, when the last one and the first were made; none while Flows answers."""


def _now() -> datetime:
    return datetime.now(UTC)


def _time_text(value: datetime) -> str:
    """The observation times: ISO 8601, UTC, to the second (``+00:00``)."""
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _listed_text(value: datetime) -> str:
    """``listed_at``: ISO 8601, UTC, at the clock's full precision, so the interval is measured as it passed (an
    observation's times stay to the second)."""
    return value.astimezone(UTC).isoformat()


def flow_step(record: Mapping[str, Any]) -> str | None:
    """The step a Flow run is in (a ``FLOW_STEPS`` value), from the run document ``get_status`` returns (Globus
    Flows' run): its latest action among ``details.action_statuses``, else the state of its
    latest event (``details.details.state_name``); ``ResultTransfer`` for a Flow that succeeded; else None. No
    further call for the run's log is made when neither names a step: one ``get_status`` per stage run."""
    details = record.get("details")
    details = details if isinstance(details, Mapping) else {}
    actions = details.get("action_statuses")
    names = []
    if isinstance(actions, list):
        names = [action.get("state_name") for action in reversed(actions) if isinstance(action, Mapping)]
    latest = details.get("details")
    if isinstance(latest, Mapping):
        names.append(latest.get("state_name"))
    for name in names:
        if isinstance(name, str) and name in FLOW_STEPS:
            return FLOW_STEPS[name]
    return "ResultTransfer" if record.get("status") == "SUCCEEDED" else None


def login_required(record: Mapping[str, Any]) -> bool:
    """Whether a Flow run waits for a Globus sign-in: its status ``INACTIVE`` and a truthy ``authorization_parameters``
    in the run's ``details`` or in any action's ``details`` (a session or consent challenge can be on either). Only the
    flag is kept: the parameters themselves (session and consent data) never reach the state file, the printed
    observation or a log."""
    if record.get("status") != "INACTIVE":
        return False
    details = record.get("details")
    details = details if isinstance(details, Mapping) else {}
    actions = details.get("action_statuses")
    challenged = [details]
    if isinstance(actions, list):
        challenged += [action.get("details") for action in actions if isinstance(action, Mapping)]
    return any(isinstance(item, Mapping) and bool(item.get("authorization_parameters")) for item in challenged)


def _paused_step(record: Mapping[str, Any]) -> str | None:
    """The ``flow_state`` of a Flow waiting for a sign-in: its step (:func:`flow_step`), else,
    when no action names a step, the run's own ``details.state_name`` (a ``FLOW_STEPS`` name as its step,
    another as it is)."""
    step = flow_step(record)
    if step is not None:
        return step
    details = record.get("details")
    name = details.get("state_name") if isinstance(details, Mapping) else None
    return FLOW_STEPS.get(name, name) if isinstance(name, str) else None


def _listing_settings(settings) -> ListingSettings:
    crux = settings.targets.crux
    return crux.listing if crux is not None else ListingSettings()


def request_timeout(bound: float) -> float:
    """The HTTP timeout (``RequestsTransport(http_timeout=...)``) of a client whose whole step :func:`_bounded`
    waits for at most ``bound``: ``REQUEST_TIMEOUT_MARGIN_SECONDS`` past it, so no answer is the bound's
    ``TimeoutError``."""
    return bound + REQUEST_TIMEOUT_MARGIN_SECONDS


def _bounded(call: Callable[[], Any], seconds: float, *, name: str = "results-listing") -> Any:
    """``call()``'s result, waiting at most ``seconds`` for it: it runs in a daemon thread, which a call still
    running then leaves behind (``TimeoutError``) to end with this process (the stage prints and exits). Its
    requests' HTTP timeout is :func:`request_timeout`'s, past ``seconds``, so it never answers for the bound."""
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["result"] = call()
        except BaseException as error:  # raised in the caller's thread
            outcome["error"] = error

    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise TimeoutError(f"no answer within {seconds} s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


class LoginRefused(Exception):
    """The listing would need a login (the launcher's identity has no Transfer consent or token): a DM stage never
    starts an interactive one."""


def _refusing(service: str) -> Callable[..., None]:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise LoginRefused(f"no {service} consent or token for the launcher's identity; run the launcher's auth stage")

    return refuse


_refuse_login = _refusing("Transfer")


def _refuse_logins(manager: Any, refuse: Callable[..., None]) -> None:
    """Make a user identity's login manager (gladier's, and GlobusApp's when a token does not validate) raise
    :class:`LoginRefused` where it would start a login; a client identity's credentials grant asks nothing."""
    from gladier.managers.login_manager import UserAppLoginManager

    if isinstance(manager, UserAppLoginManager):
        manager.login = refuse
        manager.globus_app.login = refuse


def _no_folder(error: BaseException) -> bool:
    """Whether Transfer's ``error`` says the listed folder does not exist: a Globus API error whose code ends in
    ``NotFound`` (``NotFound``, ``ClientError.NotFound``, ``ExternalError.DirListingFailed.NotFound``), whatever its
    HTTP status, or a 404 that names no code of its own. Anything else (an ``EndpointNotFound``, a refused or failing
    request) is a failed listing."""
    import globus_sdk

    if not isinstance(error, globus_sdk.GlobusAPIError):
        return False
    code = error.code if isinstance(error.code, str) else ""
    last = code.rsplit(".", 1)[-1]
    return last == "NotFound" or (error.http_status == 404 and last in ("", "Error"))


def _failure(error: BaseException) -> str:
    """What stderr says of a failed listing: the error's type and HTTP status, never its message (it may repeat a
    value), but for the stage's own ``LoginRefused``."""
    if isinstance(error, LoginRefused):
        return f"{type(error).__name__}: {error}"
    status = getattr(error, "http_status", None)
    return type(error).__name__ + (f" (HTTP {status})" if isinstance(status, int) else "")


def _listing(plan, settings) -> tuple[dict[str, datetime | None] | None, str | None]:
    """:func:`list_results`'s listing and, when it failed, why (:func:`_failure`)."""
    timeout = _listing_settings(settings).timeout_seconds
    names = {name for name, _phase, _detail in LISTED_FILES}
    try:
        endpoint, folder = eagle_run_folder(plan, settings)

        def listing() -> list[Any]:
            transfer = _transfer_client(settings, timeout=request_timeout(timeout))
            return list(transfer.operation_ls(endpoint, path=f"{folder}/results"))

        entries = _bounded(listing, timeout)
    except Exception as error:
        return ({}, None) if _no_folder(error) else (None, _failure(error))
    return {
        entry["name"]: aware_time(entry.get("last_modified"))
        for entry in entries
        if isinstance(entry, Mapping) and isinstance(entry.get("name"), str) and entry["name"] in names
    }, None


def list_results(plan, settings) -> dict[str, datetime | None] | None:
    """The status files (``LISTED_FILES``) in a ``crux`` run's ``results/`` on Eagle, by name, with their last
    modification times (None when Transfer gives none it reads): one Transfer ``operation_ls`` of
    ``<workspace>/runs/<submission>/results`` in the Eagle collection (:func:`eagle_run_folder`: the plan's result
    transfer, checked against this launcher's settings), with the launcher's Transfer client and consent (the
    ``crux`` cancel's). The whole step (the client, its token's renewal and the request) runs in a daemon thread
    waited for at most ``targets.crux.listing.timeout_seconds`` (:func:`_bounded`), and never starts a login.
    ``{}`` when the folder does not exist (:func:`_no_folder`); None when the listing failed or did not answer in
    time (the reason on stderr, never a value): the observation is then stale, and the stage carries on."""
    entries, failure = _listing(plan, settings)
    if failure is not None:
        print(f"The results listing on Eagle failed: {failure}; the observation is stale", file=sys.stderr)
    return entries


def listed_phase(
    entries: Mapping[str, datetime | None] | None, previous: Mapping[str, Any] | None, *, now: datetime, stale_after
) -> tuple[str, str, bool]:
    """The phase, detail and stale flag a listing gives: the first of ``LISTED_FILES``
    present, else ``WAITING_FOR_ENDPOINT``; stale when the listing failed (``entries`` None) or, while
    ``FINISHED_FILE`` is not listed, the newer of ``HEARTBEAT_FILES`` is more than ``stale_after`` seconds old, and
    then the last phase and detail (``previous``'s, when it is a listing's own) are kept. Never
    "Waiting for status" (the portal's "At ALCF", for a launcher that does not list)."""
    phase, detail = WAITING_FOR_ENDPOINT
    if entries is None:
        stale = True
    else:
        for name, row_phase, row_detail in LISTED_FILES:
            if name in entries:
                phase, detail = row_phase, row_detail
                break
        times = [entries[name] for name in HEARTBEAT_FILES if entries.get(name) is not None]
        stale = (
            FINISHED_FILE not in entries
            and bool(times)
            and (now - max(cast(list[datetime], times))).total_seconds() > stale_after
        )
    rows = {WAITING_FOR_ENDPOINT, *((row_phase, row_detail) for _name, row_phase, row_detail in LISTED_FILES)}
    if stale and previous is not None and previous.get("flow_state") == RUN_PBS_JOB:
        last = (previous.get("phase"), previous.get("detail"))
        if last in rows:
            phase, detail = cast(tuple[str, str], last)
    return phase, detail, stale


def _listing_wait(data: Mapping[str, Any], listing: ListingSettings) -> float:
    """The wait after the last listing (``listing_wait`` in the state file, set by failed listings), else
    ``interval_seconds``; between ``interval_seconds`` and ``MAX_LISTING_WAIT_SECONDS`` (or the interval, when it is
    longer)."""
    interval = float(listing.interval_seconds)
    wait = data.get("listing_wait")
    if not isinstance(wait, int | float) or isinstance(wait, bool):
        return interval
    return max(interval, min(float(wait), MAX_LISTING_WAIT_SECONDS))


def _after_listing(data: dict[str, Any], failure: str | None, listing: ListingSettings) -> None:
    """Keep the back-off in the state file: a failed listing doubles the wait from ``interval_seconds``
    up to ``MAX_LISTING_WAIT_SECONDS`` (``listing_wait``) and keeps why (``listing_failure``); one that answers
    resets both. One line on stderr per change: when the listings start failing, when the failure changes, when
    they answer again."""
    last = data.get("listing_failure") if isinstance(data.get("listing_failure"), str) else None
    interval = float(listing.interval_seconds)
    if failure is None:
        data.pop("listing_failure", None)
        data.pop("listing_wait", None)
        if last is not None:
            print(f"The results listing on Eagle answers again; it lists every {interval:g} s", file=sys.stderr)
        return
    wait = max(interval, min(2 * _listing_wait(data, listing), MAX_LISTING_WAIT_SECONDS))
    data["listing_failure"], data["listing_wait"] = failure, wait
    if failure != last:
        print(
            f"The results listing on Eagle failed: {failure}; the observation is stale; the next listing in {wait:g} s,"
            f" twice as long after each failure up to {MAX_LISTING_WAIT_SECONDS:g} s",
            file=sys.stderr,
        )


def _status_budget(settings) -> float:
    crux = settings.targets.crux
    return float(crux.status_failure_budget_seconds if crux is not None else STATUS_FAILURE_BUDGET_SECONDS)


def _status_wait(data: Mapping[str, Any]) -> float:
    """The wait after the last failed Flow status call (``status_wait`` in the state file), between
    ``STATUS_INTERVAL_SECONDS`` and ``MAX_STATUS_WAIT_SECONDS``."""
    wait = data.get("status_wait")
    if not isinstance(wait, int | float) or isinstance(wait, bool):
        return STATUS_INTERVAL_SECONDS
    return max(STATUS_INTERVAL_SECONDS, min(float(wait), MAX_STATUS_WAIT_SECONDS))


def _after_status(data: dict[str, Any], failure: str | None, *, now: datetime, budget: float) -> None:
    """Keep the status calls' back-off in the state file: the first failed call waits
    ``STATUS_INTERVAL_SECONDS``, each further one twice as long up to ``MAX_STATUS_WAIT_SECONDS``
    (``STATUS_FAILURE_KEYS``); an answer removes them. One line on stderr per change: when the calls start failing,
    when the failure changes, when Flows answers again (the error's type and HTTP status only)."""
    last = data.get("status_failure") if isinstance(data.get("status_failure"), str) else None
    if failure is None:
        for key in STATUS_FAILURE_KEYS:
            data.pop(key, None)
        if last is not None:
            print("Flows answers the status call again; the stage calls it on each run", file=sys.stderr)
        return
    failing = aware_time(data.get("status_failing_since")) is not None
    wait = min(2 * _status_wait(data), MAX_STATUS_WAIT_SECONDS) if failing else STATUS_INTERVAL_SECONDS
    data["status_failure"], data["status_wait"], data["status_failed_at"] = failure, wait, _listed_text(now)
    if not failing:
        data["status_failing_since"] = _listed_text(now)
    if failure != last:
        print(
            f"The Flow status call failed: {failure}; the observation is stale; the next call in {wait:g} s, twice"
            f" as long after each failure up to {MAX_STATUS_WAIT_SECONDS:g} s; the stage fails once the calls have"
            f" failed for over {budget:g} s",
            file=sys.stderr,
        )


def _flows_client(client, *, timeout: float):
    """A Globus Flows client for one status call, with the authorization of the launcher's gladier ``client``
    (the scope gladier's ``get_status`` uses): no login (:class:`LoginRefused` instead, as for the listing), the
    authorization renewed here (a renewal that fails or hangs is a failed call), then one request, no retry, each
    wait on Flows bounded by ``timeout`` (a failed call costs Flows one request)."""
    import globus_sdk
    from globus_sdk.authorizers import RenewingAuthorizer
    from globus_sdk.scopes import FlowsScopes
    from globus_sdk.transport import RequestsTransport, RetryConfig

    manager = client.login_manager
    _refuse_logins(manager, _refuse_flows_login)
    scope = str(FlowsScopes.manage_flows)
    authorizers = manager.get_manager_authorizers()
    authorizer = next((value for key, value in authorizers.items() if str(key) == scope), None)
    if authorizer is None:
        _refuse_flows_login()
    if isinstance(authorizer, RenewingAuthorizer):
        authorizer.ensure_valid_token()
    return globus_sdk.FlowsClient(
        authorizer=authorizer,
        transport=RequestsTransport(http_timeout=timeout),
        retry_config=RetryConfig(max_retries=0),
    )


_refuse_flows_login = _refusing("Flows")


def _flow_record(settings, flow_id: str, built: list[Any]) -> Mapping[str, Any]:
    """One Flow status call, the step :func:`flow_status` bounds: the launcher's client (``built`` holds it once
    made), :func:`_flows_client`, one ``get_run`` and gladier's reading of it (its ``get_status``: ``get_details``,
    which deserialises a compute function's exception)."""
    from gladier.utils.automate import get_details

    client = _client(settings)
    built.append(client)
    record = _flows_client(client, timeout=request_timeout(STATUS_TIMEOUT_SECONDS)).get_run(flow_id).data
    try:
        return get_details(record)
    except (KeyError, AttributeError):
        return record


def flow_status(settings, data: dict[str, Any]) -> Mapping[str, Any] | None:
    """The Flow's run document (one status call of ``data``'s Flow run), or None when the stage prints the kept
    observation, stale, instead (:func:`unanswered_observation`).

    The call (:func:`_flow_record`) is one Flows request with no retry, and the whole step (the launcher's client,
    its authorization's renewal, the request) runs in a daemon thread waited for at most ``STATUS_TIMEOUT_SECONDS``
    (:func:`_bounded`). A call that fails (a network error, a Globus 5xx, a renewal that fails, no answer in time)
    gives None, and the stage runs that follow make no call (nor build the client) until the wait has passed,
    measured from the failed call's run (:func:`_after_status`); a ``status_failed_at`` in the future (the clock
    stepped back) does not wait. Not retried, failing the stage at once: a login the call would need
    (:class:`LoginRefused`), and an error building the launcher's client (its settings, the credentials file,
    gladier's storage). The first call that fails once the calls have failed for longer than
    ``targets.crux.status_failure_budget_seconds`` raises its error, so the stage fails (no output, the error's
    traceback, exit 1) and DM ends the job; a ``status_failing_since`` in the future is taken
    as now (and saved), while a clock that steps forward during failures counts the step. A status Flows answers is
    returned as it is."""
    # gladier imported here, so that the bound below counts no import
    from gladier.utils.automate import get_details  # noqa: F401

    from laueportal.remote.flow import client_class  # noqa: F401

    now = _now()
    since = aware_time(data.get("status_failing_since"))
    if since is not None and since > now:
        data["status_failing_since"] = _listed_text(now)  # the clock stepped back: the budget counts from now
    failed = aware_time(data.get("status_failed_at"))
    if failed is not None and 0 <= (now - failed).total_seconds() < _status_wait(data):
        return None  # waiting: no Flows call
    budget = _status_budget(settings)
    built: list[Any] = []
    try:
        record = _bounded(
            lambda: _flow_record(settings, data["flow_id"], built), STATUS_TIMEOUT_SECONDS, name="flow-status"
        )
    except LoginRefused:
        raise  # a login it would need: not transient
    except Exception as error:
        if not built and not isinstance(error, TimeoutError):
            raise  # the launcher's client was not built: its settings, not Flows
        since = aware_time(data.get("status_failing_since")) or now
        if (now - since).total_seconds() > budget:
            raise  # failed for longer than the budget: the stage fails
        _after_status(data, _failure(error), now=now, budget=budget)
        return None
    _after_status(data, None, now=now, budget=budget)
    return record


def unanswered_observation(plan, data: Mapping[str, Any]) -> dict[str, Any]:
    """What a stage run prints while Flows does not answer (:func:`flow_status`): the kept observation (the last one
    made from an answer, which the state file keeps as it is), stale, its times unchanged; with none kept,
    ``STATUS_UNAVAILABLE``, stale, observed at the first failed call, with no Flow step (the keys of a Flow-only
    observation, in their order). Never "Waiting for status"."""
    kept = data.get("observation")
    if isinstance(kept, dict) and kept:
        return {**kept, "stale": True}
    since = aware_time(data.get("status_failing_since")) or _now()
    phase, detail = STATUS_UNAVAILABLE
    return {
        "phase": phase,
        "detail": detail,
        "observed_at": _time_text(since),
        "stale": True,
        "flow_state": None,
        "phase_since": _time_text(since),
        "submission_id": plan["submission_id"],
        "flow_run_id": data["flow_id"],
    }


def observe(plan, settings, data: dict[str, Any], record: Mapping[str, Any], *, save: Callable[[], Any]) -> None:
    """Keep a ``crux`` run's observation in its launcher state ``data`` (``observation``), from the Flow's run
    document ``record`` (the format of ``remote.observation``):

    - while the Flow is in ``RunPbsJob`` (and has not ended), at most every ``targets.crux.listing.interval_seconds``
      (twice as long after each failed listing, up to ``MAX_LISTING_WAIT_SECONDS``: :func:`_after_listing`), one
      listing of the run's ``results/`` (:func:`_listing`, :func:`listed_phase`); its time (``listed_at``) is saved
      first, so no listing is tried again before the wait, whatever it gives. Between listings the kept observation
      stands. Two monitor runs of one submission at once (one by hand during DM's) share no lock: each may list, and
      the later write wins;
    - in another step, the step's phase (``STEP_PHASES``), no listing;
    - while the Flow waits for a Globus sign-in (:func:`login_required`), ``LOGIN_REQUIRED`` in any
      step, before the step's own rules (no listing; ``flow_state`` :func:`_paused_step`);
    - ``phase_since`` is kept while the phase stays; a kept ``login_required`` observation is no previous one
      once the Flow no longer waits for a sign-in (the resumed run's phase and ``phase_since`` are
      its own), and a resumed run in ``RunPbsJob`` is listed at once, whatever the wait (the kept observation does not
      stand; with the Flow ended, none is printed).

    A failed or slow listing gives a stale observation, never a failed stage."""
    now = _now()
    previous = data.get("observation") if isinstance(data.get("observation"), dict) else None
    login = login_required(record)
    resumed = previous is not None and previous.get("phase") == LOGIN_REQUIRED[0] and not login
    if resumed:
        previous = None
        data.pop("observation")
    step = flow_step(record)
    stale: bool | None = None
    if login:
        phase, detail = LOGIN_REQUIRED
        step = _paused_step(record)
    elif step == RUN_PBS_JOB:
        listing = _listing_settings(settings)
        listed = aware_time(data.get("listed_at"))
        elapsed = None if listed is None else (now - listed).total_seconds()
        waiting = not resumed and elapsed is not None and 0 <= elapsed < _listing_wait(data, listing)
        if record.get("status") not in LISTING_FLOW or waiting:
            return  # the kept observation stands
        data["listed_at"] = _listed_text(now)
        save()
        entries, failure = _listing(plan, settings)
        _after_listing(data, failure, listing)
        phase, detail, stale = listed_phase(entries, previous, now=now, stale_after=listing.stale_after_seconds)
    elif step is None:
        phase, detail = (previous or {}).get("phase") or "queued", AWAITING_WORKFLOW
    else:
        phase, detail = STEP_PHASES[step]
    observed = _time_text(now)
    observation: dict[str, Any] = {"phase": phase, "detail": detail, "observed_at": observed}
    if stale is not None:
        observation["stale"] = stale
    observation["flow_state"] = step
    since = previous.get("phase_since") if previous is not None and previous.get("phase") == phase else None
    observation["phase_since"] = since or observed
    observation["submission_id"] = plan["submission_id"]
    observation["flow_run_id"] = data["flow_id"]
    data["observation"] = observation


def _transfer_client(settings, *, timeout=None):
    """A Globus Transfer client with the launcher's authorization (the ``crux`` cancel and clean-up, and the
    monitor's listing). With ``timeout`` (the listing, built in its bounded thread: :func:`_listing`): no login (a
    user identity's would prompt; :class:`LoginRefused` instead, while a client identity's credentials grant asks
    nothing), the authorization renewed here, then one request, no retry (none before the next listing), each wait
    on Transfer bounded by ``timeout``."""
    import globus_sdk
    from globus_sdk.scopes import TransferScopes

    client = _client(settings)
    manager = client.login_manager
    manager.add_requirements([cast(Any, TransferScopes.all)])  # a Scope; gladier annotates strings
    if timeout is not None:
        _refuse_logins(manager, _refuse_login)
    authorizer = manager.get_manager_authorizers()[str(TransferScopes.all)]
    if timeout is None:
        return globus_sdk.TransferClient(authorizer=authorizer)
    from globus_sdk.authorizers import RenewingAuthorizer
    from globus_sdk.transport import RequestsTransport, RetryConfig

    if isinstance(authorizer, RenewingAuthorizer):
        authorizer.ensure_valid_token()
    return globus_sdk.TransferClient(
        authorizer=authorizer,
        transport=RequestsTransport(http_timeout=timeout),
        retry_config=RetryConfig(max_retries=0),
    )


PERMISSIONS_RESTORED = "PERMISSIONS_RESTORED"
"""The ``permissions`` stage's status line (``Status: PERMISSIONS_RESTORED``)."""


def restore_permissions(plan, settings):
    """``03-PERMISSIONS``: after a ``crux`` run's completed Globus return,
    DM restores its permissions on exactly this submission's return directory, ``<run>/incoming/<submission>``
    (``FileDsApi.restorePermissions``: the experiment's managed ownership, and the beamline account's ACL write
    access; it does not make that account the files' owner). Refused, with no DM call, unless the state file says
    ``SUCCEEDED`` and the plan's backend is ``crux``, and unless the plan's ``incoming_directory`` is, resolved,
    ``<run>/incoming/<submission>`` (``run`` the plan's ``run_directory`` inside ``<experiment>/analysis``, with
    ``experiment_directory`` resolved strictly) and a directory: never a parent folder, another submission's or one a
    link leads elsewhere. One DM call (``DMClient.restore_permissions``: the experiment folder's name and the
    directory relative to it), bounded by ``dm.permissions_timeout_seconds``. Any failure fails the stage."""
    identifier = str(UUID(plan["submission_id"]))
    state = json.loads((_state_directory(settings) / f"{identifier}.json").read_text())
    if transport(plan["backend"]) != GLOBUS or state["status"] != "SUCCEEDED":
        raise ValueError("Permissions require a completed Globus workflow")
    if not settings.experiment_directory:
        raise ValueError("experiment_directory is required by the permissions stage")
    experiment = Path(settings.experiment_directory).resolve(strict=True)
    run = child_path(experiment / "analysis", plan["run_directory"])
    incoming = child_path(run, plan["incoming_directory"])
    if incoming != run / "incoming" / identifier or not incoming.is_dir():
        raise ValueError("Permissions must target this submission's incoming directory")
    dm = settings.dm.model_copy(update={"timeout_seconds": settings.dm.permissions_timeout_seconds})
    DMClient(dm).restore_permissions(experiment.name, incoming.relative_to(experiment).as_posix())
    return {"status": PERMISSIONS_RESTORED}


def cancel(plan, settings):
    if transport(plan["backend"]) == GLOBUS:
        import globus_sdk

        transfer = _transfer_client(settings)
        spec = plan["cancel_transfer"]
        payload = globus_sdk.TransferData(
            source_endpoint=spec["source_endpoint_id"],
            destination_endpoint=spec["destination_endpoint_id"],
            verify_checksum=True,
            sync_level="checksum",
        )
        for item in spec["transfer_items"]:
            payload.add_item(**item)
        result = transfer.submit_transfer(payload)
        if not transfer.task_wait(result["task_id"], timeout=45):
            raise TimeoutError("Cancellation marker transfer is still pending")
        if transfer.get_task(result["task_id"])["status"] != "SUCCEEDED":
            raise RuntimeError("Cancellation marker transfer failed")
        return {"status": "CANCEL_REQUESTED"}
    if continued(plan):
        return cancel_continued(plan, settings)
    state = json.loads((_state_directory(settings) / f"{plan['submission_id']}.json").read_text())
    if drains(plan) and _job_state(plan, state["pbs_id"]) in RUNNING_STATES:
        # The runner reads the stop request in the run directory and drains (its segments merged, the daemon
        # ended); ``monitor`` deletes the job if it still runs STOP_GRACE_SECONDS after the request.
        return {"status": "CANCEL_REQUESTED"}
    _qdel(plan, state["pbs_id"])
    return {"status": "CANCEL_REQUESTED"}


STOP_GRACE_SECONDS = 600
"""How long a running job that drains on a stop (:func:`drains`) has before ``monitor`` deletes it with ``qdel``."""
RUNNING_STATES = ("R", "E")
"""PBS states of a job whose runner runs (or ends)."""


def drains(plan) -> bool:
    """Whether the run's runner drains on a stop and keeps its committed frames (a run that writes segments: its
    execution policy has ``segment_seconds``): its running PBS job is then left to end on its own after a stop,
    since ``qdel`` gives a job only PBS's ``kill_delay`` between SIGTERM and SIGKILL."""
    return plan["execution"].get("segment_seconds") is not None


def _pbs_run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
    """``qstat`` and ``qdel`` on a shared-storage target (one seam, which the tests replace)."""
    return subprocess.run(command, **options)


def _pbs_command(plan, command: list[str]) -> list[str]:
    import shlex

    host = plan["target"].get("submit_host")
    return ["ssh", "-T", "-o", "BatchMode=yes", host, shlex.join(command)] if host else command


def _qdel(plan, pbs_id: str) -> None:
    _pbs_run(_pbs_command(plan, ["/opt/pbs/bin/qdel", pbs_id]), check=True, capture_output=True, text=True, timeout=30)


def _job_state(plan, pbs_id: str) -> str:
    result = _pbs_run(
        _pbs_command(plan, ["/opt/pbs/bin/qstat", "-xf", "-F", "json", pbs_id]),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return next(iter(json.loads(result.stdout)["Jobs"].values()))["job_state"]


def _stop_overdue(plan) -> bool:
    """A stop was requested (``cancel.request`` in the control directory) more than ``STOP_GRACE_SECONDS`` ago: by
    the time it holds (``requested_at``, the stop's own, which the poller writes again on each pass while the stop is
    pending), else by the file's modification time."""
    requested = _stop_time(plan)
    return requested is not None and time.time() - requested > STOP_GRACE_SECONDS


def _stop_time(plan) -> float | None:
    """When the run's stop was requested (:func:`_stop_overdue`'s rule), as a POSIX time; None without a stop."""
    path = Path(plan["control_directory"]) / "cancel.request"
    try:
        requested: float | None = path.stat().st_mtime
    except FileNotFoundError:
        return None
    except OSError:
        requested = None
    try:
        recorded = json.loads(path.read_text()).get("requested_at")
    except (OSError, ValueError, AttributeError):
        recorded = None
    if isinstance(recorded, str):
        try:
            stamp = datetime.fromisoformat(recorded)
            requested = (stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)).timestamp()
        except ValueError:
            pass
    if requested is None and path.exists():
        return time.time()
    return requested


CLEANUP_WAIT_SECONDS = 600
"""How long the ``crux`` clean-up waits for its Globus delete task (a run's inputs can be large); a task still
running then fails the stage, and running it again submits another (missing files are ignored)."""


def eagle_run_folder(plan, settings) -> tuple[str, str]:
    """A ``crux`` run's folder on Eagle, ``<workspace>/runs/<submission>`` (its control, inputs and results), as
    (Transfer collection ID, collection path), from this launcher's own settings: ``storage.eagle``'s collection
    and the collection path of ``<targets.crux.workspace>/runs/<submission>``. The plan must agree: its result
    transfer comes from that collection, and its source is an absolute path with no ``..`` whose parent is that
    folder; anything else is refused."""
    eagle, crux = settings.storage.eagle, settings.targets.crux
    if eagle is None or not eagle.collection_id or crux is None or not crux.workspace:
        raise ValueError("The crux clean-up needs storage.eagle.collection_id and targets.crux.workspace")
    submission_id = str(UUID(plan["submission_id"]))
    folder = PurePosixPath(collection_path(eagle, PurePosixPath(crux.workspace) / "runs" / submission_id))
    spec = plan["result_transfer"]
    [item] = spec["transfer_items"]
    source = PurePosixPath(item["source_path"])
    if (
        spec["source_endpoint_id"] != eagle.collection_id
        or not source.is_absolute()
        or ".." in source.parts
        or source.parent != folder
    ):
        raise ValueError("The plan's result transfer does not come from this submission's Eagle folder")
    return eagle.collection_id, str(folder)


def cleanup(plan, settings):
    """Remove what a Finished, published run left outside its run directory: on ``crux`` its Eagle folder (a
    Globus delete task), then this launcher's state file. Refused unless the run directory holds this submission's
    publication of a Finished run (``run.json``, which the portal writes last, with its submission ID and the
    status ``Finished``): a Failed or Cancelled run keeps everything, an MPI run's ``shards/`` with its ranks'
    results and diagnostics included. Running it again is harmless. The portal removes its inbox copy (the plan
    read here) once this stage is done."""
    summary = Path(plan["run_directory"]) / "run.json"
    published = json.loads(summary.read_text()) if summary.is_file() else {}
    if not isinstance(published, dict) or published.get("submission_id") != plan["submission_id"]:
        raise ValueError("The run's results are not published; nothing was removed")
    if published.get("status") != "Finished":
        raise ValueError("The run did not finish (its published status is not Finished); nothing was removed")
    if transport(plan["backend"]) == GLOBUS:
        import globus_sdk

        endpoint, folder = eagle_run_folder(plan, settings)
        transfer = _transfer_client(settings)
        payload = globus_sdk.DeleteData(
            endpoint, label=f"Laue cleanup {plan['submission_id'][:8]}", recursive=True, ignore_missing=True
        )
        payload.add_item(folder)
        result = transfer.submit_delete(payload)
        if not transfer.task_wait(result["task_id"], timeout=CLEANUP_WAIT_SECONDS, polling_interval=10):
            raise TimeoutError("The Eagle folder's delete task is still running; run the stage again later")
        if transfer.get_task(result["task_id"])["status"] != "SUCCEEDED":
            raise RuntimeError("The Eagle folder's delete task failed")
    (_state_directory(settings) / f"{plan['submission_id']}.json").unlink(missing_ok=True)
    (_state_directory(settings) / f"{plan['submission_id']}.lock").unlink(missing_ok=True)
    return {"status": "CLEANED"}


# --- Continued runs (the module's docstring) -------------------------------------------------------------------------

LOCK_WAIT_SECONDS = 45.0
"""How long ``submit``, ``monitor`` and ``cancel`` wait for a continued run's lock (another of them holds it only
for its PBS calls)."""
RECONCILE_SECONDS = 120.0
"""How long after a ``qsub`` whose reply was lost the launcher looks for its job in PBS's history before it fails the
run (it never submits the allocation again)."""
VISIBILITY_GRACE_SECONDS = 60.0
"""How long after PBS reports an allocation finished (``F``) its runner's ``run.json`` and ``result.json`` may stay
invisible to the launcher (the shared storage's attribute caches) before the allocation is taken to have ended
without either."""
TERMINAL_STATUSES = ("SUCCEEDED", "FAILED", "CANCELLED")
QUEUED_DETAIL = "Waiting for next PBS allocation"
"""A continued run's detail while a processing allocation after its first waits in PBS."""
FINALIZE_QUEUED = "Stopping: waiting to merge"
"""A stopped run's detail while its finalizing allocation waits in PBS."""
FINALIZE_RUNNING = "Stopping: merging results"
"""A stopped run's detail while its finalizing allocation runs."""


class SchedulerUnavailable(Exception):
    """A PBS command (``qstat``, ``qselect``, ``qdel``) that did not answer usefully: it failed, timed out, could not
    run, or printed what cannot be read. Its message is the concrete reason (the command and its exit status, the tail
    of its stderr), never a value from a request."""


def continued(plan) -> bool:
    """Whether ``plan`` is a run continued over PBS allocations (a ``pioneer`` plan's ``continuation``)."""
    return plan.get("continuation") is not None


@contextlib.contextmanager
def _locked(settings, submission_id: str) -> Iterator[None]:
    """Hold ``<state_directory>/<submission>.lock`` (``TimeoutError`` after ``LOCK_WAIT_SECONDS``)."""
    directory = _state_directory(settings)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(directory / f"{submission_id}.lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("another launcher stage holds this run's lock") from None
                time.sleep(0.2)
        yield
    finally:
        os.close(descriptor)  # releases the lock


def _state_path(plan, settings) -> Path:
    return _state_directory(settings) / f"{plan['submission_id']}.json"


def _save(plan, settings, data: dict[str, Any], *, ledger: bool = False) -> None:
    """Write the state file (atomically) and, with ``ledger``, its copy ``allocations/ledger.json`` in the output
    folder. The state file is the launcher's record; the ledger (the run's record of its allocations, and what the
    portal reads as the reason of a failure) is written as well as the shared storage allows: a failed write is
    printed, and the next change writes it again."""
    write_json_file(_state_path(plan, settings), data, overwrite=True)
    if not ledger:
        return
    ledger_document = {
        "format": "laue-portal-allocation-ledger",
        "version": 1,
        "submission_id": plan["submission_id"],
        "status": data["status"],
        "message": data.get("message"),
        "allocations": data["allocations"],
    }
    try:
        folder = Path(plan["output_directory"]) / ALLOCATIONS_DIRECTORY
        folder.mkdir(parents=True, exist_ok=True)
        write_json_file(folder / ALLOCATION_LEDGER_FILENAME, ledger_document, overwrite=True)
    except OSError as error:
        print(f"The allocation ledger could not be written ({_failure(error)})", file=sys.stderr)


def _cancel_requested(plan) -> bool:
    return (Path(plan["control_directory"]) / "cancel.request").exists()


def _committed_work(plan) -> bool:
    """Whether the run has a committed segment (``segments/<NNNNNN>``) in its output folder. An unreadable folder is
    an ``OSError`` (the storage failing: decided on a later pass), never "nothing committed"."""
    try:
        names = os.listdir(Path(plan["output_directory"]) / "segments")
    except FileNotFoundError:
        return False
    return any(re.fullmatch(r"[0-9]{6}", name) for name in names)


def _scheduler(plan, command: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    """Run a PBS command (``_pbs_run``, over SSH when the target has ``submit_host``); ``SchedulerUnavailable`` with
    the concrete reason when it fails, times out or cannot run."""
    name = command[0].rsplit("/", 1)[-1]
    try:
        return _pbs_run(_pbs_command(plan, command), check=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.CalledProcessError as error:
        tail = (error.stderr or "").strip()[-200:] if isinstance(error.stderr, str) else ""
        raise SchedulerUnavailable(f"{name} exit {error.returncode}" + (f": {tail}" if tail else "")) from None
    except subprocess.TimeoutExpired:
        raise SchedulerUnavailable(f"{name} did not answer within {timeout:g} s") from None
    except OSError as error:
        raise SchedulerUnavailable(f"{name} could not run: {error.strerror or type(error).__name__}") from None


def _qselect(plan, name: str) -> list[str]:
    """The IDs of every job PBS has or had (``qselect -x``) named ``name``."""
    result = _scheduler(plan, ["/opt/pbs/bin/qselect", "-x", "-N", name])
    ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not all(re.fullmatch(PBS_JOB_ID, pbs_id) for pbs_id in ids):
        raise SchedulerUnavailable("qselect printed something other than job IDs")
    return ids


def _qstat(plan, pbs_id: str) -> Mapping[str, Any]:
    result = _scheduler(plan, ["/opt/pbs/bin/qstat", "-xf", "-F", "json", pbs_id])
    try:
        record = next(iter(json.loads(result.stdout)["Jobs"].values()))
        if not isinstance(record, dict) or not isinstance(record.get("job_state"), str):
            raise ValueError("no job_state")
    except (ValueError, KeyError, TypeError, StopIteration, AttributeError):
        raise SchedulerUnavailable(f"qstat printed no readable state of {pbs_id}") from None
    return record


def _qdel_continued(plan, pbs_id: str) -> None:
    _scheduler(plan, ["/opt/pbs/bin/qdel", pbs_id])


def _now_text() -> str:
    return _time_text(_now())


def _age(value: Any) -> float:
    stamp = aware_time(value)
    return float("inf") if stamp is None else (_now() - stamp).total_seconds()


def _new_allocation(plan, data: dict[str, Any], *, finalize: bool = False) -> dict[str, Any]:
    """Add the run's next allocation, ``submitting``: its folder, ``allocation.json`` (its number, name and whether it
    only ends a stopped run) and PBS script (a finalizing allocation's asks for the target's CPU ``finalize_pbs``) are
    written, then it is appended to ``data`` (saved by the caller before any ``qsub``). Written again in full when an
    earlier pass failed before the state file recorded it: no ``qsub`` was made for it."""
    sequence = len(data["allocations"]) + 1
    name = allocation_name(plan, sequence)
    folder = allocation_directory(plan["output_directory"], sequence)
    folder.mkdir(parents=True, exist_ok=True)
    now = _now_text()
    record = {
        "format": "laue-portal-allocation",
        "version": 1,
        "submission_id": plan["submission_id"],
        "sequence": sequence,
        "name": name,
        "finalize": finalize,
        "created_at": now,
    }
    write_json_file(folder / ALLOCATION_RECORD_FILENAME, record, overwrite=True)
    partial = folder / ".job.pbs.partial"
    partial.write_text(build_script(plan, allocation=sequence, finalize=finalize), encoding="utf-8")
    os.replace(partial, folder / "job.pbs")
    entry: dict[str, Any] = {
        "sequence": sequence,
        "name": name,
        "mode": "finalize" if finalize else "process",
        "state": "submitting",
        "created_at": now,
    }
    data["allocations"].append(entry)
    return entry


def _fail(plan, data: dict[str, Any], reason: str) -> None:
    """End the run ``FAILED`` (the module's docstring: the one answer to anything unexpected). Nothing is submitted
    after it; an allocation still in PBS is deleted, and the message says so, or says that it may still run; the
    message names the run's output folder, where the committed segments, ``checkpoint.json`` and every allocation's
    PBS output are kept."""
    entry = data["allocations"][-1] if data["allocations"] else None
    notes = [reason]
    if entry is not None and entry["state"] == "submitted":
        try:
            _qdel_continued(plan, entry["pbs_id"])
        except SchedulerUnavailable as error:
            notes.append(f"PBS job {entry['pbs_id']} could not be deleted ({error}) and may still run")
        else:
            entry["qdel_sent"] = _now_text()
            notes.append(f"Deletion requested for PBS job {entry['pbs_id']}")
    notes.append(f"the run's files are kept in {plan['output_directory']} (committed segments, checkpoint.json, logs)")
    data.update(status="FAILED", message="; ".join(notes))


def advance_submission(plan, settings, data: dict[str, Any], entry: dict[str, Any], *, first: bool = False) -> None:
    """Bring the ``submitting`` allocation ``entry`` into PBS, exactly once (the module's docstring): adopt the job
    PBS has of its name; else, after a ``qsub`` whose reply was lost (or a stage killed during one), wait
    ``RECONCILE_SECONDS`` for it to appear, then fail the run; else, for a processing allocation after a stop, submit
    nothing (``not_submitted``); else record the ``qsub``, save, and run it. A refusal fails the run (``first``: the
    ``submit`` stage, which then fails too). PBS commands that fail raise ``SchedulerUnavailable``."""
    ids = _qselect(plan, entry["name"])
    if ids:
        entry.update(state="submitted", pbs_id=ids[0], submitted_at=entry.get("submitted_at") or _now_text())
        return
    if entry.get("qsub") is not None:  # a qsub was made, and PBS's history has no job of the name
        if _age(entry["qsub_at"]) < RECONCILE_SECONDS:
            return
        _fail(
            plan,
            data,
            f"PBS never confirmed allocation {entry['sequence']}'s submission (qsub: {entry['qsub']}; no job named"
            f" {entry['name']} in its history after {RECONCILE_SECONDS:g} s). Nothing was submitted again: check"
            f" qselect -x -N {entry['name']}. The failure ledger blocks a late job only if it can be written;"
            " confirm PBS has no active job before restarting or salvaging",
        )
        return
    if entry["mode"] == "process" and _cancel_requested(plan):
        entry.update(state="not_submitted", ended_at=_now_text())
        _after_allocation(plan, settings, data, entry)
        return
    entry.update(qsub="pending", qsub_at=_now_text())
    try:
        _save(plan, settings, data)  # the qsub is on record before PBS is asked
    except BaseException:
        del entry["qsub"], entry["qsub_at"]  # not recorded, so never made
        raise
    folder = allocation_directory(plan["output_directory"], entry["sequence"])
    command = _pbs_command(
        plan, ["/opt/pbs/bin/qsub", "-o", str(folder / PBS_OUTPUT_FILENAME), str(folder / "job.pbs")]
    )
    try:
        result = _pbs_run(command, check=False, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        entry["qsub"] = "no reply within 60 s"
        return
    except OSError as error:  # the command never ran
        refused = f"qsub could not run: {error.strerror or type(error).__name__}"
    else:
        pbs_id = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(PBS_JOB_ID, pbs_id):
            entry.update(qsub="accepted", state="submitted", pbs_id=pbs_id, submitted_at=_now_text())
            return
        if result.returncode in (0, 255):  # an unreadable reply, or ssh's own failure: PBS may have the job
            entry["qsub"] = f"exit {result.returncode}, no job ID in its reply"
            return
        refused = f"qsub exit {result.returncode}: {result.stderr.strip()[-300:]}"
    entry.update(qsub="refused", state="not_submitted", ended_at=_now_text())
    _fail(plan, data, f"PBS refused allocation {entry['sequence']} ({refused})")
    if first:
        _save(plan, settings, data, ledger=True)
        raise RuntimeError(data["message"])


def submit_continued(plan, settings) -> dict[str, Any]:
    """``01-SUBMIT`` of a continued run: its output folder, then its first allocation, recorded before ``qsub``
    (:func:`advance_submission`). A lost reply, and PBS that does not answer, are left to the monitor (``RUNNING``)."""
    state = _state_path(plan, settings)
    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _locked(settings, plan["submission_id"]):
        if state.exists():
            raise FileExistsError("This submission was already attempted; inspect its saved state before retrying")
        Path(plan["output_directory"]).mkdir(parents=True, exist_ok=False)
        data: dict[str, Any] = {"status": "RUNNING", "allocations": []}
        entry = _new_allocation(plan, data)
        _save(plan, settings, data)
        try:
            advance_submission(plan, settings, data, entry, first=True)
        except SchedulerUnavailable as error:  # qselect: nothing was submitted yet; the monitor tries again
            print(f"PBS did not answer the first submission: {error}", file=sys.stderr)
        _save(plan, settings, data, ledger=True)
    return data


RESULT_MISSING = "it wrote no result"
""":func:`read_result`'s problem when the allocation's ``result.json`` is absent (which may be the shared storage's
lag: ``VISIBILITY_GRACE_SECONDS``)."""


def read_result(plan, entry: Mapping[str, Any], before: int) -> tuple[dict[str, Any] | None, str | None]:
    """The runner's ``result.json`` of the allocation ``entry``, when it checks: this format, submission and
    allocation; ``handoff`` or ``interrupted``; committed counts that are whole and add up, more inputs than
    ``before`` for a hand-off, and fewer than the manifest. The next allocation checks every committed segment itself.
    Else None and why: ``RESULT_MISSING`` when the file is absent. An error reading a file that is there (``EIO``, a
    stale handle) is raised: the storage failing, not the allocation."""
    folder = allocation_directory(plan["output_directory"], entry["sequence"])
    try:
        os.listdir(folder)  # a fresh listing of the folder before the look-up (the client's directory cache)
        text = (folder / ALLOCATION_RESULT_FILENAME).read_text()
    except FileNotFoundError:
        return None, RESULT_MISSING
    try:
        result = json.loads(text)
        if not isinstance(result, dict) or result.get("format") != ALLOCATION_RESULT_FORMAT:
            raise ValueError("not an allocation result")
        if (result.get("submission_id"), result.get("allocation")) != (plan["submission_id"], entry["sequence"]):
            raise ValueError("another allocation's result")
        if result.get("status") not in ("handoff", "interrupted"):
            raise ValueError("an unknown status")
        committed = result["committed"]
        attempted, succeeded, failed = (committed[key] for key in ("n_attempted", "n_succeeded", "n_failed"))
        if not all(type(value) is int and value >= 0 for value in (attempted, succeeded, failed)) or (
            succeeded + failed != attempted
        ):
            raise ValueError("its committed counts do not add up")
        if attempted >= int(plan["n_inputs"]) or (result["status"] == "handoff" and attempted <= before):
            raise ValueError(f"it committed {attempted} inputs (before it, {before}; the run has {plan['n_inputs']})")
    except (ValueError, TypeError, KeyError) as error:
        return None, f"its result does not check: {error}"
    return result, None


def _committed_before(data: Mapping[str, Any], sequence: int) -> int:
    """The inputs committed by the last hand-off before allocation ``sequence`` (0 before any)."""
    for entry in reversed(data["allocations"]):
        if entry["sequence"] < sequence and isinstance(entry.get("committed"), int):
            return entry["committed"]
    return 0


def _next_allocation(plan, settings, data: dict[str, Any], *, finalize: bool = False) -> None:
    entry = _new_allocation(plan, data, finalize=finalize)
    _save(plan, settings, data, ledger=True)  # recorded before any qsub
    advance_submission(plan, settings, data, entry)


def _summary_written(plan) -> bool:
    """Whether the run's summary (``run.json``) is in its output folder (``OSError`` but for its absence)."""
    try:
        os.stat(Path(plan["output_directory"]) / "run.json")
    except FileNotFoundError:
        return False
    return True


def _after_allocation(plan, settings, data: dict[str, Any], entry: dict[str, Any]) -> None:
    """Decide what follows allocation ``entry``, which PBS reports finished (``ended``) or which was not submitted
    after a stop (``not_submitted``). Re-entrant: until it has added the next allocation or ended the run, the entry
    stays the run's last and the next pass decides again (an error reading the shared storage, or a launcher killed,
    in between never leads to a second allocation).

    - The run's summary is written: the run ends as it says (``final``).
    - Neither the summary nor the allocation's ``result.json`` is visible yet: wait (``VISIBILITY_GRACE_SECONDS``).
    - A hand-off that checks: the next processing allocation, or after a stop the finalizing one.
    - A processing allocation that did not hand the run on after a stop: the finalizing allocation when work is
      committed, else ``CANCELLED``.
    - Anything else (no result, one that does not check, an interrupted allocation, a finalizing allocation without
      the summary): ``FAILED`` (:func:`_fail`)."""
    if _summary_written(plan):
        entry["result"] = "final"
        data["status"] = "SUCCEEDED" if entry.get("exit_status") == 0 else "FAILED"
        return
    stopped = _cancel_requested(plan)
    result = problem = None
    if entry["state"] == "ended":
        result, problem = read_result(plan, entry, _committed_before(data, entry["sequence"]))
        if problem == RESULT_MISSING and _age(entry["ended_at"]) < VISIBILITY_GRACE_SECONDS:
            return
        entry["result"] = result["status"] if result is not None else "none"
        if result is not None:
            entry["committed"] = result["committed"]["n_attempted"]
        if problem:
            entry["problem"] = problem
    if result is not None and result["status"] == "handoff" and entry["mode"] == "process":
        _next_allocation(plan, settings, data, finalize=stopped)
        return
    if entry["mode"] == "process" and stopped:
        if _committed_work(plan):
            _next_allocation(plan, settings, data, finalize=True)
        else:
            data.update(status="CANCELLED", message="Stopped before any input was committed")
        return
    if result is not None:
        problem = f"it was interrupted ({result.get('reason')})"
    log = allocation_directory(plan["output_directory"], entry["sequence"]) / PBS_OUTPUT_FILENAME
    _fail(
        plan,
        data,
        f"{'The finalizing allocation' if entry['mode'] == 'finalize' else 'Allocation'} {entry['sequence']} (PBS"
        f" {entry.get('pbs_id')}, exit status {entry.get('exit_status')}) ended without the run's summary or a"
        f" hand-off: {problem}; see {log}",
    )


def _stop_continued(plan, entry: dict[str, Any], job_state: str) -> None:
    """A stop while ``entry`` is in PBS: a queued processing allocation is deleted at once; a running processing
    allocation that has not drained ``STOP_GRACE_SECONDS`` after the stop (or after it started running, when later)
    is deleted. A finalizing allocation is never deleted: it only merges the committed work."""
    requested = _stop_time(plan)
    if requested is None or entry.get("qdel_sent") or job_state == "F" or entry["mode"] != "process":
        return
    if job_state in RUNNING_STATES:
        running = aware_time(entry.get("running_since"))
        if time.time() - max(requested, running.timestamp() if running is not None else requested) <= (
            STOP_GRACE_SECONDS
        ):
            return
    _qdel_continued(plan, entry["pbs_id"])
    entry["qdel_sent"] = _now_text()


def _step(plan, settings, data: dict[str, Any]) -> None:
    entry = data["allocations"][-1]
    if entry["state"] == "submitting":
        advance_submission(plan, settings, data, entry)
        return
    if entry["state"] == "submitted":
        record = _qstat(plan, entry["pbs_id"])
        job_state = entry["job_state"] = str(record["job_state"])
        if job_state in RUNNING_STATES and not entry.get("running_since"):
            entry["running_since"] = _now_text()
        if job_state != "F":
            _stop_continued(plan, entry, job_state)
            return
        entry.update(state="ended", ended_at=_now_text(), exit_status=record.get("Exit_status"))
        _save(plan, settings, data)  # PBS's answer on record before the decision that follows it
    _after_allocation(plan, settings, data, entry)


def monitor_continued(plan, settings) -> dict[str, Any]:
    """``02-MONITOR`` of a continued run (the module's docstring): under the run's lock, one step of its current
    allocation (reconcile or submit it, read its PBS state, act on a stop, decide what follows its end), then its
    observation. A run whose lock another stage holds past ``LOCK_WAIT_SECONDS`` prints its kept observation, stale."""
    try:
        with _locked(settings, plan["submission_id"]):
            return _monitor_step(plan, settings)
    except TimeoutError:
        data = json.loads(_state_path(plan, settings).read_text())
        kept = data.get("observation")
        return {**data, "observation": {**kept, "stale": True} if isinstance(kept, dict) else None}


def _monitor_step(plan, settings) -> dict[str, Any]:
    """One monitor pass under the lock. PBS that does not answer (``SchedulerUnavailable``) and the shared storage
    failing (``OSError``) are waited out: the run stays ``RUNNING``, its observation stale, the reason kept in the
    state file (``unavailable``), and the next pass resumes where this one stopped; an allocation PBS cannot be asked
    about is never taken to have ended. Any other error fails the run (:func:`_fail`)."""
    data = json.loads(_state_path(plan, settings).read_text())
    if data["status"] in TERMINAL_STATUSES:
        return data
    before = json.dumps([data["status"], data["allocations"]], sort_keys=True)
    try:
        _step(plan, settings, data)
    except SchedulerUnavailable as error:
        data["unavailable"] = f"PBS: {error}"
    except OSError as error:
        data["unavailable"] = f"shared storage: {type(error).__name__}: {error.strerror or error}"
    except Exception as error:  # anything else: the run fails, its files kept
        _fail(plan, data, f"The launcher failed: {type(error).__name__}: {error}")
        data.pop("unavailable", None)
    else:
        data.pop("unavailable", None)
    if data.get("unavailable"):
        print(f"{data['unavailable']}; the run waits", file=sys.stderr)
    try:
        observation = None if data.get("unavailable") else continued_observation(plan, data)
    except OSError:
        observation = None
    if observation is None:  # the kept observation, stale (none before the first one)
        kept = data.get("observation")
        observation = {**kept, "stale": True} if isinstance(kept, dict) else None
    data["observation"] = observation
    _save(plan, settings, data, ledger=json.dumps([data["status"], data["allocations"]], sort_keys=True) != before)
    return data


def cancel_continued(plan, settings) -> dict[str, Any]:
    """The cancel stage of a continued run (``cancel.request`` is already in the control folder), under its lock: a
    queued processing allocation is deleted at once (:func:`_stop_continued`); the monitor does the rest. PBS that
    does not answer, or the lock held past ``LOCK_WAIT_SECONDS``, leaves it to the monitor: the stage still reports
    the stop requested, so DM's cancel workflow never fails for them."""
    try:
        with _locked(settings, plan["submission_id"]):
            state = _state_path(plan, settings)
            if not state.exists():
                return {"status": "CANCEL_REQUESTED"}
            data = json.loads(state.read_text())
            entry = data["allocations"][-1] if data.get("allocations") else None
            if data["status"] not in TERMINAL_STATUSES and entry is not None and entry["state"] == "submitted":
                try:
                    _stop_continued(plan, entry, str(_qstat(plan, entry["pbs_id"])["job_state"]))
                except SchedulerUnavailable as error:
                    print(f"PBS did not answer the stop ({error}); the monitor deletes the job", file=sys.stderr)
                _save(plan, settings, data, ledger=True)
    except TimeoutError:
        print("Another launcher stage holds this run's lock; the monitor acts on the stop", file=sys.stderr)
    return {"status": "CANCEL_REQUESTED"}


def continued_observation(plan, data: Mapping[str, Any]) -> dict[str, Any]:
    """A continued run's observation (the format of ``remote.observation``): ``queued`` while its allocation waits
    (``Waiting for PBS allocation``, then ``Waiting for next PBS allocation``; a finalizing one ``FINALIZE_QUEUED``),
    ``running`` with the run's committed frames while a processing allocation runs (``progress.json``: the whole run's,
    never fewer than shown before; ``FINALIZE_RUNNING`` for a finalizing one), and ``Finishing processing`` once the
    run has ended. ``pbs`` names the allocation's job, number and mode (``process``, ``finalize``)."""
    now = _now()
    previous = data.get("observation") if isinstance(data.get("observation"), dict) else None
    entry = data["allocations"][-1] if data.get("allocations") else {}
    shown = previous.get("frames_done") if previous is not None else None
    done = shown if type(shown) is int else 0
    heartbeat = None
    try:
        progress = json.loads((Path(plan["output_directory"]) / "progress.json").read_text())
        done = max(done, int(progress["n_succeeded"]) + int(progress["n_failed"]))
        heartbeat = aware_time(progress.get("heartbeat_at"))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    stopping = _cancel_requested(plan)
    stale = None
    running = entry.get("state") == "submitted" and entry.get("job_state") in RUNNING_STATES
    if data.get("status") in TERMINAL_STATUSES or entry.get("result") == "final":
        phase, detail = "running", "Finishing processing"
    elif entry.get("state") in ("ended", "not_submitted"):
        phase, detail = "running", f"Ending allocation {entry.get('sequence')}"
    elif entry.get("mode") == "finalize":
        phase, detail = ("running", FINALIZE_RUNNING) if running else ("queued", FINALIZE_QUEUED)
    elif running:
        phase, detail = "running", f"{'Stopping' if stopping else 'Processing'}: {done} of {plan['n_inputs']} frames"
        stale = heartbeat is not None and (now - heartbeat).total_seconds() > PBS_STALE_SECONDS
    elif stopping:
        phase, detail = "queued", "Stopping"
    else:
        phase, detail = PBS_QUEUED if int(entry.get("sequence", 1)) == 1 else ("queued", QUEUED_DETAIL)
    observed = _time_text(now)
    observation: dict[str, Any] = {"phase": phase, "detail": detail, "observed_at": observed}
    if stale is not None:
        observation["stale"] = stale
    since = previous.get("phase_since") if previous is not None and previous.get("phase") == phase else None
    observation["phase_since"] = since or observed
    observation["submission_id"] = plan["submission_id"]
    observation["pbs"] = {"job_id": entry.get("pbs_id"), "allocation": entry.get("sequence"), "mode": entry.get("mode")}
    observation["frames_done"] = done
    return observation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("action", choices=["submit", "monitor", "permissions", "check", "cancel", "cleanup", "auth"])
    parser.add_argument("submission_id", nargs="?")
    args = parser.parse_args()
    # Before any write: every file a stage writes (the state file, whichever path writes it; gladier's) is the
    # account's own (0600, 0700), whatever umask DM gives. The caller's umask is back once the stage ends.
    previous = os.umask(0o077)
    try:
        return _stage(args)
    finally:
        os.umask(previous)


def _stage(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    if args.action == "auth":
        from laueportal.remote.auth import authorize

        client = _client(settings)
        result = authorize(client, settings)
        print("Authorization ready.")
        print(f"Flow ID: {result['flow_id']}")
        for name, function_id in result["function_ids"].items():
            print(f"Compute function {name}: {function_id}")
        return 0
    plan = json.loads(submission_path(settings, args.submission_id).read_text())
    if plan.get("submission_id") != str(UUID(args.submission_id)):  # the state file and the folders are named by it
        raise ValueError("The plan in the DM inbox names another submission than the one requested")
    if args.action == "check":
        state = json.loads((_state_directory(settings) / f"{plan['submission_id']}.json").read_text())
        if state["status"] != "SUCCEEDED":
            return 1
        summary_path = Path(plan.get("incoming_directory", plan["output_directory"])) / "run.json"
        return 0 if json.loads(summary_path.read_text())["status"] == "Finished" else 1
    data = {
        "submit": submit,
        "monitor": monitor,
        "permissions": restore_permissions,
        "cancel": cancel,
        "cleanup": cleanup,
    }[args.action](plan, settings)
    print(f"Status: {data['status']}")
    if data.get("observation"):  # monitor, crux: 02-MONITOR's regex keeps it as remoteObservation
        print("Observation: " + json.dumps(data["observation"], separators=(",", ":")))
    if data.get("flow_id"):
        print(f"Flow Action ID: {data['flow_id']}")
        print(f"URL: {data['url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
