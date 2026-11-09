"""Run-level details for the job page: counters, configuration, versions, artifacts, diagnostics.

Everything here reads the run's constant set of artifacts (``request.json``,
``run.json``, ``failures.jsonl``, a remote run's plan ``bdp.json``, whose path map
gives the failure report's sources in the portal's view, and its PBS output ``pbs-output.log``)
and the migration export of a historical run.
Nothing enumerates inputs: the failure report is read one bounded page at a time
and the logs are truncated to their tails (a log file is read from its end).

**Paths.** They are the job's own records: its run directory, request and failure report paths (written by the
portal, or by v1's migration for a historical run, whose failure report and ``history.json`` live in the
migration export), and the run files' constant names inside the run directory. Values a run wrote inside its
files (artifacts, provenance, failure sources) are shown, never opened here; a remote run's failure sources are
mapped to the portal's view with the run's own plan (``failure_records``), and
the PBS output is read only inside the run directory (``remote.paths.child_path``).

**Every read reports its outcome** (``FileRead``, a ``FileState``) and never raises: production's paths need not
exist on the host serving the page (``/net/...`` on a development machine). A file is opened only when it is a
regular file, without following a symbolic link (``_open_regular``, and ``remote.paths.run_path_map`` for the
plan: a FIFO would block the reader, and a link could point anywhere the portal can read). What is read is
bounded: a JSON run file at most ``MAX_JSON_BYTES``, the failure report at most ``MAX_FAILURE_REPORT_BYTES`` and
each of its records ``MAX_FAILURE_RECORD_BYTES``, a log file's tail ``MAX_TAIL_BYTES``. A JSON run file the
response could not serve (a lone surrogate escape, nesting deeper than ``MAX_JSON_DEPTH``) is unreadable; a failure
record's lone surrogates are served as U+FFFD, and a log file's undecodable bytes are replaced the same way.

The results are raw values (typed dataclasses); labels, placeholders for missing values and
date formatting are the frontend's job.
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy.orm import Session

from laueportal.files.probe import OPEN_CHECK_BYTES, NotResponding, bounded
from laueportal.files.roots import recorded_real_path
from laueportal.jobs.models import Job
from laueportal.jobs.progress import RunProgress
from laueportal.jobs.rundir import (
    FAILURE_REPORT_FILENAME,
    INCOMING_DIRECTORY,
    PBS_OUTPUT_FILENAME,
    REQUEST_FILENAME,
    RUN_SUMMARY_FILENAME,
    FailureRecord,
    ManifestError,
    read_failure_records,
)
from laueportal.jobs.schemas import FAILURE_PAGE_SIZE, FileState
from laueportal.jobs.state import JobStatus
from laueportal.remote.paths import child_path, portal_path, run_path_map

MAX_LOG_LINES = 50
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_JSON_DEPTH = 100
"""The deepest nesting served from a run file's JSON (objects and lists; the document is level 1): the response
nests a run file's values a few levels deeper still, and its serializer refuses more than 255 levels."""
_SURROGATE_ESCAPE = re.compile(rb"\\u[dD][89a-fA-F]")
"""A JSON escape of a UTF-16 surrogate: the only way a lone surrogate gets into a parsed document."""
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")
"""A surrogate in parsed text: the parser joins an escaped pair into one character, so any left is alone, and the
response (UTF-8) cannot encode it."""
LONE_SURROGATE = "text that is not valid Unicode (a lone surrogate)"
HISTORY_FILENAME = "history.json"
RUN_DIRECTORY = "run directory"
"""The ``FileRead.name`` of the run directory itself."""
MAX_FAILURE_REPORT_BYTES = 256 * 1024 * 1024
"""The largest failure report read (a page beyond its end reads it whole)."""
MAX_FAILURE_RECORD_BYTES = 64 * 1024
"""The longest failure record (one line) read: a longer one on a page makes the page unreadable."""
TAIL_BLOCK_BYTES = 64 * 1024
MAX_TAIL_BYTES = 1024 * 1024
"""The most a log tail reads from the end of a file, however many lines are asked for: a log of very long lines
gives the lines that fit (``tail_lines``)."""


@dataclass(frozen=True)
class FileRead:
    """What reading one of the run's files (or its run directory) gave."""

    name: str
    path: str | None
    """Where it was read or looked for; None when the job records no path for it."""
    state: FileState
    detail: str | None = None
    """Why it is unreadable; None otherwise."""


