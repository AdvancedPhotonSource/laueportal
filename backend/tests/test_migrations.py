"""The Alembic history: the v1 baseline, the cleanup revisions, and head against the models.

Every v1 shape the migrations accept (``dde1bde``, ``3ad1557``, ``cb1159c``) is built from the committed schemas.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import DateTime, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

import laueportal.models as models
from laueportal.core import migrations
from laueportal.core.db import Base, database_engine
from laueportal.core.settings import DatabaseSettings
from laueportal.core.timeutil import UTCDateTime
from laueportal.jobs.live import has_live_marker, is_live_run
from laueportal.jobs.live_status import latest_session
from tests.migration_support import (
    CB1159C_COLUMN,
    CB1159C_LIVE_TABLES,
    NEW_TABLES_0007,
    QUEUE_COLUMNS,
    REMOTE_COLUMNS,
    TIMESTAMP_COLUMNS,
    V1_SCHEMAS,
    add_remote_columns_at_the_end,
    add_v1_live_tables,
    columns,
    create_v1_cb1159c_database,
    create_v1_database,
    database_at,
    database_with_early_numbering,
    execute,
    file_digest,
    journal_header,
    normalize_sql,
    normalized,
    rebuild_table,
    schema_objects,
    v1_schema_fixture,
    v1_schema_objects,
)

REVISIONS = ["0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009"]


UTC_FROM_0007 = {
    ("job", "moved_to_front_at"),
    *(("idempotency_key", name) for name in ("claimed_at", "expires_at")),
    *(("conversion", name) for name in ("requested_at", "started_at", "ended_at")),
    *(("worker", name) for name in ("started_at", "stopped_at", "dispatcher_at", "reconciler_at", "poller_at")),
}
"""Revision 0007's own DATETIME columns, UTC from the start (0005 converted v1's)."""


def _module(revision: str) -> Any:
    script = migrations.script_directory().get_revision(revision)
    assert script is not None
    return script.module


def _insert(database: DatabaseSettings, table: str, values: dict[str, Any]) -> None:
    names = ", ".join(f'"{name}"' for name in values)
    marks = ", ".join("?" for _ in values)
    execute(database, f'INSERT INTO "{table}" ({names}) VALUES ({marks})', tuple(values.values()))


def _rows(database: DatabaseSettings, table: str) -> list[tuple[Any, ...]]:
    return execute(database, f'SELECT * FROM "{table}" ORDER BY 1')


def _job_sql(database: DatabaseSettings) -> str:
    [(sql,)] = execute(database, "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'job'")
    return sql


V1_ROWS: dict[str, list[dict[str, Any]]] = {
    "job": [
        {
            "job_id": 1,
            "computer_name": "host",
            "status": 2,
            "priority": 0,
            "submit_time": "2026-01-23 11:31:56.448330",
            "start_time": "2026-01-23 11:31:56.638050",
            "finish_time": "2026-01-23 11:32:08.491324",
            "updated_at": "2026-01-23 11:32:08.491324",
            "phase": "finished",
            "n_inputs": 1,
            "n_succeeded": 1,
        },
        {
            "job_id": 2,
            "computer_name": "host",
            "status": 3,
            "priority": 0,
            "submit_time": "2026-07-23 20:28:11.000001",
            "start_time": None,
            "finish_time": "2026-07-23 23:59:59.999999",
            "heartbeat_at": "2026-07-23 21:00:00.000000",
            "cancel_requested_at": "2026-07-23 22:00:00.000000",
        },
    ],
    "metadata": [
        {
            "scanNumber": 274036,
            "time_epoch": 1019277242,
            "time": "2022-04-19 23:34:02.000000",
            "source_ringCurrent": 101.5,
            "source_CCDshutter": "open",
            "sample_XYZ": "1 2 3",
            "knifeEdge_XYZ": "4 5 6",
            "scanEnd_time": "2022-04-20 09:23:14",
            "scanEnd_source_ringCurrent": 99.5,
            "motorGroup_sample_npts_total": 10,
            "motorGroup_other_cpt_total": 3,
        }
    ],
    "scan": [
        {
            "id": 1,
            "scanNumber": 274036,
            "scan_dim": 1,
            "scan_npts": 10,
            "scan_after": "0",
            **{f"scan_positioner{i}_PV": f"34ide:m{i}.VAL" for i in range(1, 5)},
            **{f"scan_positioner{i}_ar": "0" for i in range(1, 5)},
            **{f"scan_positioner{i}_mode": "LINEAR" for i in range(1, 5)},
            **{f"scan_positioner{i}": f"{i} 2 3" for i in range(1, 5)},
            **{f"scan_detectorTrig{i}_PV": f"34ide:det{i}" for i in range(1, 5)},
            **{f"scan_detectorTrig{i}_VAL": str(i) for i in range(1, 5)},
            "scan_cpt": 10,
        }
    ],
    "catalog": [
        {
            "catalog_id": 1,
            "scanNumber": 274036,
            "filefolder": "/data/scan_274036",
            "filenamePrefix": '["wire_%d"]',
            "aperture": "wire",
        }
    ],
    "reconstruction_run": [
        {
            "id": 1,
            "scan_number": 274036,
            "job_id": 1,
            "method": "wire",
            "input_path": "/data/scan_274036",
            "created_at": "2026-01-23 11:31:56.448330",
        }
    ],
    "wire_reconstruction_parameters": [
        {
            "reconstruction_id": 1,
            "filename_prefixes": '["wire_%d"]',
            "geometry_file": "/geo.xml",
            "percent_brightest": 100.0,
            "wire_edges": "leading",
            "depth_start": -50.0,
            "depth_end": 150.0,
            "depth_resolution": 1.0,
            "num_threads": 1,
            "memory_limit_mb": 512,
            "scan_points": "1-10",
            "scan_points_len": 10,
            "verbose": 1,
        }
    ],
    "indexing_run": [
        {
            "id": 1,
            "scan_number": 274036,
            "reconstruction_id": 1,
            "job_id": 2,
            "method": "lauego",
            "input_path": "/runs/rec_1",
            "created_at": "2026-07-23 20:28:11.000001",
        }
    ],
}
"""A small v1 database: one scan with a wire reconstruction and an indexing run of it."""


def v1_database(tmp_path: Path, *, name: str = "v1.db", **settings: Any) -> DatabaseSettings:
    """``V1_ROWS`` in a database at revision 0001 (v1's schema, as Alembic built it)."""
    database = database_at(tmp_path, "0001", name=name, **settings)
    for table, rows in V1_ROWS.items():
        for row in rows:
            _insert(database, table, row)
    return database


def unstamped_v1_database(tmp_path: Path, *, name: str = "unstamped.db") -> DatabaseSettings:
    """``V1_ROWS`` in a database as v1 ``dde1bde`` created it (``create_all``'s DDL, no Alembic revision)."""
    database = DatabaseSettings(path=create_v1_database(tmp_path / name))
    for table, rows in V1_ROWS.items():
        for row in rows:
            _insert(database, table, row)
    return database


# ---------------------------------------------------------------------------
# History and baseline


def test_the_history_is_linear_and_ends_at_head():
    assert migrations.revision_ids() == REVISIONS
    assert migrations.head_revision() == "0009"
    names = sorted(path.name for path in (migrations.MIGRATIONS_DIRECTORY / "versions").glob("*.py"))
    assert names == [
        "0001_v1_baseline.py",
        "0002_v1_remote_execution.py",
        "0003_snake_case.py",
        "0004_drop_calib_and_ca.py",
        "0005_utc_timestamps.py",
        "0006_selected_xml.py",
        "0007_job_table_queue.py",
        "0008_remote_observation.py",
        "0009_laue_matching_parameters.py",
    ]
    assert migrations.V1_REVISIONS == {"0001": "v1 dde1bde", "0002": "v1 3ad1557"}
    assert migrations.V1_REVISIONS == {revision: f"v1 {commit}" for revision, commit in V1_SCHEMAS.items()}


def test_baseline_0001_builds_exactly_v1s_create_all(tmp_path):
    """0001 on an empty database stores v1 ``dde1bde``'s DDL object for object, byte for byte.

    Only Alembic's ``alembic_version`` table is left out.
    """
    database = database_at(tmp_path, "0001")
    assert schema_objects(database.path) == v1_schema_objects()
    assert migrations.current_revision(database) == "0001"


def test_head_on_an_empty_database_matches_the_models(tmp_path):
    """Alembic finds no difference between head and the models, and the DDL is ``create_all``'s.

    The DDL comparison also covers what ``compare_metadata`` does not look at (CHECK constraints, server
    defaults, constraint order). Quotes SQLite kept around renamed identifiers are normalized.
    """
    database = database_at(tmp_path, "head")
    assert migrations.check(database) == []
    engine = database_engine(database)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            assert compare_metadata(context, Base.metadata) == []
    finally:
        engine.dispose()

    fresh = tmp_path / "create_all.db"
    engine = create_engine(f"sqlite:///{fresh}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    assert normalized(schema_objects(database.path)) == normalized(schema_objects(fresh))
    assert {item["name"] for item in schema_objects(database.path) if item["type"] == "table"} == {
        "catalog",
        "conversion",
        "idempotency_key",
        "indexing_run",
        "job",
        "laue_matching_indexing_parameters",
        "lauego_indexing_parameters",
        "metadata",
        "reconstruction_run",
        "scan",
        "wire_reconstruction_parameters",
        "worker",
    }


def test_every_revision_downgrades_to_the_v1_baseline_and_to_nothing(tmp_path):
    database = database_at(tmp_path, "head")
    for revision in reversed(REVISIONS[:-1]):
        assert migrations.downgrade(database, revision)[1] == revision
        if revision == "0002":  # quotes: 0003 and 0004 renamed columns and rebuilt a table on the way down
            assert normalized(schema_objects(database.path)) == normalized(v1_schema_objects("3ad1557"))
    assert normalized(schema_objects(database.path)) == normalized(v1_schema_objects())

    assert migrations.downgrade(database, "base") == ("0001", None)
    assert schema_objects(database.path) == []
    assert migrations.upgrade(database) == (None, "0009")
    assert migrations.check(database) == []


# ---------------------------------------------------------------------------
# 0002: v1 3ad1557's remote-execution columns


def test_0002_builds_exactly_v1_3ad1557s_create_all(tmp_path):
    """0002 on an empty database stores v1 ``3ad1557``'s DDL object for object, byte for byte
    (``fixtures/v1_schema_3ad1557.json``, captured from v1's own models). Only ``alembic_version`` is left out."""
    database = database_at(tmp_path, "0002")
    assert schema_objects(database.path) == v1_schema_objects("3ad1557")
    assert migrations.current_revision(database) == "0002"


def test_v1_3ad1557_differs_from_dde1bde_only_by_the_five_job_columns():
    """The two fixtures agree with v1's ``git diff dde1bde 3ad1557 -- laue_portal/database``: five nullable
    ``VARCHAR`` columns after ``queue_job_id``, nothing else."""
    before, after = v1_schema_objects("dde1bde"), v1_schema_objects("3ad1557")
    assert [item for item in after if item["name"] != "job"] == [item for item in before if item["name"] != "job"]
    [old] = [item["sql"] for item in before if item["name"] == "job"]
    [new] = [item["sql"] for item in after if item["name"] == "job"]
    added = "".join(f"\n\t{column} VARCHAR, " for column in REMOTE_COLUMNS)
    assert new == old.replace("\tqueue_job_id VARCHAR, ", f"\tqueue_job_id VARCHAR, {added}", 1) != old


def test_0002_adds_the_columns_after_queue_job_id_and_keeps_the_rows(tmp_path):
    database = v1_database(tmp_path)
    before = {table: _rows(database, table) for table in V1_ROWS}
    migrations.upgrade(database, "0002")

    names = columns(database, "job")
    after_queue_job_id = names.index("queue_job_id") + 1
    assert names[after_queue_job_id : after_queue_job_id + len(REMOTE_COLUMNS)] == list(REMOTE_COLUMNS)
    assert execute(database, f"SELECT {', '.join(REMOTE_COLUMNS)} FROM job") == [(None,) * 5, (None,) * 5]
    kept = ", ".join(name for name in names if name not in REMOTE_COLUMNS)
    assert execute(database, f"SELECT {kept} FROM job ORDER BY 1") == before["job"]
    assert {table: _rows(database, table) for table in V1_ROWS if table != "job"} == {
        table: rows for table, rows in before.items() if table != "job"
    }
    for table in ("calib", "indexing_run", "reconstruction_run"):  # they reference the new job table
        references = {(row[2], row[3], row[4]) for row in execute(database, f'PRAGMA foreign_key_list("{table}")')}
        assert ("job", "job_id", "job_id") in references, table
    assert execute(database, "PRAGMA foreign_key_check") == []

    migrations.downgrade(database, "0001")
    assert schema_objects(database.path) == v1_schema_objects()  # byte for byte
    assert {table: _rows(database, table) for table in V1_ROWS} == before


def test_0002_keeps_the_rest_of_jobs_stored_ddl(tmp_path):
    """Production's ``job`` (made by v1's migration script) has a quoted name and its CHECK constraints in the
    other order. 0002 inserts the five columns into that text, and its downgrade takes them out again:
    0001 -> 0002 -> 0001 gives back the stored DDL unchanged."""
    database = v1_database(tmp_path)
    original = rebuild_table(
        database, "job", lambda sql: sql.replace(f"{NON_NEGATIVE}, \n\t{BOUNDED}", f"{BOUNDED}, \n\t{NON_NEGATIVE}")
    )
    assert original.startswith('CREATE TABLE "job"') and original.index(BOUNDED) < original.index(NON_NEGATIVE)
    rows = _rows(database, "job")

    migrations.upgrade(database, "0002")
    added = "".join(f", \n\t{column} VARCHAR" for column in REMOTE_COLUMNS)
    assert _job_sql(database) == original.replace("queue_job_id VARCHAR", f"queue_job_id VARCHAR{added}", 1)

    migrations.downgrade(database, "0001")
    assert _job_sql(database) == original
    assert _rows(database, "job") == rows


def test_v1s_own_upgrade_of_a_dde1bde_database_is_stamped_0002(tmp_path):
    """v1 ``3ad1557``'s ``scripts/migrate_workflow_database.py`` upgrades a ``dde1bde`` database by appending the
    five columns (``ALTER TABLE ... ADD COLUMN``). That database is at v1 ``3ad1557``'s schema too: it is
    detected and stamped 0002 (column order does not count), reaches head, and its downgrade to 0001 takes
    the columns out wherever they are."""
    database = unstamped_v1_database(tmp_path)
    original, rows = _job_sql(database), _rows(database, "job")
    appended = add_remote_columns_at_the_end(database)
    assert appended == original.replace(
        "failure_report_path VARCHAR", "failure_report_path VARCHAR" + "".join(f", {c} VARCHAR" for c in REMOTE_COLUMNS)
    )

    assert migrations.detect_v1_schema(database) == migrations.V1Schema("0002", "v1 3ad1557")
    migrations.stamp(database, "0002")
    assert migrations.upgrade(database) == ("0002", "0009")
    assert migrations.check(database) == []
    # Where v1 put them, then revision 0008's after external_url and revision 0007's after the last column.
    last = [*REMOTE_COLUMNS, "remote_observation", *QUEUE_COLUMNS]
    assert columns(database, "job")[-len(last) :] == last

    migrations.downgrade(database, "0002")
    assert _job_sql(database) == appended
    migrations.downgrade(database, "0001")
    assert _job_sql(database) == original
    assert _rows(database, "job") == rows


def test_productions_job_table_after_v1s_own_upgrade_is_stamped_0002_and_comes_back_from_head(tmp_path):
    """Production's ``job`` as v1 ``3ad1557``'s own migration script left it (``v1_schema_3ad1557.json``'s
    ``upgraded_dde1bde_job``: a quoted name, its CHECK constraints in the other order, the five columns appended),
    every other object ``dde1bde``'s. The same ``ALTER TABLE`` statements on production's ``dde1bde`` table give
    that text; the database is detected and stamped 0002, reaches head with nothing for ``db check`` to report, and
    its downgrade to 0002 gives the same text back, its rows kept."""
    expected = v1_schema_fixture("3ad1557")["upgraded_dde1bde_job"]["sql"]
    database = unstamped_v1_database(tmp_path)
    rebuild_table(
        database, "job", lambda sql: sql.replace(f"{NON_NEGATIVE}, \n\t{BOUNDED}", f"{BOUNDED}, \n\t{NON_NEGATIVE}")
    )
    assert add_remote_columns_at_the_end(database) == expected
    rows = _rows(database, "job")

    assert migrations.stamp_detected(database) == migrations.V1Schema("0002", "v1 3ad1557")
    assert migrations.upgrade(database) == ("0002", "0009")
    assert migrations.check(database) == []

    assert migrations.downgrade(database, "0002") == ("0009", "0002")
    assert _job_sql(database) == expected
    assert _rows(database, "job") == rows


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("backend", "crux"),
        ("submission_id", "laue-2-5f0c"),
        ("external_id", "dm-job-42"),
        ("external_state", "running"),
        ("external_url", "https://app.globus.org/runs/42"),
    ],
)
def test_0002_downgrade_refuses_to_drop_a_remote_runs_identity(tmp_path, column, value):
    database = v1_database(tmp_path)
    migrations.upgrade(database, "0002")
    execute(database, "UPDATE job SET backend = 'local' WHERE job_id = 1")  # a local run: nothing is lost
    execute(database, f"UPDATE job SET {column} = ? WHERE job_id = 2", (value,))
    schema, rows = schema_objects(database.path), _rows(database, "job")

    with pytest.raises(
        migrations.MigrationError, match=re.escape("but 1 jobs are remote runs (2). Nothing was changed.")
    ):
        migrations.downgrade(database, "0001")

    assert migrations.current_revision(database) == "0002"
    assert (schema_objects(database.path), _rows(database, "job")) == (schema, rows)
    execute(database, f"UPDATE job SET {column} = NULL WHERE job_id = 2")
    assert migrations.downgrade(database, "0001") == ("0002", "0001")
    assert schema_objects(database.path) == v1_schema_objects()


