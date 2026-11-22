"""Where a LaueMatching run writes its frames: one results file and one native-tables file, or committed segments.

``laue_matching.h5`` (:class:`NativeTables`) keeps every frame's tables as the indexer wrote them, grouped by frame
through offsets. A local run writes it and ``output.h5`` directly (:class:`SingleOutput`). A remote run writes
**segments** (:class:`Segments`): every ``segment_seconds`` the frames since the last commit are closed into their own
pair of files, with a ``segment.json`` of what they hold, and the folder is renamed from ``segments/.<n>.partial`` to
``segments/<n>``. A committed segment is never changed, so a killed allocation loses only the frames of the segment it
was writing, and the committed files can be read while the run goes on. At the end the segments are merged
(``lauelab.indexing.merge_results_files``, :func:`merge_native_tables`) into the run's two files, which are validated
against every frame's identity, and the segment folders are removed.

A segment's record names every input it attempted, in manifest order (``attempted_ids``), which of them succeeded
(``frame_ids``) and failed (``failed_ids``, with each failure's full record in the segment's ``failures.jsonl``), the
checksum of each of its files, and the run's identity (``identity``, the checksum of ``checkpoint.json``:
:func:`checkpoint_identity`). A run that continues over several allocations (``pioneer``) is therefore resumed from its
committed segments alone (:meth:`Segments.resume`): each record and file is checked, and the attempted inputs together
must be exactly the start of the manifest (:func:`skip_committed`), so no input is attempted, counted or published
twice, and none is skipped. Anything that does not check is a :class:`CheckpointError`: the run stops and its files
are kept. A continued run's failures are reported (``RunHooks.record_failure``) once, when the run ends
(:meth:`Segments.committed_failures`), so that the failure report names only frames whose result is durable, each once;
a run of one allocation reports a segment's failures when the segment is committed.

A continued run's end merges the segments into partial files, validates and publishes them; the runner then writes
the failure report and the run's summary, and only then removes the segments. An end that is killed is not resumed:
merged files found in the output folder at a later end are never trusted or replaced (``CheckpointError``), and the
committed segments stay for the operator (:func:`committed_segments` reads them, checked, without changing them).

Standard library, numpy and h5py only (import-linter rule 3).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, cast

import h5py
import numpy as np

from laueportal.jobs.contract import RunHooks
from laueportal.jobs.rundir import RESULTS_FILENAME, FailureRecord, ManifestEntry, json_text, write_json_file

SOLUTION_COLUMNS = 34
SPOT_COLUMNS = 11
NATIVE_FILENAME = "laue_matching.h5"
NATIVE_FORMAT = "laue-matching-native-tables"
SEGMENTS_DIRNAME = "segments"
SEGMENT_RECORD = "segment.json"
SEGMENT_FORMAT = "laue-matching-segment"
SEGMENT_VERSION = 2
SEGMENT_FAILURES = "failures.jsonl"
"""A segment's failure records, one ``FailureRecord`` JSON line each, in the order the inputs were attempted."""
CHECKPOINT_RECORD = "checkpoint.json"
CHECKPOINT_FORMAT = "laue-matching-checkpoint"
_COMMITTED = re.compile(r"[0-9]{6}")
_PARTIAL = re.compile(r"\.[0-9]{6}\.partial")


class CheckpointError(ValueError):
    """A committed checkpoint that does not check against its records or the run: the run stops, its files kept."""


class FrameTables(Protocol):
    """A processed frame's native tables (``compute._FrameOutput``)."""

    @property
    def solutions(self) -> np.ndarray: ...
    @property
    def kept(self) -> np.ndarray: ...
    @property
    def spots(self) -> np.ndarray: ...
    @property
    def unique_labels(self) -> np.ndarray: ...


class Writer(Protocol):
    """A ``lauelab.indexing.ResultsWriter``."""

    failed: bool
    error: Any
    count: int

    def __enter__(self) -> Writer: ...
    def __exit__(self, *exc: Any) -> None: ...
    def append(self, result: Any, frame_id: Any = None) -> None: ...