class _Unreadable(Exception):
    """Something is at the path, but it is not read: the text says what it is."""


def _open_regular(path: str) -> int:
    """A read-only descriptor of the regular file at ``path``. A final symbolic link is not followed and the open
    does not block (a FIFO would): either is ``_Unreadable``, and so is anything but a regular file. ``OSError``
    (``FileNotFoundError`` when nothing is there) and ``ValueError`` (a NUL in the path) as ``os.open`` raises."""

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        if error.errno == errno.ELOOP:  # O_NOFOLLOW refused a symbolic link (or a link loop)
            raise _Unreadable("a symbolic link") from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _Unreadable("not a regular file")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _check(path: str) -> None:
    """Open ``path`` (``_open_regular``) and read its first bytes through ``files.probe`` before the caller reads it
    in its own thread (on NFS an open asks the server, where a stat can answer from the attribute cache):
    ``NotResponding`` when its file system does not answer; ``_Unreadable`` or an ``OSError`` as the read would
    raise it."""

    def look() -> None:
        descriptor = _open_regular(path)
        try:
            os.read(descriptor, OPEN_CHECK_BYTES)
        finally:
            os.close(descriptor)

    bounded(path, look)


def _missing(error: OSError) -> bool:
    """Whether an error means nothing is at the path (a component that is not a directory included)."""

    return isinstance(error, (FileNotFoundError, NotADirectoryError))


@dataclass(frozen=True)
class RunDetails:
    """What the job page shows besides the job row itself."""

    progress: RunProgress
    kind: str | None
    display_id: str | None
    run_directory: str | None
    request: dict[str, Any] | None  # request.json: the frozen submission
    summary: dict[str, Any] | None  # run.json: effective execution, versions, artifacts
    history: dict[str, Any] | None  # migration export of a historical per-input run
    results_path: str | None
    problems: tuple[str, ...] = field(default_factory=tuple)
    directory: FileRead = FileRead(RUN_DIRECTORY, None, FileState.NOT_RECORDED)
    """The run directory: whether it is on this host."""
    files: tuple[FileRead, ...] = field(default_factory=tuple)
    """The files read, in order: ``request.json``, ``run.json``, and ``history.json`` when it was looked for."""

    @property
    def artifacts(self) -> dict[str, str]:
        """``run.json``'s artifacts (name → path; an entry whose path is not text is left out), plus an indexation's
        recorded results when ``run.json`` names none."""

        artifacts = {
            str(name): path
            for name, path in _mapping((self.summary or {}).get("artifacts")).items()
            if isinstance(path, str) and path
        }
        if self.results_path and "results" not in artifacts:
            artifacts["results"] = self.results_path
        return artifacts

    @property
    def is_historical(self) -> bool:
        return self.summary is None and self.request is None and self.history is not None

    @property
    def log_tail(self) -> list[str]:
        return (_log_entries(self.summary) or [])[-MAX_LOG_LINES:]

    @property
    def log_truncated(self) -> int:
        return max(len(_log_entries(self.summary) or []) - MAX_LOG_LINES, 0)


def _unservable(document: dict[str, Any], *, escapes: bool) -> str | None:
    """Why a parsed run file cannot be served, or None: a text or key holding a lone surrogate (looked for only when
    the file holds a surrogate escape, ``escapes``), or nesting deeper than ``MAX_JSON_DEPTH``. ``json.loads``
    accepts both; the response's serializer would fail on them."""

    pending: list[tuple[dict[str, Any] | list[Any], int]] = [(document, 1)]
    while pending:
        container, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            return f"nesting deeper than {MAX_JSON_DEPTH} levels"
        if isinstance(container, dict):
            if escapes and any(_LONE_SURROGATE.search(key) for key in container):
                return LONE_SURROGATE
            values: Any = container.values()
        else:
            values = container
        for value in values:
            if isinstance(value, (dict, list)):
                pending.append((value, depth + 1))
            elif escapes and isinstance(value, str) and _LONE_SURROGATE.search(value):
                return LONE_SURROGATE
    return None


