"""Use DM's supported Python API through its installed environment.

A development host can use a configured SSH bridge; the deployed portal can invoke the same API locally. Passwords
remain in DM_LOGIN_FILE and never in arguments. The DM workflow names and arguments are the deployed ones
(``deploy/remote/``).

Each call is one subprocess (``DMClient._call``): one shell and Python process, one SSH session behind a bridge, bounded
by ``dm.timeout_seconds``; no persistent client. The shell is no login shell (``bash -c``): DM's setup file supplies its
environment, and a login profile's banner or modules could corrupt the JSON the call prints. The remote poller's cycle
makes one call for every active remote run (``DMClient.fetch``, the ``fetch`` operation: every record it needs, and the
look-ups by submission ID after a lost reply), whose answer reports a record DM could not give per record (``Records``).
Every process a client starts goes through ``DMClient.run``. Its callers:
- the launcher's ``permissions`` stage on the DM host (``remote.stage.restore_permissions``: one call, bounded by
  ``dm.permissions_timeout_seconds``);
- an API request's submission (``remote.submission``), and the api's DAQ list and bind check (``jobs.daq``): each
  call in the api's process group, which the API program's stop reaches;
- the worker's remote poller thread (``remote.poller``) and the catalog thread of a live run's job process
  (``jobs.catalog``): each call in a session of its own (``own_session``), killed whole, with everything it started,
  at its timeout and at ``DMClient.abort`` (the worker's end; the catalog thread's close, before its run's seal and at
  the run's end), and, with a timeout (``dm.timeout_seconds``, ``live.dm_timeout_seconds``; none when it is null),
  bounded by itself too (coreutils ``timeout``, ``SELF_BOUND``), so that a worker or a job process killed outright (an
  OOM kill, SIGKILL) leaves no call running long past its limit: what the bound guarantees with each ``timeout`` is in
  ``SELF_BOUND``'s note. Neither the service stop's SIGUSR1 (sent to the worker's job processes by pid) nor
  supervisor's or the dispatcher's group kill reaches such a call's session. Started by the worker, a call's process
  inherits the worker's ignored SIGUSR1 (``jobs.worker``), which is intended: a stray SIGUSR1 cannot end a DM call
  half-way; a job process's catalog thread ends at its run's stop by the stop flag, the service stop or its close.

Live runs add three operations, each one call of the same kind, with the ``[live]`` settings' client
(``jobs.daq.live_client``): the DAQ list (``DMClient.daqs``, the Live page's shared answer), a DAQ's info
(``DMClient.daq``, the bind check at a live run's creation) and the catalog thread's cycle (``DMClient.catalog``: every
page of its queries, one request at a time, within a row cap and a time). A DAQ record or a catalog row that cannot be
read is reported per record; an answer without the operation's shape fails the call. Each operation imports and builds
only the DM API it uses (the workflow API for ``submit``, ``get`` and ``fetch``; the DAQ API; the file catalog's).
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from laueportal.remote.settings import DMSettings, RemoteSettingsError
from laueportal.remote.transport import shell_command

_API_SCRIPT = """
import json, sys, time
script_started = time.monotonic()
request = json.load(sys.stdin)
operation, args = request['operation'], request['args']
# Each operation imports and builds only the DM API it uses: the remote runs' the workflow API, the live runs' the
# DAQ API or the file catalog's (a DM setup for one service only serves its own operations).
if operation in ('submit', 'get', 'fetch', 'workflow'):
    from dm.proc_web_service.api.workflowProcApi import WorkflowProcApi
    api = WorkflowProcApi()
if operation == 'workflow':
    result = [api.getWorkflowByName(args['owner'], name) for name in args['workflows']]
elif operation == 'submit':
    result = api.startProcessingJob(args['owner'], args['workflow'], args['arguments'])
elif operation == 'get':
    try:
        result = api.getProcessingJobById(args['owner'], args['id'])
    except Exception:
        result = api.getProcessingJobRecord(args['id'])