def test_0002_refuses_a_job_table_that_already_has_the_columns(tmp_path):
    """A v1 ``3ad1557`` database stamped 0001 without the schema check is not given the columns twice."""
    database = DatabaseSettings(path=create_v1_database(tmp_path / "v1.db", "3ad1557"))
    migrations.stamp(database, "0001", verify=False)
    digest = file_digest(database.path)

    with pytest.raises(migrations.MigrationError, match="but it already has backend, submission_id, external_id"):
        migrations.upgrade(database)

    assert migrations.current_revision(database) == "0001"
    assert file_digest(database.path) == digest


# ---------------------------------------------------------------------------
# 0003: snake_case


def test_the_rename_map_covers_every_mixed_case_column_of_v1(tmp_path):
    renames = _module("0003").RENAMES
    v1 = database_at(tmp_path, "0002")
    mixed = {
        (table, column)
        for table in [row[0] for row in execute(v1, "SELECT name FROM sqlite_master WHERE type = 'table'")]
        if table != migrations.VERSION_TABLE
        for column in columns(v1, table)
        if column != column.lower()
    }
    assert {(table, old) for table, names in renames.items() for old in names} == mixed
    assert len(mixed) == 51 and sum(len(names) for names in renames.values()) == 51
    for table, names in renames.items():
        assert all(re.fullmatch(r"[a-z][a-z0-9_]*", new) for new in names.values()), table
        others = set(columns(v1, table)) - set(names)
        assert len(set(names.values())) == len(names) and not set(names.values()) & others, table  # no collision

    head = database_at(tmp_path, "head", name="head.db")
    for table in [row[0] for row in execute(head, "SELECT name FROM sqlite_master WHERE type = 'table'")]:
        assert all(column == column.lower() for column in columns(head, table)), table