def _read_json(path: str | None, problems: list[str], label: str) -> tuple[dict[str, Any] | None, FileRead]:
    """The JSON object at ``path`` and how reading it went. A file that is there but not read adds a problem; a
    missing one does not. A document the response could not serve (``_unservable``) is not read."""

    if not path:
        return None, FileRead(label, None, FileState.NOT_RECORDED)

    def unreadable(problem: str, detail: str) -> tuple[None, FileRead]:
        problems.append(problem)
        return None, FileRead(label, path, FileState.UNREADABLE, detail)

    try:
        _check(path)
        descriptor = _open_regular(path)
        with open(descriptor, "rb") as handle:
            if os.fstat(handle.fileno()).st_size > MAX_JSON_BYTES:
                data = None
            else:
                data = handle.read(MAX_JSON_BYTES + 1)
                data = None if len(data) > MAX_JSON_BYTES else data
        if data is None:
            limit = f"larger than {MAX_JSON_BYTES // (1024 * 1024)} MB"
            return unreadable(f"{label} at {path} is {limit} and was not read", limit)
        document = json.loads(data.decode("utf-8"))
    except NotResponding:
        return None, FileRead(label, path, FileState.NOT_RESPONDING)
    except _Unreadable as reason:
        return unreadable(f"{label} at {path} is {reason} and was not read", str(reason))
    except OSError as error:
        if _missing(error):
            return None, FileRead(label, path, FileState.MISSING)
        return unreadable(f"{label} at {path} could not be read: {error}", str(error))
    except (ValueError, RecursionError) as error:  # not UTF-8, not JSON, nested too deeply, a NUL in the path
        return unreadable(f"{label} at {path} could not be read: {error}", str(error))
    if not isinstance(document, dict):
        return unreadable(f"{label} at {path} is not a JSON object", "not a JSON object")
    reason = _unservable(document, escapes=_SURROGATE_ESCAPE.search(data) is not None)
    if reason is not None:
        return unreadable(f"{label} at {path} was not read: it holds {reason}", reason)
    return document, FileRead(label, path, FileState.AVAILABLE)


def read_json_file(path: str | None, label: str) -> tuple[dict[str, Any] | None, FileRead]:
    """A run file's JSON object and how reading it went (``_read_json``: through ``files.probe``, bounded, its state
    a ``FileRead``), without the job page's problem lines (the Live page's ``live.json`` and
    ``arrival-status.json``, ``jobs.live_status``)."""
    return _read_json(path, [], label)


ARRIVAL_TAIL_BYTES = 64 * 1024
"""The end of ``arrivals.jsonl`` ``count_arrivals`` reads for its last whole line (a line is about 250 bytes; its path
at most 4,096)."""
MAX_COUNTED_BYTES = 1024 * 1024
"""The largest ``arrivals.jsonl`` ``count_arrivals`` counts line by line, when its last whole line holds no arrival
number (a file the contract does not describe); a larger one is ``unreadable``."""
MIN_ARRIVAL_LINE_BYTES = len(b'{"n":0}\n')


def _last_whole_line(descriptor: int, size: int) -> bytes | None:
    """The last line of the open file that ends in a newline, without it, read from its last ``ARRIVAL_TAIL_BYTES``:
    ``b""`` when the file has no whole line, None when that line does not fit in them."""
    start = max(0, size - ARRIVAL_TAIL_BYTES)
    data = os.pread(descriptor, size - start, start)
    end = data.rfind(b"\n")
    if end < 0:
        return b"" if start == 0 else None
    previous = data.rfind(b"\n", 0, end)
    if previous < 0 and start > 0:
        return None
    return data[previous + 1 : end]


def _arrival_count(line: bytes, size: int) -> int | None:
    """The arrivals a ledger whose last whole line is ``line`` holds: its ``n`` + 1 (``n`` is 0, 1, 2, … in arrival
    order); None for a line without such a number, or one ``size`` bytes cannot hold."""
    try:
        values = json.loads(line)
    except (ValueError, RecursionError):
        return None
    number = values.get("n") if isinstance(values, dict) else None
    if isinstance(number, bool) or not isinstance(number, int) or number < 0:
        return None
    return number + 1 if (number + 1) * MIN_ARRIVAL_LINE_BYTES <= size else None