elif operation == 'fetch':
    # The remote poller's one call per cycle: the record of every DM job asked for, then the jobs of each
    # submission asked for by its submissionId (after a lost reply), with the record of a single job found.
    # Whatever fails for one record is that record's error; the others stand.
    def failure(error):
        return {'error': ('%s: %s' % (type(error).__name__, error))[:500]}
    def plain(value):
        data = getattr(value, 'data', None)
        return data if isinstance(data, dict) else value
    ids, records, found = list(args['ids']), {}, {}
    for submission_id in args['submission_ids']:
        try:
            jobs = list(api.listProcessingJobRecords(queryDict={'submissionId': submission_id}, limit=10))
        except Exception as error:
            found[submission_id] = failure(error)
            continue
        found[submission_id] = {'records': jobs}
        single = plain(jobs[0]) if len(jobs) == 1 else None
        if isinstance(single, dict) and single.get('id') is not None and single['id'] not in ids:
            ids.append(single['id'])
    for job_id in ids:
        try:
            try:
                records[job_id] = {'record': api.getProcessingJobById(args['owner'], job_id)}
            except Exception:
                records[job_id] = {'record': api.getProcessingJobRecord(job_id)}
        except Exception as error:
            records[job_id] = failure(error)
    result = {'records': records, 'found': found}
elif operation == 'restore_permissions':
    # The launcher's permissions stage (03-PERMISSIONS): DM's storage service restores the experiment's
    # managed ownership and ACLs on one returned directory, named relative to the experiment's folder.
    from dm.ds_web_service.api.fileDsApi import FileDsApi
    result = FileDsApi().restorePermissions(experimentPath=args['relative_path'], experimentName=args['experiment'])
elif operation == 'daqs':
    # Live runs: the DAQs DM knows, for the Live page's list.
    from dm.daq_web_service.api.experimentDaqApi import ExperimentDaqApi
    result = ExperimentDaqApi().listDaqs('any')
elif operation == 'daq':
    # Live runs: one DAQ's record, for the bind check at a live run's creation; null when DM has no such DAQ.
    from dm.daq_web_service.api.experimentDaqApi import ExperimentDaqApi
    try:
        result = ExperimentDaqApi().getDaqInfo(args['id'])
    except Exception as error:
        if type(error).__name__ != 'ObjectNotFound':
            raise
        result = None
elif operation == 'catalog':
    # Live runs: the catalog thread's cycle. Each read's pages, in order, from its skip: one request at a time of at
    # most 'limit' rows, until a shorter page ends the read, within 'cap' rows in all; after the first request, none
    # that the longest so far says would end past 'seconds' from this script's start. A read the cap or the time
    # stopped (or never began) has 'more'.
    from dm.cat_web_service.api.fileCatApi import FileCatApi
    api, longest, budget, reads = FileCatApi(), None, args['cap'], []
    keys = ['experimentFilePath', 'fileSize', 'fileProcessingTime', 'daqId', 'experimentName']
    for read in args['reads']:
        files, skip, more = [], read['skip'], True
        while more and budget > 0 and (
                longest is None or time.monotonic() - script_started + longest <= args['seconds']):
            limit, begun = min(args['limit'], budget), time.monotonic()
            page = list(api.getExperimentFiles(
                args['experiment'], queryDict=read['query'], skip=skip, limit=limit, keyList=keys))
            longest = max(longest or 0.0, time.monotonic() - begun)
            if len(page) > limit:
                raise ValueError('DM gave %d catalog rows for a page of %d' % (len(page), limit))
            files.extend(page)
            skip, budget, more = skip + len(page), budget - len(page), len(page) == limit
        reads.append({'files': files, 'more': more})
    result = {'reads': reads}
else:
    raise ValueError('Unsupported operation')
def serialize(value):
    # DM returns DmObject instances, including ProcessingJob and job lists.
    # Their string representation is a Python dict repr, not an API record.
    if isinstance(getattr(value, 'data', None), dict):
        return value.data
    return str(value)
print(json.dumps(result, default=serialize))
"""

STAGE_MODULE = "laueportal.remote.stage"
"""The launcher's stage commands."""
LAUNCHER_PACKAGES = "backend/src"
"""Where a checkout of this portal keeps the package (``deploy/remote/dm-stage.sh`` puts it on ``PYTHONPATH``)."""