class NativeTables:
    """``laue_matching.h5``: ``solutions`` holds every orientation best first (``kept``: passed the filter;
    ``unique_labels``: the spots no better orientation of the frame claimed first), ``spots`` every predicted spot; the
    rows of frame ``i`` are ``offsets[i]`` to ``offsets[i + 1]``. Each table's ``columns`` attribute names its
    columns."""

    def __init__(self, path: Path) -> None:
        self.file = h5py.File(path, "w")
        self.file.attrs["format"] = NATIVE_FORMAT
        self.file.attrs["version"] = 1
        self.tables: dict[str, h5py.Dataset] = {}
        for name, shape, dtype in (
            ("solutions", (0, SOLUTION_COLUMNS), np.float64),
            ("spots", (0, SPOT_COLUMNS), np.float64),
            ("kept", (0,), np.bool_),
            ("unique_labels", (0,), np.int32),
            ("solution_offsets", (1,), np.int64),
            ("spot_offsets", (1,), np.int64),
            ("frame_ids", (0,), h5py.string_dtype()),
        ):
            self.tables[name] = self.file.create_dataset(
                name, shape, maxshape=(None, *shape[1:]), dtype=dtype, chunks=(1024, *shape[1:])
            )

    def extend(self, name: str, rows: Any) -> None:
        table = self.tables[name]
        start = table.shape[0]
        table.resize(start + len(rows), axis=0)
        if len(rows):
            table[start:] = rows

    def append(self, frame_id: str, output: FrameTables) -> None:
        self.extend("solutions", output.solutions)
        self.extend("spots", output.spots)
        self.extend("kept", output.kept)
        self.extend("unique_labels", output.unique_labels)
        self.extend("solution_offsets", [self.tables["solutions"].shape[0]])
        self.extend("spot_offsets", [self.tables["spots"].shape[0]])
        self.extend("frame_ids", [frame_id])

    def set_columns(self, solutions: str | Sequence[str], spots: str | Sequence[str]) -> None:
        for name, columns in (("solutions", solutions), ("spots", spots)):
            self.tables[name].attrs["columns"] = (
                columns.lstrip("%").split() if isinstance(columns, str) else list(columns)
            )

    def close(self) -> None:
        self.file.close()


def merge_native_tables(paths: Sequence[Path], destination: Path) -> int:
    """Concatenate native-tables files in order into ``destination``, each frame's offsets moved by the rows before
    it; the number of frames. The columns are the first file's that names them; a file naming others is refused."""
    tables = NativeTables(destination)
    columns: tuple[list[str], list[str]] | None = None
    frames = 0
    try:
        for path in paths:
            with h5py.File(path, "r") as source:
                if source.attrs.get("format") != NATIVE_FORMAT:
                    raise ValueError(f"{path} is not a LaueMatching native-tables file")

                def dataset(name: str, source: h5py.File = source) -> h5py.Dataset:
                    return cast(h5py.Dataset, source[name])

                named = tuple(
                    [str(value) for value in cast(Any, dataset(name).attrs["columns"])]
                    if "columns" in dataset(name).attrs
                    else None
                    for name in ("solutions", "spots")
                )
                if named[0] is not None and named[1] is not None:
                    if columns is None:
                        columns = (named[0], named[1])
                    elif columns != named:
                        raise ValueError(f"{path} names other columns than the files before it")
                solution_base = tables.tables["solutions"].shape[0]
                spot_base = tables.tables["spots"].shape[0]
                for name in ("solutions", "spots", "kept", "unique_labels"):
                    tables.extend(name, dataset(name)[...])
                tables.extend("solution_offsets", dataset("solution_offsets")[1:] + solution_base)
                tables.extend("spot_offsets", dataset("spot_offsets")[1:] + spot_base)
                ids = dataset("frame_ids").asstr()[...]
                tables.extend("frame_ids", ids)
                frames += len(ids)
        if columns is not None:
            tables.set_columns(*columns)
    finally:
        tables.close()
    return frames


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