def count_arrivals(path: str | None, label: str) -> tuple[int | None, FileRead]:
    """The arrivals in a live run's ``arrivals.jsonl`` at ``path`` (its whole lines: a last line without its newline is
    still being written) and how reading it went: checked through ``files.probe``, then its last whole line's ``n`` + 1
    from a bounded tail (``ARRIVAL_TAIL_BYTES``), whatever the ledger's length. A last whole line without an arrival
    number: the lines counted, in a file of at most ``MAX_COUNTED_BYTES`` (a larger one is ``unreadable``). None unless
    it was read."""

    if not path:
        return None, FileRead(label, None, FileState.NOT_RECORDED)
    try:
        _check(path)
        descriptor = _open_regular(path)
        with open(descriptor, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            line = _last_whole_line(handle.fileno(), size) if size else b""
            count = 0 if line == b"" else None if line is None else _arrival_count(line, size)
            if count is None:
                if size > MAX_COUNTED_BYTES:
                    reason = "its last line holds no arrival number"
                    return None, FileRead(label, path, FileState.UNREADABLE, reason)
                count = handle.read(MAX_COUNTED_BYTES).count(b"\n")
    except NotResponding:
        return None, FileRead(label, path, FileState.NOT_RESPONDING)
    except _Unreadable as reason:
        return None, FileRead(label, path, FileState.UNREADABLE, str(reason))
    except OSError as error:
        if _missing(error):
            return None, FileRead(label, path, FileState.MISSING)
        return None, FileRead(label, path, FileState.UNREADABLE, str(error))
    except ValueError as error:  # a NUL in the path
        return None, FileRead(label, path, FileState.UNREADABLE, str(error))
    return count, FileRead(label, path, FileState.AVAILABLE)


def _directory(path: str | None) -> FileRead:
    """Whether the run directory is on this host (looked at through ``files.probe``)."""

    if not path:
        return FileRead(RUN_DIRECTORY, None, FileState.NOT_RECORDED)
    try:
        mode = bounded(path, lambda: os.stat(path)).st_mode
    except NotResponding:
        return FileRead(RUN_DIRECTORY, path, FileState.NOT_RESPONDING)
    except OSError as error:
        if _missing(error):
            return FileRead(RUN_DIRECTORY, path, FileState.MISSING)
        return FileRead(RUN_DIRECTORY, path, FileState.UNREADABLE, str(error))
    except ValueError as error:  # a NUL in the path
        return FileRead(RUN_DIRECTORY, path, FileState.UNREADABLE, str(error))
    if not stat.S_ISDIR(mode):
        return FileRead(RUN_DIRECTORY, path, FileState.UNREADABLE, "not a directory")
    return FileRead(RUN_DIRECTORY, path, FileState.AVAILABLE)


def _summary_path(run_directory: str | None) -> str | None:
    return os.path.join(run_directory, RUN_SUMMARY_FILENAME) if run_directory else None


def read_run_summary(run_directory: str | None) -> tuple[dict[str, Any] | None, FileRead]:
    """The run's ``run.json`` (written when the run ends) and how reading it went."""

    return _read_json(_summary_path(run_directory), [], RUN_SUMMARY_FILENAME)


def _within(path: str, directory: str) -> bool:
    """Whether ``path`` is ``directory`` or lies under it, by their (normalized) text."""

    path, directory = os.path.normpath(path), os.path.normpath(directory)
    try:
        return os.path.commonpath([path, directory]) == directory
    except ValueError:  # one absolute, the other relative
        return False


def _history_path(progress: RunProgress, run_directory: str | None) -> str | None:
    """Where a run migrated from v1's per-input records keeps its ``history.json``: next to its failure report, in
    the migration export. None for a run without a failure report, or whose report lies in its run directory (a run
    that was never migrated: the executor and the poller publish the report there)."""

    report = progress.failure_report_path
    if not report:
        return None
    export = os.path.dirname(report)
    if run_directory and _within(export, run_directory):
        return None
    return os.path.join(export, HISTORY_FILENAME)


@dataclass(frozen=True)
class RunRecord:
    """What the job page reads of a job's rows (``run_record``), so that its files are read after the session
    closed (``read_run_details``)."""

    progress: RunProgress
    request_path: str | None
    run_directory: str | None
    results_path: str | None
    """An indexation's recorded results."""
    kind: str | None
    """The run's kind by its row, when its files name none."""
    display_id: str | None


def run_record(job: Job) -> RunRecord:
    """``job``'s record (its run's rows are loaded: call it while its session is open)."""

    kind = display_id = results_path = None
    if job.indexing_run is not None:
        results_path = job.indexing_run.results_path
        kind, display_id = f"{job.indexing_run.method}_indexing", f"Indexing I{job.indexing_run.id}"
    elif job.reconstruction_run is not None:
        kind = f"{job.reconstruction_run.method}_reconstruction"
        display_id = f"Reconstruction R{job.reconstruction_run.id}"
    return RunRecord(RunProgress.from_job(job), job.request_path, job.run_directory, results_path, kind, display_id)


def read_run_details(record: RunRecord) -> RunDetails:
    """The run's files as the job page shows them (no database: a hung mount holds no connection)."""

    problems: list[str] = []
    request, request_file = _read_json(record.request_path, problems, REQUEST_FILENAME)
    summary, summary_file = _read_json(_summary_path(record.run_directory), problems, RUN_SUMMARY_FILENAME)
    files = [request_file, summary_file]
    history = None
    history_path = None if summary is not None else _history_path(record.progress, record.run_directory)
    if history_path is not None:
        history, history_file = _read_json(history_path, problems, HISTORY_FILENAME)
        files.append(history_file)

    kind = _text((summary or {}).get("kind")) or _text((request or {}).get("kind")) or record.kind
    display_id = _text((summary or {}).get("display_id")) or record.display_id
    return RunDetails(
        progress=record.progress,
        kind=kind,
        display_id=display_id,
        run_directory=record.run_directory,
        request=request,
        summary=summary,
        history=history,
        results_path=record.results_path,
        problems=tuple(problems),
        directory=_directory(record.run_directory),
        files=tuple(files),
    )


def load_run_details(session: Session, job_id: int) -> RunDetails | None:
    """``read_run_details`` of the job ``job_id``; None for an unknown job."""

    job = session.get(Job, job_id)
    if job is None:
        return None
    return read_run_details(run_record(job))


NOT_CONFIGURATION_KEYS = frozenset({"submitted_at", "computer_name", "priority"})
"""``request.json`` request keys the job row already shows (submit time, computer) or that are not shown (priority)."""
MAX_MESSAGE_CLASSES = 10


@dataclass(frozen=True)
class ManifestInfo:
    """The ``manifest`` entry of ``request.json``: the frozen input list."""

    path: str | None
    n_inputs: int | None
    sha256: str | None


@dataclass(frozen=True)
class SubmittedRequest:
    """The submitted configuration (``request.json``), raw values in submission order."""

    parameters: dict[str, Any]
    """The ``request`` section without ``NOT_CONFIGURATION_KEYS``."""
    output: dict[str, Any]
    manifest: ManifestInfo | None


@dataclass(frozen=True)
class ExecutionInfo:
    """The effective execution (``run.json``): versions, policy, host, execution window, counters."""

    provenance: dict[str, Any]
    policy: dict[str, Any]
    host: str | None
    started_at: str | None
    """As recorded in ``run.json`` (ISO 8601 text)."""
    finished_at: str | None
    n_indexed: int | None
    """Frames indexed (LaueGo runs)."""


@dataclass(frozen=True)
class MessageClass:
    message: str
    count: int
    """Inputs that recorded this message."""


@dataclass(frozen=True)
class HistoryInfo:
    """A run migrated from v1's per-input records (``history.json``)."""

    original_status_name: str | None
    first_input_at: str | None
    """Start of the per-input work (``subjob_time_range[0]``, as recorded)."""
    last_input_at: str | None
    message_classes: tuple[MessageClass, ...]
    """The most frequent messages (at most ``MAX_MESSAGE_CLASSES``), most frequent first."""


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)