class DMRecordError(RuntimeError):
    """What DM answered for one record of a batched call (``DMClient.fetch``) instead of the record; the call's
    other records stand."""


@dataclass(frozen=True)
class Records:
    """One batched call's answer (``DMClient.fetch``): DM's records by job ID, and the jobs found by submission ID;
    one DM could not give is its ``DMRecordError``, raised when it is asked for."""

    by_id: Mapping[str, dict[str, Any] | DMRecordError] = field(default_factory=dict)
    by_submission: Mapping[str, list[dict[str, Any]] | DMRecordError] = field(default_factory=dict)

    def __contains__(self, external_id: object) -> bool:
        return external_id in self.by_id

    def get(self, external_id: str) -> dict[str, Any]:
        """The record of DM job ``external_id``."""
        value = self.by_id.get(external_id)
        if value is None:
            raise DMRecordError(f"DM's answer has no record of job {external_id}")
        if isinstance(value, DMRecordError):
            raise value
        return value

    def find(self, submission_id: str) -> list[dict[str, Any]]:
        """The DM jobs whose ``submissionId`` is ``submission_id``."""
        value = self.by_submission.get(submission_id)
        if value is None:
            raise DMRecordError(f"DM's answer has no look-up of submission {submission_id}")
        if isinstance(value, DMRecordError):
            raise value
        return value


def parse_records(answer: Any) -> Records:
    """The ``fetch`` operation's answer as ``Records``; ``ValueError`` when it does not have the operation's shape
    (the call failed: no ``records`` or ``found`` mapping, an entry with neither its value nor an ``error``, a
    look-up that is no list). A record that is not a JSON object, or a look-up holding one, is that record's
    ``DMRecordError``: the call's other records stand."""

    def entry(value: Any, key: str) -> Any:
        """An entry's value (its ``key``), or the error DM gave instead."""
        if isinstance(value, dict) and isinstance(value.get("error"), str):
            return DMRecordError(value["error"])
        if not isinstance(value, dict) or key not in value:
            raise ValueError(f"DM's answer to the batched call is malformed: {key}")
        return value[key]

    if not isinstance(answer, dict) or not isinstance(answer.get("records"), dict):
        raise ValueError("DM's answer to the batched call is malformed: records")
    if not isinstance(answer.get("found"), dict):
        raise ValueError("DM's answer to the batched call is malformed: found")
    by_id: dict[str, dict[str, Any] | DMRecordError] = {}
    for key, value in answer["records"].items():
        record = entry(value, "record")
        if not isinstance(record, (dict, DMRecordError)):
            record = DMRecordError(f"DM's record of job {key} is not an object")
        by_id[str(key)] = record
    by_submission: dict[str, list[dict[str, Any]] | DMRecordError] = {}
    for key, value in answer["found"].items():
        jobs = entry(value, "records")
        if not isinstance(jobs, (list, DMRecordError)):
            raise ValueError("DM's answer to the batched call is malformed: records")
        if isinstance(jobs, list) and not all(isinstance(job, dict) for job in jobs):
            jobs = DMRecordError(f"DM's look-up of submission {key} holds a record that is not an object")
        by_submission[str(key)] = jobs
    return Records(by_id, by_submission)


# ---------------------------------------------------------------------------
# Live runs' DM operations: the DAQ list, a DAQ's info and the catalog's cycle, each one call


@dataclass(frozen=True)
class DaqRecord:
    """A DAQ as DM's ``ExperimentDaqApi`` describes it: a summary's keys and nothing else (no
    setting or credential of DM's reaches a portal answer). The directories are DM's text, unchecked."""

    id: str
    experiment: str
    status: str | None
    data_directory: str | None
    storage_directory: str | None
    """The experiment's storage directory: the catalog's ``experimentFilePath`` is relative to it."""
    dest_directory: str | None
    """Where the DAQ writes, relative to ``storage_directory`` (DM's ``destDirectory``; none: the storage itself)."""


