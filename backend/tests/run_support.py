"""Shared helpers for whole-run queue tests: a published run in a temporary database, queued or claimed."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from laueportal.jobs.models import Job
from laueportal.jobs.queue import RunPolicy
from laueportal.reconstruction import service as reconstruction_service
from laueportal.reconstruction.wire.params import WireParamsV1
from laueportal.scans.models import Metadata
from tests.conftest import create_test_metadata
from tests.settings_support import make_database

FAST_POLICY = RunPolicy(
    job_timeout_seconds=600,
    heartbeat_seconds=0.05,
    progress_seconds=0.05,
    cancel_poll_seconds=0.02,
    stale_heartbeat_seconds=1,
    shutdown_grace_seconds=5,
    workers=1,
)


def publish_wire_run(
    engine, tmp_path, *, count: int = 3, name: str = "wire", scan_number: int = 1, notes: str | None = None
):
    """Create a published wire reconstruction run with ``count`` fake input files."""

    with Session(engine) as session:
        if session.get(Metadata, scan_number) is None:
            session.add(create_test_metadata(scan_number))
            session.commit()
    input_dir = tmp_path / f"{name}-input"
    input_dir.mkdir(exist_ok=True)
    for index in range(1, count + 1):
        (input_dir / f"{name}_{index}.h5").write_text("")
    request = WireParamsV1(
        scan_number=scan_number,
        input_path=os.fspath(input_dir),
        output_path_template=os.fspath(tmp_path / f"{name}-run_%d"),
        filename_prefixes=[f"{name}_%d.h5"],
        geometry_file="/config/geometry.xml",
        percent_brightest=5.0,
        wire_edges="leading",
        depth_start=-10.0,
        depth_end=10.0,
        depth_resolution=0.5,
        num_threads=1,
        memory_limit_mb=256,
        scan_points=f"1-{count}" if count > 1 else "1",
        verbose=0,
        notes=notes,
        computer_name="test-host",
        submitted_at=datetime(2026, 9, 16, 10, 0, 0, tzinfo=UTC),
    )
    return reconstruction_service.create_reconstruction(request, engine=engine)


def enqueue_with(engine: Engine, job_id: int, **kwargs: Any) -> str:
    """``jobs.queue.enqueue_run``: a local run is queued by its committed row (the job table is the queue)."""

    from laueportal.jobs import queue as run_queue

    return run_queue.enqueue_run(job_id, engine=engine, **kwargs)


def claimed(engine: Engine, job_id: int) -> None:
    """Queue a published local run and claim it as the worker's dispatcher does (``jobs.dispatch.claim_run``):
    Running, ready for ``execute_run`` in its job process."""

    from laueportal.jobs.dispatch import claim_run

    enqueue_with(engine, job_id)
    assert claim_run(engine, job_id), f"job {job_id} was not claimed"


def dispatch_queue(engine: Engine) -> list[tuple[str, int]]:
    """What the worker's dispatcher would take, in its order (``jobs.dispatch.queued_in_order``)."""

    from laueportal.jobs.dispatch import queued_in_order

    with Session(engine) as session:
        return list(queued_in_order(session))


def load_job(engine, job_id):
    with Session(engine) as session:
        return session.get(Job, job_id)


def make_engine(tmp_path: Path, name: str = "runs.db") -> Engine:
    """An engine on a fresh database ``tmp_path/<name>`` with every table (test settings, ``core.db``)."""

    _, engine = make_database(tmp_path, database={"path": tmp_path / name})
    return engine
