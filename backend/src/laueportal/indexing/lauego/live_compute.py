"""Live LaueGo indexing: of raw frames, or of a live wire run's completed points (a
follower); each arriving frame indexed in a persistent pool, the results in shards, a preview ``output.h5``
republished as they come, the Output XML stitched at the seal.

A live LaueGo run (``lauego-live/v1``) is registered here for run kind ``lauego_indexing``
(``register_live_compute_function``); the executor runs it with its live hooks (``jobs.executor``). Its source
(``live.json``) is a DAQ or ``notify`` (raw frames), or a live wire run (``run``: a follower, whose arrivals its
follower's thread admits from the wire run's ``completed.jsonl``, ``jobs.follower``: a batch run on a
reconstruction, live). A follower's inputs are its points' selected depths (the request's ``depth_range``, else every
depth), one input each, handed out together per point (``LiveExecutorHooks``); a selected depth a point lacks is
one failed input (lauelab's read of the frame refuses it, an input error). Its frames go through the batch path a run
on a reconstruction takes (a ``ScanFrame`` of the point file to ``Indexer.index``, as lauelab's batch scheduler
indexes it, the depth the request's override else the point's), in this pool, from the calibration read at the start:
never lauelab's ``iter_index``, whose workers build their indexers from the geometry's path again.

- **The start.** The calibration files the request names (geometry, crystal, mask) are read once, here
  (``Calibration.read``), and the whole session uses what was read, even if a file changes or goes: the pool's
  indexers are built from it, and the results files embed the geometry text read then (lauelab's own writer reads the
  geometry file again for each file it starts, ``_SessionResultsWriter``). The settings are the batch compute's
  (``compute.settings_from_request``), so a frame gives what a batch run gives.
- **The pool:** a ``ProcessPoolExecutor`` of spawned workers, one per core of the dispatcher's share
  (``RunRequest.cores``; ``live_workers``), kept for the whole run. Each worker builds its indexer once,
  at its first frame, from the calibration read at the start (``_worker_indexer``),
  and indexes each frame with ``Indexer.index(path, depth=..., mask=..., keep_image=False)`` (the depth: the request's
  override, else the file's). An input error of the frame (lauelab's ``EXPECTED_INPUT_ERRORS``) is a failed input in
  ``failures.jsonl``, as a batch run's; so is a frame whose file is no longer the one admitted (its identity in
  ``arrivals.jsonl``, checked before and after the indexing). Any other error of a worker is a
  run-level error: one the frame's indexing raised names the frame (``WorkerError``, as lauelab's batch scheduler's
  "indexing worker failed on input ...") and fails it; a lost worker or an indexer a worker could not build is no
  frame's. Progress is reported per input.
- **Order**: frames leave the pool in arrival order, whatever order they finish
  in (a frame waits for those that arrived before it, as lauelab's ``iter_index`` delivers them); the shards and the
  preview follow it.
- **Shards:** the results go into ``shards/batch_<n>/output.h5`` (``n`` from 0, in order), with the frames' Output XML
  beside it (``output.xml``), once a shard holds as many frames as the pool has workers or its first frame has waited
  ``SHARD_SECONDS``; each is written privately, validated and published by a rename. A shard holds
  only indexed frames (a failed frame has no result). A follower's are per point, ``shards/<point_id>/``, each
  written once the outcome of its point's every input is in (its frames ``<point_id>_<depth index>``, as a batch run
  names them); a point with no frame indexed has none.
- **Publication**: the preview ``output.h5`` merges the shards (``merge_results_files``, the preview
  and the shards written since, so each frame is copied once more per publication) and is replaced atomically, at most
  every ``live.publish_seconds`` (``RunRequest.publish_seconds``) and only when a shard was written since the last; the
  first as soon as there is one. As the preview grows its merge takes longer, so the next is due no sooner than
  ``MERGE_SPACING`` (9) times the last merge's seconds after it, when that is longer: the merges take at most about a
  tenth of the run's time. A merge runs in a thread of its own (one at a time), so the pool
  goes on working meanwhile; at the run's end the merge in flight is waited for, then the last. The run's ``results`` artifact names it from then on. Like a batch run's, it is the
  authoritative output, HDF5 (no conversion).
- **The seal** (``should_stop``: every arrival handed out): every admitted frame is still indexed; the last shard is
  written and the preview published at once, then the Output XML (``output_xml``, a file name in the run's folder) is
  stitched from the shards' (``merge_xml_files``), once. Its problems are warnings, as a batch run's. With no frame
  indexed nothing is published (no empty ``output.h5``). The executor's live end rule decides the run's end.
- **A service stop** (``shutting_down``): no further frame starts; those waiting are not run (``interrupted``); the
  running ones finish and are published, and the XML stitched, as at the seal.
- **A run-level error** (the arrivals' ledger, a worker's error or a lost worker, a shard's or the preview's write) ends
  the run at once (Failed at once): the pool's workers are ended first and waited for
  (``jobs.processes.end_pool_workers``), the outcomes that came in meanwhile are taken, the frames still running are
  failed inputs in ``failures.jsonl`` (``interrupted``), the frames already indexed are written into a shard and
  published, as far as that can be done, and the error is raised; what was published stays, and no XML is stitched (as a
  batch run's at such an error). Unlike lauelab's batch scheduler (``iter_index``, so a batch LaueGo run), which records
  nothing for its running frames, a live run ends them at once and marks them ``interrupted``, as the live wire compute
  does.

No Resume. Like ``compute``, it never imports the database, the settings or a service (import-linter
rule 3), nor pydantic (rule 3b).
"""