def test_renames_keep_the_rows_and_move_the_foreign_keys(tmp_path):
    database = v1_database(tmp_path)
    v1_rows = {table: _rows(database, table) for table in V1_ROWS}
    migrations.upgrade(database, "0002")  # the revision the renames apply to (job has 0002's columns)
    before = {table: _rows(database, table) for table in V1_ROWS}
    migrations.upgrade(database, "0003")

    assert execute(
        database,
        "SELECT scan_number, source_ring_current, source_ccd_shutter, sample_xyz, knife_edge_xyz, scan_end_time,"
        " scan_end_source_ring_current, motor_group_sample_npts_total, motor_group_other_cpt_total FROM metadata",
    ) == [(274036, 101.5, "open", "1 2 3", "4 5 6", "2022-04-20 09:23:14", 99.5, 10, 3)]
    assert execute(
        database, "SELECT scan_number, scan_positioner1_pv, scan_detector_trig4_pv, scan_detector_trig4_val FROM scan"
    ) == [(274036, "34ide:m1.VAL", "34ide:det4", "4")]
    assert execute(database, "SELECT scan_number, filename_prefix FROM catalog") == [(274036, '["wire_%d"]')]
    for table in ("scan", "catalog", "calib", "indexing_run", "reconstruction_run"):
        references = {(row[2], row[3], row[4]) for row in execute(database, f'PRAGMA foreign_key_list("{table}")')}
        assert ("metadata", "scan_number", "scan_number") in references, table
    assert execute(database, "PRAGMA foreign_key_check") == []

    assert {table: _rows(database, table) for table in V1_ROWS} == before  # same rows, same column order

    migrations.downgrade(database, "0001")
    assert columns(database, "metadata")[:3] == ["scanNumber", "time_epoch", "time"]
    assert {table: _rows(database, table) for table in V1_ROWS} == v1_rows


# ---------------------------------------------------------------------------
# 0004: calib and CA


def test_calib_and_the_ca_method_are_gone_at_head(tmp_path):
    database = v1_database(tmp_path)
    migrations.upgrade(database)
    assert "calib" not in [row[0] for row in execute(database, "SELECT name FROM sqlite_master")]
    assert not hasattr(models, "Calib")

    engine = database_engine(database)
    try:
        with Session(engine) as session:
            session.add(models.Job(job_id=9, computer_name="h", status=0, priority=0))
            session.add(
                models.ReconstructionRun(
                    job_id=9, method="ca", input_path="/in", created_at=datetime(2026, 10, 15, tzinfo=UTC)
                )
            )
            with pytest.raises(IntegrityError, match="ck_reconstruction_run_method"):
                session.commit()
    finally:
        engine.dispose()


def test_rebuilding_reconstruction_run_keeps_its_rows_and_its_children(tmp_path):
    """Dropping the old table must not cascade into the wire parameters (foreign keys are off) and the
    indexing run must still point at the reconstruction."""
    database = v1_database(tmp_path)
    before = {table: _rows(database, table) for table in ("reconstruction_run", "wire_reconstruction_parameters")}
    migrations.upgrade(database, "0004")

    assert _rows(database, "wire_reconstruction_parameters") == before["wire_reconstruction_parameters"]
    assert _rows(database, "reconstruction_run") == before["reconstruction_run"]
    assert execute(database, "SELECT id, reconstruction_id FROM indexing_run") == [(1, 1)]
    assert execute(database, "PRAGMA foreign_key_check") == []
    assert {row[1] for row in execute(database, "PRAGMA index_list(reconstruction_run)")} == {
        "ix_reconstruction_run_scan_number",
        "ix_reconstruction_run_method",
        "sqlite_autoindex_reconstruction_run_1",
    }

    migrations.downgrade(database, "0003")
    _insert(
        database,
        "reconstruction_run",
        {"id": 2, "job_id": 2, "method": "ca", "input_path": "/in", "created_at": "2026-01-01 00:00:00.000000"},
    )  # 'ca' is allowed again below 0004


@pytest.mark.parametrize(
    ("table", "row", "message"),
    [
        (
            "reconstruction_run",
            {"id": 7, "job_id": 2, "method": "ca", "input_path": "/in", "created_at": "2026-01-01 00:00:00.000000"},
            "1 reconstruction runs with method 'ca' (7)",
        ),
        (
            "calib",
            {
                "calib_id": 3,
                "scanNumber": 274036,
                "job_id": 2,
                "filefolder": "/f",
                "filenamePrefix": "[]",
                "calib_config": "c",
                **dict.fromkeys(("cenx", "dist", "anglez", "angley", "anglex", "cenz", "shift_parameter"), 0.0),
            },
            "1 calib rows (3)",
        ),
    ],
)
def test_0004_refuses_to_drop_ca_data_and_the_upgrade_changes_nothing(tmp_path, table, row, message):
    database = v1_database(tmp_path)
    if table == "reconstruction_run":
        execute(database, "DELETE FROM indexing_run")  # job 2 is free for the CA run
    _insert(database, table, row)
    schema = schema_objects(database.path)
    data = {name: _rows(database, name) for name in [*V1_ROWS, "calib"]}

    with pytest.raises(migrations.MigrationError, match=re.escape(message)):
        migrations.upgrade(database)

    assert migrations.current_revision(database) == "0001"  # 0002's columns and 0003's renames were rolled back too
    assert schema_objects(database.path) == schema
    assert {name: _rows(database, name) for name in data} == data


# ---------------------------------------------------------------------------
# 0005: UTC


def test_every_datetime_column_is_converted_and_is_utc_in_the_models(tmp_path):
    expected = {(table, column) for table, names in TIMESTAMP_COLUMNS.items() for column in names}
    at_0004 = database_at(tmp_path, "0004")
    tables = [row[0] for row in execute(at_0004, "SELECT name FROM sqlite_master WHERE type = 'table'")]
    assert {
        (table, row[1])
        for table in tables
        for row in execute(at_0004, f'PRAGMA table_info("{table}")')
        if row[2] == "DATETIME"
    } == expected

    converted = _module("0005").COLUMNS
    assert {table: names for table, (_key, names) in converted.items()} == TIMESTAMP_COLUMNS
    model_columns = {
        (table.name, column.name): column.type
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, UTCDateTime | DateTime)
    }
    assert set(model_columns) == expected | UTC_FROM_0007
    assert all(isinstance(kind, UTCDateTime) for kind in model_columns.values())


