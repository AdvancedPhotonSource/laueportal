"""Where a LaueMatching run writes its frames: one results file and one native-tables file, or committed segments.

``laue_matching.h5`` (:class:`NativeTables`) keeps every frame's tables as the indexer wrote them, grouped by frame
through offsets. A local run writes it and ``output.h5`` directly (:class:`SingleOutput`). A remote run writes
**segments** (:class:`Segments`): every ``segment_seconds`` the frames since the last commit are closed into their own
pair of files, with a ``segment.json`` of what they hold, and the folder is renamed from ``segments/.<n>.partial`` to
``segments/<n>``. A committed segment is never changed, so a killed allocation loses only the frames of the segment it
was writing, and the committed files can be read while the run goes on. At the end the segments are merged
(``lauelab.indexing.merge_results_files``, :func:`merge_native_tables`) into the run's two files, which are validated
against every frame's identity, and the segment folders are removed.

A frame's failure is reported (``RunHooks.record_failure``) when its segment is committed, so that the failure report
names only frames whose result is durable.

Standard library, numpy and h5py only (import-linter rule 3).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol, cast

import h5py
import numpy as np

from laueportal.jobs.contract import RunHooks
from laueportal.jobs.rundir import RESULTS_FILENAME, FailureRecord, write_json_file

SOLUTION_COLUMNS = 34
SPOT_COLUMNS = 11
NATIVE_FILENAME = "laue_matching.h5"
NATIVE_FORMAT = "laue-matching-native-tables"
SEGMENTS_DIRNAME = "segments"
SEGMENT_RECORD = "segment.json"
SEGMENT_FORMAT = "laue-matching-segment"


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

    def append(self, frame_id: str, result: Any, tables: FrameTables) -> None:
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
    """A remote run's output in committed segments under ``<run>/segments/`` (the module's docstring)."""

    def __init__(
        self, run_directory: Path, writer: Callable[[Path], Writer], hooks: RunHooks, *, seconds: float
    ) -> None:
        self.root = run_directory / SEGMENTS_DIRNAME
        self.root.mkdir(parents=True, exist_ok=True)
        self.writer_factory = writer
        self.hooks = hooks
        self.seconds = seconds
        self.committed: list[dict[str, Any]] = []
        self.frame_ids: list[str] = []
        self.columns: tuple[str, str] | None = None
        self._open: dict[str, Any] | None = None

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
                "failures": [],
                "started": time.monotonic(),
            }
        return self._open

    def append(self, frame_id: str, result: Any, tables: FrameTables) -> None:
        segment = self._segment()
        segment["writer"].append(result, frame_id=frame_id)
        segment["native"].append(frame_id, tables)
        segment["frame_ids"].append(frame_id)

    def fail(self, record: FailureRecord) -> None:
        self._segment()["failures"].append(record)

    def set_columns(self, columns: tuple[str, str]) -> None:
        if self.columns is None:
            self.columns = columns
            if self._open is not None:
                self._open["native"].set_columns(*columns)

    def due(self) -> bool:
        return self._open is not None and time.monotonic() - self._open["started"] >= self.seconds

    def commit(self) -> None:
        """Close the open segment's files, record what they hold and rename its folder into place; then report its
        failures. Nothing to do when no segment is open."""
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
        record = {
            "format": SEGMENT_FORMAT,
            "version": 1,
            "number": segment["number"],
            "frame_ids": segment["frame_ids"],
            "n_failed": len(segment["failures"]),
            "failed_ids": [failure.input_id for failure in segment["failures"]],
            "files": {name: sha256(partial / name) for name in (RESULTS_FILENAME, NATIVE_FILENAME)},
        }
        write_json_file(partial / SEGMENT_RECORD, record)
        final = self.root / f"{segment['number']:06d}"
        os.rename(partial, final)
        _sync_directory(self.root)
        self.committed.append({**record, "folder": final})
        self.frame_ids.extend(segment["frame_ids"])
        for failure in segment["failures"]:
            self.hooks.record_failure(failure)

    def close(self) -> None:
        self.commit()

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
                "n_frames": len(segment["frame_ids"]),
                "n_failed": segment["n_failed"],
                "files": segment["files"],
            }
            for segment in self.committed
        ]


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