def _texts(value: Any) -> list[str]:
    """A list of text (``run.json``'s ``warnings``): each item as text, a lone text as itself, nothing otherwise."""

    if isinstance(value, list):
        return [str(item) for item in value if item is not None and item != ""]
    return [value] if isinstance(value, str) and value else []


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _log_entries(summary: dict[str, Any] | None) -> list[str] | None:
    """``run.json``'s ``log`` as text, one entry per line; None without ``run.json`` or for a log that is not a
    list."""

    if summary is None:
        return None
    log = summary.get("log")
    if log is None:
        return []
    return [str(entry) for entry in log] if isinstance(log, list) else None


def submitted_request(details: RunDetails) -> SubmittedRequest | None:
    """The submitted configuration, or None when the run has no ``request.json``."""

    if details.request is None:
        return None
    document = details.request
    parameters = {
        key: value for key, value in _mapping(document.get("request")).items() if key not in NOT_CONFIGURATION_KEYS
    }
    manifest = _mapping(document.get("manifest"))
    return SubmittedRequest(
        parameters=parameters,
        output=_mapping(document.get("output")),
        manifest=(
            ManifestInfo(
                path=_text(manifest.get("path")),
                n_inputs=_integer(manifest.get("n_inputs")),
                sha256=_text(manifest.get("sha256")),
            )
            if manifest
            else None
        ),
    )