def _identifier(value: Any) -> str | None:
    """DM's ID (text, or a whole number as text: ``str(record["id"])``), or None."""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def parse_daq(value: Any) -> DaqRecord:
    """One DAQ record of DM's answer; ``DMRecordError`` when it is not an object with an ``id`` and an
    ``experimentName``. A key that is not text (a directory, the status) reads as absent."""
    if not isinstance(value, dict):
        raise DMRecordError("DM's DAQ record is not an object")
    identifier = _identifier(value.get("id"))
    if identifier is None:
        raise DMRecordError("DM's DAQ record has no id")
    experiment = value.get("experimentName")
    if not isinstance(experiment, str) or not experiment:
        raise DMRecordError(f"DM's DAQ {identifier} has no experimentName")

    def text(key: str) -> str | None:
        found = value.get(key)
        return found if isinstance(found, str) else None

    return DaqRecord(
        id=identifier,
        experiment=experiment,
        status=text("status"),
        data_directory=text("dataDirectory"),
        storage_directory=text("storageDirectory"),
        dest_directory=text("destDirectory"),
    )


def parse_daqs(answer: Any) -> list[DaqRecord | DMRecordError]:
    """The ``daqs`` operation's answer: each DAQ, or the ``DMRecordError`` of a record that cannot be read (the others
    stand); ``ValueError`` when the answer is not a list (the call failed)."""
    if not isinstance(answer, list):
        raise ValueError("DM's answer to the DAQ list is malformed: not a list")
    daqs: list[DaqRecord | DMRecordError] = []
    for value in answer:
        try:
            daqs.append(parse_daq(value))
        except DMRecordError as error:
            daqs.append(error)
    return daqs


@dataclass(frozen=True)
class CatalogRow:
    """One row of DM's file catalog: what the catalog thread reads of it. Only the path is required here: the size and
    the time are checked by the catalog thread for the run's own files only (the folder's
    other files are never admitted or reported; their time only bounds a provisional cursor, ``jobs.catalog``)."""

    path: str
    """``experimentFilePath``: the file's path relative to its experiment's storage directory (unchecked here)."""
    size: int | None
    """``fileSize``: the file's whole size, which the file must have to be admitted; None when it is not a positive
    whole number."""
    processing_time: float | None
    """``fileProcessingTime``: when DM catalogued it (DM's clock, seconds), the catalog thread's cursor; None when it is
    not a finite, non-negative number of seconds."""
    daq_id: str | None
    experiment: str | None


def parse_catalog_row(value: Any) -> CatalogRow:
    """One catalog row; ``DMRecordError`` when it is not an object with an ``experimentFilePath``. A size or a time
    that cannot be used reads as None (``CatalogRow``)."""
    if not isinstance(value, dict):
        raise DMRecordError("a catalog row is not an object")
    path = value.get("experimentFilePath")
    if not isinstance(path, str) or not path:
        raise DMRecordError("a catalog row has no experimentFilePath")
    size = value.get("fileSize")
    seconds = value.get("fileProcessingTime")
    processing_time = None
    if not isinstance(seconds, bool) and isinstance(seconds, (int, float)) and math.isfinite(seconds) and seconds >= 0:
        processing_time = float(seconds)
    experiment = value.get("experimentName")
    return CatalogRow(
        path=path,
        size=size if type(size) is int and size > 0 else None,
        processing_time=processing_time,
        daq_id=_identifier(value.get("daqId")),
        experiment=experiment if isinstance(experiment, str) else None,
    )


@dataclass(frozen=True)
class CatalogRead:
    """What one catalog call read of one query (``DMClient.catalog``): its rows from the query's ``skip``, the
    ``DMRecordError`` of each row that cannot be read (the others stand), how many rows DM gave, unreadable ones
    included (the query's next ``skip`` is this much further), and whether more may follow (``more``: the call's row
    cap or its time stopped it on a full page, or before its first page)."""

    rows: tuple[CatalogRow, ...]
    failed: tuple[DMRecordError, ...]
    count: int
    more: bool


