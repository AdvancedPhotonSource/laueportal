"""Tests for the unified workflow database schema (``laueportal.models``)."""

import importlib.util
import json
import sqlite3
import subprocess
import sys
import textwrap
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from laueportal import models
from laueportal.core.db import make_engine
from tests.conftest import (
    create_test_job,
    create_test_metadata,
    create_test_reconstruction_run,
    create_test_wire_reconstruction_parameters,
)
from tests.settings_support import fresh_interpreter_env, make_settings


@pytest.fixture
def workflow_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'workflow.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    models.Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def test_fresh_database_contains_unified_workflow_tables_and_no_per_input_table(workflow_engine):
    inspector = inspect(workflow_engine)
    table_names = set(inspector.get_table_names())

    assert {
        "reconstruction_run",
        "wire_reconstruction_parameters",
        "indexing_run",
        "lauego_indexing_parameters",
    } <= table_names
    assert {"recon", "wirerecon", "peakindex", "subjob"}.isdisjoint(table_names)
    job_columns = {column["name"] for column in inspector.get_columns("job")}
    assert {"n_inputs", "n_succeeded", "n_failed", "n_not_run", "phase", "failure_report_path"} <= job_columns
    assert not hasattr(models, "SubJob")


def test_removed_workflow_models_and_helpers_are_not_exported():
    from laueportal.core import db
    from laueportal.files import roots
    from laueportal.indexing import results
    from laueportal.indexing import service as indexing_service
    from laueportal.jobs import progress, queue
    from laueportal.scans import service as scans_service
    from laueportal.system import status

    for name in ("Recon", "WireRecon", "PeakIndex", "MASK_FOCUS_TABLE", "Calib"):
        assert not hasattr(models, name)
    for module in (roots, scans_service):
        for name in ("make_IDnumber", "parse_IDnumber", "get_data_from_id", "import_catalog_row", "get_catalog_data"):
            assert not hasattr(module, name)
    # Removed helpers, and helpers left without a caller.
    assert not hasattr(db, "SessionLocal")
    assert not hasattr(roots, "remove_root_path_prefix")
    assert not hasattr(results, "all_indexing_ids")
    assert not hasattr(progress, "load_progress_many")
    assert not hasattr(queue, "check_redis_connection")
    for name in ("get_job_status", "get_active_jobs"):
        assert not hasattr(status, name)
    # The forms are validated by */validation.py alone.
    assert importlib.util.find_spec("laueportal.indexing.legacy_validation") is None
    assert not hasattr(roots, "resolve_path_with_root")
    assert not hasattr(indexing_service, "parse_workflow_identities")  # the SN/R/I text: the pickers replaced it


def test_reconstruction_relationships_are_one_to_one(workflow_engine):
    with Session(workflow_engine) as session:
        metadata = create_test_metadata()
        job = create_test_job()
        run = create_test_reconstruction_run()
        run.wire_parameters = create_test_wire_reconstruction_parameters()
        session.add_all([metadata, job, run])
        session.commit()
        run_id = run.id

    with Session(workflow_engine) as session:
        run = session.get(models.ReconstructionRun, run_id)
        assert run is not None
        assert run.scan.scan_number == 1
        assert run.job.job_id == 1
        assert run.wire_parameters.reconstruction_id == run.id
        assert run.wire_parameters.reconstruction is run


@pytest.mark.parametrize(
    ("model", "method", "required_values"),
    [
        (
            models.ReconstructionRun,
            "unknown",
            {
                "job_id": 1,
                "input_path": "/input",
                "created_at": datetime(2022, 1, 1, tzinfo=UTC),
            },
        ),
        (
            models.IndexingRun,
            "unknown",
            {
                "job_id": 1,
                "input_path": "/input",
                "created_at": datetime(2022, 1, 1, tzinfo=UTC),
            },
        ),
    ],
)
def test_method_check_constraints_reject_unknown_values(workflow_engine, model, method, required_values):
    with Session(workflow_engine) as session:
        session.add(create_test_job())
        session.add(model(method=method, **required_values))
        with pytest.raises(IntegrityError):
            session.commit()


def test_application_engine_enables_wal_mode(tmp_path):
    database_path = tmp_path / "wal.db"

    engine = make_engine(make_settings(tmp_path, database={"path": database_path}))
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    finally:
        engine.dispose()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