def test_naive_local_times_become_utc_with_the_daylight_saving_rule(tmp_path):
    database = v1_database(tmp_path)
    migrations.upgrade(database, "0004")
    v1_jobs = _rows(database, "job")
    cases = {
        # job_id: (stored by v1, expected UTC). America/Chicago: CST = UTC-6, CDT = UTC-5.
        10: ("2026-01-23 11:32:08.491324", "2026-01-23T17:32:08.491324Z"),  # winter
        11: ("2026-07-23 20:28:11.000001", "2026-07-24T01:28:11.000001Z"),  # summer, crosses midnight UTC
        12: ("2025-11-02 01:30:00.000000", "2025-11-02T06:30:00.000000Z"),  # ambiguous: first occurrence (CDT)
        13: ("2026-03-08 02:30:00.000000", "2026-03-08T08:30:00.000000Z"),  # skipped hour: offset before (CST)
        14: ("2026-03-08 03:30:00.000000", "2026-03-08T08:30:00.000000Z"),  # just after the gap (CDT)
        15: ("2025-11-02 02:30:00.000000", "2025-11-02T08:30:00.000000Z"),  # after the repeated hour (CST)
        16: ("2026-01-23T11:32:08", "2026-01-23T17:32:08.000000Z"),  # other ISO spellings are read too
        17: ("2026-01-23T17:32:08+00:00", "2026-01-23T17:32:08.000000Z"),  # an explicit offset wins
    }
    for job_id, (local, _utc) in cases.items():
        _insert(
            database,
            "job",
            {"job_id": job_id, "computer_name": "h", "status": 2, "priority": 0, "submit_time": local},
        )

    migrations.upgrade(database)

    stored = dict(execute(database, "SELECT job_id, submit_time FROM job WHERE job_id >= 10"))
    assert stored == {job_id: utc for job_id, (_local, utc) in cases.items()}
    assert execute(
        database, "SELECT finish_time, start_time, heartbeat_at, cancel_requested_at FROM job WHERE job_id = 2"
    ) == [("2026-07-24T04:59:59.999999Z", None, "2026-07-24T02:00:00.000000Z", "2026-07-24T03:00:00.000000Z")]
    assert execute(database, "SELECT time, scan_end_time, time_epoch FROM metadata") == [
        ("2022-04-20T04:34:02.000000Z", "2022-04-20 09:23:14", 1019277242)  # scan_end_time stays text
    ]
    assert execute(
        database, "SELECT created_at FROM reconstruction_run UNION ALL SELECT created_at FROM indexing_run"
    ) == [
        ("2026-01-23T17:31:56.448330Z",),
        ("2026-07-24T01:28:11.000001Z",),
    ]

    engine = database_engine(database)
    try:
        with Session(engine) as session:
            job = session.get(models.Job, 10)
            assert job is not None and job.submit_time == datetime(2026, 1, 23, 17, 32, 8, 491324, tzinfo=UTC)
            assert session.scalar(select(models.Metadata.time)) == datetime(2022, 4, 20, 4, 34, 2, tzinfo=UTC)
    finally:
        engine.dispose()

    migrations.downgrade(database, "0004")
    restored = dict(execute(database, "SELECT job_id, submit_time FROM job WHERE job_id >= 10"))
    assert restored == {
        10: "2026-01-23 11:32:08.491324",
        11: "2026-07-23 20:28:11.000001",
        12: "2025-11-02 01:30:00.000000",
        13: "2026-03-08 03:30:00.000000",  # a skipped-hour time comes back an hour later
        14: "2026-03-08 03:30:00.000000",
        15: "2025-11-02 02:30:00.000000",
        16: "2026-01-23 11:32:08.000000",
        17: "2026-01-23 11:32:08.000000",
    }
    assert [row for row in _rows(database, "job") if row[0] < 10] == v1_jobs  # v1's values exactly


def test_the_legacy_time_zone_comes_from_the_settings(tmp_path):
    database = v1_database(tmp_path, legacy_timezone="Europe/Berlin")
    migrations.upgrade(database)
    assert execute(database, "SELECT finish_time FROM job WHERE job_id = 1") == [("2026-01-23T10:32:08.491324Z",)]


def test_0005_refuses_values_it_cannot_read_and_changes_nothing(tmp_path):
    database = v1_database(tmp_path)
    migrations.upgrade(database, "0004")
    execute(database, "UPDATE job SET heartbeat_at = 'yesterday' WHERE job_id = 1")
    execute(database, "UPDATE job SET updated_at = 12 WHERE job_id = 2")

    with pytest.raises(migrations.MigrationError) as error:
        migrations.upgrade(database)

    assert "cannot convert 2 timestamps" in str(error.value)
    assert "job.heartbeat_at where job_id = 1" in str(error.value)
    assert "job.updated_at where job_id = 2" in str(error.value)
    assert migrations.current_revision(database) == "0004"
    assert execute(database, "SELECT submit_time FROM job WHERE job_id = 1") == [("2026-01-23 11:31:56.448330",)]


# ---------------------------------------------------------------------------
# 0006: the results viewer's chosen XML


def test_0006_keeps_the_runs_and_its_downgrade_refuses_to_drop_a_choice(tmp_path):
    database = v1_database(tmp_path)
    migrations.upgrade(database, "0005")
    schema, rows = schema_objects(database.path), _rows(database, "indexing_run")

    assert migrations.upgrade(database, "0006") == ("0005", "0006")
    assert _rows(database, "indexing_run") == [(*row, None) for row in rows]
    execute(database, "UPDATE indexing_run SET selected_xml = 'b.xml' WHERE id = 1")
    chosen = schema_objects(database.path), _rows(database, "indexing_run")
    with pytest.raises(
        migrations.MigrationError, match=re.escape("but 1 runs hold a chosen XML (I1). Nothing was changed.")
    ):
        migrations.downgrade(database, "0005")
    assert migrations.current_revision(database) == "0006"
    assert (schema_objects(database.path), _rows(database, "indexing_run")) == chosen

    execute(database, "UPDATE indexing_run SET selected_xml = NULL WHERE id = 1")
    assert migrations.downgrade(database, "0005") == ("0006", "0005")
    assert (schema_objects(database.path), _rows(database, "indexing_run")) == (schema, rows)


# ---------------------------------------------------------------------------
# 0007: the idempotency keys, conversions, the worker and the job columns of the queue


def _v1_database_at(tmp_path: Path, commit: str) -> DatabaseSettings:
    """``V1_ROWS`` in a database as v1 ``commit`` created it, stamped with its revision (0001 or 0002)."""
    database = DatabaseSettings(path=create_v1_database(tmp_path / f"v1-{commit}.db", commit))
    for table, rows in V1_ROWS.items():
        for row in rows:
            _insert(database, table, row)
    migrations.stamp(database, migrations.detect_v1_schema(database).revision)
    return database


@pytest.mark.parametrize("commit", ["dde1bde", "3ad1557"])
def test_0007_adds_the_tables_and_job_columns_keeps_the_rows_and_downgrades_to_the_same_text(tmp_path, commit):
    """Both v1 schemas at 0006 reach 0007: the job rows keep every value with the four columns NULL after them, the
    three tables are empty, and the database, with 0008 on top, matches head. What 0007 holds goes with its
    downgrade, which gives back 0006's schema text and rows exactly."""
    database = _v1_database_at(tmp_path, commit)
    migrations.upgrade(database, "0006")
    schema, rows = schema_objects(database.path), _rows(database, "job")

    assert migrations.upgrade(database, "0007") == ("0006", "0007")
    assert columns(database, "job")[-len(QUEUE_COLUMNS) :] == list(QUEUE_COLUMNS)
    assert _rows(database, "job") == [(*row, None, None, None, None) for row in rows]
    assert {table: _rows(database, table) for table in NEW_TABLES_0007} == dict.fromkeys(NEW_TABLES_0007, [])
    assert execute(
        database, "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'idempotency_key' ORDER BY name"
    ) == [
        ("ix_idempotency_key_expires_at",),
        ("sqlite_autoindex_idempotency_key_1",),
    ]

    now = "2026-10-24T12:00:00.000000Z"
    execute(database, "UPDATE job SET moved_to_front_at = ?, process_host = 'h', process_pid = 7, process_since_boot = 1.5",
            (now,))  # fmt: skip
    _insert(database, "conversion", {"indexing_id": 1, "status": "queued", "requested_at": now})
    _insert(database, "worker", {"name": "worker", "host": "h", "pid": 7, "started_at": now})
    _insert(database, "idempotency_key", {"key": "k", "fingerprint": "f", "state": "pending", "claimed_at": now,
                                          "expires_at": now})  # fmt: skip
    assert migrations.upgrade(database) == ("0007", "0009")
    assert migrations.check(database) == []
    assert migrations.downgrade(database, "0006") == ("0009", "0006")
    assert (schema_objects(database.path), _rows(database, "job")) == (schema, rows)


@pytest.mark.parametrize(
    ("revision", "statement", "message"),
    [
        ("0007", "CREATE TABLE worker (name VARCHAR)", "already has worker."),
        ("0007", "ALTER TABLE job ADD COLUMN process_pid INTEGER", "job already has process_pid."),
        ("0006", "ALTER TABLE indexing_run ADD COLUMN selected_xml VARCHAR", "indexing_run already has selected_xml."),
        (
            "0008",
            "ALTER TABLE job ADD COLUMN remote_observation VARCHAR",
            "job.remote_observation is VARCHAR, not v1 cb1159c's nullable JSON.",
        ),
    ],
    ids=["0007-table", "0007-job-column", "0006-column", "0008-other-type"],
)
def test_0006_to_0008_refuse_a_database_that_has_what_they_add_and_change_nothing(
    tmp_path, revision, statement, message
):
    """A revision whose table or column is already there refuses the database and leaves the file as it was (0008:
    unless it is v1 ``cb1159c``'s column, which it keeps)."""
    before = f"{int(revision) - 1:04d}"
    database = database_at(tmp_path, before)
    execute(database, statement)
    digest = file_digest(database.path)

    with pytest.raises(migrations.MigrationError, match=re.escape(f"{message} Nothing was changed.")):
        migrations.upgrade(database, revision)
    assert migrations.current_revision(database) == before
    assert file_digest(database.path) == digest


def test_conversion_rows_need_their_indexing_run(tmp_path):
    """``conversion.indexing_id`` is ``indexing_run``'s: a conversion of no run is refused (foreign keys on, as the
    app's engine has them)."""
    database = database_at(tmp_path, "head")
    engine = database_engine(database)
    try:
        with engine.begin() as connection, pytest.raises(IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                models.Conversion.__table__.insert().values(  # type: ignore[attr-defined]
                    indexing_id=1, status="queued", requested_at=datetime.now(UTC)
                )
            )
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 0008: v1 cb1159c's job.remote_observation

OBSERVATION = {
    "phase": "running",
    "detail": "Processing",
    "observed_at": "2026-10-25T12:00:00+00:00",
    "submission_id": "3f2b9c1e-6a4d-4e8f-9b7a-2c5d8e1f0a39",
}
"""A remote observation as v1 ``cb1159c``'s poller stored it (``remote.observation``)."""


