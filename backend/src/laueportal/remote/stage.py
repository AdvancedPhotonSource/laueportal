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
It reads only the YAML settings: no database, no portal settings
(import-linter rule 3). gladier and globus-sdk (``requirements/launcher.in``) are imported where needed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import UUID

from laueportal.jobs.rundir import write_json_file
from laueportal.remote.dm import DMClient
from laueportal.remote.observation import aware_time
from laueportal.remote.paths import child_path
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
    state = _state_directory(settings) / f"{plan['submission_id']}.json"
    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state.exists():
        raise FileExistsError("This submission was already attempted; inspect its saved state before retrying")
    # Also retain an uncertain state when a network request succeeds but its reply is lost.
    write_json_file(state, {"status": "SUBMISSION_UNCERTAIN"})
    if plan["backend"] == "crux":
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
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
        record = next(iter(json.loads(result.stdout)["Jobs"].values()))
        if record["job_state"] == "F":
            data["status"] = "SUCCEEDED" if record.get("Exit_status") == 0 else "FAILED"
    else:
        raise ValueError("Submission has no returned remote ID; manual reconciliation is required")
    write_json_file(state, data, overwrite=True)
    return data


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
    if plan["backend"] != "crux" or state["status"] != "SUCCEEDED":
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
    if plan["backend"] == "crux":
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
    import shlex

    state = json.loads((_state_directory(settings) / f"{plan['submission_id']}.json").read_text())
    command = ["/opt/pbs/bin/qdel", state["pbs_id"]]
    if plan["target"].get("submit_host"):
        command = ["ssh", "-T", "-o", "BatchMode=yes", plan["target"]["submit_host"], shlex.join(command)]
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
    return {"status": "CANCEL_REQUESTED"}


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
    if plan["backend"] == "crux":
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
    return {"status": "CLEANED"}


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
