"""Live runs for the tests: a live wire run (or a live LaueGo run on raw frames) created and queued as the Live page's
API does (``jobs.creation.create_live_run``), its folder's files, the executor's live hooks in this process, and a
sampler of its counters."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from laueportal.core.settings import Settings
from laueportal.indexing.lauego.live_params import LaueGoLiveParamsV1
from laueportal.jobs import executor, programs
from laueportal.jobs.creation import create_live_run
from laueportal.jobs.dispatch import claim_run
from laueportal.jobs.executor import LiveExecutorHooks
from laueportal.jobs.lifecycle import ProgressRecorder
from laueportal.jobs.live import LiveDaq, LiveDocument, LiveFollows, LiveSource
from laueportal.jobs.models import Job
from laueportal.jobs.queue import RunPolicy
from laueportal.jobs.rundir import FailureReportWriter
from laueportal.reconstruction.wire.live_params import WireLiveParamsV1
from laueportal.scans.models import Metadata
from tests.conftest import create_test_metadata
from tests.jobs.job_computes import LIVE_PROCESSED
from tests.settings_support import make_settings


def live_settings(tmp_path: Path, database: Path | None = None) -> Settings:
    """Test settings whose one root is ``tmp_path`` (a live run's output folder must lie inside a root)."""
    values: dict[str, Any] = {"files": {"runs_root": tmp_path / "runs", "roots": {"tmp": tmp_path}}}
    if database is not None:
        values["database"] = {"path": database}
    return make_settings(tmp_path, **values)


@dataclass
class LiveRun:
    """A created live run: its IDs, its folder and its run directory."""

    job_id: int
    run_id: int
    folder: Path
    run_directory: Path
    name: str

    def file(self, number: int, content: bytes = b"") -> Path:
        """Write the folder's file number ``number`` (``<name>_<number>.h5``) and return its path."""
        path = self.folder / f"{self.name}_{number}.h5"
        path.write_bytes(content)
        return path

    def processed(self) -> list[str]:
        """The stand-in live compute's processed inputs (``job_computes.LIVE_PROCESSED``), in order."""
        path = self.run_directory / LIVE_PROCESSED
        return path.read_text().splitlines() if path.exists() else []


def live_wire_params(folder: Path, output: Path, name: str, **values: Any) -> WireLiveParamsV1:
    return WireLiveParamsV1(
        **{
            "scan_number": 1,
            "input_path": os.fspath(folder),
            "output_path_template": os.fspath(output),
            "filename_prefixes": [f"{name}_%d.h5"],
            "geometry_file": "/config/geometry.xml",
            "percent_brightest": 5.0,
            "wire_edges": "leading",
            "depth_start": -10.0,
            "depth_end": 10.0,
            "depth_resolution": 0.5,
            "num_threads": 1,
            "memory_limit_mb": 256,
            "verbose": 0,
            "computer_name": "test-host",
            "submitted_at": datetime(2026, 10, 26, 10, 0, 0, tzinfo=UTC),
            **values,
        }
    )


def live_document(folder: Path, name: str, source: LiveSource = "notify", **values: Any) -> LiveDocument:
    daq = LiveDaq(id="1a2b", experiment="e1", storage=os.fspath(folder.parent), destination=os.fspath(folder.parent))
    return LiveDocument(
        **{
            "source": source,
            "daq": daq if source == "daq" else None,
            "folder": os.fspath(folder),
            "filename_template": f"{name}_%d.h5",
            **values,
        }
    )


def create_live_wire_run(
    engine: Engine,
    tmp_path: Path,
    *,
    name: str = "live",
    source: LiveSource = "notify",
    settings: Settings | None = None,
    params: dict[str, Any] | None = None,
    template: bool = True,
) -> LiveRun:
    """A live wire run on ``tmp_path/<name>-input`` (made unless it exists; files ``<name>_%d.h5``), created and queued
    (``create_live_run``), whose source is ``source``; ``params``: its parameters' other values
    (``live_wire_params``); ``template`` False: no file name template (every file whose name ends ``.h5``)."""
    with Session(engine) as session:
        if session.get(Metadata, 1) is None:
            session.add(create_test_metadata(1))
            session.commit()
    folder = tmp_path / f"{name}-input"
    folder.mkdir(exist_ok=True)
    untemplated: dict[str, Any] = {} if template else {"filename_prefixes": []}
    wire_params = live_wire_params(folder, tmp_path / f"{name}-run_%d", name, **untemplated, **(params or {}))
    document = live_document(folder, name, source, **({} if template else {"filename_template": None}))
    created = create_live_run(wire_params, document, settings=settings or live_settings(tmp_path), engine=engine)
    assert created.failure is None, created.failure
    with Session(engine) as session:
        run_directory = session.scalar(select(Job.run_directory).where(Job.job_id == created.job_id))
    assert run_directory is not None
    return LiveRun(created.job_id, created.run.id, folder, Path(run_directory), name)


def lauego_live_params(settings: Settings, **values: Any) -> LaueGoLiveParamsV1:
    """A live LaueGo run's parameters from the configured defaults, as the Live page's form gives them."""
    defaults = {
        name: value for name, value in programs.form_defaults("lauego", settings).items() if name != "scan_points"
    }
    form = {
        **defaults,
        "scan_number": 1,
        "reconstruction_id": None,
        "geometry_file": "/config/geometry.xml",
        "crystal_file": "/config/Ni.xml",
        **values,
    }
    return LaueGoLiveParamsV1.from_form(form, fixed=programs.fixed_values("lauego", settings))


def create_live_lauego_run(
    engine: Engine,
    tmp_path: Path,
    *,
    name: str = "frame",
    settings: Settings | None = None,
    params: dict[str, Any] | None = None,
) -> LiveRun:
    """A live LaueGo run on the raw frames of ``tmp_path/<name>-input`` (made unless it exists; files ``<name>_%d.h5``)
    whose source is ``notify``, created and queued (``create_live_run``); ``params``: its parameters' other values
    (``lauego_live_params``: the configured defaults otherwise)."""
    settings = settings or live_settings(tmp_path)
    with Session(engine) as session:
        if session.get(Metadata, 1) is None:
            session.add(create_test_metadata(1))
            session.commit()
    folder = tmp_path / f"{name}-input"
    folder.mkdir(exist_ok=True)
    lauego_params = lauego_live_params(
        settings,
        input_path=os.fspath(folder),
        output_path_template=os.fspath(tmp_path / f"{name}-run_%d"),
        filename_prefixes=[f"{name}_%d.h5"],
        **(params or {}),
    )
    created = create_live_run(lauego_params, live_document(folder, name), settings=settings, engine=engine)
    assert created.failure is None, created.failure
    with Session(engine) as session:
        run_directory = session.scalar(select(Job.run_directory).where(Job.job_id == created.job_id))
    assert run_directory is not None
    return LiveRun(created.job_id, created.run.id, folder, Path(run_directory), name)


def create_live_follower(
    engine: Engine,
    tmp_path: Path,
    wire: LiveRun,
    *,
    settings: Settings | None = None,
    params: dict[str, Any] | None = None,
) -> int:
    """A live LaueGo run following the live wire run ``wire`` (the session's second run: its ``input_path`` the wire
    run's catalog, its ``reconstruction_id`` the wire run), created and queued; its job ID. ``params``: its
    parameters' other values (``lauego_live_params``: the configured defaults and ``depth_range`` 0-2 otherwise)."""
    settings = settings or live_settings(tmp_path)
    params = lauego_live_params(
        settings,
        input_path=os.fspath(wire.run_directory / "reconstruction" / "scan.h5"),
        reconstruction_id=wire.run_id,
        output_path_template=os.fspath(tmp_path / f"follow-{wire.name}_%d"),
        filename_prefixes=[f"{wire.name}_%d.h5"],
        **{"depth_range": "0-2", **(params or {})},
    )
    follows = LiveFollows(job_id=wire.job_id, directory=os.fspath(wire.run_directory))
    document = live_document(wire.folder, wire.name, "run", follows=follows)
    created = create_live_run(params, document, settings=settings, engine=engine)
    assert created.failure is None, created.failure
    return created.job_id


def live_hooks(
    engine: Engine, job_id: int, *, interval: float = 0.0, clock: Callable[[], float] = time.monotonic
) -> tuple[LiveExecutorHooks, SimpleNamespace]:
    """The executor's hooks of the live run ``job_id``, claimed (Running), in this process, and a stand-in for its
    monitor: the test sets its ``stop_requested`` and ``stop_reason`` as the monitor would once it reads the flag.
    ``interval`` 0: every call looks at ``arrivals.jsonl`` (the rate's own test sets one, and ``clock``)."""
    assert claim_run(engine, job_id)
    request, skipped = executor._start(engine, job_id, RunPolicy())
    assert request is not None and request.live is not None, skipped
    monitor = SimpleNamespace(stop_requested=False, stop_reason=None)
    recorder = ProgressRecorder(engine, job_id, 60)
    failures = FailureReportWriter(os.path.join(request.run_directory, "failures.jsonl"))
    hooks = LiveExecutorHooks(
        job_id,
        monitor,  # type: ignore[arg-type]
        recorder,
        failures,
        engine=engine,
        request=request,
        interval=interval,
        clock=clock,
    )
    return hooks, monitor


@dataclass
class CounterSampler:
    """Reads a live run's counters every 10 ms in a thread of its own until ``stop``: ``samples`` holds each
    ``(n_inputs, n_succeeded, n_failed, n_not_run)`` read, and ``handed`` each ``(processed, n_inputs)``: the inputs
    the stand-in live compute had processed (``LiveRun.processed``, read first), then the row's count."""

    engine: Engine
    run: LiveRun
    samples: list[tuple[int, int, int, int]] = field(default_factory=list)
    handed: list[tuple[int, int]] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start(self) -> CounterSampler:
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._thread.start()
        return self

    def _sample(self) -> None:
        while not self._stop.is_set():
            processed = len(self.run.processed())
            with Session(self.engine) as session:
                job = session.get(Job, self.run.job_id)
                if job is not None:
                    self.samples.append((job.n_inputs, job.n_succeeded, job.n_failed, job.n_not_run))
                    self.handed.append((processed, job.n_inputs))
            self._stop.wait(0.01)

    def stop(self) -> list[tuple[int, int, int, int]]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(10)
        return self.samples

    def assert_bounded_and_growing(self, worker_log: str) -> None:
        """``counters_bounded`` in every sample, and ``n_inputs`` never smaller than before. The database refuses a
        write that would break the first (``state.record_progress``, backed by its CHECK), so that alone cannot fail:
        what shows an input processed before it was counted is the compute's output, read before the row (every input
        it processed was counted by then: ``processed <= n_inputs``), and the refused progress write the job process
        logs and goes on from (``worker_log``, the worker's and its job processes' output: no "progress write
        failed")."""
        samples = self.stop()
        assert samples
        for inputs, succeeded, failed, not_run in samples:
            assert succeeded + failed + not_run <= inputs, samples
        counts = [sample[0] for sample in samples]
        assert counts == sorted(counts), counts
        assert all(processed <= inputs for processed, inputs in self.handed), self.handed
        assert "progress write failed" not in worker_log, worker_log