def test_v1_cb1159cs_create_all_is_v1_3ad1557s_with_the_live_tables_and_the_observation():
    """The two fixtures (``fixtures/v1_schema_cb1159c.json``, captured from ``cb1159c``'s own models): ``cb1159c``
    adds its three live tables with their indexes and ``job.remote_observation`` after ``external_url``, nothing
    else; so ``create_v1_cb1159c_database``'s ``create_all`` shape (all but the live tables) is ``cb1159c``'s."""
    before, after = v1_schema_objects("3ad1557"), v1_schema_objects("cb1159c")
    live = [item for item in after if item["tbl_name"] in CB1159C_LIVE_TABLES]
    assert {item["type"] for item in live} == {"table", "index"} and len(live) == 12
    kept = [item for item in after if item["tbl_name"] not in CB1159C_LIVE_TABLES]
    assert [item for item in kept if item["name"] != "job"] == [item for item in before if item["name"] != "job"]
    [old] = [item["sql"] for item in before if item["name"] == "job"]
    [new] = [item["sql"] for item in kept if item["name"] == "job"]
    after_url = "\texternal_url VARCHAR, \n"
    assert new == old.replace(after_url, f"{after_url}\t{CB1159C_COLUMN}, \n", 1) != old


def test_0008_adds_the_column_after_external_url_and_downgrades_to_the_same_text(tmp_path):
    """A database at 0007 reaches 0008: ``remote_observation`` after ``external_url`` (``create_all``'s place, so
    head's DDL is its text), NULL in every row, the other values kept. Its downgrade drops the observations and gives
    back 0007's schema text and rows exactly."""
    database = _v1_database_at(tmp_path, "3ad1557")
    migrations.upgrade(database, "0007")
    schema, rows = schema_objects(database.path), _rows(database, "job")

    assert migrations.upgrade(database) == ("0007", "0009")
    assert migrations.check(database) == []
    names = columns(database, "job")
    at = names.index("external_url") + 1
    assert names[at] == "remote_observation"
    assert _rows(database, "job") == [(*row[:at], None, *row[at:]) for row in rows]

    execute(database, "UPDATE job SET remote_observation = ?", (json.dumps(OBSERVATION),))
    assert migrations.check(database) == []
    assert migrations.downgrade(database, "0007") == ("0009", "0007")
    assert (schema_objects(database.path), _rows(database, "job")) == (schema, rows)


@pytest.mark.parametrize("appended", [False, True], ids=["create_all", "v1s-migration-script"])
def test_a_v1_cb1159c_job_keeps_its_observations_to_head(tmp_path, appended):
    """0008's choice (added only where absent): v1 ``cb1159c``'s ``job.remote_observation`` without its live tables
    (what its ``scripts/migrate_workflow_database.py`` leaves: the column where ``create_all`` writes it, as from a
    legacy database, or appended, as from an older v1's), stamped 0002 by ``db stamp --detect`` with the column kept and
    nothing dropped, reaches head with every observation: 0007's rebuild of ``job`` keeps the column and its values, and
    0008 leaves the table as it was. ``create_all``'s shape then has head's DDL text exactly."""
    database = DatabaseSettings(path=create_v1_cb1159c_database(tmp_path / "cb1159c.db", appended=appended))
    for table, rows in V1_ROWS.items():
        for row in rows:
            _insert(database, table, row)
    stored = json.dumps(OBSERVATION)
    execute(database, "UPDATE job SET remote_observation = ? WHERE job_id = 1", (stored,))
    schema = schema_objects(database.path)

    assert migrations.stamp_detected(database) == migrations.V1Schema("0002", "v1 cb1159c")
    assert migrations.current_revision(database) == "0002"
    assert schema_objects(database.path) == schema

    migrations.upgrade(database, "0007")
    assert CB1159C_COLUMN in _job_sql(database)
    assert execute(database, "SELECT job_id, remote_observation FROM job ORDER BY 1") == [(1, stored), (2, None)]
    schema, rows = schema_objects(database.path), _rows(database, "job")
    assert migrations.upgrade(database, "0008") == ("0007", "0008")
    assert (schema_objects(database.path), _rows(database, "job")) == (schema, rows)
    assert migrations.upgrade(database) == ("0008", "0009")
    assert _rows(database, "job") == rows
    assert migrations.check(database) == []
    if not appended:
        head = database_at(tmp_path, "head", name="head.db")
        assert normalize_sql(_job_sql(database)) == normalize_sql(_job_sql(head))

    engine = database_engine(database)
    try:
        with Session(engine) as session:
            job = session.get(models.Job, 1)
            assert job is not None and job.remote_observation == OBSERVATION
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# db stamp --detect on a v1 cb1159c database

LIVE_SESSIONS: list[dict[str, Any]] = [
    {"id": 1, "reconstruction_id": 1, "reconstruction_job_id": 1, "state": "finished"},
    {"id": 2, "state": "failed"},
    {"id": 3, "state": "cancelled"},
]
"""v1 ``cb1159c``'s live sessions, each ended (``migrations.LIVE_SESSION_ENDED``): the first reconstructed
``V1_ROWS``' wire run (R1, job 1)."""
NOT_ENDED_ADVICE = (
    "\nv2 does not continue v1-started live sessions. Before migrating, end each in v1 (its Live page's Stop; a "
    "session in recovery is abandoned by Stop), stop v1, and copy its database again; a rehearsal's copy: take another "
    "once no session is open (docs/runbooks/deploy.md step 4)"
)
"""What a refusal for a live session not ended says to do (before migrating, and for a rehearsal's copy)."""


def v1_cb1159c_database(
    tmp_path: Path,
    *,
    appended: bool = False,
    sessions: list[dict[str, Any]] = LIVE_SESSIONS,
    queue_job_id: str = "live_1_5f0c2a9e4b7d",
) -> DatabaseSettings:
    """``V1_ROWS`` in a database as v1 ``cb1159c`` leaves it (``create_v1_cb1159c_database``, then its live tables
    as its app adds them): ``sessions``, the first one's checkpoint and point, job 1 with that session's v1 queue
    identity (``live_<session>`` at its creation, ``live_<session>_<hex>`` once v1 queued it) and job 2 with a remote
    observation."""
    database = DatabaseSettings(path=create_v1_cb1159c_database(tmp_path / "cb1159c.db", appended=appended))
    add_v1_live_tables(database)
    for table, rows in V1_ROWS.items():
        for row in rows:
            _insert(database, table, row)
    for session in sessions:
        values = {"control_directory": f"/runs/live/session_{session['id']}", "recipe": "{}", "revision": 3}
        _insert(database, "live_session", {**values, "created_at": "2026-10-20 10:00:00.000000", **session})
    first = sessions[0]["id"]
    _insert(database, "live_checkpoint", {"session_id": first, "state": '{"points": 1}'})
    point = {"id": 1, "session_id": first, "point_number": 1, "point_id": "wire_1", "source": "/data/wire_1.h5"}
    counts = dict.fromkeys(("indexing_inputs", "indexing_succeeded", "indexing_failed", "indexed_frames"), 0)
    states = {"reconstruction_state": "complete", "indexing_state": "disabled"}
    arrived = {"source_identity": '{"size": 10}', "arrived_at": "2026-10-20 10:01:00.000000"}
    _insert(database, "live_point", {**point, **arrived, **states, **counts})
    execute(database, "UPDATE job SET queue_job_id = ? WHERE job_id = 1", (queue_job_id,))
    execute(database, "UPDATE job SET remote_observation = ? WHERE job_id = 2", (json.dumps(OBSERVATION),))
    return database


def test_the_live_tables_the_stamp_knows_are_v1_cb1159cs():
    """``migrations.CB1159C_LIVE_DDL`` (what ``db stamp --detect`` knows it drops) makes the fixture's live tables
    exactly: their DDL, columns and indexes (the constraints' own by name)."""
    fixture = [item for item in v1_schema_objects("cb1159c") if item["tbl_name"] in CB1159C_LIVE_TABLES]

    def built(statements: list[str]) -> tuple[Any, ...]:
        engine = create_engine("sqlite://")
        try:
            with engine.connect() as connection:
                for statement in statements:
                    connection.exec_driver_sql(statement)
                indexes = connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
                return migrations.schema_ddl(connection), migrations.table_columns(connection), sorted(indexes)
        finally:
            engine.dispose()

    ours = built(list(migrations.CB1159C_LIVE_DDL))
    theirs = built([item["sql"] for item in sorted(fixture, key=lambda item: item["type"] != "table") if item["sql"]])
    assert migrations.ddl_differences(ours[0], theirs[0]) == []
    assert ours[1:] == theirs[1:]
    assert set(migrations.CB1159C_LIVE_TABLES) == set(CB1159C_LIVE_TABLES) == set(ours[1])


