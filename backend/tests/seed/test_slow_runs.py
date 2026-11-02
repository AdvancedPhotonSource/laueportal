"""``laueportal seed slow-runs`` (``seed.slow``): long demo indexing runs queued for the run monitor's live test, on
the job table (the queue). Nothing is computed here (no worker takes them); the demo database is a
stand-in: migrated, scan 990001, its runs root marked as its own and four staged frames."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from laueportal.core import migrations
from laueportal.core.db import database_engine
from laueportal.core.settings import Settings
from laueportal.indexing.models import IndexingRun
from laueportal.jobs.models import Job
from laueportal.jobs.state import JobStatus, RunPhase
from laueportal.seed import build, demo, slow
from tests.run_support import dispatch_queue
from tests.settings_support import make_settings


def demo_settings(tmp_path: Path) -> Settings:
    files = {"runs_root": tmp_path / "runs", "roots": {"runs": tmp_path / "runs"}}
    return make_settings(tmp_path, database={"path": tmp_path / "demo.db"}, files=files)


def queued(settings: Settings) -> list[tuple[str, int]]:
    engine = database_engine(settings.database)
    try:
        return dispatch_queue(engine)
    finally:
        engine.dispose()


def layout_of(settings: Settings) -> demo.DemoLayout:
    runs = settings.files.runs_root
    return demo.DemoLayout(data=demo.default_data_directory(), inputs=runs / build.INPUTS, runs_root=runs)


def make_demo(settings: Settings, *, scan: bool = True, marker: bool = True, frames: int = 4) -> None:
    """What ``laueportal seed demo`` leaves that the slow runs need (not its runs)."""
    migrations.upgrade(settings.database)
    layout = layout_of(settings)
    layout.runs_root.mkdir(parents=True)
    if marker:
        (layout.runs_root / build.MARKER).write_text(json.dumps({"database": str(settings.database.path.resolve())}))
    if scan:
        engine = database_engine(settings.database)
        demo.add_demo_scan(engine, layout)
        engine.dispose()
    layout.indexing_inputs.mkdir(parents=True)
    for number in range(1, frames + 1):
        (layout.indexing_inputs / f"frame_{number}.h5").write_bytes(f"frame {number}".encode())


def jobs(settings: Settings) -> list[tuple[int, int, str | None, int, int | None, str | None]]:
    engine = database_engine(settings.database)
    try:
        with Session(engine) as session:
            return [
                (job.job_id, job.status, job.phase, job.n_inputs, run.scan_number, run.notes)
                for job, run in session.query(Job, IndexingRun).join(IndexingRun, IndexingRun.job_id == Job.job_id)
            ]
    finally:
        engine.dispose()


def test_slow_runs_are_created_and_queued_in_order_on_the_demo_database(tmp_path: Path) -> None:
    settings = demo_settings(tmp_path)
    make_demo(settings)

    result = slow.queue_slow_runs(settings, count=2, n_inputs=10)

    assert result.database == settings.database.path.resolve()
    first, second = result.runs
    assert jobs(settings) == [
        (first.job_id, JobStatus.QUEUED, RunPhase.QUEUED, 10, demo.SCAN_NUMBER, slow.SLOW_NOTES),
        (second.job_id, JobStatus.QUEUED, RunPhase.QUEUED, 10, demo.SCAN_NUMBER, slow.SLOW_NOTES),
    ]
    assert queued(settings) == [("run", first.job_id), ("run", second.job_id)]

    # The frames: links to the demo's four staged frames in turn.
    layout = layout_of(settings)
    frames = layout.inputs / slow.SLOW_INPUT_DIR
    assert sorted(path.name for path in frames.iterdir()) == sorted(f"frame_{n}.h5" for n in range(1, 11))
    for number in (1, 5, 9):
        assert (frames / f"frame_{number}.h5").samefile(layout.indexing_inputs / "frame_1.h5")
    assert (frames / "frame_8.h5").samefile(layout.indexing_inputs / "frame_4.h5")

    # Again, with more frames: the existing ones are kept, the new run is queued behind the others.
    (third,) = slow.queue_slow_runs(settings, count=1, n_inputs=12).runs
    assert len(list(frames.iterdir())) == 12
    assert queued(settings)[-1] == ("run", third.job_id)


@pytest.mark.parametrize("missing", ["database", "marker", "scan", "frames"])
def test_nothing_is_queued_without_the_demo_database(tmp_path: Path, missing: str) -> None:
    settings = demo_settings(tmp_path)
    if missing != "database":
        make_demo(settings, marker=missing != "marker", scan=missing != "scan", frames=3 if missing == "frames" else 4)

    with pytest.raises(slow.SlowRunsError) as error:
        slow.queue_slow_runs(settings, count=1, n_inputs=5)

    reason = {
        "database": "does not exist",
        "marker": "is not the demo database",
        "scan": "has no scan 990001",
        "frames": "frame_4.h5 is missing",
    }[missing]
    assert reason in str(error.value)
    if missing != "database":
        assert jobs(settings) == [] and queued(settings) == []
        assert not (layout_of(settings).inputs / slow.SLOW_INPUT_DIR).exists()


@pytest.mark.parametrize(("count", "n_inputs"), [(0, 5), (21, 5), (1, 0), (1, 10_001)])
def test_counts_out_of_range_are_refused(tmp_path: Path, count: int, n_inputs: int) -> None:
    with pytest.raises(slow.SlowRunsError, match="must be 1 to"):
        slow.queue_slow_runs(demo_settings(tmp_path), count=count, n_inputs=n_inputs)
