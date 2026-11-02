"""The run directory: immutable input manifests, frozen requests, run-level failure reports, and
the publication of every run file. Nothing here touches the database or the settings
(import-linter rule 3): compute functions and the remote runners (``jobs.runner``,
``jobs.mpi_runner``) use this module as is. The files, their writers and the remote layout follow
a fixed format byte for byte (the launcher reads them; ``tests/fixtures/remote_goldens`` pins it).

Run metadata is stored alongside the output:

~~~text
inputs.jsonl            input manifest in run order, one JSON object per input
request.json            the frozen execution request (paths, validated settings, identities)
failures.jsonl          one JSON object per failed or unattempted input, only when needed
run.json                the run summary the executor (or, for a remote run, the runner) writes
bdp.json, incoming/, cancel.request, progress.json, ...
                        remote execution (RESERVED_RUN_FILENAMES lists every name)
~~~

A wire reconstruction also writes one lauelab scan directory,
``reconstruction/``: the catalog ``scan.h5`` and one standalone point file per
reconstructed point under ``reconstruction/points/``. The catalog path is the
run's reconstruction artifact; keep the whole directory together when moving it.

Frame IDs and ordering:

* A manifest ``index`` is the 0-based position in run order. Run order is ``files.inputs.input_sort_key`` order, applied
  when the inputs are resolved (deterministic): filename in natural order (``f2`` before ``f10``), then scan point and
  depth indices, then directory, then filename template, then point. It does not depend on the order of the filename
  templates or of the requested points, nor on directory listings.
* ``input_id`` is the source file stem (the whole filename when two inputs of
  the run share a stem), which is what lauelab HDF5 results store as
  ``frames/frame_ids``.
* A frame of a reconstructed point has ``source`` = the point file plus
  ``point_id`` and zero-based ``depth_index``. Its ``input_id`` is
  ``<point_id>_<depth_index>``, the stem the same frame had as a per-depth
  file ``<point_id>_<depth_index>.h5``.
* Historical merged ``output.xml`` files were assembled by ``merge_xml_files``
  from one XML per frame, in ``sorted()`` order of the per-frame XML filenames.
  That order is lexicographic, so ``..._10.xml`` precedes ``..._2.xml``. The
  portal's ``step_index`` / "Step #" is the 0-based position in that merged
  file, and a step's frame identity is ``<detector><inputImage>`` (a path).
* Positions differ between formats and between migrated runs (form order) and
  the portal's runs (sorted order), so anything that refers to a frame uses ``input_id``
  (or the ``inputImage`` stem of a migrated run's step), never a position.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from laueportal.files.inputs import ResolvedInput

# --- publication ---------------------------------------------------------------
#
# Publish run support files without overwriting a previous run's files.
#
# A file is written completely to ``partial_path(final)`` in the same directory,
# fsynced, and then published by a hard link, which fails atomically when
# ``final`` already exists; the directory is fsynced after. ``overwrite=True``
# uses ``os.replace`` instead. lauelab uses the same names for the scientific
# output it publishes itself (``reconstruction/``), without the fsyncs. JSON documents, which
# more than one writer may publish at once (``progress.json`` from the runner's heartbeat
# thread and its last write, ``cancel.request`` from two poller passes), go through a partial
# file unique to the writer instead (``create_unique_partial``).

PARTIAL_SUFFIX = ".partial"
_LINK_UNSUPPORTED = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK}


def partial_path(final: str | os.PathLike[str]) -> Path:
    final = Path(final)
    return final.with_name(final.name + PARTIAL_SUFFIX)


def publish_file(partial: str | os.PathLike[str], final: str | os.PathLike[str], *, overwrite: bool = False) -> Path:
    """Rename ``partial`` to ``final`` durably; refuse to clobber unless ``overwrite``.

    The caller has written and closed (or flushed) ``partial``. Its data are fsynced before it is
    published, and the directory after, so a published file survives a crash of the host whole.
    """

    partial = Path(partial)
    final = Path(final)
    if partial.parent != final.parent:
        raise ValueError(f"{partial} and {final} must be in the same directory")
    if not partial.exists():
        raise FileNotFoundError(errno.ENOENT, "partial file is missing", str(partial))
    _fsync_file(partial)
    if overwrite:
        os.replace(partial, final)
    else:
        try:
            os.link(partial, final)
        except FileExistsError:
            raise
        except OSError as error:
            if error.errno not in _LINK_UNSUPPORTED:
                raise
            if final.exists():
                raise FileExistsError(errno.EEXIST, "File exists", str(final)) from None
            os.replace(partial, final)
        else:
            os.unlink(partial)
    _fsync_directory(final.parent)
    return final


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    """Best-effort: some filesystems cannot open or fsync a directory."""

    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def discard_partial(final: str | os.PathLike[str]) -> None:
    """Remove a leftover partial file, ignoring its absence."""

    try:
        partial_path(final).unlink()
    except FileNotFoundError:
        pass


def create_unique_partial(final: str | os.PathLike[str]) -> tuple[Path, int]:
    """Create an empty partial file only this writer uses; returns its path and an open descriptor.

    The name is ``<name>.<12 hex digits>.partial`` in the directory of ``final``, created with
    ``O_EXCL`` (and the usual permissions, after the umask), so two writers of the same document
    never write into, publish or remove each other's partial file.
    """

    final = Path(final)
    while True:
        candidate = final.with_name(f"{final.name}.{secrets.token_hex(6)}{PARTIAL_SUFFIX}")
        try:
            descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o666)
        except FileExistsError:
            continue
        return candidate, descriptor


# --- manifest and request ------------------------------------------------------

MANIFEST_FORMAT = "laue-portal-input-manifest"
MANIFEST_VERSION = 1
REQUEST_FORMAT = "laue-portal-run-request"
REQUEST_VERSION = 1
FAILURE_REPORT_FORMAT = "laue-portal-failure-report"
FAILURE_REPORT_VERSION = 1

MANIFEST_FILENAME = "inputs.jsonl"
REQUEST_FILENAME = "request.json"
FAILURE_REPORT_FILENAME = "failures.jsonl"
RUN_SUMMARY_FILENAME = "run.json"
RESULTS_FILENAME = "output.h5"
RECONSTRUCTION_DIRECTORY = "reconstruction"
RECONSTRUCTION_CATALOG_FILENAME = "scan.h5"  # the name lauelab gives a scan directory's catalog
LOG_FILENAME = "run.log"  # reserved; nothing writes it today
# A live run's files: ``live.json`` (``jobs.live``), the arrivals' ledger
# and its lock (``jobs.arrivals``), the catalog's status and a live wire run's completed points.
LIVE_FILENAME = "live.json"
ARRIVALS_FILENAME = "arrivals.jsonl"
ARRIVALS_LOCK_FILENAME = "arrivals.lock"
ARRIVAL_STATUS_FILENAME = "arrival-status.json"
COMPLETED_FILENAME = "completed.jsonl"
# Remote execution.
PLAN_FILENAME = "bdp.json"  # the submission plan, in the run directory and the control directory
INCOMING_DIRECTORY = "incoming"  # <run>/incoming/<submission>/: the returned (or, for aps, written) outputs
CANCEL_REQUEST_FILENAME = "cancel.request"  # the portal's stop request, in the control directory
PROGRESS_FILENAME = "progress.json"  # the runner's heartbeat and counters, in the output directory
RUNNER_LOCK_FILENAME = ".runner.lock"  # the runner's start-once lock, in the output directory
RANKS_FILENAME = "ranks.json"  # the MPI runner's per-rank summaries, in the output directory
SHARDS_DIRECTORY = "shards"  # the MPI runner's rank-private outputs, beside the output directory
PBS_OUTPUT_FILENAME = "pbs-output.log"  # PBS's output of the batch job (qsub -o), in the output directory

# Names a run directory reserves for support and authoritative files: an Output XML may not take one
# of them, and neither may a directory of a relative Output XML name.
CLEANUP_JOB_FILENAME = ".cleanup-job.json"
"""In a remote run's directory: the DM clean-up job of what the run left elsewhere (``remote.cleanup``)."""