def execution_info(details: RunDetails) -> ExecutionInfo | None:
    """The effective execution, or None when the run has no ``run.json``."""

    if details.summary is None:
        return None
    summary = details.summary
    return ExecutionInfo(
        provenance=_mapping(summary.get("provenance")),
        policy=_mapping(summary.get("policy")),
        host=_text(summary.get("host")),
        started_at=_text(summary.get("started_at")),
        finished_at=_text(summary.get("finished_at")),
        n_indexed=_integer(_mapping(summary.get("counters")).get("n_indexed")),
    )


def history_info(details: RunDetails) -> HistoryInfo | None:
    """What a run migrated from per-input records kept, or None for a run without ``history.json``."""

    if details.history is None:
        return None
    history = details.history
    time_range = history.get("subjob_time_range")
    first, last = (
        (time_range[0], time_range[1]) if isinstance(time_range, list) and len(time_range) == 2 else (None, None)
    )
    classes = []
    for message, count in _mapping(history.get("message_classes")).items():
        number = _integer(count)
        if number is not None:
            classes.append(MessageClass(message=str(message), count=number))
    classes.sort(key=lambda item: (-item.count, item.message))
    return HistoryInfo(
        original_status_name=_text(history.get("original_status_name")),
        first_input_at=_text(first),
        last_input_at=_text(last),
        message_classes=tuple(classes[:MAX_MESSAGE_CLASSES]),
    )


def validation_error(details: RunDetails) -> str | None:
    """Why the run's output failed validation (``run.json``'s ``validation_error``), or None."""

    return _text((details.summary or {}).get("validation_error"))


def run_warnings(details: RunDetails) -> list[str]:
    """``run.json``'s warnings."""

    return _texts((details.summary or {}).get("warnings"))


def has_partial_results(details: RunDetails) -> bool:
    """Whether a run that ended without finishing (Failed or Cancelled) published results holding its completed
    inputs: an indexation's ``results`` or a reconstruction's ``reconstruction``."""

    progress = details.progress
    if not progress.is_terminal or progress.status == JobStatus.FINISHED:
        return False
    artifacts = details.artifacts
    return bool(artifacts.get("results") or artifacts.get("reconstruction"))


# ---------------------------------------------------------------------------
# The failure report


def _servable(text: str | None) -> str | None:
    """``text`` with each lone surrogate (a JSON escape, or a file name's undecodable byte as Python escapes it)
    replaced by U+FFFD, which the response can encode."""

    return None if text is None else _LONE_SURROGATE.sub("\ufffd", text)


def _portal_record(record: FailureRecord, path_map: dict[str, str] | None) -> FailureRecord:
    """``record`` as the portal shows it: its source in the portal's view (as written without a plan, mapped with
    one; None for a source that is not text or that the rule refuses), its other optional values as text (a record
    is JSON: any value may be of another type), and every text servable (``_servable``)."""

    source = record.source if isinstance(record.source, str) else None
    if source is not None and path_map is not None:
        source = portal_path(source, path_map, resolve=recorded_real_path)  # through files.probe: NotResponding
    return replace(
        record,
        source=_servable(source),
        input_id=_servable(_text(record.input_id)),
        category=_servable(record.category) or "",
        message=_servable(record.message) or "",
        error_type=_servable(_text(record.error_type)),
        recorded_at=_servable(_text(record.recorded_at)),
    )