@pytest.mark.parametrize("appended", [False, True], ids=["create_all", "v1s-migration-script"])
def test_stamp_detect_drops_an_ended_v1_cb1159c_databases_live_tables_and_the_chain_keeps_the_rest_to_head(
    tmp_path, appended
):
    """Every live session ended (finished, failed, cancelled), ``db stamp --detect`` drops the three live tables (their
    indexes with them) and nothing else, and records 0002; the sessions' runs and jobs stay as history and job 2's
    remote observation stays (revision 0008 adds the column only where absent). ``db upgrade`` reaches head and
    ``db check`` passes, the observation read by the models."""
    database = v1_cb1159c_database(tmp_path, appended=appended)
    schema, rows = schema_objects(database.path), {table: _rows(database, table) for table in V1_ROWS}

    found = migrations.stamp_detected(database)

    assert found == migrations.V1Schema("0002", "v1 cb1159c", ("live_point", "live_checkpoint", "live_session"))
    assert migrations.current_revision(database) == "0002"
    assert schema_objects(database.path) == [item for item in schema if item["tbl_name"] not in CB1159C_LIVE_TABLES]
    assert {table: _rows(database, table) for table in V1_ROWS} == rows
    assert migrations.upgrade(database) == ("0002", "0009")
    assert migrations.check(database) == []
    engine = database_engine(database)
    try:
        with Session(engine) as session:
            job = session.get(models.Job, 2)
            assert job is not None and job.remote_observation == OBSERVATION
    finally:
        engine.dispose()


@pytest.mark.parametrize("state", ["running", "recovery"])
def test_stamp_detect_refuses_a_v1_cb1159c_database_while_a_live_session_has_not_ended(tmp_path, state):
    """The portal does not continue v1-started live sessions: one that has not ended is refused by name, read-only (the
    file not even opened for writing). ``recovery`` is v1's interrupted session, which it can resume: its jobs are
    already Failed, so a look for Queued or Running jobs does not show it."""
    session = {"id": 4, "active_slot": 1, "reconstruction_id": 1, "reconstruction_job_id": 1, "state": state}
    database = v1_cb1159c_database(tmp_path, sessions=[session, *LIVE_SESSIONS[1:]])
    digest = file_digest(database.path)

    with pytest.raises(migrations.MigrationError) as error:
        migrations.stamp_detected(database)

    assert str(error.value) == (
        f"database {database.path} has v1 cb1159c's live sessions that have not ended, so it is not stamped and "
        f"nothing was changed:\n  live session 4: '{state}' (R1)" + NOT_ENDED_ADVICE
    )
    assert file_digest(database.path) == digest
    assert migrations.current_revision(database) is None


@pytest.mark.parametrize(
    ("journal", "unchanged", "header"),
    [
        ("wal", "nothing was changed", (2, 2)),
        ("delete", "nothing was changed but its journal mode (delete, now wal)", (2, 2)),
    ],
    ids=["wal", "rollback-journal"],
)
def test_stamp_detect_checks_the_live_sessions_again_inside_its_transaction(
    tmp_path, monkeypatch, journal, unchanged, header
):
    """A session that v1 resumed after the read-only check (here set to recovery right after it) is refused by the check
    inside the stamp's transaction: nothing dropped or recorded. That transaction's connection has already set the
    journal mode the settings name (WAL), which a rollback cannot undo: a rollback-journal file's refusal says so."""
    database = v1_cb1159c_database(tmp_path)
    execute(database, f"PRAGMA journal_mode={journal}")
    schema = schema_objects(database.path)
    checks = migrations.open_live_sessions
    found: list[int] = []

    def resumed_after_the_first_check(connection: Any) -> list[str]:
        lines = checks(connection)
        found.append(len(lines))
        if len(found) == 1:
            execute(database, "UPDATE live_session SET state = 'recovery' WHERE id = 2")
        return lines

    monkeypatch.setattr(migrations, "open_live_sessions", resumed_after_the_first_check)
    with pytest.raises(migrations.MigrationError) as error:
        migrations.stamp_detected(database)

    assert found == [0, 1]
    assert str(error.value) == (
        f"database {database.path} has v1 cb1159c's live sessions that have not ended, so it is not stamped and "
        f"{unchanged}:\n  live session 2: 'recovery'" + NOT_ENDED_ADVICE
    )
    assert schema_objects(database.path) == schema
    assert migrations.current_revision(database) is None
    assert journal_header(database.path) == header


@pytest.mark.parametrize(
    ("statement", "difference"),
    [
        ("CREATE TABLE live_frame (id INTEGER PRIMARY KEY)", "unexpected table live_frame"),
        ("DROP TABLE live_checkpoint", "missing table live_checkpoint"),
        ('ALTER TABLE "live_point" ADD COLUMN depth INTEGER', "live_point: unexpected column depth"),
        ("CREATE INDEX ix_live_session_state ON live_session (state)", "unexpected index ix_live_session_state"),
        (
            "CREATE TRIGGER job_live AFTER INSERT ON job BEGIN INSERT INTO live_checkpoint VALUES (new.job_id, '{}'); END",
            "unexpected trigger job_live",
        ),
        ("CREATE VIEW live_points AS SELECT * FROM live_point", "unexpected view live_points"),
        (
            ("DROP INDEX ix_live_point_session_id", "CREATE INDEX ix_live_point_session_id ON live_point (point_id)"),
            "index ix_live_point_session_id is `CREATE INDEX ix_live_point_session_id ON live_point (point_id)`, "
            "expected `CREATE INDEX ix_live_point_session_id ON live_point (session_id)`",
        ),
    ],
    ids=["a-table", "a-table-missing", "a-column", "an-index", "a-trigger", "a-view", "index-sql"],
)
def test_stamp_detect_refuses_a_v1_cb1159c_database_with_objects_it_does_not_know(tmp_path, statement, difference):
    """The stamp drops tables, so it takes only v1 ``cb1159c``'s schema exactly (``cb1159c_differences``): another
    table or column (a later v1's), an index on a live table it does not make or v1's named index on other columns,
    a view or trigger (one could use a live table) is refused, changing nothing."""
    database = v1_cb1159c_database(tmp_path)
    for sql in (statement,) if isinstance(statement, str) else statement:
        execute(database, sql)
    digest = file_digest(database.path)

    with pytest.raises(migrations.MigrationError) as error:
        migrations.stamp_detected(database)

    assert str(error.value) == (
        f"database {database.path} has v1 cb1159c's live tables or job.remote_observation but not its schema, so it "
        f"is not stamped and nothing was changed:\n  compared with v1 cb1159c's schema:\n    {difference}"
    )
    assert file_digest(database.path) == digest
    assert migrations.current_revision(database) is None


@pytest.mark.parametrize(
    ("statement", "difference"),
    [
        ("CREATE TABLE live_frame (id INTEGER PRIMARY KEY)", "unexpected table live_frame"),
        ('ALTER TABLE "job" ADD COLUMN live_session_id INTEGER', "job: unexpected column live_session_id"),
        ("CREATE VIEW observed AS SELECT job_id, remote_observation FROM job", "unexpected view observed"),
    ],
    ids=["a-table", "a-column", "a-view"],
)
def test_stamp_detect_refuses_v1_cb1159cs_schema_without_live_tables_with_objects_it_does_not_know(
    tmp_path, statement, difference
):
    """v1 ``cb1159c``'s schema without its live tables (its migration script's output) is taken only exactly, as the
    one with them: anything else is refused with how it differs from it, changing nothing."""
    database = DatabaseSettings(path=create_v1_cb1159c_database(tmp_path / "cb1159c.db"))
    execute(database, statement)
    digest = file_digest(database.path)

    with pytest.raises(migrations.MigrationError) as error:
        migrations.stamp_detected(database)

    assert str(error.value) == (
        f"database {database.path} has v1 cb1159c's live tables or job.remote_observation but not its schema, so it "
        "is not stamped and nothing was changed:\n  compared with v1 cb1159c's schema without its live tables:\n    "
        f"{difference}"
    )
    assert file_digest(database.path) == digest
    assert migrations.current_revision(database) is None


def test_stamp_detect_drops_the_live_tables_and_stamps_in_one_transaction(tmp_path, monkeypatch):
    """A failure after the drops (here the stamp's own, the live tables already gone in its connection) leaves the
    live tables, their rows and no revision."""
    database = v1_cb1159c_database(tmp_path)
    before = schema_objects(database.path), {table: _rows(database, table) for table in CB1159C_LIVE_TABLES}
    run = migrations._run  # the reference databases of the detection are built through it too
    dropped: list[set[str]] = []

    def failing(operation: Any, database: DatabaseSettings, connection: Any, revision: str) -> None:
        if operation is not migrations.command.stamp:
            return run(operation, database, connection, revision)
        dropped.append({str(table) for table in CB1159C_LIVE_TABLES} - migrations.table_columns(connection).keys())
        raise migrations.MigrationError("the stamp failed")

    monkeypatch.setattr(migrations, "_run", failing)
    with pytest.raises(migrations.MigrationError, match="the stamp failed"):
        migrations.stamp_detected(database)

    assert dropped == [set(CB1159C_LIVE_TABLES)]
    assert (schema_objects(database.path), {table: _rows(database, table) for table in CB1159C_LIVE_TABLES}) == before
    assert migrations.current_revision(database) is None