def parse_catalog(answer: Any, reads: int, cap: int) -> tuple[CatalogRead, ...]:
    """The ``catalog`` operation's answer, one ``CatalogRead`` per read asked for; ``ValueError`` when it has not the
    operation's shape (no ``reads`` list of that many reads, each with a ``files`` list and a ``more`` flag, or more
    rows in all than the cap): the call failed."""
    found = answer.get("reads") if isinstance(answer, dict) else None
    if not isinstance(found, list) or len(found) != reads:
        raise ValueError("DM's answer to the catalog call is malformed: reads")
    if not all(isinstance(read, dict) and isinstance(read.get("files"), list) for read in found):
        raise ValueError("DM's answer to the catalog call is malformed: files")
    if not all(isinstance(read.get("more"), bool) for read in found):
        raise ValueError("DM's answer to the catalog call is malformed: more")
    total = sum(len(read["files"]) for read in found)
    if total > cap:
        raise ValueError(f"DM's answer to the catalog call is malformed: {total} rows for a cap of {cap}")
    parsed: list[CatalogRead] = []
    for read in found:
        rows: list[CatalogRow] = []
        failed: list[DMRecordError] = []
        for value in read["files"]:
            try:
                rows.append(parse_catalog_row(value))
            except DMRecordError as error:
                failed.append(error)
        parsed.append(CatalogRead(tuple(rows), tuple(failed), len(read["files"]), read["more"]))
    return tuple(parsed)


def catalog_query(daq_id: str, folder: str, since: float | None) -> dict[str, Any]:
    """DM's catalog query for the DAQ's files directly in ``folder`` (relative to the experiment's storage directory;
    ``.`` the storage itself), catalogued at ``since`` or later (None: all of them)."""
    prefix = "" if folder in ("", ".") else re.escape(folder) + "/"
    query: dict[str, Any] = {"daqId": daq_id, "experimentFilePath": {"$regex": "^" + prefix + r"[^/]+$"}}
    if since is not None:
        query["fileProcessingTime"] = {"$gte": since}
    return query


ABORT_WAIT_SECONDS = 1.0
"""How long ``DMClient.abort`` waits, at most, for the processes it killed to be gone."""

SELF_BOUND_MARGIN_SECONDS = 5
"""An own-session call with a timeout runs under ``timeout --kill-after=<this> <its timeout + this>`` (``SELF_BOUND``):
the caller's own timeout comes first while the caller lives (its ``TimeoutExpired`` and messages unchanged); after a
caller killed outright, the call's process group gets SIGTERM at its timeout plus this, and SIGKILL this later (all of
it or its first process alone: ``SELF_BOUND``'s note). A call without a timeout has no wrapper."""

SELF_BOUND = "timeout"
"""coreutils ``timeout`` (GNU or uutils; a system tool, not a package): it runs the command in its own process group and
sends that group SIGTERM at the limit even after its parent died. Its ``--kill-after`` SIGKILL differs: GNU coreutils'
reaches the whole group, so every process of the call is gone by then; uutils' (0.10, some distributions' default)
reaches the call's first process alone, so there a process of the call that survives SIGTERM (a handler, an ignored
TERM) outlives the limit."""


class CallAborted(RuntimeError):
    """A DM call or launcher command ``DMClient.abort`` ended (the worker's end; the catalog thread's close), or one
    asked for after it."""


class DaqNotFound(DMRecordError):
    """DM has no DAQ of that ID (its ``ObjectNotFound``; ``DMClient.daq``)."""


FAILED_WAIT_SECONDS = 60.0
"""Each failed DM call doubles its caller's wait, up to this: the live callers' (``jobs.catalog``,
``jobs.daq.DaqList``). The remote poller keeps its own ``remote.poller.MAX_WAIT_SECONDS``, of the same value."""


def describe_failure(error: BaseException) -> str:
    """A failed DM call in one line: no answer in time, or its exit status and the last line it wrote to stderr;
    never its command (the API script). The remote poller's and the live callers'."""
    if isinstance(error, subprocess.TimeoutExpired):
        return f"no answer within {error.timeout:g} s"
    if isinstance(error, subprocess.CalledProcessError):
        stderr = error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else error.stderr or ""
        lines = [line.strip() for line in stderr.splitlines() if line.strip()]
        return f"exit status {error.returncode}" + (f": {lines[-1][:200]}" if lines else "")
    return f"{type(error).__name__}: {str(error)[:200]}"