def _failure_page(path: str, *, offset: int, limit: int) -> tuple[list[FailureRecord], bool]:
    """A page of the report at ``path``, opened as a regular file (``_open_regular``) of at most
    ``MAX_FAILURE_REPORT_BYTES``, its records at most ``MAX_FAILURE_RECORD_BYTES`` long."""

    descriptor = _open_regular(path)
    with open(descriptor, "rb") as handle:
        if os.fstat(handle.fileno()).st_size > MAX_FAILURE_REPORT_BYTES:
            raise _Unreadable(f"larger than {MAX_FAILURE_REPORT_BYTES // (1024 * 1024)} MB")
        return read_failure_records(
            handle, offset=offset, limit=limit, max_record_bytes=MAX_FAILURE_RECORD_BYTES, name=path
        )


def failure_records(
    details: RunDetails, *, offset: int, limit: int = FAILURE_PAGE_SIZE
) -> tuple[list[FailureRecord], bool]:
    """One bounded page of failed or unattempted inputs (``failure_report_page``'s records); empty when the report
    is not recorded, not on this host or cannot be read.

    A remote run's ``failures.jsonl`` names its inputs as the remote host saw them: each ``source`` is mapped to the
    portal's view with the run's plan (``remote.paths.portal_path``, ``run_path_map``), and
    is ``None`` (unavailable) where the rule refuses it. A run without a plan keeps its sources as written.
    """

    page = failure_report_page(details.progress, details.run_directory, offset=offset, limit=limit)
    return page.records, page.has_more


@dataclass(frozen=True)
class FailureReportPage:
    """A page of the failure report, and how reading it went."""

    records: list[FailureRecord]
    has_more: bool
    file: FileRead


def failure_report_page(
    progress: RunProgress, run_directory: str | None, *, offset: int, limit: int = FAILURE_PAGE_SIZE
) -> FailureReportPage:
    """One page of the run's failure report, each record as the portal shows it (``_portal_record``: a remote run's
    sources mapped through its plan). A report that is not recorded, not on this host, or that cannot be read (not
    a regular file, larger than ``MAX_FAILURE_REPORT_BYTES``, a record on the page that is malformed or longer than
    ``MAX_FAILURE_RECORD_BYTES``) gives an empty page, and its ``file`` says why."""

    path = progress.failure_report_path
    if not path:
        return FailureReportPage([], False, FileRead(FAILURE_REPORT_FILENAME, None, FileState.NOT_RECORDED))

    def unreadable(detail: str) -> FailureReportPage:
        return FailureReportPage([], False, FileRead(FAILURE_REPORT_FILENAME, path, FileState.UNREADABLE, detail))

    try:
        _check(path)
        records, has_more = _failure_page(path, offset=max(offset, 0), limit=limit)
        path_map = run_path_map(run_directory) if run_directory else None
        records = [_portal_record(record, path_map) for record in records]
    except NotResponding:
        return FailureReportPage([], False, FileRead(FAILURE_REPORT_FILENAME, path, FileState.NOT_RESPONDING))
    except _Unreadable as reason:
        return unreadable(str(reason))
    except OSError as error:
        if _missing(error):
            return FailureReportPage([], False, FileRead(FAILURE_REPORT_FILENAME, path, FileState.MISSING))
        return unreadable(str(error))
    except (ManifestError, ValueError, RecursionError) as error:  # a malformed record, a NUL in the path
        return unreadable(str(error))
    return FailureReportPage(records, has_more, FileRead(FAILURE_REPORT_FILENAME, path, FileState.AVAILABLE))


def failure_count(progress: RunProgress) -> int:
    """How many records the failure report holds for a terminal run: failed plus not-run inputs."""

    return progress.n_failed + (progress.n_not_run if progress.is_terminal else 0)


def failure_total(details: RunDetails) -> int:
    """How many records the failure report holds for a terminal run: failed plus not-run inputs."""

    return failure_count(details.progress)


# ---------------------------------------------------------------------------
# The logs


@dataclass(frozen=True)
class LogTail:
    """The last lines of one log, oldest first."""

    lines: list[str]
    truncated: bool
    """Whether the log holds earlier lines."""
    n_lines: int | None
    """How many lines the log holds, when known (``run.json``'s log); None for a file read from its end."""
    file: FileRead


def run_log(summary: dict[str, Any] | None, file: FileRead, *, limit: int) -> LogTail:
    """The last ``limit`` lines of the run's log: ``run.json``'s ``log`` (``summary``,
    read as ``file``), the messages the compute function logged. No lines without ``run.json`` or for a log that is
    not a list (``n_lines`` None)."""

    entries = _log_entries(summary)
    if entries is None:
        return LogTail([], False, None, file)
    return LogTail(entries[-limit:], len(entries) > limit, len(entries), file)