@pytest.mark.parametrize("queue_job_id", ["live_1", "live_1_5f0c2a9e4b7d"], ids=["created", "queued"])
def test_a_v1_live_sessions_jobs_stay_as_history_and_never_read_as_v2_live_runs(tmp_path, queue_job_id):
    """The rule, exactly: a live run's queue identity is ``live_<its job id>`` and, once ended, its run directory holds
    ``live.json``. v1 gives a session's jobs ``live_<session id>`` at its creation, which may be a job's own (session 1,
    job 1), and ``live_<session id>_<hex>`` once it queues them; their run directories have v1's ``run.json``, no
    ``live.json``. Stamped and upgraded, job 1 is no live run and ``GET /live/session`` finds no session."""
    database = v1_cb1159c_database(tmp_path, queue_job_id=queue_job_id)
    run_directory = tmp_path / "runs" / "rec_1"
    run_directory.mkdir(parents=True)
    (run_directory / "run.json").write_text('{"format": "laue-portal-run-summary", "live_session_id": 1}')
    execute(database, "UPDATE job SET run_directory = ? WHERE job_id = 1", (str(run_directory),))
    migrations.stamp_detected(database)
    migrations.upgrade(database)

    engine = database_engine(database)
    try:
        with Session(engine) as session:
            job = session.get(models.Job, 1)
            assert job is not None and (job.queue_job_id, job.status) == (queue_job_id, 2)
            assert has_live_marker(job) is (queue_job_id == "live_1")
            assert not is_live_run(job)
        assert latest_session(sessionmaker(engine)).session is None
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Commands: stamp, upgrade, check, current


@pytest.mark.parametrize(("commit", "revision"), [("dde1bde", "0001"), ("3ad1557", "0002")])
def test_a_v1_database_is_stamped_then_upgraded(tmp_path, commit, revision):
    database = DatabaseSettings(path=create_v1_database(tmp_path / "v1.db", commit))
    assert migrations.current_revision(database) is None

    with pytest.raises(migrations.MigrationError, match="no Alembic revision.*laueportal db stamp --detect"):
        migrations.upgrade(database)
    assert migrations.current_revision(database) is None

    assert migrations.detect_v1_schema(database) == migrations.V1Schema(revision, f"v1 {commit}")
    migrations.stamp(database, revision)
    assert migrations.upgrade(database) == (revision, "0009")
    assert migrations.check(database) == []


def test_stamp_refuses_the_other_v1_schema_and_names_it(tmp_path):
    """Each v1 schema is accepted for its own revision only; the refusal says which revision it has."""
    dde1bde = DatabaseSettings(path=create_v1_database(tmp_path / "dde1bde.db"))
    v3ad1557 = DatabaseSettings(path=create_v1_database(tmp_path / "3ad1557.db", "3ad1557"))
    digests = {database.path: file_digest(database.path) for database in (dde1bde, v3ad1557)}

    with pytest.raises(migrations.MigrationError) as error:
        migrations.stamp(v3ad1557, "0001")
    assert str(error.value) == (
        f"database {v3ad1557.path} does not have the schema of revision 0001, so it was not stamped:\n  "
        + "\n  ".join(f"job: unexpected column {column}" for column in sorted(REMOTE_COLUMNS))
        + "\nIt has the schema of revision 0002 (v1 3ad1557): stamp 0002."
    )
    with pytest.raises(migrations.MigrationError) as error:
        migrations.stamp(dde1bde, "0002")
    assert str(error.value) == (
        f"database {dde1bde.path} does not have the schema of revision 0002, so it was not stamped:\n  "
        + "\n  ".join(f"job: missing column {column}" for column in sorted(REMOTE_COLUMNS))
        + "\nIt has the schema of revision 0001 (v1 dde1bde): stamp 0001."
    )

    assert {database.path: file_digest(database.path) for database in (dde1bde, v3ad1557)} == digests
    assert migrations.current_revision(dde1bde) is migrations.current_revision(v3ad1557) is None

    # Under Alembic already: the refusal names its revision, and asks for no stamp.
    migrations.stamp(v3ad1557, "0002")
    with pytest.raises(migrations.MigrationError) as error:
        migrations.stamp(v3ad1557, "0001")
    assert str(error.value).endswith(
        "job: unexpected column submission_id\nIts tables match revision 0002 (v1 3ad1557); it is at revision 0002."
    )
    assert migrations.current_revision(v3ad1557) == "0002"


def test_detection_picks_a_v1_revision_and_refuses_anything_else(tmp_path):
    def schema(name: str, *statements: str, commit: str = "dde1bde") -> DatabaseSettings:
        database = DatabaseSettings(path=create_v1_database(tmp_path / name, commit))
        for statement in statements:
            execute(database, statement)
        return database

    appended = schema("appended.db")
    add_remote_columns_at_the_end(appended)
    assert migrations.detect_v1_schema(schema("dde1bde.db")) == migrations.V1Schema("0001", "v1 dde1bde")
    assert migrations.detect_v1_schema(schema("3ad1557.db", commit="3ad1557")) == migrations.V1Schema(
        "0002", "v1 3ad1557"
    )
    assert migrations.detect_v1_schema(appended) == migrations.V1Schema("0002", "v1 3ad1557")

    partial = schema("partial.db", 'ALTER TABLE "job" ADD COLUMN backend VARCHAR')
    legacy = DatabaseSettings(path=tmp_path / "legacy.db")
    execute(legacy, "CREATE TABLE peakindex (peakindex_id INTEGER PRIMARY KEY)")
    stamped = database_at(tmp_path, "0001", name="stamped.db")
    digests = {database.path: file_digest(database.path) for database in (partial, legacy, stamped)}

    with pytest.raises(migrations.MigrationError) as error:
        migrations.detect_v1_schema(partial)
    assert str(error.value) == (
        f"database {partial.path} has neither v1 schema, so its revision cannot be detected:\n"
        "  compared with revision 0001 (v1 dde1bde):\n    job: unexpected column backend\n"
        "  compared with revision 0002 (v1 3ad1557):\n    "
        + "\n    ".join(f"job: missing column {column}" for column in sorted(REMOTE_COLUMNS[1:]))
    )
    with pytest.raises(migrations.MigrationError, match="has neither v1 schema") as error:
        migrations.detect_v1_schema(legacy)
    assert "missing table metadata" in str(error.value) and "unexpected table peakindex" in str(error.value)
    with pytest.raises(migrations.MigrationError, match="is already at revision 0001; detection is for a v1 database"):
        migrations.detect_v1_schema(stamped)

    assert {database.path: file_digest(database.path) for database in (partial, legacy, stamped)} == digests


def test_stamp_refuses_a_database_without_that_schema(tmp_path):
    legacy = tmp_path / "legacy.db"
    connection = sqlite3.connect(legacy)
    connection.executescript(
        'CREATE TABLE metadata ("scanNumber" INTEGER PRIMARY KEY, time DATETIME);'
        "CREATE TABLE peakindex (peakindex_id INTEGER PRIMARY KEY);"
        "CREATE TABLE job (job_id INTEGER PRIMARY KEY, status INTEGER);"
    )
    connection.close()
    database = DatabaseSettings(path=legacy)

    with pytest.raises(migrations.MigrationError) as error:
        migrations.stamp(database, "0001")

    text = str(error.value)
    assert "does not have the schema of revision 0001" in text
    assert "missing table indexing_run" in text and "unexpected table peakindex" in text
    assert "job: missing column computer_name" in text
    assert "metadata.time: DATETIME, expected DATETIME NOT NULL" in text
    assert migrations.current_revision(database) is None


def test_check_reports_what_is_wrong(tmp_path):
    missing = DatabaseSettings(path=tmp_path / "missing.db")
    with pytest.raises(migrations.MigrationError, match="does not exist"):
        migrations.check(missing)
    for command in (migrations.current_revision, lambda database: migrations.stamp(database, "0001")):
        with pytest.raises(migrations.MigrationError, match="does not exist"):
            command(missing)
    assert not missing.path.exists()

    unstamped = DatabaseSettings(path=create_v1_database(tmp_path / "v1.db"))
    assert migrations.check(unstamped) == [
        f"database {unstamped.path} is not under Alembic (no revision): a v1 database is stamped with "
        "`laueportal db stamp --detect` (0001 for v1 dde1bde's schema, 0002 for v1 3ad1557's), then upgraded"
    ]

    behind = database_at(tmp_path, "0004", name="behind.db")
    assert migrations.check(behind) == [
        f"database {behind.path} is at revision 0004, head is 0009: run `laueportal db upgrade`"
    ]

    drifted = database_at(tmp_path, "head", name="drifted.db")
    execute(drifted, "ALTER TABLE job ADD COLUMN extra VARCHAR")
    execute(drifted, "DROP INDEX ix_indexing_run_method")
    assert sorted(migrations.check(drifted)) == [
        "differs from head's DDL: missing index ix_indexing_run_method",
        "differs from head's DDL: table job: unexpected column `extra VARCHAR`",
        "differs from the models: add_index ix_indexing_run_method on indexing_run",
        "differs from the models: remove_column job.extra",
    ]