class DMClient:
    """DM's API through its installed environment (the module's calls). Its processes (``run``: a DM call, the
    launcher's command) start one at a time, one caller's (an API request's submission, the api's DAQ list or bind
    check, the remote poller thread, a live run's catalog thread: the module's docstring).

    With ``own_session`` (the worker's remote poller, a job process's catalog thread: the service stop signals the
    worker and its job processes by pid alone) each runs in a session of its own, killed whole with everything it
    started (the shell, Python, an SSH bridge) at its timeout and at ``abort`` (the worker's end, the catalog
    thread's close); with a timeout, it also runs under coreutils ``timeout`` (``SELF_BOUND``), which signals the
    session ``SELF_BOUND_MARGIN_SECONDS`` after the call's timeout even when its caller is gone (killed outright: no
    ``abort``, and supervisor's or the dispatcher's group kill does not reach another session); its SIGKILL step
    reaches the whole call with GNU's ``timeout`` only (``SELF_BOUND``'s note). Without a timeout
    (``dm.timeout_seconds`` null) there is no wrapper, and a killed worker's call runs on unbounded.
    Without it (the API, the command line) each runs in the caller's
    process group, which a stop of that group reaches (the API program's ``stopasgroup``, a terminal's Ctrl-C), and
    is killed alone at its timeout, as ``subprocess.run`` kills it."""

    def __init__(self, settings: DMSettings, *, own_session: bool = False):
        self.settings = settings
        self.own_session = own_session
        self._guard = threading.Lock()
        self._running: subprocess.Popen[str] | None = None
        self._aborted = False
        self._held = False
        """``abort(later=True)``: no call starts until ``resume``."""
        self._ended: subprocess.Popen[str] | None = None
        """The call ``abort(later=True)`` ended (``CallAborted``, not a failed call)."""

    def run(
        self, argv: list[str], *, input: str | None = None, timeout: float | None
    ) -> subprocess.CompletedProcess[str]:
        """``argv`` as ``subprocess.run(argv, input=input, capture_output=True, text=True, check=True,
        timeout=timeout)`` runs it (``CalledProcessError``, ``TimeoutExpired``; killed at its timeout or an
        interrupt), kept while it runs, so that ``abort`` can end it (then ``CallAborted``). An own-session call with
        a timeout runs under ``SELF_BOUND`` (the class's note); its errors name ``argv``."""
        command = argv
        if self.own_session and timeout is not None:
            margin = SELF_BOUND_MARGIN_SECONDS
            command = [SELF_BOUND, f"--kill-after={margin}", f"{timeout + margin:g}", *argv]
        with self._guard:
            if self._aborted or self._held:
                raise CallAborted("the client was aborted: no DM call or launcher command starts any more")
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL if input is None else subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=self.own_session,
            )
            self._running = process
        try:
            with process:  # closes the pipes and reaps it
                try:
                    stdout, stderr = process.communicate(input, timeout=timeout)
                except BaseException as error:  # its timeout or an interrupt: the call ends, as subprocess.run's
                    self._kill(process, signal.SIGKILL)
                    if isinstance(error, subprocess.TimeoutExpired):
                        error.cmd = argv  # the call, not its ``timeout`` wrapper
                    raise
        finally:
            with self._guard:
                self._running = None
        if process.returncode:
            if self._aborted or self._ended is process:
                raise CallAborted("the client was aborted: its call was ended")
            raise subprocess.CalledProcessError(process.returncode, argv, stdout, stderr)
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)

    def abort(self, *, later: bool = False) -> None:
        """End the process running now with everything it started (``own_session``; ``CallAborted``), and refuse any
        after it (the worker's end, ``remote.poller.RemotePoller.close``, before the poller lock goes; the catalog
        thread's close, ``jobs.catalog.CatalogThread.close``, at its run's end); returns once they are gone, or after
        ``ABORT_WAIT_SECONDS``. ``later``: no call starts until ``resume`` (the catalog thread at a Stop ends its call
        in flight and refuses its thread's next, then makes its last read itself)."""
        with self._guard:
            if later:
                self._held = True
            else:
                self._aborted = True
            process = self._running
            if process is None or process.returncode is not None:
                return
            self._ended = process
            self._kill(process, signal.SIGKILL)
        deadline = time.monotonic() + ABORT_WAIT_SECONDS
        while time.monotonic() < deadline and self._kill(process, 0):
            time.sleep(0.01)

    def resume(self) -> None:
        """Let calls start again after ``abort(later=True)`` (never after ``abort()``)."""
        with self._guard:
            self._held = False

    def _kill(self, process: subprocess.Popen[str], signum: int) -> bool:
        """Send ``signum`` to the call's session (``own_session``: everything it started), else to its process
        alone, as ``subprocess.run`` does; whether any of them was there (signal 0 only asks)."""
        try:
            if self.own_session:
                os.killpg(process.pid, signum)  # the session outlives its first process while another lives
            elif process.poll() is None:
                os.kill(process.pid, signum)
            else:
                return False
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def _required(self, key: str) -> str:
        """A ``dm`` key this call reads (``remote.settings.portal_settings`` checks the DM API's three)."""
        value = getattr(self.settings, key)
        if not value:
            raise RemoteSettingsError(f"dm.{key} is required to call DM")
        return value

    def _call(self, operation: str, **args: Any) -> Any:
        setup_file = self._required("setup_file")
        command = f"source {shlex.quote(setup_file)} >/dev/null; exec python -c {shlex.quote(_API_SCRIPT)}"
        # A shell that is no login shell: the setup file supplies DM's environment, and a login
        # profile's banner or modules could corrupt the JSON on stdout.
        argv = shell_command(command, self.settings.ssh_host)
        request = json.dumps({"operation": operation, "args": args})
        result = self.run(argv, input=request, timeout=self.settings.timeout_seconds)
        return json.loads(result.stdout)

    def submit(self, plan: dict[str, Any]) -> Any:
        return self._call(
            "submit",
            owner=self._required("owner"),
            workflow=self._required("workflow"),
            arguments={
                "submissionId": plan["submission_id"],
                "experimentName": "LauePortal",
                "source": "laue-portal",
                "analysisMachine": plan["backend"],
                # DM requires an input file specification for every job. Use
                # one frozen shared control file, so it runs the workflow once.
                "filePath": str(Path(plan["run_directory"]) / "request.json"),
            },
        )

    def get(self, external_id: str) -> Any:
        return self._call("get", owner=self._required("owner"), id=external_id)

    def restore_permissions(self, experiment: str, relative_path: str) -> Any:
        """DM's ``FileDsApi.restorePermissions`` on ``relative_path`` (POSIX, relative to the experiment's folder) of
        ``experiment`` (its folder's name), in one call: the launcher's ``permissions`` stage
        (``remote.stage.restore_permissions``)."""
        return self._call("restore_permissions", experiment=experiment, relative_path=relative_path)

    def fetch(self, ids: Iterable[str], submission_ids: Iterable[str]) -> Records:
        """The records of the DM jobs ``ids``, and the jobs of ``submission_ids`` by their ``submissionId`` (after
        a lost reply, with the record of a single job found), in one call: the remote poller's cycle. The
        call raises as every call does (``subprocess.CalledProcessError``, ``subprocess.TimeoutExpired``, an
        ``OSError``, ``CallAborted`` at the worker's end; ``ValueError`` for an answer without the operation's
        shape); a record DM could not give, or one that is not an object, is reported per record (``Records``)."""
        answer = self._call(
            "fetch",
            owner=self._required("owner"),
            ids=list(dict.fromkeys(ids)),
            submission_ids=list(dict.fromkeys(submission_ids)),
        )
        return parse_records(answer)

    def daqs(self) -> list[DaqRecord | DMRecordError]:
        """The DAQs DM knows (a live run's source; ``jobs.daq.DaqList``), in one call; a record that cannot be read is
        reported per record (``parse_daqs``). The call raises as every call does (``ValueError`` for an answer that is
        not a list)."""
        return parse_daqs(self._call("daqs"))

    def daq(self, daq_id: str) -> DaqRecord:
        """DAQ ``daq_id``'s record (the bind check, ``jobs.daq.bind_daq``), in one call; ``DaqNotFound`` when DM has
        no such DAQ, ``DMRecordError`` when its answer is no DAQ record."""
        answer = self._call("daq", id=daq_id)
        if answer is None:
            raise DaqNotFound(f"DM has no DAQ {daq_id}")
        return parse_daq(answer)

    def catalog(
        self,
        experiment: str,
        reads: Sequence[tuple[Mapping[str, Any], int]],
        *,
        limit: int,
        cap: int,
        seconds: float,
    ) -> tuple[CatalogRead, ...]:
        """The catalog thread's cycle (``jobs.catalog``), in one call: for each of ``reads`` (a query,
        ``catalog_query``, and the row to start from), in order, the pages of ``experiment``'s file catalog
        (``FileCatApi``) it matches, one request at a time of at most ``limit`` rows, until a shorter page ends it;
        at most ``cap`` rows in all, and, after the first request, none that the longest request so far says would
        end past ``seconds`` from the script's own start (the script's ``catalog``).
        A row that cannot be read is reported per row (``CatalogRead.failed``); the call raises as every call does
        (``ValueError`` for an answer without the operation's shape)."""
        answer = self._call(
            "catalog",
            experiment=experiment,
            reads=[{"query": dict(query), "skip": skip} for query, skip in reads],
            limit=limit,
            cap=cap,
            seconds=seconds,
        )
        return parse_catalog(answer, len(reads), cap)

    def cancel(self, submission_id: str) -> Any:
        """Request cancellation on the DM VM without a portal-to-workflow-host SSH key."""
        return self._call(
            "submit",
            owner=self._required("owner"),
            workflow=self._required("cancel_workflow"),
            arguments={
                "cancelSubmissionId": str(UUID(submission_id)),
                "source": "laue-portal",
                "filePath": self._required("launcher_config"),
            },
        )

    def workflow(self) -> Any:
        """Read the configured workflow registrations in one call, without starting a DM job."""
        workflows = [self._required("workflow")]
        workflows.extend(name for name in (self.settings.cancel_workflow, self.settings.cleanup_workflow) if name)
        return self._call("workflow", owner=self._required("owner"), workflows=workflows)

    def clean_up(self, submission_id: str) -> Any:
        """Run the launcher's ``cleanup`` stage on the DM VM (``dm.cleanup_workflow``), as ``cancel`` does."""
        return self._call(
            "submit",
            owner=self._required("owner"),
            workflow=self._required("cleanup_workflow"),
            arguments={
                "cleanupSubmissionId": str(UUID(submission_id)),
                "source": "laue-portal",
                "filePath": self._required("launcher_config"),
            },
        )