class SingleOutput:
    """A local run's output: every frame into ``results`` and ``native`` (the run's partial files) as it finishes,
    and each failure reported at once."""

    def __init__(self, results: Path, native: Path, writer: Callable[[Path], Writer], hooks: RunHooks) -> None:
        self.results, self.native_path, self.hooks = results, native, hooks
        self.native = NativeTables(native)
        self.writer = writer(results).__enter__()
        self.frame_ids: list[str] = []
        self.columns: tuple[str, str] | None = None

    def append(self, frame_id: str, result: Any, tables: FrameTables, **counts: Any) -> None:
        """One processed frame (``counts``, which :class:`Segments` keeps, are the caller's here)."""
        self.writer.append(result, frame_id=frame_id)
        self.native.append(frame_id, tables)
        self.frame_ids.append(frame_id)

    def fail(self, record: FailureRecord) -> None:
        self.hooks.record_failure(record)

    def set_columns(self, columns: tuple[str, str]) -> None:
        if self.columns is None:
            self.columns = columns
            self.native.set_columns(*columns)

    def due(self) -> bool:
        return False

    def commit(self) -> None:
        pass

    def close(self) -> None:
        """Close both files; ``RuntimeError`` when the results writer failed."""
        try:
            self.writer.__exit__(None, None, None)
        finally:
            self.native.close()
        if self.writer.failed:
            raise RuntimeError(f"results file write failed after {self.writer.count} frames: {self.writer.error}")

    def segments(self) -> list[dict[str, Any]]:
        return []