@pytest.mark.parametrize(
    ("recorded", "schema"),
    [
        pytest.param("0002", "0003", id="early-0002-snake-case"),
        pytest.param("0003", "0004", id="early-0003-calib-dropped"),
        pytest.param("0004", "0005", id="early-0004-head"),  # the early numbering's development and e2e databases
        pytest.param("0005", "0005", id="upgraded-without-the-guard"),
    ],
)
def test_a_database_migrated_with_the_early_numbering_is_refused_and_left_as_it_was(tmp_path, recorded, schema):
    """With the early numbering, 0002-0004 were the first cleanups (now 0003-0005). A database migrated with it records
    one of them but lacks 0002's job columns. Upgrading it would run the wrong migrations (0005 again on the early head,
    recording head without the columns), so every command refuses it, changes nothing, and says to rebuild it."""
    database = database_with_early_numbering(tmp_path, recorded, schema)
    _insert(database, "job", {"job_id": 7, "computer_name": "host", "status": 2, "priority": 0})
    digest = file_digest(database.path)
    problem = (
        f"database {database.path} is at revision {recorded}, but its job table lacks backend, submission_id, "
        "external_id, external_state, external_url, the columns of revision 0002: it was migrated with the early "
        "numbering of the revisions, before their 0002-0004 moved to 0003-0005 (0002 is now v1 3ad1557's "
        "remote-execution columns). Its revision does not name its schema, so it cannot be migrated: rebuild it "
        "with `laueportal seed demo --force` (a demo database)"
    )

    for target in ("head", "0005"):
        with pytest.raises(migrations.MigrationError) as error:
            migrations.upgrade(database, target)
        assert str(error.value) == f"{problem} (nothing was changed)"
    with pytest.raises(migrations.MigrationError) as error:
        migrations.downgrade(database, "0001")
    assert str(error.value) == f"{problem} (nothing was changed)"
    assert migrations.check(database) == [problem]
    assert migrations.describe_current(database) == (
        f"{recorded} (the early numbering of the revisions: job lacks revision 0002's columns; rebuild it with "
        "`laueportal seed demo --force` (a demo database))"
    )
    with pytest.raises(migrations.MigrationError) as error:
        migrations.stamp(database, "0005")
    assert "job: missing column backend" in str(error.value) and str(error.value).endswith(f"\n{problem}")
    with pytest.raises(migrations.MigrationError) as error:
        migrations.detect_v1_schema(database)
    assert str(error.value).endswith(f"not under Alembic yet\n{problem}")

    assert file_digest(database.path) == digest
    assert migrations.current_revision(database) == recorded


def test_every_revision_from_0002_has_the_remote_columns(tmp_path):
    """What the early numbering's guard relies on: a database this chain migrated to 0002 or later has 0002's columns,
    so none is mistaken for one the early numbering migrated."""
    database = DatabaseSettings(path=tmp_path / "chain.db")
    for revision in REVISIONS:
        migrations.upgrade(database, revision)
        assert migrations.describe_current(database).startswith(f"{revision} (head")
        assert (set(REMOTE_COLUMNS) <= set(columns(database, "job"))) == (revision != "0001")
    assert migrations.check(database) == []


BOUNDED = "CONSTRAINT ck_job_counters_bounded CHECK (n_succeeded + n_failed + n_not_run <= n_inputs)"
NON_NEGATIVE = (
    "CONSTRAINT ck_job_counters_non_negative CHECK (n_inputs >= 0 AND n_succeeded >= 0 AND n_failed >= 0 "
    "AND n_not_run >= 0)"
)


@pytest.mark.parametrize(
    ("table", "edit", "problem"),
    [
        pytest.param(
            "reconstruction_run",
            lambda sql: sql.replace("method IN ('wire')", "method IN ('wire', 'ca')"),
            "table reconstruction_run: constraint `CONSTRAINT ck_reconstruction_run_method CHECK (method IN "
            "('wire', 'ca'))`, expected `CONSTRAINT ck_reconstruction_run_method CHECK (method IN ('wire'))`",
            id="method-check-still-allows-ca",
        ),
        pytest.param(
            "job",
            lambda sql: re.sub(r",\s*CONSTRAINT ck_job_counters_bounded CHECK \([^)]*\)", "", sql),
            f"table job: missing constraint `{BOUNDED}`",
            id="job-counter-check-missing",
        ),
        pytest.param(
            "job",
            lambda sql: sql.replace("n_inputs INTEGER DEFAULT '0'", "n_inputs INTEGER DEFAULT '5'"),
            "table job: column `n_inputs INTEGER DEFAULT '5' NOT NULL`, expected `n_inputs INTEGER DEFAULT '0' NOT NULL`",
            id="job-counter-default",
        ),
    ],
)
def test_check_compares_the_ddl_with_head(tmp_path, table, edit, problem):
    """What Alembic's ``compare_metadata`` does not see (CHECK constraints, server defaults) fails the check:
    the database's DDL is compared with a new database at head."""
    database = database_at(tmp_path, "head")
    for row in V1_ROWS["job"]:  # rows survive the rebuild (timestamps left out: they are v1's local text)
        _insert(database, "job", {name: value for name, value in row.items() if name not in TIMESTAMP_COLUMNS["job"]})
    rebuild_table(database, table, edit)

    assert migrations.check(database) == [f"differs from head's DDL: {problem}"]
    assert execute(database, "SELECT job_id FROM job") == [(1,), (2,)]


def test_check_ignores_the_order_of_columns_and_constraints(tmp_path):
    """Production's ``job`` (v1's migration script) has its two CHECK constraints in the other order than
    ``create_all`` and a quoted name; that is the same schema."""
    database = database_at(tmp_path, "head")

    stored = rebuild_table(
        database,
        "job",
        lambda sql: sql.replace(f"{NON_NEGATIVE}, \n\t{BOUNDED}", f"{BOUNDED}, \n\t{NON_NEGATIVE}"),
    )

    assert stored.startswith('CREATE TABLE "job"') and stored.index(BOUNDED) < stored.index(NON_NEGATIVE)
    assert migrations.check(database) == []


def test_ddl_normalization_keeps_literals_and_unquotes_identifiers():
    sql = 'CREATE TABLE "job" (\n\t"scan_number"  INTEGER DEFAULT \'a  "b"\', \n\t"scanNumber" INTEGER\n)'
    assert migrations.normalize_ddl(sql) == (
        'CREATE TABLE job ( scan_number INTEGER DEFAULT \'a  "b"\', "scanNumber" INTEGER )'
    )


def test_upgrade_never_goes_down_and_changes_nothing_when_refused(tmp_path):
    database = database_at(tmp_path, "head")
    digest = file_digest(database.path)

    for revision, named in (("0003", "0003"), ("base", "base")):
        with pytest.raises(migrations.MigrationError) as error:
            migrations.upgrade(database, revision)
        assert str(error.value) == (
            f"database {database.path} is at revision 0009, past the requested {named}: upgrade does not go down, "
            "and downgrades are not available from the command line"
        )
    assert migrations.current_revision(database) == "0009"
    assert file_digest(database.path) == digest
    assert migrations.upgrade(database, "0009") == ("0009", "0009")


def test_upgrade_creates_a_new_database_but_not_its_directory(tmp_path):
    with pytest.raises(migrations.MigrationError, match="does not exist"):
        migrations.upgrade(DatabaseSettings(path=tmp_path / "nowhere" / "new.db"))
    assert not (tmp_path / "nowhere").exists()

    with pytest.raises(migrations.MigrationError, match="unknown revision 'nope'"):
        migrations.upgrade(DatabaseSettings(path=tmp_path / "new.db"), "nope")
    assert not (tmp_path / "new.db").exists()

    database = DatabaseSettings(path=tmp_path / "new.db", journal_mode="wal")
    assert migrations.upgrade(database, "0003") == (None, "0003")
    assert migrations.upgrade(database) == ("0003", "0009")
    assert migrations.upgrade(database) == ("0009", "0009")
    assert execute(database, "PRAGMA journal_mode") == [("wal",)]


# ---------------------------------------------------------------------------
# 0009: LaueMatching's parameters


def test_0009_adds_the_table_and_its_downgrade_refuses_to_drop_a_runs_parameters(tmp_path):
    database = database_at(tmp_path, "0008")
    assert migrations.upgrade(database) == ("0008", "0009")
    assert migrations.check(database) == []
    columns = [row[1] for row in execute(database, "PRAGMA table_info(laue_matching_indexing_parameters)")]
    assert columns[:2] == ["indexing_id", "filename_prefixes"]

    execute(database, "PRAGMA foreign_keys = OFF")
    values = (1, "[]", "1", 1, "g", "c", "n", 0, "percentile", 99.8, 0.0, 4, 1, 5.0, 30.0, 30, 8, 4, 50.0, 3.0, 1)
    execute(
        database,
        "INSERT INTO laue_matching_indexing_parameters (indexing_id, filename_prefixes, scan_points, scan_points_len, "
        "geometry_file, crystal_file, forward_cache, create_forward_cache, threshold_method, threshold_percentile, "
        "threshold_value, min_area, watershed_image, elo, ehi, max_nr_laue_spots, min_nr_spots, min_good_spots, "
        f"min_intensity, max_angle, robust_filter) VALUES ({', '.join('?' * len(values))})",
        values,
    )
    digest = file_digest(database.path)
    with pytest.raises(migrations.MigrationError, match="holds the parameters of 1 run"):
        migrations.downgrade(database, "0008")
    assert migrations.current_revision(database) == "0009"
    assert file_digest(database.path) == digest

    execute(database, "DELETE FROM laue_matching_indexing_parameters")
    assert migrations.downgrade(database, "0008") == ("0009", "0008")
    assert execute(database, "SELECT name FROM sqlite_master WHERE name = 'laue_matching_indexing_parameters'") == []


def test_0009_refuses_a_database_that_already_has_the_table(tmp_path):
    database = database_at(tmp_path, "0008")
    execute(database, "CREATE TABLE laue_matching_indexing_parameters (indexing_id INTEGER)")
    digest = file_digest(database.path)
    with pytest.raises(migrations.MigrationError, match="already has laue_matching_indexing_parameters"):
        migrations.upgrade(database)
    assert migrations.current_revision(database) == "0008"
    assert file_digest(database.path) == digest
