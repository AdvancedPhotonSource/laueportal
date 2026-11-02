"""``laueportal seed slow-runs``: queue long LaueGo indexing runs on the demo database, for trying the run monitor's
controls by hand (queue a long demo job, Move to Front, then Stop).

Each run is created by ``indexing.service.create_indexing`` (scan 990001, I1's settings, ``SLOW_NOTES``) and handed to
the queue by ``jobs.queue.enqueue_run``, in order; the stack's worker then runs them like any run (one at a time), and
the run monitor's Stop and Move to Front act on them through the API. Nothing about a run's states differs.

A run indexes ``n_inputs`` frames: ``<runs_root>/inputs/slow-indexing/frame_<n>.h5``, hard links to the demo's four
staged frames in turn (``seed.demo.stage_inputs``: ``synthetic-indexing/frame_1.h5`` to ``frame_4.h5``, which
``laueportal seed demo`` stages), made once. A frame takes about a quarter of a second on the development machine
(one indexing process, ``execution.workers = 1``), so 1000 frames keep a run busy for about four minutes; a stop ends
it within seconds (the frames in flight finish).

It needs the demo database (``laueportal seed demo``): the configured database with scan 990001, its runs root
(marked as that database's) and its staged frames. Nothing is created when a check fails.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from laueportal.core.db import database_engine
from laueportal.core.settings import Settings
from laueportal.indexing import service as indexing_service
from laueportal.jobs.queue import enqueue_run
from laueportal.scans.models import Metadata
from laueportal.seed import demo
from laueportal.seed.build import INPUTS, marker_database

SLOW_INPUT_DIR = "slow-indexing"
"""Under the staged inputs (``<runs_root>/inputs``): the slow runs' frames."""
SLOW_NOTES = (
    "Slow demo run, queued by `laueportal seed slow-runs` for the run monitor's live test: copies of the four "
    "synthetic demo frames, indexed with I1's settings."
)
DEFAULT_COUNT = 2
DEFAULT_INPUTS = 1000
MAX_COUNT = 20
MAX_INPUTS = 10_000


class SlowRunsError(RuntimeError):
    """The runs could not be queued. The message says why and what to do; nothing was created."""


@dataclass(frozen=True)
class SlowRun:
    job_id: int
    indexing_id: int


@dataclass(frozen=True)
class SlowRuns:
    database: Path
    runs: tuple[SlowRun, ...]
    """In the order they were queued (the worker takes the first one first)."""


def stage_slow_inputs(layout: demo.DemoLayout, n_inputs: int) -> Path:
    """``n_inputs`` frames in ``<inputs>/slow-indexing`` (``frame_1.h5`` …), hard links to the demo's staged
    frames in turn (copies where links are refused); those already there are kept. Returns the directory."""

    directory = layout.inputs / SLOW_INPUT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    for number in range(1, n_inputs + 1):
        destination = directory / f"frame_{number}.h5"
        if destination.exists():
            continue
        source = layout.indexing_inputs / f"frame_{(number - 1) % len(demo.FRAMES) + 1}.h5"
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)
    return directory


def _check(settings: Settings, layout: demo.DemoLayout, database: Path) -> None:
    """Refuse anything but the demo database with its runs root and staged frames."""

    if not database.is_file():
        raise SlowRunsError(f"the database {database} does not exist; build the demo first (laueportal seed demo)")
    owner = marker_database(layout.runs_root)
    if owner != str(database):
        raise SlowRunsError(
            f"the runs root {layout.runs_root} is not the demo database {database}'s (laueportal seed demo makes it)"
        )
    missing = [
        path
        for path in (layout.indexing_inputs / f"frame_{number}.h5" for number in range(1, len(demo.FRAMES) + 1))
        if not path.is_file()
    ]
    if missing:
        raise SlowRunsError(
            f"the demo's staged frame {missing[0]} is missing; build the demo again (seed demo --force)"
        )
    engine = database_engine(settings.database, create=False)
    try:
        with Session(engine) as session:
            if session.get(Metadata, demo.SCAN_NUMBER) is None:
                raise SlowRunsError(f"{database} has no scan {demo.SCAN_NUMBER}: it is not the demo database")
    finally:
        engine.dispose()


def queue_slow_runs(settings: Settings, *, count: int = DEFAULT_COUNT, n_inputs: int = DEFAULT_INPUTS) -> SlowRuns:
    """Create ``count`` LaueGo indexing runs of scan 990001 with ``n_inputs`` frames each on the configured (demo)
    database and queue them in that order (the job table is the queue). Raises ``SlowRunsError`` before creating
    anything when the database, its runs root or its staged frames are not there."""

    if not 1 <= count <= MAX_COUNT:
        raise SlowRunsError(f"count must be 1 to {MAX_COUNT}")
    if not 1 <= n_inputs <= MAX_INPUTS:
        raise SlowRunsError(f"inputs must be 1 to {MAX_INPUTS}")
    database = settings.database.path.expanduser().resolve()
    runs_root = settings.files.runs_root.expanduser().resolve()
    layout = demo.DemoLayout(data=demo.default_data_directory(), inputs=runs_root / INPUTS, runs_root=runs_root)
    _check(settings, layout, database)

    directory = stage_slow_inputs(layout, n_inputs)
    engine = database_engine(settings.database, create=False)
    runs: list[SlowRun] = []
    try:
        for _ in range(count):
            request = demo.indexing_request(layout).with_values(
                input_path=str(directory),
                scan_points=f"1-{n_inputs}",
                notes=SLOW_NOTES,
            )
            run = indexing_service.create_indexing(request, engine=engine, settings=settings)
            enqueue_run(run.job_id, engine=engine)
            runs.append(SlowRun(job_id=run.job_id, indexing_id=run.id))
    finally:
        engine.dispose()
    return SlowRuns(database=database, runs=tuple(runs))