class Segments:
    """A remote run's output in committed segments under ``<run>/segments/`` (the module's docstring).

    ``identity``: the run's identity (:func:`checkpoint_identity`), recorded in every segment and required of every
    segment :meth:`resume` reads; ``allocation``: the PBS allocation writing the segments (None for a run of one)."""

    def __init__(
        self,
        run_directory: Path,
        writer: Callable[[Path], Writer],
        hooks: RunHooks,
        *,
        seconds: float,
        identity: str | None = None,
        allocation: int | None = None,
        report_at_commit: bool = False,
    ) -> None:
        """``report_at_commit``: report each segment's failures when it is committed (a run of one allocation);
        else once at the run's end (:meth:`committed_failures`)."""
        self.report_at_commit = report_at_commit
        self.root = run_directory / SEGMENTS_DIRNAME
        self.root.mkdir(parents=True, exist_ok=True)
        self.writer_factory = writer
        self.hooks = hooks
        self.seconds = seconds
        self.identity = identity
        self.allocation = allocation
        self.committed: list[dict[str, Any]] = []
        self.frame_ids: list[str] = []
        self.attempted_ids: list[str] = []
        self.n_failed = 0
        self.n_indexed = 0
        self.unmatched = 0
        self.deep = False
        self.columns: tuple[str, str] | None = None
        self._open: dict[str, Any] | None = None

    def resume(self) -> int:
        """Read the segments committed before (by earlier allocations), checked (:func:`committed_segments`), after
        removing any segment folder an allocation never committed (``.<n>.partial``). Returns how many inputs they
        attempted."""
        if self.committed or self._open is not None:
            raise RuntimeError("segments resume before any frame of this allocation")
        for name in os.listdir(self.root):
            if _PARTIAL.fullmatch(name):
                shutil.rmtree(self.root / name)
        for record in committed_segments(self.root, self.identity):
            self.committed.append(record)
            self.attempted_ids.extend(record["attempted_ids"])
            self.frame_ids.extend(record["frame_ids"])
            self.n_failed += record["n_failed"]
            self.n_indexed += record["n_indexed"]
            self.unmatched += record["unmatched_spots"]
            self.deep = self.deep or record["deep"]
        return len(self.attempted_ids)

    def _segment(self) -> dict[str, Any]:
        if self._open is None:
            number = len(self.committed)
            partial = self.root / f".{number:06d}.partial"
            shutil.rmtree(partial, ignore_errors=True)  # a segment this attempt never committed
            partial.mkdir()
            native = NativeTables(partial / NATIVE_FILENAME)
            if self.columns is not None:
                native.set_columns(*self.columns)
            self._open = {
                "number": number,
                "partial": partial,
                "writer": self.writer_factory(partial / RESULTS_FILENAME).__enter__(),
                "native": native,
                "frame_ids": [],
                "attempted_ids": [],
                "failures": [],
                "n_indexed": 0,
                "unmatched_spots": 0,
                "deep": False,
                "started": time.monotonic(),
            }
        return self._open

    def append(
        self,
        frame_id: str,
        result: Any,
        tables: FrameTables,
        *,
        indexed: bool = False,
        unmatched: int = 0,
        deep: bool = False,
    ) -> None:
        """One processed frame; ``indexed``, ``unmatched`` and ``deep`` are its counts for the run's summary, which
        the segment keeps so that a resumed run still counts them."""
        segment = self._segment()
        segment["writer"].append(result, frame_id=frame_id)
        segment["native"].append(frame_id, tables)
        segment["frame_ids"].append(frame_id)
        segment["attempted_ids"].append(frame_id)
        segment["n_indexed"] += int(bool(indexed))
        segment["unmatched_spots"] += int(unmatched)
        segment["deep"] = segment["deep"] or bool(deep)

    def fail(self, record: FailureRecord) -> None:
        segment = self._segment()
        segment["failures"].append(record)
        segment["attempted_ids"].append(record.input_id)

    def set_columns(self, columns: tuple[str, str]) -> None:
        if self.columns is None:
            self.columns = columns
            if self._open is not None:
                self._open["native"].set_columns(*columns)

    def due(self) -> bool:
        return self._open is not None and time.monotonic() - self._open["started"] >= self.seconds

    def commit(self) -> None:
        """Close the open segment's files, write its failure records, record what it holds and rename its folder into
        place. Nothing to do when no segment is open."""
        segment, self._open = self._open, None
        if segment is None:
            return
        writer = segment["writer"]
        try:
            writer.__exit__(None, None, None)
        finally:
            segment["native"].close()
        if writer.failed:
            raise RuntimeError(f"results file write failed after {writer.count} frames: {writer.error}")
        partial: Path = segment["partial"]
        names = [RESULTS_FILENAME, NATIVE_FILENAME]
        if segment["failures"]:
            with open(partial / SEGMENT_FAILURES, "x", encoding="utf-8", newline="\n") as stream:
                stream.writelines(failure.to_json() + "\n" for failure in segment["failures"])
                stream.flush()
                os.fsync(stream.fileno())
            names.append(SEGMENT_FAILURES)
        record = {
            "format": SEGMENT_FORMAT,
            "version": SEGMENT_VERSION,
            "number": segment["number"],
            "allocation": self.allocation,
            "identity": self.identity,
            "first": len(self.attempted_ids),
            "attempted_ids": segment["attempted_ids"],
            "frame_ids": segment["frame_ids"],
            "n_failed": len(segment["failures"]),
            "failed_ids": [failure.input_id for failure in segment["failures"]],
            "n_indexed": segment["n_indexed"],
            "unmatched_spots": segment["unmatched_spots"],
            "deep": segment["deep"],
            "files": {name: sha256(partial / name) for name in names},
        }
        _sync_files(partial, names)
        write_json_file(partial / SEGMENT_RECORD, record)
        final = self.root / f"{segment['number']:06d}"
        os.rename(partial, final)
        _sync_directory(self.root)
        self.committed.append({**record, "folder": final})
        self.attempted_ids.extend(segment["attempted_ids"])
        self.frame_ids.extend(segment["frame_ids"])
        self.n_failed += record["n_failed"]
        self.n_indexed += record["n_indexed"]
        self.unmatched += record["unmatched_spots"]
        self.deep = self.deep or record["deep"]
        if self.report_at_commit:
            for failure in segment["failures"]:
                self.hooks.record_failure(failure)

    def close(self) -> None:
        self.commit()

    def committed_failures(self) -> Iterator[FailureRecord]:
        """Every committed segment's failures, in the order attempted, each checked against its segment's record."""
        for segment in self.committed:
            yield from read_segment_failures(segment["folder"], segment)

    def merge(self, results: Path, native: Path, merge_results: Callable[..., Any]) -> None:
        """Merge the committed segments into ``results`` and ``native`` (partial files; ``merge_results`` is
        ``lauelab.indexing.merge_results_files``), checking each segment's files against its record first."""
        for segment in self.committed:
            for name, digest in segment["files"].items():
                if sha256(segment["folder"] / name) != digest:
                    raise ValueError(f"segment {segment['number']}'s {name} changed after it was committed")
        merge_results(
            [segment["folder"] / RESULTS_FILENAME for segment in self.committed],
            results,
            frame_ids=self.frame_ids,
            overwrite=True,
        )
        merge_native_tables([segment["folder"] / NATIVE_FILENAME for segment in self.committed], native)

    def remove(self) -> None:
        """Remove the segment folders once the merged files are published."""
        shutil.rmtree(self.root, ignore_errors=True)

    def segments(self) -> list[dict[str, Any]]:
        """What each committed segment held, for the run's provenance."""
        return [
            {
                "number": segment["number"],
                "allocation": segment.get("allocation"),
                "n_frames": len(segment["frame_ids"]),
                "n_failed": segment["n_failed"],
                "files": segment["files"],
            }
            for segment in self.committed
        ]

    def handoff(self) -> dict[str, Any]:
        """What the committed segments hold, for an allocation's hand-off record (``jobs.runner``): the inputs
        attempted, succeeded and failed."""
        return {"n_attempted": len(self.attempted_ids), "n_succeeded": len(self.frame_ids), "n_failed": self.n_failed}