from __future__ import annotations

import io
import math
import multiprocessing
import os
import tempfile
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, ThreadPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

# lauelab d5de751 writes a results file's geometry text with its layout's own dataset writer, which it does not
# export (lauelab._results_layout); _SessionResultsWriter writes the same dataset with it. Check it again on an upgrade.
from lauelab._results_layout import write_dataset
from lauelab.indexing import (
    EXPECTED_INPUT_ERRORS,
    Crystal,
    FrameResult,
    Geometry,
    Indexer,
    InputError,
    ResultsFileSummary,
    ResultsWriter,
    ScanFrame,
    WorkerError,
    load_crystal,
    load_mask,
    merge_results_files,
    merge_xml_files,
    validate_results_file,
)

from laueportal.core.provenance import library_provenance
from laueportal.indexing.lauego.compute import LAUEGO_KIND, IndexingSettings, settings_from_request, xml_destination
from laueportal.jobs.contract import RunHooks, RunOutcome, RunRequest, register_live_compute_function
from laueportal.jobs.processes import end_pool_workers
from laueportal.jobs.rundir import (
    CATEGORY_INTERRUPTED,
    RESULTS_FILENAME,
    SHARDS_DIRECTORY,
    FailureRecord,
    ManifestEntry,
    ManifestError,
    partial_path,
    publish_file,
    unchanged_since_admission,
)

ENGINE = "lauelab liblaue (live: a persistent pool of local worker processes, results in shards merged into a preview)"
POLL_SECONDS = 0.2
"""How long the loop waits for a worker's outcome (or sleeps, with none running) between its looks at the arrivals
(the hooks bound those to ``live.arrivals_seconds``), its shards' and its publication's clocks."""
SHARD_SECONDS = 1.0
"""The longest an indexed frame waits for its shard (less when ``publish_seconds`` is shorter)."""
MERGE_SPACING = 9.0
"""After a preview's merge, the next is due no sooner than this many times its seconds (when that is longer than
``publish_seconds``), so that the merges take at most about a tenth of the run's time as the preview grows."""
SHARD_PREFIX = "batch_"
XML_SHARD_FILENAME = "output.xml"
CHANGED_AFTER_ADMISSION = "Input changed after it was admitted"
CHANGED_WHILE_INDEXED = "Input changed while it was indexed"


def live_workers(cores: int | None, fallback_workers: int) -> int:
    """The pool's workers: one per core of the dispatcher's share (at least 1); without cores (a call outside the
    dispatcher's job process), ``fallback_workers`` (the execution policy's ``workers``)."""
    return max(1, fallback_workers if cores is None else cores)