def launcher_command(settings: DMSettings, action: str, submission_id: str) -> list[str]:
    """The launcher's ``cancel`` stage, run directly (no ``dm.cancel_workflow``). Only UUIDs and fixed administrator
    settings enter the remote shell.

    ``dm.launcher_code`` is the launcher's checkout (``deploy/remote/dm-stage.sh``: the package is under
    ``backend/src``, which the launcher's environment does not install). The command runs what its ``dm-stage.sh``
    runs: ``laueportal.remote.stage`` in the checkout, with ``PYTHONPATH=<checkout>/backend/src`` and
    ``PYTHONNOUSERSITE=1``."""
    if action not in {"cancel"}:
        raise ValueError(action)
    for key in ("launcher_python", "launcher_config", "launcher_code"):
        if not getattr(settings, key):
            raise ValueError(f"dm.{key} is required to run the launcher directly")
    code = str(settings.launcher_code)
    arguments = ["--config", str(settings.launcher_config), action, str(UUID(submission_id))]
    python = str(settings.launcher_python)
    stage = shlex.join([python, "-m", STAGE_MODULE, *arguments])
    packages = shlex.quote(str(PurePosixPath(code) / LAUNCHER_PACKAGES))
    shell = f"cd {shlex.quote(code)} && export PYTHONNOUSERSITE=1 PYTHONPATH={packages} && exec {stage}"
    # The API bridge may be on another host, while cancellation must reach the launcher
    # with Globus access. Avoid the VM account's interactive login profile. A
    # launcher_ssh_host key, even null, wins; only a missing one falls back to the bridge.
    host = settings.launcher_ssh_host if "launcher_ssh_host" in settings.model_fields_set else settings.ssh_host
    return shell_command(shell, host)