# The XML is written into the output directory, whose entries are published into the run directory,
# and for the aps target the run directory is also the control directory. The names are
# what the portal, plan, poller, runners and PBS task create in those directories.
RESERVED_RUN_FILENAMES = frozenset(
    {
        # The run files.
        MANIFEST_FILENAME,
        REQUEST_FILENAME,
        FAILURE_REPORT_FILENAME,
        RUN_SUMMARY_FILENAME,
        RESULTS_FILENAME,
        RECONSTRUCTION_DIRECTORY,
        LOG_FILENAME,
        # The portal side: the plan, the return inbox and the cancel request; the record of the clean-up job of a
        # Finished, published remote run.
        PLAN_FILENAME,
        INCOMING_DIRECTORY,
        CANCEL_REQUEST_FILENAME,
        CLEANUP_JOB_FILENAME,
        # The runners, in the output directory (published with the outputs, except the lock), and the
        # MPI runner's rank directories beside it.
        PROGRESS_FILENAME,
        RUNNER_LOCK_FILENAME,
        RANKS_FILENAME,
        SHARDS_DIRECTORY,
        # The PBS task: its state (and that file's partial) and the job's output in the output
        # directory, the batch script in the control directory.
        "pbs.json",
        "pbs.json.tmp",
        PBS_OUTPUT_FILENAME,
        "job.pbs",
        # A live run's files, reserved for every run.
        LIVE_FILENAME,
        ARRIVALS_FILENAME,
        ARRIVALS_LOCK_FILENAME,
        ARRIVAL_STATUS_FILENAME,
        COMPLETED_FILENAME,
    }
)
# Name prefixes of the portal's markers and temporary files in a run directory: .bdp-submission.lock,
# .bdp-publication.json and the publication's .bdp-return-* copies; .cancel-job.json and .cancel-sent.
RESERVED_RUN_PREFIXES = (".bdp-", ".cancel-")