@dataclass(frozen=True, eq=False)
class Calibration:
    """The calibration files the request names, as read once, when the job starts: the geometry file's bytes,
    the crystal and the mask. Every indexer of the session is built from them, never from the paths again."""

    geometry_file: str
    geometry: bytes
    crystal: Crystal
    mask: np.ndarray | None

    @classmethod
    def read(cls, settings: IndexingSettings) -> Calibration:
        return cls(
            geometry_file=settings.geometry_file,
            geometry=Path(settings.geometry_file).read_bytes(),
            crystal=load_crystal(settings.crystal_file),
            mask=load_mask(settings.mask_file) if settings.mask_file else None,
        )

    def geometry_text(self) -> str | None:
        """The geometry's text as lauelab's results writer embeds it (``Path.read_text``: the locale's encoding,
        universal newlines), from the bytes read at the start; None where that read fails (it then embeds none)."""
        try:
            return io.TextIOWrapper(io.BytesIO(self.geometry)).read()
        except UnicodeError:
            return None

    def indexer(self, settings: IndexingSettings) -> Indexer:
        """An indexer of ``settings`` on this calibration. lauelab reads a geometry only from a file, so the bytes go
        to a private file for its parsing, removed at once (as lauelab's own frozen geometries do); the geometry then
        names the file it was read from, which its results and XML record, as a batch run's."""
        suffix = Path(self.geometry_file).suffix or ".xml"
        with tempfile.TemporaryDirectory(prefix="laueportal-geometry-") as directory:
            copy = Path(directory) / f"geometry{suffix}"
            copy.write_bytes(self.geometry)
            try:
                geometry = Geometry(copy)
            except ValueError as error:
                raise ValueError(str(error).replace(os.fspath(copy), self.geometry_file)) from None
        geometry.path = Path(self.geometry_file)
        return Indexer(
            geometry,
            self.crystal,
            peak_params=settings.peak_params,
            index_params=settings.index_params,
            detector_index=settings.detector_index,
            cosmic_filter=settings.cosmic_filter,
        )


class _SessionResultsWriter(ResultsWriter):
    """lauelab's results writer, with the geometry text read at the job's start. lauelab d5de751's
    ``ResultsWriter._write_geometry`` reads ``geometry.path`` again whenever a file starts; this one writes the same
    group (its ``path`` attribute, the text as ``/geometry/xml``, none when it could not be read) from what was read
    then, so every shard and the merged preview embed one geometry. Check it again on an upgrade."""

    def __init__(self, path: Path, indexer: Indexer, geometry_text: str | None) -> None:
        super().__init__(
            path,
            crystal=indexer.crystal,
            geometry=indexer.geometry,
            peak_params=indexer.peak_params,
            index_params=indexer.index_params,
            detector_index=indexer.detector_index,
            detector_id=indexer.detector_id,
            cosmic_filter=indexer.cosmic_filter,
        )
        self._geometry_text = geometry_text

    def _write_geometry(self, target: Any) -> None:
        group = target.create_group("geometry")
        group.attrs["path"] = str(Path(self.geometry.path))
        if self._geometry_text is not None:
            write_dataset(target, "/geometry/xml", self._geometry_text)


# ---------------------------------------------------------------------------
# A pool worker: the calibration and settings given at its start, its indexer built at its first frame


_session: tuple[Calibration, IndexingSettings] | None = None
_indexer: Indexer | None = None

Indexed = tuple[FrameResult | None, BaseException | None, float]
"""A frame's outcome from a worker: its result, or its input error; and its seconds."""


class IndexerNotBuilt(RuntimeError):
    """A pool worker could not build its indexer: a run-level error that is no frame's (lauelab's batch scheduler:
    "indexing worker initialization failed")."""


def _start_worker(calibration: Calibration, settings: IndexingSettings) -> None:
    global _session, _indexer
    _session, _indexer = (calibration, settings), None