def _sync_files(folder: Path, names: Iterable[str]) -> None:
    for name in names:
        descriptor = os.open(folder / name, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def committed_segments(root: Path, identity: str | None) -> list[dict[str, Any]]:
    """Every committed segment's record under ``root`` (``segments/``), each checked (:func:`_checked_segment`) and
    with its ``folder``: numbered from 0 with none missing, each record's run identity ``identity``, each segment's
    first input right after the ones before it, and each file its recorded checksum. A folder never committed
    (``.<n>.partial``) is skipped; anything else in ``root``, or anything that does not check, is a
    ``CheckpointError``. Nothing is written."""
    names = sorted(os.listdir(root))
    for name in names:
        if not (_COMMITTED.fullmatch(name) or _PARTIAL.fullmatch(name)):
            raise CheckpointError(f"{SEGMENTS_DIRNAME}/{name} is not a segment")
    numbers = [int(name) for name in names if _COMMITTED.fullmatch(name)]
    if numbers != list(range(len(numbers))):
        raise CheckpointError(f"the committed segments are not numbered from 0 without a gap: {numbers}")
    records: list[dict[str, Any]] = []
    first = 0
    for number in numbers:
        folder = root / f"{number:06d}"
        record = _checked_segment(folder, number, identity, first=first)
        first += len(record["attempted_ids"])
        records.append({**record, "folder": folder})
    return records


def _ids(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CheckpointError(f"its {name} is not a list of input IDs")
    return value


def _checked_segment(folder: Path, number: int, identity: str | None, *, first: int) -> dict[str, Any]:
    """A committed segment's record, checked (``CheckpointError`` otherwise, naming the segment): this version's
    format and ``number``, the run's ``identity``, its first input right after the segments before it (``first``),
    its succeeded and failed inputs exactly its attempted ones in their order, its counts whole numbers, and each file
    named in it present with its recorded checksum."""
    where = f"segment {number}"
    try:
        record = json.loads((folder / SEGMENT_RECORD).read_text())
    except (FileNotFoundError, ValueError) as error:
        raise CheckpointError(f"{where} has no readable record: {error}") from None
    try:
        if not isinstance(record, dict) or record.get("format") != SEGMENT_FORMAT:
            raise CheckpointError("it is not a LaueMatching segment record")
        if record.get("version") != SEGMENT_VERSION or record.get("number") != number:
            raise CheckpointError("it is not this version's record of this segment")
        if record.get("identity") != identity:
            raise CheckpointError("it was committed by another run, or for other inputs, cache or parameters")
        if record.get("first") != first:
            raise CheckpointError(f"its first input is number {record.get('first')}, not {first}")
        attempted = _ids(record.get("attempted_ids"), "attempted_ids")
        succeeded = _ids(record.get("frame_ids"), "frame_ids")
        failed = _ids(record.get("failed_ids"), "failed_ids")
        if not attempted or len(set(attempted)) != len(attempted):
            raise CheckpointError("its attempted inputs are empty or repeat one")
        succeeded_set, failed_set = set(succeeded), set(failed)
        if succeeded_set & failed_set or [i for i in attempted if i in succeeded_set] != succeeded:
            raise CheckpointError("its succeeded inputs are not its attempted ones in order")
        if [i for i in attempted if i in failed_set] != failed or len(succeeded) + len(failed) != len(attempted):
            raise CheckpointError("its failed inputs are not the rest of its attempted ones in order")
        for key in ("n_failed", "n_indexed", "unmatched_spots"):
            if type(record.get(key)) is not int or record[key] < 0:
                raise CheckpointError(f"its {key} is not a count")
        if record["n_failed"] != len(failed) or record["n_indexed"] > len(succeeded):
            raise CheckpointError("its counts disagree with its inputs")
        if not isinstance(record.get("deep"), bool):
            raise CheckpointError("its deep flag is not a boolean")
        files = record.get("files")
        expected = {RESULTS_FILENAME, NATIVE_FILENAME} | ({SEGMENT_FAILURES} if failed else set())
        if not isinstance(files, dict) or set(files) != expected:
            raise CheckpointError("its files are not the segment's")
        for name, digest in files.items():
            try:
                actual = sha256(folder / name)
            except FileNotFoundError as error:
                raise CheckpointError(f"its {name} cannot be read: {error.strerror}") from None
            if actual != digest:
                raise CheckpointError(f"its {name} changed after it was committed")
    except CheckpointError as error:
        raise CheckpointError(f"{where}: {error}") from None
    return record


def read_segment_failures(folder: Path, record: Mapping[str, Any]) -> list[FailureRecord]:
    """A committed segment's failure records, which must be exactly its record's ``failed_ids`` in order."""
    if not record["failed_ids"]:
        return []
    try:
        records = [
            FailureRecord.from_dict(json.loads(line))
            for line in (folder / SEGMENT_FAILURES).read_text(encoding="utf-8").splitlines()
            if line
        ]
    except (FileNotFoundError, ValueError, RuntimeError) as error:  # ManifestError: a malformed record
        raise CheckpointError(f"segment {record['number']}'s failure records cannot be read: {error}") from None
    if [failure.input_id for failure in records] != record["failed_ids"]:
        raise CheckpointError(f"segment {record['number']}'s failure records are not its failed inputs")
    return records


def checkpoint_identity(directory: Path, identity: Mapping[str, Any]) -> str:
    """The run's identity in ``<directory>/checkpoint.json``: what its results depend on (the request, the manifest,
    the files read, the cache, the matcher), written by its first allocation. Every later allocation must compute the
    same document (``CheckpointError`` otherwise: the run's inputs, cache or code changed). Returns its checksum, which
    every segment records."""
    path = directory / CHECKPOINT_RECORD
    text = json_text(identity)
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        try:
            write_json_file(path, identity)
        except FileExistsError:
            existing = path.read_text(encoding="utf-8")
        else:
            existing = text
    if existing != text:
        try:
            before = json.loads(existing)
        except ValueError:
            before = None
        changed = (
            sorted(key for key in set(before) | set(identity) if before.get(key) != identity.get(key))
            if isinstance(before, dict)
            else ["the whole record"]
        )
        raise CheckpointError(
            f"the run's {CHECKPOINT_RECORD} disagrees with this allocation ({', '.join(changed)}): its inputs, cache "
            "or code changed since its first allocation"
        )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def skip_committed(entries: Iterable[ManifestEntry], attempted: Sequence[str]) -> Iterator[ManifestEntry]:
    """The manifest entries after the committed ones, checking that the committed inputs are exactly the manifest's
    first ones in order (``CheckpointError`` otherwise)."""
    remaining = iter(entries)
    for position, input_id in enumerate(attempted):
        entry = next(remaining, None)
        if entry is None or entry.input_id != input_id:
            raise CheckpointError(
                f"the committed inputs are not the start of the manifest (input {position} is "
                f"{None if entry is None else entry.input_id!r}, the segments have {input_id!r})"
            )
    return remaining


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_segment(folder: Path) -> dict[str, Any]:
    """A committed segment's record (``segment.json``)."""
    record = json.loads((folder / SEGMENT_RECORD).read_text())
    if not isinstance(record, dict) or record.get("format") != SEGMENT_FORMAT:
        raise ValueError(f"{folder} holds no LaueMatching segment record")
    return record