def reconstruction_catalog_path(run_directory: str | os.PathLike[str]) -> str:
    """Return the expected catalog path for a wire reconstruction."""

    return os.path.join(run_directory, RECONSTRUCTION_DIRECTORY, RECONSTRUCTION_CATALOG_FILENAME)


class ManifestError(RuntimeError):
    """Raised when a manifest cannot be built, written, or read."""


@dataclass(frozen=True)
class ManifestEntry:
    """An input ID, source, and selection indices in execution order."""

    index: int
    input_id: str
    source: str
    template_index: int
    scan_point: int | None
    depth_point: int | None
    depth: float | None = None  # physical depth override in micrometres; None = from file
    point_id: str | None = None  # reconstructed point; source is then its point file
    depth_index: int | None = None  # zero-based frame of that point
    identity: tuple[int, int, int, int] | None = None
    """A live run's input: its file's identity when it was admitted (``arrivals.jsonl``'s ``size``, ``mtime_ns``,
    ``inode``, ``device``; ``file_identity``), which the live computes check before they process it; None for a
    manifest's input. Never in ``inputs.jsonl``."""

    def to_json(self) -> str:
        values = asdict(self)
        del values["identity"]
        if self.point_id is None:  # file inputs keep the original line format
            del values["point_id"], values["depth_index"]
        return json.dumps(values, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> ManifestEntry:
        try:
            return cls(
                index=int(values["index"]),
                input_id=str(values["input_id"]),
                source=str(values["source"]),
                template_index=int(values["template_index"]),
                scan_point=None if values.get("scan_point") is None else int(values["scan_point"]),
                depth_point=None if values.get("depth_point") is None else int(values["depth_point"]),
                depth=None if values.get("depth") is None else float(values["depth"]),
                point_id=None if values.get("point_id") is None else str(values["point_id"]),
                depth_index=None if values.get("depth_index") is None else int(values["depth_index"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ManifestError(f"malformed manifest entry: {error}") from error


def file_identity(path: str | os.PathLike[str]) -> tuple[int, int, int, int]:
    """A file's identity as a live run's admission records it: its size, modification time in
    nanoseconds, inode and device, not following a final link; ``OSError`` when it cannot be looked at."""

    status = os.lstat(path)
    return (status.st_size, status.st_mtime_ns, status.st_ino, status.st_dev)


def unchanged_since_admission(path: str | os.PathLike[str], identity: Sequence[int] | None) -> bool:
    """Whether the file ``path`` is still the one admitted with ``identity`` (``ManifestEntry.identity``): False when
    its identity differs or it cannot be looked at; True without one."""

    if identity is None:
        return True
    try:
        return file_identity(path) == tuple(identity)
    except OSError:
        return False


@dataclass(frozen=True)
class ManifestSummary:
    path: str
    n_inputs: int
    sha256: str


def input_identity(path: str | os.PathLike[str]) -> str:
    """Use the filename stem as the frame ID."""

    return Path(path).stem


def scan_frame_identity(point_id: str, depth_index: int) -> str:
    """Build the frame ID used by reconstruction results and per-depth exports."""

    return f"{point_id}_{depth_index}"


def _stem(entry: ResolvedInput) -> str:
    if entry.point_id is None:
        return input_identity(entry.path)
    if entry.depth_index is None:
        raise ManifestError(f"input {entry.path!r} names point {entry.point_id!r} but no depth index")
    return scan_frame_identity(entry.point_id, entry.depth_index)


def build_manifest_entries(
    inputs: Sequence[ResolvedInput],
    *,
    depths: Mapping[int, float] | None = None,
) -> tuple[ManifestEntry, ...]:
    """Assign manifest indices and unique identities to resolved inputs.

    Identities are file stems. If two inputs share a stem (for example ``a.h5``
    and ``a.tif``), every identity in the run falls back to the full filename so
    one rule applies to the whole manifest. Duplicate filenames are an error.
    A reconstructed frame's identity is ``<point_id>_<depth_index>``.
    """

    if not inputs:
        raise ManifestError("a run needs at least one input")
    stems = [_stem(entry) for entry in inputs]
    if len(set(stems)) == len(stems):
        identities = stems
    elif any(entry.point_id is not None for entry in inputs):
        duplicate = next(name for name in stems if stems.count(name) > 1)
        raise ManifestError(f"input identity {duplicate!r} is not unique within the run")
    else:
        identities = [os.path.basename(entry.path) for entry in inputs]
        if len(set(identities)) != len(identities):
            duplicate = next(name for name in identities if identities.count(name) > 1)
            raise ManifestError(f"input identity {duplicate!r} is not unique within the run")
    depths = depths or {}
    return tuple(
        ManifestEntry(
            index=index,
            input_id=identity,
            source=entry.path,
            template_index=entry.template_index,
            scan_point=entry.scan_point,
            depth_point=entry.depth_point,
            depth=depths.get(index),
            point_id=entry.point_id,
            depth_index=entry.depth_index,
        )
        for index, (entry, identity) in enumerate(zip(inputs, identities, strict=True))
    )


def write_manifest(
    path: str | os.PathLike[str],
    entries: Iterable[ManifestEntry],
    *,
    overwrite: bool = False,
) -> ManifestSummary:
    """Stream entries to ``path`` through a partial file; publish only when complete."""

    final = Path(path)
    partial = partial_path(final)
    digest = hashlib.sha256()
    count = 0
    try:
        with open(partial, "w", encoding="utf-8", newline="\n") as handle:
            for expected_index, entry in enumerate(entries):
                if entry.index != expected_index:
                    raise ManifestError(
                        f"manifest entry {entry.input_id!r} has index {entry.index}, expected {expected_index}"
                    )
                line = entry.to_json() + "\n"
                handle.write(line)
                digest.update(line.encode("utf-8"))
                count += 1
        if count == 0:
            raise ManifestError("a manifest must contain at least one input")
        publish_file(partial, final, overwrite=overwrite)
    except BaseException:
        discard_partial(final)
        raise
    return ManifestSummary(path=os.fspath(final), n_inputs=count, sha256=digest.hexdigest())


def read_manifest(path: str | os.PathLike[str]) -> Iterator[ManifestEntry]:
    """Stream manifest entries, checking that indices run 0, 1, 2, ..."""

    with open(path, encoding="utf-8") as handle:
        for expected_index, line in enumerate(handle):
            if not line.strip():
                raise ManifestError(f"{path}: blank line at entry {expected_index}")
            try:
                values = json.loads(line)
            except json.JSONDecodeError as error:
                raise ManifestError(f"{path}: entry {expected_index} is not JSON: {error}") from error
            entry = ManifestEntry.from_dict(values)
            if entry.index != expected_index:
                raise ManifestError(f"{path}: entry {expected_index} carries index {entry.index}")
            yield entry


def manifest_digest(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def count_manifest_entries(path: str | os.PathLike[str]) -> int:
    count = 0
    with open(path, "rb") as handle:
        for _ in handle:
            count += 1
    return count


def verify_manifest(path: str | os.PathLike[str], *, expected_digest: str, expected_count: int) -> None:
    """Check the manifest against the digest and input count recorded for the run."""

    if not os.path.isfile(path):
        raise ManifestError(f"manifest {path} is missing")
    digest = manifest_digest(path)
    if digest != expected_digest:
        raise ManifestError(f"manifest {path} digest {digest} does not match recorded {expected_digest}")
    count = count_manifest_entries(path)
    if count != expected_count:
        raise ManifestError(f"manifest {path} holds {count} inputs; the run recorded {expected_count}")


def _json_ready(value: Any) -> Any:
    """``value`` with the types JSON lacks converted; anything else is left for ``json.dumps``.

    A datetime becomes its ``isoformat()`` text (naive or aware, as given). The runners and the poller pass
    their times as text already.
    """

    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return os.fspath(value)
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_ready(item) for item in value]
    return value


def write_json_file(
    path: str | os.PathLike[str], payload: Mapping[str, Any] | Sequence[Any], *, overwrite: bool = False
) -> str:
    """Write a JSON document (an object, or a list such as ``ranks.json``) through a partial file unique
    to this call; returns its sha256.

    Without ``overwrite`` the document is created only if it does not exist (``FileExistsError``
    otherwise); of several writers racing to create it, exactly one succeeds. With ``overwrite``
    it is replaced atomically. Either way a reader sees no file or a whole document, and the
    writer's partial file is gone when this returns or raises.
    """

    final = Path(path)
    text = json_text(payload)
    partial, descriptor = create_unique_partial(final)
    try:
        with open(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        publish_file(partial, final, overwrite=overwrite)
    except BaseException:
        try:
            partial.unlink()
        except FileNotFoundError:
            pass
        raise
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_text(payload: Any) -> str:
    """The text :func:`write_json_file` writes for ``payload``, the format's bytes:
    two-space indentation, sorted keys, non-ASCII kept, a final newline; NaN and the infinities as
    Python's ``NaN``, ``Infinity`` and ``-Infinity`` tokens. Raises ``TypeError`` for an
    arbitrary object and ``UnicodeEncodeError`` for text UTF-8 cannot encode, before anything is written."""

    text = json.dumps(_json_ready(payload), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    text.encode("utf-8")  # a lone surrogate would only fail in the middle of the write
    return text


def write_request(path: str | os.PathLike[str], payload: Mapping[str, Any], *, overwrite: bool = False) -> str:
    """Freeze an execution request; the document carries its format marker."""

    document = {"format": REQUEST_FORMAT, "version": REQUEST_VERSION, **payload}
    return write_json_file(path, document, overwrite=overwrite)


def read_request(path: str | os.PathLike[str]) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict) or document.get("format") != REQUEST_FORMAT:
        raise ManifestError(f"{path} is not a {REQUEST_FORMAT} document")
    if document.get("version") != REQUEST_VERSION:
        raise ManifestError(f"{path} has request version {document.get('version')!r}; expected {REQUEST_VERSION}")
    return document


class ParameterModel(Protocol):
    """A parameter model (``core.params.ProgramParams``), as this module sees it: no pydantic import here, since the
    compute side imports this module and a compute node has no pydantic."""

    def model_dump(self) -> dict[str, Any]: ...


def request_document(request: ParameterModel) -> dict[str, Any]:
    """Serialize a parameter model (``core.params.ProgramParams``: ``lauego/v1``, ``wire/v1``): its fields, the
    ``request`` keys plus ``schema_id``. Its derived values (``scan_point_values``, ``depth_values``) are properties,
    not fields, so they are left out.

    A datetime (``submitted_at``) is written as ``isoformat()`` text, aware UTC with ``+00:00``.
    """

    return {name: _request_value(value) for name, value in request.model_dump().items()}


def _request_value(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def write_run_inputs(
    run_directory: str,
    *,
    inputs: Sequence[ResolvedInput],
    request_payload: Mapping[str, Any],
    depths: Mapping[int, float] | None = None,
) -> ManifestSummary:
    """Write ``inputs.jsonl`` and ``request.json`` into ``run_directory``, which is never reused.

    The request document records the manifest's input count and digest.
    """

    manifest_path = os.path.join(run_directory, MANIFEST_FILENAME)
    request_path = os.path.join(run_directory, REQUEST_FILENAME)
    os.makedirs(run_directory, exist_ok=True)
    for existing in (manifest_path, request_path):
        if os.path.exists(existing):
            raise ManifestError(f"{existing} already exists; a run directory is never reused")
    entries = build_manifest_entries(inputs, depths=depths)
    summary = write_manifest(manifest_path, entries)
    payload = dict(request_payload)
    payload["manifest"] = {"path": MANIFEST_FILENAME, "n_inputs": summary.n_inputs, "sha256": summary.sha256}
    write_request(request_path, payload)
    return summary


EMPTY_MANIFEST_SHA256 = hashlib.sha256(b"").hexdigest()


def write_empty_file(path: str | os.PathLike[str]) -> None:
    """Create ``path`` empty, exclusively (``FileExistsError`` when it exists), durably (fsynced, then its
    directory): a live run's ``inputs.jsonl``, ``arrivals.lock`` and ``arrivals.jsonl``."""

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o666)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(Path(path).parent)


def write_live_run_inputs(run_directory: str, *, request_payload: Mapping[str, Any]) -> ManifestSummary:
    """Write a live run's ``inputs.jsonl`` (empty: its inputs are the files that arrive while it runs) and its
    ``request.json`` into ``run_directory``, which is never reused.

    ``write_manifest`` refuses an empty manifest, so the empty one is written here; its digest and count (0) are the
    run's, so the executor's manifest check holds when the run starts.
    """

    manifest_path = os.path.join(run_directory, MANIFEST_FILENAME)
    request_path = os.path.join(run_directory, REQUEST_FILENAME)
    os.makedirs(run_directory, exist_ok=True)
    for existing in (manifest_path, request_path):
        if os.path.exists(existing):
            raise ManifestError(f"{existing} already exists; a run directory is never reused")
    write_empty_file(manifest_path)
    payload = dict(request_payload)
    payload["manifest"] = {"path": MANIFEST_FILENAME, "n_inputs": 0, "sha256": EMPTY_MANIFEST_SHA256}
    write_request(request_path, payload)
    return ManifestSummary(path=manifest_path, n_inputs=0, sha256=EMPTY_MANIFEST_SHA256)


@dataclass(frozen=True)
class CompletedPoint:
    """One line of a live wire run's ``completed.jsonl``: a point whose file and
    a catalog listing it complete are published. ``n`` counts the lines from 0, in the order the points were
    published; ``scan_point`` is its arrival's (the template's ``%d``), ``path`` its point file (absolute, under
    ``reconstruction/points/``), ``completed_at`` aware UTC to the second."""

    n: int
    point_id: str
    scan_point: int | None
    path: str
    n_depths: int
    completed_at: str

    def line(self) -> bytes:
        """Its line: compact JSON and a newline (JSONL)."""
        return (json.dumps(asdict(self), separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def append_completed(run_directory: str | os.PathLike[str], points: Sequence[CompletedPoint]) -> None:
    """Append ``points``' lines to ``completed.jsonl`` (created by the first): one ``write`` and an ``fsync``. Its one
    writer is the live wire run's job process (no lock); a reader takes whole lines only. A short write or a failed
    ``fsync`` truncates the file back to its size before the call, then raises: none of these lines stays, so a
    retry appends them whole after the last whole line, never after a fragment."""

    if not points:
        return
    data = b"".join(point.line() for point in points)
    path = os.path.join(run_directory, COMPLETED_FILENAME)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o666)
    try:
        size = os.fstat(descriptor).st_size  # the one writer: its lines end here
        try:
            written = os.write(descriptor, data)
            if written != len(data):
                raise OSError(f"wrote {written} of {len(data)} bytes of {COMPLETED_FILENAME}")
            os.fsync(descriptor)
        except BaseException:
            os.ftruncate(descriptor, size)
            raise
    finally:
        os.close(descriptor)


# --- failure report -----------------------------------------------------------

# Categories are stable strings for grouping in the UI; ``error_type`` keeps
# the exact exception class for diagnosis.
CATEGORY_INPUT = "input"  # the input itself is unreadable or malformed
CATEGORY_NUMERICAL = "numerical"  # native processing failed for this input
CATEGORY_MEMORY = "memory"
CATEGORY_CANCELLED = "cancelled"  # not attempted because the run was cancelled
CATEGORY_INTERRUPTED = "interrupted"  # not attempted because the system stopped
CATEGORY_NOT_RUN = "not_run"  # not attempted for another recorded reason
CATEGORY_ERROR = "error"  # anything else

_CLASS_CATEGORIES = (
    ("NumericalIndexingError", CATEGORY_NUMERICAL),
    ("InputError", CATEGORY_INPUT),
    ("MemoryError", CATEGORY_MEMORY),
    ("FileNotFoundError", CATEGORY_INPUT),
    ("PermissionError", CATEGORY_INPUT),
    ("IsADirectoryError", CATEGORY_INPUT),
    ("KeyError", CATEGORY_INPUT),
    ("ValueError", CATEGORY_INPUT),
    ("OSError", CATEGORY_INPUT),
)


def classify_error(error: BaseException) -> str:
    """Map an exception to a report category without importing lauelab."""

    names = {klass.__name__ for klass in type(error).__mro__}
    for class_name, category in _CLASS_CATEGORIES:
        if class_name in names:
            return category
    return CATEGORY_ERROR


@dataclass(frozen=True)
class FailureRecord:
    """One failed or unattempted input."""

    index: int | None
    input_id: str | None
    source: str | None
    category: str
    message: str
    error_type: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    recorded_at: str | None = None

    @classmethod
    def from_error(
        cls,
        entry: ManifestEntry,
        error: BaseException,
        *,
        context: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> FailureRecord:
        return cls(
            index=entry.index,
            input_id=entry.input_id,
            source=entry.source,
            category=classify_error(error),
            message=str(error) or type(error).__name__,
            error_type=type(error).__name__,
            context=dict(context or {}),
            recorded_at=(now or datetime.now()).isoformat(timespec="seconds"),
        )

    @classmethod
    def not_run(
        cls,
        entry: ManifestEntry,
        *,
        category: str = CATEGORY_NOT_RUN,
        message: str = "not attempted",
        now: datetime | None = None,
    ) -> FailureRecord:
        return cls(
            index=entry.index,
            input_id=entry.input_id,
            source=entry.source,
            category=category,
            message=message,
            recorded_at=(now or datetime.now()).isoformat(timespec="seconds"),
        )

    def to_json(self) -> str:
        """One compact JSON line (NaN and the infinities as :func:`json_text` writes them);
        ``TypeError`` for a value JSON cannot hold."""

        return json.dumps(_json_ready(asdict(self)), separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> FailureRecord:
        """A record from its JSON object; ``ManifestError`` for anything else (not an object, a key missing, an
        index that is not a whole number, such as ``1e400``, which JSON reads as infinity). The error names what is
        wrong, never the record's values."""
        if not isinstance(values, Mapping):
            raise ManifestError(f"malformed failure record: not a JSON object ({type(values).__name__})")
        index = values.get("index")
        if index is not None:
            try:
                index = int(index)
            except (TypeError, ValueError, OverflowError):
                raise ManifestError("malformed failure record: its index is not a whole number") from None
        try:
            return cls(
                index=index,
                input_id=values.get("input_id"),
                source=values.get("source"),
                category=str(values["category"]),
                message=str(values["message"]),
                error_type=values.get("error_type"),
                context=dict(values.get("context") or {}),
                recorded_at=values.get("recorded_at"),
            )
        except (KeyError, TypeError, ValueError) as error:  # a key missing, a context that is no mapping
            raise ManifestError(f"malformed failure record: {error}") from error


class FailureReportWriter:
    """Append-only JSONL failure report that creates no file when nothing failed.

    Records go to the partial file as they arrive and are published on
    ``close()``; a crash leaves the partial in place for diagnosis without ever
    presenting an unfinished report as complete.
    """

    def __init__(self, path: str | os.PathLike[str], *, overwrite: bool = False) -> None:
        self._final = Path(path)
        self._overwrite = overwrite
        self._handle = None
        self._count = 0
        self._closed = False
        if not overwrite and self._final.exists():
            raise FileExistsError(f"failure report {self._final} already exists")

    @property
    def count(self) -> int:
        return self._count

    @property
    def path(self) -> str:
        return os.fspath(self._final)

    def append(self, record: FailureRecord) -> None:
        if self._closed:
            raise RuntimeError("failure report is closed")
        if self._handle is None:
            self._handle = open(partial_path(self._final), "w", encoding="utf-8", newline="\n")
        self._handle.write(record.to_json() + "\n")
        self._handle.flush()
        self._count += 1

    def close(self) -> str | None:
        """Publish the report and return its path, or None when nothing was recorded."""

        if self._closed:
            return self.path if self._count else None
        self._closed = True
        if self._handle is None:
            return None
        try:
            self._handle.close()
            publish_file(partial_path(self._final), self._final, overwrite=self._overwrite)
        except BaseException:
            discard_partial(self._final)
            raise
        return self.path

    def __enter__(self) -> FailureReportWriter:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is None:
            self.close()
            return
        self._closed = True
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                pass  # keep the partial file for diagnosis


def count_failure_records(path: str | os.PathLike[str]) -> int:
    return count_manifest_entries(path)


def read_failure_report(
    path: str | os.PathLike[str],
    *,
    offset: int = 0,
    limit: int = 100,
) -> tuple[list[FailureRecord], bool]:
    """Read one bounded page of records; returns ``(records, has_more)`` (``read_failure_records``)."""

    with open(path, "rb") as handle:
        return read_failure_records(handle, offset=offset, limit=limit, name=os.fspath(path))


def read_failure_records(
    handle: BinaryIO,
    *,
    offset: int = 0,
    limit: int = 100,
    max_record_bytes: int | None = None,
    name: str = FAILURE_REPORT_FILENAME,
) -> tuple[list[FailureRecord], bool]:
    """One bounded page of a failure report open for binary reading at its start: ``(records, has_more)``.

    ``offset`` counts lines, blank ones included (they hold no record). Only the page's lines are parsed: one that
    is not UTF-8, not JSON (or nested too deeply to parse) or not a record is ``ManifestError`` (``name`` names the
    report in the message). With ``max_record_bytes`` no line is held longer than that: a longer record on the page
    is ``ManifestError``, one before the page is skipped piece by piece, and one after it only means ``has_more``.
    """

    if offset < 0 or limit <= 0:
        raise ValueError("offset must be >= 0 and limit > 0")
    records: list[FailureRecord] = []
    position = 0
    while True:
        line = handle.readline() if max_record_bytes is None else handle.readline(max_record_bytes + 1)
        if not line:
            return records, False
        whole = max_record_bytes is None or len(line) <= max_record_bytes or line.endswith(b"\n")
        if position >= offset:
            if len(records) == limit:
                return records, True
            if not whole:
                raise ManifestError(f"{name}: record {position} is longer than {max_record_bytes} bytes")
            if line.strip():
                records.append(_failure_record(line, name=name, position=position))
        elif not whole:
            _skip_rest_of_line(handle)
        position += 1


def _failure_record(line: bytes, *, name: str, position: int) -> FailureRecord:
    try:
        values = json.loads(line.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise ManifestError(f"{name}: record {position} is not UTF-8: {error}") from error
    except ValueError as error:  # not JSON, or an integer too long to convert
        raise ManifestError(f"{name}: record {position} is not JSON: {error}") from error
    except RecursionError:
        raise ManifestError(f"{name}: record {position} is nested too deeply") from None
    return FailureRecord.from_dict(values)


def _skip_rest_of_line(handle: BinaryIO, piece_bytes: int = 64 * 1024) -> None:
    while True:
        piece = handle.readline(piece_bytes)
        if not piece or piece.endswith(b"\n"):
            return