def _worker_indexer() -> Indexer:
    """The worker's indexer, built once, at its first frame, from the calibration read at the job's start."""
    global _indexer
    if _indexer is None:
        assert _session is not None, "a pool worker starts with the session's calibration"
        calibration, settings = _session
        _indexer = calibration.indexer(settings)
    return _indexer


def _index_frame(frame: str | ScanFrame, depth: float | None, identity: tuple[int, int, int, int] | None) -> Indexed:
    """Index ``frame`` (a raw frame's file, or a followed point's depth frame: a ``ScanFrame`` of its point file, as a
    batch run on a reconstruction indexes it), whose file was admitted with ``identity`` (``ManifestEntry.identity``):
    a file that changed after its admission, or while it was indexed, is a failed input."""
    try:
        indexer = _worker_indexer()
    except Exception as error:  # the run's: the worker cannot index anything
        raise IndexerNotBuilt(_described(error)) from error
    assert _session is not None
    source = frame.path if isinstance(frame, ScanFrame) else frame
    started = time.perf_counter()
    try:
        if not unchanged_since_admission(source, identity):
            raise InputError(CHANGED_AFTER_ADMISSION)
        result = indexer.index(frame, depth=depth, mask=_session[0].mask, keep_image=False)
        if not unchanged_since_admission(source, identity):
            raise InputError(CHANGED_WHILE_INDEXED)
    except EXPECTED_INPUT_ERRORS as error:
        return None, error, time.perf_counter() - started
    return result, None, time.perf_counter() - started


# ---------------------------------------------------------------------------
# The run


@dataclass
class _Frame:
    entry: ManifestEntry
    result: FrameResult | None = None
    done: bool = False
    """Its outcome is in: indexed, failed, or not run."""


@dataclass(frozen=True)
class _Shard:
    path: Path
    frame_ids: list[str]


