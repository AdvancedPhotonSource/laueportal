"""``seed.build`` without native compute: the checks that run before anything changes, emptying a database in
place, and the runs root's ownership (``laueportal seed demo``; the full build is ``test_seed_demo.py``)."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from laueportal.core import migrations
from laueportal.core.settings import DatabaseSettings, Settings
from laueportal.indexing import conversion
from laueportal.indexing.models import Conversion
from laueportal.jobs import dispatch, worker
from laueportal.seed import build, demo
from tests.indexing.test_viewer import add_run
from tests.run_support import enqueue_with, publish_wire_run
from tests.settings_support import make_database, make_settings


def demo_settings(tmp_path: Path, **sections) -> Settings:
    """Settings for a demo in ``tmp_path``: database ``demo.db``, runs root ``runs``."""
    files = {"runs_root": tmp_path / "runs", "roots": {"runs": tmp_path / "runs"}}
    return make_settings(tmp_path, database={"path": tmp_path / "demo.db"}, files=files, **sections)


def stand_in_data(directory: Path) -> Path:
    """Empty files at every committed path the demo reads (enough for the checks; nothing is computed)."""
    for name in demo.DATA_FILES:
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(b"")
    return directory


def snapshot(directory: Path) -> list[str]:
    return sorted(str(path.relative_to(directory)) for path in directory.rglob("*"))


# ---------------------------------------------------------------------------
# Emptying a database in place


def test_empty_database_keeps_the_file_and_other_connections_see_the_empty_schema(tmp_path: Path) -> None:
    path = tmp_path / "demo.db"
    database = DatabaseSettings(path=path)
    migrations.upgrade(database)
    with sqlite3.connect(path) as setup:
        setup.execute("CREATE VIEW recent AS SELECT * FROM job")
        setup.execute("CREATE TRIGGER touch AFTER INSERT ON job BEGIN SELECT 1; END")
    setup.close()
    other = sqlite3.connect(path)  # e.g. the API's pooled connection
    assert other.execute("SELECT count(*) FROM job").fetchone() == (0,)
    inode = path.stat().st_ino

    build.empty_database(path)

    assert path.stat().st_ino == inode
    remaining = other.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    assert remaining == []
    assert migrations.current_revision(database) is None
    assert migrations.upgrade(database) == (None, migrations.head_revision())  # built again from scratch
    assert other.execute("SELECT count(*) FROM job").fetchone() == (0,)
    other.close()


# ---------------------------------------------------------------------------
# The runs root


@pytest.mark.parametrize("layout", ["database inside", "inside the database's path"])
def test_a_runs_root_that_overlaps_the_database_is_refused(tmp_path: Path, layout: str) -> None:
    runs, database = tmp_path / "runs", tmp_path / "demo.db"
    if layout == "database inside":
        database = runs / "demo.db"
    else:
        runs = database / "runs"
    with pytest.raises(build.SeedError, match="overlap"):
        build._check_runs_root(runs, database)


def test_a_runs_root_with_files_the_demo_did_not_make_is_refused(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    (runs / "analysis" / "rec_3").mkdir(parents=True)
    build._check_runs_root(runs, tmp_path / "demo.db")  # empty directories are fine
    (runs / "analysis" / "rec_3" / "request.json").write_text("{}")
    with pytest.raises(build.SeedError, match="did not make"):
        build._check_runs_root(runs, tmp_path / "demo.db")


def test_a_runs_root_belongs_to_the_database_that_was_seeded_into_it(tmp_path: Path) -> None:
    runs, mine, other = tmp_path / "runs", tmp_path / "demo.db", tmp_path / "other.db"
    (runs / "scan_990001" / "rec_1").mkdir(parents=True)
    (runs / "scan_990001" / "rec_1" / "request.json").write_text("{}")
    (runs / build.MARKER).write_text(json.dumps({"database": str(mine)}))

    build._check_runs_root(runs, mine)
    with pytest.raises(build.SeedError, match="belongs to the demo database"):
        build._check_runs_root(runs, other)

    build._reset_runs_root(runs, mine)
    assert snapshot(runs) == [build.MARKER]
    assert json.loads((runs / build.MARKER).read_text())["database"] == str(mine)


# ---------------------------------------------------------------------------
# Checks before anything changes


def test_missing_data_files_are_named(tmp_path: Path) -> None:
    data = stand_in_data(tmp_path / "data")
    (data / demo.FIXTURES / "synthetic_ni_grain_b.h5").unlink()
    with pytest.raises(build.SeedError, match=r"1 file\(s\) of the demo's data are missing, e.g. .*grain_b.h5"):
        build.build_demo(demo_settings(tmp_path), data=data, report=lambda _line: None)
    assert not (tmp_path / "demo.db").exists() and not (tmp_path / "runs").exists()


def test_the_demo_reads_this_checkouts_committed_data() -> None:
    assert demo.DemoLayout(demo.default_data_directory(), Path("inputs"), Path("runs")).missing_data() == []


def test_the_crux_run_is_refused_while_remote_execution_is_enabled(tmp_path: Path) -> None:
    """R4 has no DM job behind it, so ``--crux-run`` never seeds a database whose settings would have the remote poller
    ask DM about it; nothing is written."""
    settings = demo_settings(tmp_path, remote={"enabled": True, "settings_file": tmp_path / "bdp.yaml"})
    with pytest.raises(build.SeedError, match=r"--crux-run is refused while remote execution is enabled"):
        build.build_demo(settings, crux_run=True, report=lambda _line: None)
    assert list(tmp_path.iterdir()) == []  # no database, no runs root


def test_an_existing_database_is_kept_without_force(tmp_path: Path) -> None:
    (tmp_path / "demo.db").write_bytes(b"precious")
    with pytest.raises(build.SeedError, match="exists; pass --force"):
        build.build_demo(demo_settings(tmp_path), report=lambda _line: None)
    assert (tmp_path / "demo.db").read_bytes() == b"precious"


# ---------------------------------------------------------------------------
# Under a running worker


def test_a_running_conversion_s_live_job_process_keeps_the_database(tmp_path: Path) -> None:
    """The check before anything changes counts a running conversion's live job process, as a Running run's."""
    _, engine = make_database(tmp_path, database={"path": tmp_path / "demo.db"})
    try:
        add_run(engine, 101, None)
        conversion.enqueue_conversion(101, sessions=engine)
        assert dispatch.claim_conversion(engine, 101)
        assert dispatch.record_process(engine, dispatch.CONVERT, 101, dispatch.process_record(os.getpid()))

        with pytest.raises(build.SeedError, match=rf"has job processes running \(\[{os.getpid()}\]\)"):
            build.build_demo(demo_settings(tmp_path), force=True, report=lambda _line: None)

        with Session(engine) as session:
            row = session.get(Conversion, 101)
            assert row is not None and row.status == dispatch.CONVERSION_RUNNING
    finally:
        engine.dispose()


@pytest.mark.parametrize("lets_go", [False, True], ids=["holds-the-lock", "lets-the-lock-go"])
def test_a_worker_that_leaves_r1_and_i1_queued_is_named_and_a_burst_worker_runs_them_only_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lets_go: bool
) -> None:
    """The worker holding the database's lock must start R1 and I1 within ``WORKER_START_SECONDS``: past that, the
    seed fails and names it (a burst worker would only exit 1 at the lock); a burst worker runs them only once no
    worker holds the lock."""
    monkeypatch.setattr(build, "WORKER_START_SECONDS", 0.4)
    monkeypatch.setattr(build, "POLL_SECONDS", 0.02)
    bursts: list[demo.DemoRuns] = []
    monkeypatch.setattr(build, "_burst_worker", lambda runs, **_: bursts.append(runs) or "a burst worker")
    settings, engine = make_database(tmp_path)
    try:
        job_ids = []
        for name in ("r1", "i1"):
            run = publish_wire_run(engine, tmp_path, name=name)
            enqueue_with(engine, run.job_id)
            job_ids.append(run.job_id)
        runs = demo.DemoRuns(1, job_ids[0], 1, job_ids[1])
        worker.start_row(engine)  # this process, as the worker's row names it
        held = worker.take_lock(worker.lock_path(settings.database.path))
        assert held is not None
        timer = threading.Timer(0.1, held.close) if lets_go else None
        try:
            if timer is not None:
                timer.start()
                assert build._run_to_finish(runs, engine=engine, database=settings.database.path, report=print) == (
                    "a burst worker"
                )
                assert bursts == [runs]
            else:
                with pytest.raises(
                    build.SeedError, match=rf"the worker \(pid {os.getpid()} on .*did not start R1 and I1"
                ):
                    build._run_to_finish(runs, engine=engine, database=settings.database.path, report=print)
                assert bursts == []
        finally:
            if timer is not None:
                timer.join()
            held.close()
    finally:
        engine.dispose()