DOMAIN_MODELS = [
    "Catalog",
    "Conversion",
    "IdempotencyKey",
    "IndexingRun",
    "Job",
    "LaueGoIndexingParameters",
    "LaueMatchingIndexingParameters",
    "Metadata",
    "ReconstructionRun",
    "Scan",
    "WireReconstructionParameters",
    "Worker",
]

# Modules whose first ORM use failed in a fresh process when only laueportal.models registered every model:
# the queue entry points (the job process's execute_run, the worker), the results-convert CLI's service and every
# other service.
ORM_ENTRY_MODULES = [
    "laueportal.indexing.results",
    "laueportal.indexing.service",
    "laueportal.jobs.controls",
    "laueportal.jobs.executor",
    "laueportal.jobs.lifecycle",
    "laueportal.jobs.progress",
    "laueportal.jobs.queue",
    "laueportal.jobs.records",
    "laueportal.jobs.state",
    "laueportal.jobs.summary",
    "laueportal.jobs.worker",
    "laueportal.reconstruction.service",
    "laueportal.scans.importer",
    "laueportal.scans.service",
    "laueportal.seed.demo",
]

MAPPER_PROBE = textwrap.dedent(
    """
    import importlib, json, os, pkgutil, sys

    import sqlalchemy.orm

    for library in ("numpy", "h5py", "fastapi", "lauelab", "pydantic_settings"):
        importlib.import_module(library)  # third-party only: preloaded so that each child imports quickly

    import laueportal
    from laueportal.core.db import Base

    assert not Base.registry.mappers, "a model was imported before the probe started"
    names = sorted(
        info.name
        for info in pkgutil.walk_packages(laueportal.__path__, "laueportal.")
        if info.name != "laueportal.__main__"  # runs the CLI
    )
    # The children, each independent of the others, run one per CPU this process may use at a time (its affinity:
    # os.cpu_count() counts the host's), at most 8: a container's CPU quota does not narrow the affinity, and each
    # child holds a module's imports in memory.
    cpus = min(len(os.sched_getaffinity(0)), 8)
    running = []  # (pid, the read end of its pipe), oldest first

    def report(pid, read):
        with os.fdopen(read) as pipe:
            line = pipe.read()
        os.waitpid(pid, 0)
        print(line)

    for name in names:
        if len(running) >= cpus:
            report(*running.pop(0))
        read, write = os.pipe()
        pid = os.fork()
        if pid == 0:  # the child imports one module into an interpreter that holds no model yet
            os.close(read)
            try:
                importlib.import_module(name)
                mapped = sorted(mapper.class_.__name__ for mapper in Base.registry.mappers)
                if mapped:
                    sqlalchemy.orm.configure_mappers()
                result = {"mapped": mapped, "aggregate": "laueportal.models" in sys.modules}
            except BaseException as error:
                result = {"error": f"{type(error).__name__}: {error}"}
            os.write(write, json.dumps({"module": name, **result}).encode())
            os._exit(0)
        os.close(write)
        running.append((pid, read))
    for child in running:
        report(*child)
    """
)


def test_importing_any_module_registers_and_configures_every_mapper(tmp_path):
    """The first ORM use configures the mappers whichever module a process imported first.

    Each domain's models module imports the models its relationships name, so nothing has to import
    ``laueportal.models`` first. Each module is imported in a forked child of a fresh interpreter (no conftest, no model
    loaded yet), which then calls ``configure_mappers()``.
    """

    result = subprocess.run(
        [sys.executable, "-c", MAPPER_PROBE],
        cwd=tmp_path,
        env=fresh_interpreter_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    reports = {report["module"]: report for report in map(json.loads, result.stdout.splitlines())}

    assert len(reports) > 50
    assert {name: report["error"] for name, report in reports.items() if "error" in report} == {}
    for name, report in reports.items():
        if report["mapped"] and name != "laueportal.models":
            assert report["mapped"] == DOMAIN_MODELS, name  # one domain's models bring every other domain's
    for name in ORM_ENTRY_MODULES:
        assert (reports[name]["mapped"], reports[name]["aggregate"]) == (DOMAIN_MODELS, False), name
    assert reports["laueportal.models"]["mapped"] == DOMAIN_MODELS  # Calib went with revision 0004