class _LiveIndexing:
    """The pool, the shards and the publication (the module's docstring)."""

    def __init__(
        self,
        request: RunRequest,
        hooks: RunHooks,
        outcome: RunOutcome,
        *,
        settings: IndexingSettings,
        calibration: Calibration,
        indexer: Indexer,
        workers: int,
        publish_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.request = request
        self.clock = clock
        self.hooks = hooks
        self.outcome = outcome
        self.settings = settings
        self.calibration = calibration
        self.indexer = indexer
        self.geometry_text = calibration.geometry_text()
        self.workers = workers
        self.publish_seconds = publish_seconds
        self.shard_seconds = min(SHARD_SECONDS, publish_seconds)
        self.run_directory = Path(request.run_directory)
        self.results = self.run_directory / RESULTS_FILENAME
        self.follows = is_follower(request)
        self.point_inputs: dict[str, int] = {}  # a follower's: each point's inputs (its selected depths)
        self.point_out: dict[str, int] = {}  # and those whose outcome is in and released
        self.arrived: deque[_Frame] = deque()  # in arrival order, until released
        self.waiting: deque[_Frame] = deque()
        self.running: dict[Future[Indexed], _Frame] = {}
        self.released: list[_Frame] = []  # indexed, in arrival order, not in a shard yet
        self.released_at = 0.0
        self.shards: list[_Shard] = []
        self.unpublished: list[_Shard] = []  # written since the last publication
        self.published: list[str] = []  # the preview's frames
        self.published_at = -math.inf  # when the last merge ended
        self.publish_interval = publish_seconds  # the next merge is due so long after it
        self.merger: ThreadPoolExecutor | None = None
        self.merging: Future[tuple[ResultsFileSummary, float, float]] | None = None
        self.merging_shards: list[_Shard] = []
        self.merging_ids: list[str] = []
        self.summary: ResultsFileSummary | None = None
        self.xml_shards: list[Path] = []
        self.xml_error: str | None = None
        self.xml_steps = 0
        self.succeeded = 0
        self.failed = 0
        self.indexed = 0
        self.pool: ProcessPoolExecutor | None = None

    def run(self) -> bool:
        """Admit, index, shard and publish until the seal (or the service stop) and every running frame is in; then the
        last shard and publication. Returns whether the service stopped it."""
        self.pool = ProcessPoolExecutor(
            max_workers=self.workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_start_worker,
            initargs=(self.calibration, self.settings),
        )
        stopping = interrupted = False
        while True:
            if not stopping:
                for entry in self.hooks.arrivals():
                    frame = _Frame(entry)
                    self.arrived.append(frame)
                    self.waiting.append(frame)
                    if entry.point_id is not None:
                        self.point_inputs[entry.point_id] = self.point_inputs.get(entry.point_id, 0) + 1
                stopping = self.hooks.should_stop()
            if not interrupted and self.hooks.shutting_down():
                interrupted = True
                self._withdraw()
            if not interrupted:
                self._dispatch()
            if stopping and not self.running and not self.waiting:
                break
            self._collect()
            self._shard_if_due()
            self._publish_if_due()
        self.pool.shutdown(wait=True)
        self._release()
        self._shard()
        try:
            self._publish()
        finally:
            self._close_merger()
        return interrupted

    def abandon(self) -> None:
        """A run-level error's end (the module's docstring)."""
        if self.pool is not None:
            left = end_pool_workers(self.pool)  # first: no worker goes on once the run has failed
            if left:
                self.hooks.log(f"after a run-level error, {left} worker process(es) did not end, even after SIGKILL")
        for future in [future for future in self.running if future.done() and not future.cancelled()]:
            try:
                self._take(future)  # an outcome that came in before the workers were ended: kept
            except Exception as error:
                self.hooks.log(f"after a run-level error, an outcome could not be taken: {_described(error)}")
        for frame in self.running.values():  # dispatched, no outcome
            frame.done = True
            try:
                self.hooks.record_failure(
                    FailureRecord.not_run(
                        frame.entry,
                        category=CATEGORY_INTERRUPTED,
                        message="the run failed (a run-level error) while this input was being indexed",
                    )
                )
            except Exception as error:
                self.hooks.log(f"after a run-level error, a running input could not be marked: {_described(error)}")
        self.running.clear()
        try:
            self._merged(wait=True)  # a merge in flight: its shards go to the next one if it failed
        except Exception as error:
            self.hooks.log(f"after a run-level error, a preview being merged failed: {_described(error)}")
        try:
            self._release()
            self._shard()
            self._publish()
        except Exception as error:
            self.hooks.log(f"after a run-level error, the frames indexed could not be published: {_described(error)}")
        finally:
            self._close_merger()

    def stitch(self) -> None:
        """The Output XML, once, from the shards' (a batch run's rules: its problems are warnings)."""
        if not self.published:
            return
        if self.xml_error is not None:
            self.outcome.warnings.append(f"XML output incomplete: {self.xml_error}")
            return
        destination = xml_destination(self.request)
        try:
            merge_xml_files(self.xml_shards, destination, n_steps=len(self.published))
        except FileExistsError:
            self.outcome.warnings.append(f"XML not published: {destination} already exists")
            return
        except Exception as error:
            self.outcome.warnings.append(f"XML not published: {error}")
            return
        self.outcome.artifacts["xml"] = destination

    def _withdraw(self) -> None:
        """The service stops: the frames not started are not run."""
        while self.waiting:
            frame = self.waiting.popleft()
            frame.done = True
            self.hooks.record_failure(
                FailureRecord.not_run(
                    frame.entry, category=CATEGORY_INTERRUPTED, message="service stopped before this input"
                )
            )

    def _dispatch(self) -> None:
        assert self.pool is not None
        while self.waiting and len(self.running) < self.workers:
            frame = self.waiting.popleft()
            entry = frame.entry
            depth = entry.depth if entry.depth is not None else self.settings.depth_override
            self.running[self.pool.submit(_index_frame, _input_frame(entry), depth, entry.identity)] = frame

    def _collect(self) -> None:
        """Take the outcomes of the frames that ended, in dispatch order (``_take``); the first run-level error among
        them is raised once every other outcome of the same wait is in."""
        if not self.running:
            time.sleep(POLL_SECONDS)
            return
        done, _ = wait(self.running, timeout=POLL_SECONDS, return_when=FIRST_COMPLETED)
        error: Exception | None = None
        for future in [future for future in self.running if future in done]:
            taken = self._take(future)  # every outcome is taken; the first error is kept
            error = error or taken
        if error is not None:
            raise error

    def _take(self, future: Future[Indexed]) -> Exception | None:
        """Take the outcome of a frame that ended: indexed, or a failed input. A worker's error is a run-level error,
        returned: a lost worker (``BrokenProcessPool``) or an indexer the worker could not build is no frame's, and its
        frame stays running; any other is its frame's own, named in the error (as lauelab's batch ``WorkerError``) and
        the frame failed."""
        frame = self.running[future]
        try:
            result, failure, seconds = future.result()
        except BrokenProcessPool as error:
            return error
        except IndexerNotBuilt as error:
            return _worker_error(f"indexing worker initialization failed: {error}", error)
        except Exception as error:
            self._took(frame, future, None, error, None)
            return _worker_error(f"indexing worker failed on input {frame.entry.input_id}: {_described(error)}", error)
        self._took(frame, future, result, failure, seconds)
        return None

    def _took(
        self,
        frame: _Frame,
        future: Future[Indexed],
        result: FrameResult | None,
        failure: BaseException | None,
        seconds: float | None,
    ) -> None:
        del self.running[future]
        frame.done = True
        if failure is not None:
            self.failed += 1
            context = None if seconds is None else {"seconds": seconds}
            self.hooks.record_failure(FailureRecord.from_error(frame.entry, failure, context=context))
        else:
            assert result is not None
            frame.result = result
            self.succeeded += 1
            if result.n_patterns > 0:
                self.indexed += 1
        self.outcome.n_succeeded, self.outcome.n_failed, self.outcome.n_indexed = (
            self.succeeded,
            self.failed,
            self.indexed,
        )
        self.hooks.report_progress(succeeded=self.succeeded, failed=self.failed)

    def _release(self) -> None:
        """The frames whose outcome is in, in arrival order up to the first still out: the indexed ones to the next
        shard (a follower's: its point's)."""
        while self.arrived and self.arrived[0].done:
            frame = self.arrived.popleft()
            point = frame.entry.point_id
            if point is not None:
                self.point_out[point] = self.point_out.get(point, 0) + 1
            if frame.result is not None:
                if not self.released:
                    self.released_at = self.clock()
                self.released.append(frame)

    def _shard_if_due(self) -> None:
        self._release()
        if self.follows:
            self._shard_points(whole=True)
        elif self.released and (
            len(self.released) >= self.workers or self.clock() - self.released_at >= self.shard_seconds
        ):
            self._shard()

    def _shard(self) -> None:
        """Write what is released now (and at the run's end): raw frames into the next shard, ``batch_<n>``; a
        follower's point by point."""
        if self.follows:
            self._shard_points(whole=False)
        elif self.released:
            self._write_shard(self.released, f"{SHARD_PREFIX}{len(self.shards)}")
            self.released = []

    def _shard_points(self, *, whole: bool) -> None:
        """A follower's shards, ``shards/<point_id>/``: the released frames point by point (a point's inputs arrive
        together, so its frames are released together), each point's once the outcome of its every input is in
        (``whole``; at the run's end, whatever is released: a point a run-level error cut short too). A point with no
        frame indexed has none."""
        while self.released:
            point = self.released[0].entry.point_id
            count = next(
                (index for index, frame in enumerate(self.released) if frame.entry.point_id != point),
                len(self.released),
            )
            if point is None or (whole and self.point_out.get(point, 0) < self.point_inputs.get(point, 0)):
                return
            self._write_shard(self.released[:count], point)
            self.released = self.released[count:]

    def _write_shard(self, frames: list[_Frame], name: str) -> None:
        """Write ``frames`` into the shard ``shards/<name>``: ``output.h5`` written privately, validated and
        published, then their XML beside it."""
        folder = self.run_directory / SHARDS_DIRECTORY / name
        folder.mkdir(parents=True)
        final = folder / RESULTS_FILENAME
        partial = partial_path(final)
        frame_ids = [frame.entry.input_id for frame in frames]
        results = [frame.result for frame in frames if frame.result is not None]
        with _SessionResultsWriter(partial, self.indexer, self.geometry_text) as writer:
            for result, frame_id in zip(results, frame_ids, strict=True):
                writer.append(result, frame_id=frame_id)  # a failure here ends the run
        validate_results_file(partial, frame_ids=frame_ids)
        publish_file(partial, final)
        shard = _Shard(final, frame_ids)
        self.shards.append(shard)
        self.unpublished.append(shard)
        if self.xml_error is None:
            try:
                self.indexer.write_many_xml(results, folder / XML_SHARD_FILENAME)
                self.xml_shards.append(folder / XML_SHARD_FILENAME)
                self.xml_steps += len(results)
            except Exception as error:  # the XML document is auxiliary
                self.xml_error = _described(error)
                self.outcome.warnings.append(f"XML output stopped after {self.xml_steps} steps: {error}")

    def _publish_if_due(self) -> None:
        """Take a merge that ended; start the next once it is due: a shard written since, and ``publish_interval``
        passed since the last merge ended."""
        self._merged()
        if self.merging is None and self.unpublished and self.clock() - self.published_at >= self.publish_interval:
            self._merge()

    def _publish(self) -> None:
        """Publish now (the run's end): the merge in flight, then one of the shards written since."""
        self._merged(wait=True)
        if self.unpublished:
            self._merge()
            self._merged(wait=True)

    def _merge(self) -> None:
        """Start replacing the preview ``output.h5`` with the merge of the last one and the shards since, in the
        merger's thread, so the loop meanwhile goes on dispatching, collecting and sharding (the pool is not left
        idle). One merge at a time; only the loop's thread calls the hooks and changes the run's state."""
        sources = ([self.results] if self.published else []) + [shard.path for shard in self.unpublished]
        frame_ids = self.published + [frame_id for shard in self.unpublished for frame_id in shard.frame_ids]
        if self.merger is None:
            self.merger = ThreadPoolExecutor(max_workers=1, thread_name_prefix="laueportal-preview")
        self.merging = self.merger.submit(self._write_preview, sources, frame_ids)
        # handed over once the merge's thread has it: a submit that failed leaves them to the next merge
        self.merging_ids, self.merging_shards, self.unpublished = frame_ids, self.unpublished, []

    def _write_preview(self, sources: list[Path], frame_ids: list[str]) -> tuple[ResultsFileSummary, float, float]:
        """The merger's thread: the merge written privately, then published by a rename; with its seconds and its
        end."""
        started = self.clock()
        partial = partial_path(self.results)
        summary = merge_results_files(sources, partial, frame_ids=frame_ids, overwrite=True)
        publish_file(partial, self.results, overwrite=True)
        ended = self.clock()
        return summary, ended - started, ended

    def _merged(self, *, wait: bool = False) -> None:
        """Take the merge in flight once it has ended (``wait``: wait for it): the preview's frames, and the next
        merge due ``max(publish_seconds, MERGE_SPACING × its seconds)`` after it ended. A merge that failed raises its
        error (a run-level error), its shards given back to the next merge."""
        if self.merging is None or (not wait and not self.merging.done()):
            return
        merging, self.merging = self.merging, None
        try:
            summary, seconds, ended = merging.result()
        except BaseException:
            self.unpublished = self.merging_shards + self.unpublished
            raise
        self.summary = summary
        self.published = self.merging_ids
        self.published_at = ended
        self.publish_interval = max(self.publish_seconds, MERGE_SPACING * seconds)
        self.outcome.artifacts["results"] = os.fspath(self.results)

    def _close_merger(self) -> None:
        if self.merger is not None:
            self.merger.shutdown(wait=True)
            self.merger = None


def is_follower(request: RunRequest) -> bool:
    """Whether the live run follows a live wire run (``live.json``'s source ``run``): its inputs are the depth frames of
    the wire run's completed points."""
    return (request.live or {}).get("source") == "run"


def _input_frame(entry: ManifestEntry) -> str | ScanFrame:
    """What a worker indexes for ``entry``: a raw frame's file, or a followed point's depth frame (a ``ScanFrame`` of
    its point file, as the batch compute's ``_AdmittedEntries`` gives a reconstruction's frames)."""
    if entry.point_id is None:
        return entry.source
    if entry.depth_index is None:
        raise ManifestError(f"live input {entry.input_id!r} names point {entry.point_id!r} but no depth")
    return ScanFrame(entry.source, entry.point_id, entry.depth_index)


def _described(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _worker_error(message: str, cause: BaseException) -> WorkerError:
    error = WorkerError(message)
    error.__cause__ = cause
    return error


def _provenance(settings: IndexingSettings, indexer: Indexer, request: RunRequest, workers: int) -> dict[str, Any]:
    return {
        **library_provenance(ENGINE),
        "geometry_file": settings.geometry_file,
        "crystal_file": settings.crystal_file,
        "mask_file": settings.mask_file,
        "detector_index": indexer.detector_index,
        "detector_id": indexer.detector_id,
        "peak_params": asdict(indexer.peak_params),
        "index_params": asdict(indexer.index_params),
        "cosmic_filter": {"recorded": settings.cosmic_filter, "applied": False},
        "depth_override_um": settings.depth_override,
        "cores": request.cores,
        "workers": workers,
        "publish_seconds": request.publish_seconds,
        "output_layout": f"{SHARDS_DIRECTORY}/{'<point_id>' if is_follower(request) else SHARD_PREFIX + '<n>'}/"
        f"{RESULTS_FILENAME} merged into {RESULTS_FILENAME}, in arrival order",
    }


def compute_live_lauego_indexing(request: RunRequest, hooks: RunHooks) -> RunOutcome:
    """A live LaueGo run's compute, on raw frames or following a live wire run (the module's docstring)."""

    if request.publish_seconds is None:
        raise RuntimeError("a live run's request has no publish_seconds (its job process gives it)")
    settings = settings_from_request(request.parameters)
    calibration = Calibration.read(settings)  # once
    indexer = calibration.indexer(settings)  # lauelab validates the parameters, geometry and crystal here
    workers = live_workers(request.cores, request.workers)
    os.makedirs(request.run_directory, exist_ok=True)
    results = os.path.join(request.run_directory, RESULTS_FILENAME)
    if os.path.exists(results):
        raise FileExistsError(f"{results} already exists; a run never overwrites a previous run's results")
    outcome = RunOutcome(n_succeeded=0, n_failed=0, provenance=_provenance(settings, indexer, request, workers))
    hooks.report_outcome(outcome)
    live = _LiveIndexing(
        request,
        hooks,
        outcome,
        settings=settings,
        calibration=calibration,
        indexer=indexer,
        workers=workers,
        publish_seconds=request.publish_seconds,
    )
    try:
        interrupted = live.run()
    except BaseException:
        live.abandon()
        raise
    outcome.stopped = True  # sealed (or stopped by the service): no further arrival is admitted
    if interrupted:
        hooks.log("the service stopped the run: the frames not started were not run")
    outcome.provenance["n_shards"] = len(live.shards)
    if live.summary is None:
        outcome.summary = "no frame was processed"  # nothing published: no empty output.h5
        return outcome
    live.stitch()
    summary = live.summary
    outcome.provenance["results_summary"] = {
        "n_frames": summary.n_frames,
        "n_peaks": summary.n_peaks,
        "n_patterns": summary.n_patterns,
        "n_assignments": summary.n_assignments,
    }
    outcome.summary = (
        f"{summary.n_frames} frames, {summary.n_patterns} patterns, {summary.n_peaks} peaks in {RESULTS_FILENAME}"
        + (f"; {live.indexed} frames indexed" if live.indexed else "; no frame indexed")
    )
    return outcome


register_live_compute_function(LAUEGO_KIND, compute_live_lauego_indexing)