def tail_lines(descriptor: int, limit: int, *, max_bytes: int = MAX_TAIL_BYTES) -> tuple[list[str], bool]:
    """The last ``limit`` lines of an open file, oldest first, and whether earlier text exists. The file is read
    backwards in blocks until ``limit`` whole lines are in, or its start, or ``max_bytes``: never whole. Lines are
    UTF-8 (anything else replaced), without their line breaks; when ``max_bytes`` ends the read inside a line, that
    line is left out, unless it is the only one (then it is cut at its start)."""

    end = os.fstat(descriptor).st_size
    position = end
    chunks: list[bytes] = []
    newlines = 0
    while position > 0 and newlines <= limit and end - position < max_bytes:
        size = min(TAIL_BLOCK_BYTES, position, max_bytes - (end - position))
        chunk = os.pread(descriptor, size, position - size)
        if len(chunk) < size:  # the file shrank while it was read: keep what was read after this point
            break
        position -= size
        chunks.append(chunk)
        newlines += chunk.count(b"\n")
    data = b"".join(reversed(chunks))
    truncated = position > 0
    if truncated:  # the read starts inside the file: its first line may be cut; drop it if a whole one follows
        first_break = data.find(b"\n")
        if 0 <= first_break < len(data) - 1:
            data = data[first_break + 1 :]
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":  # the last line's break
        lines.pop()
    truncated = truncated or len(lines) > limit
    return [line.decode("utf-8", "replace").removesuffix("\r") for line in lines[-limit:]], truncated


def _inbox(run_directory: str, submission_id: str | None) -> str | None:
    """A remote run's return inbox, ``<run>/incoming/<submission>/``, for a
    recorded submission ID that is a UUID as the portal writes it; None otherwise."""

    if not isinstance(submission_id, str):
        return None
    try:
        if str(uuid.UUID(submission_id)) != submission_id:
            return None
    except ValueError:
        return None
    return os.path.join(run_directory, INCOMING_DIRECTORY, submission_id)


def _file_tail(run_directory: str, path: str, *, limit: int) -> LogTail:
    def outcome(state: FileState, detail: str | None = None) -> LogTail:
        return LogTail([], False, None, FileRead(PBS_OUTPUT_FILENAME, path, state, detail))

    try:
        bounded(path, lambda: child_path(run_directory, path))  # no symbolic link leaves the run directory
    except NotResponding:
        return outcome(FileState.NOT_RESPONDING)
    except (OSError, RuntimeError, ValueError):
        return outcome(FileState.UNREADABLE, "outside the run directory")
    try:
        descriptor = _open_regular(path)
    except _Unreadable as reason:
        return outcome(FileState.UNREADABLE, str(reason))
    except OSError as error:
        return outcome(FileState.MISSING) if _missing(error) else outcome(FileState.UNREADABLE, str(error))
    except ValueError as error:  # a NUL in the path
        return outcome(FileState.UNREADABLE, str(error))
    try:
        lines, truncated = tail_lines(descriptor, limit)
    except OSError as error:
        return outcome(FileState.UNREADABLE, str(error))
    finally:
        os.close(descriptor)
    return LogTail(lines, truncated, None, FileRead(PBS_OUTPUT_FILENAME, path, FileState.AVAILABLE))


def pbs_output(run_directory: str | None, submission_id: str | None, *, limit: int) -> LogTail:
    """The last ``limit`` lines of a remote run's PBS output (``pbs-output.log``, the batch job's standard output
    and error): in the run directory once publication moved it there, else in the return inbox (a return that
    publication refused, or an ``aps`` run's output directory before publication). Missing until the batch job
    has ended and its output returned."""

    if not run_directory:
        return LogTail([], False, None, FileRead(PBS_OUTPUT_FILENAME, None, FileState.NOT_RECORDED))
    published = _file_tail(run_directory, os.path.join(run_directory, PBS_OUTPUT_FILENAME), limit=limit)
    inbox = _inbox(run_directory, submission_id)
    if published.file.state != FileState.MISSING or inbox is None:
        return published
    returned = _file_tail(run_directory, os.path.join(inbox, PBS_OUTPUT_FILENAME), limit=limit)
    return published if returned.file.state == FileState.MISSING else returned
