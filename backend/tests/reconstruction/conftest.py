"""The wire viewer's API on a fresh database (``viewer``): runs made from the synthetic reconstruction and
generated legacy per-depth fixtures (``tests/recon_support.py``)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, update
from sqlalchemy.orm import Session

from laueportal import models
from laueportal.app import create_app
from laueportal.jobs.state import JobStatus
from tests.conftest import create_test_wire_reconstruction_parameters
from tests.settings_support import make_database

RUNS = "/api/v1/reconstruction-runs"
REMOTE_RUN = "/compute/workspace/runs/run-1"
"""A remote run's directory as the remote side sees it (the plan maps the portal's run directory to it)."""


@dataclass
class Viewer:
    client: TestClient
    engine: Engine
    root: Path
    """The file root ``data``, holding the run directories."""

    def add_run(
        self, reconstruction_id: int, *, status: JobStatus = JobStatus.FINISHED, directory: bool = True
    ) -> Path:
        """Run ``reconstruction_id`` with wire parameters, its job ``status`` and its directory
        ``<root>/rec_<id>`` (created; recorded only with ``directory``)."""
        run_directory = self.root / f"rec_{reconstruction_id}"
        run_directory.mkdir()
        with Session(self.engine) as session:
            session.add(
                models.Job(
                    job_id=reconstruction_id,
                    computer_name="h",
                    status=int(status),
                    priority=0,
                    submit_time=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
            run = models.ReconstructionRun(
                id=reconstruction_id,
                job_id=reconstruction_id,
                scan_number=None,
                method="wire",
                input_path=str(self.root / "raw"),
                output_path=str(run_directory) if directory else None,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            run.wire_parameters = create_test_wire_reconstruction_parameters()
            session.add(run)
            session.commit()
        return run_directory

    def set_status(self, reconstruction_id: int, status: JobStatus) -> None:
        with Session(self.engine) as session:
            session.execute(update(models.Job).where(models.Job.job_id == reconstruction_id).values(status=int(status)))
            session.commit()

    def get(self, reconstruction_id: int, product: str = "", **headers: str) -> Any:
        path = f"{RUNS}/{reconstruction_id}" + (f"/{product}" if product else "")
        return self.client.get(path, headers=headers)


@pytest.fixture
def viewer(tmp_path: Path) -> Iterator[Viewer]:
    """The API on a fresh database whose file roots are ``tmp_path/data`` and its subfolder ``data/raw``."""
    root = tmp_path / "data"
    (root / "raw").mkdir(parents=True)
    settings, engine = make_database(tmp_path, files={"roots": {"data": root, "raw": root / "raw"}})
    try:
        with TestClient(create_app(settings)) as client:
            yield Viewer(client, engine, root)
    finally:
        engine.dispose()
