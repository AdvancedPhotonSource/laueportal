"""``laueportal db upgrade|stamp|current|check|backup``, and the protected originals that no command writes."""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from laueportal import cli
from laueportal.core import db, migrations
from laueportal.core.settings import DatabaseSettings, get_settings
from laueportal.core.timeutil import utcnow
from tests.migration_support import (
    add_v1_live_tables,
    create_v1_cb1159c_database,
    create_v1_database,
    database_at,
    database_with_early_numbering,
    execute,
    file_digest,
    journal_header,
)
from tests.settings_support import SOURCE_DIR, mount_table

REPO_ROOT = SOURCE_DIR.parent.parent


def configure(env: pytest.MonkeyPatch, tmp_path: Path, database: Path, protected: list[Path] | None = None) -> Path:
    config = tmp_path / "test.toml"
    listed = ", ".join(f'"{path}"' for path in protected or [])
    config.write_text(
        f'[database]\npath = "{database}"\njournal_mode = "wal"\nprotected = [{listed}]\n'
        f'[files]\nruns_root = "{tmp_path}/runs"\n'
    )
    env.setenv("LAUEPORTAL_CONFIG", str(config))
    return config


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    status = cli.main(list(argv))
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def test_db_commands_build_check_and_report_the_configured_database(tmp_path, clean_env, capsys):
    database = tmp_path / "dev.db"
    configure(clean_env, tmp_path, database)

    status, _out, err = run(capsys, "db", "current")
    assert (status, err.strip()) == (1, f"laueportal db current: database {database} does not exist")
    assert run(capsys, "db", "check")[0] == 1
    assert not database.exists()

    assert run(capsys, "db", "upgrade") == (0, f"{database}: upgraded (empty) -> 0009\n", "")
    assert run(capsys, "db", "current") == (0, "0009 (head)\n", "")
    status, out, err = run(capsys, "db", "check")
    assert (status, out.splitlines()[-1], err) == (0, f"{database}: OK", "")
    assert run(capsys, "db", "upgrade", "head") == (0, f"{database}: already at 0009\n", "")


def test_db_check_and_current_say_what_to_do(tmp_path, clean_env, capsys):
    configure(clean_env, tmp_path, tmp_path / "configured.db")
    other = tmp_path / "other.db"

    assert run(capsys, "db", "upgrade", "0004", "--db", str(other))[:2] == (0, f"{other}: upgraded (empty) -> 0004\n")
    assert run(capsys, "db", "current", "--db", str(other))[1] == "0004 (head is 0009: run `laueportal db upgrade`)\n"
    status, _out, err = run(capsys, "db", "check", "--db", str(other))
    assert status == 1
    assert (
        err == f"laueportal db check: database {other} is at revision 0004, head is 0009: run `laueportal db upgrade`\n"
    )

    status, _out, err = run(capsys, "db", "upgrade", "0099", "--db", str(other))
    assert status == 1 and "unknown revision '0099'" in err
    assert not (tmp_path / "configured.db").exists()  # --db replaced the configured database everywhere


@pytest.mark.parametrize(("commit", "revision"), [("dde1bde", "0001"), ("3ad1557", "0002")])
def test_a_v1_database_moves_to_head_by_stamp_then_upgrade(tmp_path, clean_env, capsys, commit, revision):
    v1 = create_v1_database(tmp_path / "v1.db", commit)
    configure(clean_env, tmp_path, v1)

    status, _out, err = run(capsys, "db", "upgrade")
    assert status == 1 and "has tables but no Alembic revision" in err and "laueportal db stamp --detect" in err
    assert run(capsys, "db", "current")[1] == (
        "none (not under Alembic; head is 0009): a v1 database is stamped with `laueportal db stamp --detect` "
        "(0001 for v1 dde1bde's schema, 0002 for v1 3ad1557's)\n"
    )
    status, _out, err = run(capsys, "db", "check")
    assert status == 1 and "is not under Alembic" in err

    assert run(capsys, "db", "stamp", revision) == (0, f"{v1}: stamped {revision}\n", "")
    assert run(capsys, "db", "upgrade")[:2] == (0, f"{v1}: upgraded {revision} -> 0009\n")
    assert run(capsys, "db", "check")[0] == 0


@pytest.mark.parametrize(("commit", "revision"), [("dde1bde", "0001"), ("3ad1557", "0002")])
def test_db_stamp_detect_stamps_the_v1_revision_of_the_schema(tmp_path, clean_env, capsys, commit, revision):
    v1 = create_v1_database(tmp_path / "v1.db", commit)
    configure(clean_env, tmp_path, v1)

    assert run(capsys, "db", "stamp", "--detect") == (
        0,
        f"{v1}: stamped {revision} (detected: v1 {commit}'s schema)\n",
        "",
    )
    assert run(capsys, "db", "current")[1] == f"{revision} (head is 0009: run `laueportal db upgrade`)\n"
    status, _out, err = run(capsys, "db", "stamp", "--detect")
    assert (status, err) == (
        1,
        f"laueportal db stamp: database {v1} is already at revision {revision}; detection is for a v1 database "
        "that is not under Alembic yet\n",
    )


def test_db_stamp_detect_refuses_a_database_with_neither_v1_schema(tmp_path, clean_env, capsys):
    partial = create_v1_database(tmp_path / "partial.db")
    with sqlite3.connect(partial) as connection:
        connection.execute('ALTER TABLE "job" ADD COLUMN backend VARCHAR')
    connection.close()
    configure(clean_env, tmp_path, partial)
    digest = file_digest(partial)

    status, out, err = run(capsys, "db", "stamp", "--detect")

    assert (status, out) == (1, "")
    assert err.startswith(f"laueportal db stamp: database {partial} has neither v1 schema, so its revision cannot be")
    assert "job: unexpected column backend" in err and "job: missing column external_url" in err
    assert file_digest(partial) == digest
    assert migrations.current_revision(DatabaseSettings(path=partial)) is None


def test_db_stamp_detect_takes_a_v1_cb1159c_database_once_its_live_sessions_have_ended(tmp_path, clean_env, capsys):
    """``db stamp 0002`` refuses v1 ``cb1159c``'s schema and says how to stamp it; ``--detect`` refuses it, changing
    nothing, while a v1 live session is open, and once it has ended drops the live tables and stamps 0002, from which
    ``db upgrade`` reaches head."""
    v1 = create_v1_cb1159c_database(tmp_path / "cb1159c.db")
    database = DatabaseSettings(path=v1)
    add_v1_live_tables(database)
    execute(
        database,
        "INSERT INTO live_session (id, state, control_directory, recipe, revision, created_at) "
        "VALUES (1, 'stopping', '/runs/live/session_1', '{}', 2, '2026-10-20 10:00:00.000000')",
    )
    configure(clean_env, tmp_path, v1)
    digest = file_digest(v1)

    status, _out, err = run(capsys, "db", "stamp", "0002")
    assert status == 1 and "job: unexpected column remote_observation" in err
    assert err.endswith(
        "It has v1 cb1159c's schema: stamp it with `laueportal db stamp --detect`, which drops its live tables "
        "(live_point, live_checkpoint, live_session) and records 0002.\n"
    )
    status, out, err = run(capsys, "db", "stamp", "--detect")
    assert (status, out) == (1, "")
    assert err.startswith(f"laueportal db stamp: database {v1} has v1 cb1159c's live sessions that have not ended")
    assert "\n  live session 1: 'stopping'\n" in err
    assert file_digest(v1) == digest

    execute(database, "UPDATE live_session SET state = 'finished'")
    assert run(capsys, "db", "stamp", "--detect") == (
        0,
        f"{v1}: stamped 0002 (detected: v1 cb1159c's schema; its live tables live_point, live_checkpoint, "
        "live_session dropped)\n",
        "",
    )
    assert run(capsys, "db", "upgrade")[:2] == (0, f"{v1}: upgraded 0002 -> 0009\n")
    assert run(capsys, "db", "check")[0] == 0


def test_db_stamp_detect_takes_v1_cb1159cs_schema_without_its_live_tables(tmp_path, clean_env, capsys):
    """The migration script of v1 ``cb1159c`` gives a database ``job.remote_observation`` and never the live tables.
    ``db stamp 0002`` refuses it and says how to stamp it; ``--detect`` stamps 0002, dropping nothing, and
    ``db upgrade`` and ``db check`` pass."""
    v1 = create_v1_cb1159c_database(tmp_path / "cb1159c.db")
    configure(clean_env, tmp_path, v1)

    status, _out, err = run(capsys, "db", "stamp", "0002")
    assert status == 1 and "job: unexpected column remote_observation" in err
    assert err.endswith(
        "It has v1 cb1159c's schema without its live tables: stamp it with `laueportal db stamp --detect`, which "
        "records 0002 and keeps job.remote_observation.\n"
    )
    assert run(capsys, "db", "stamp", "--detect") == (
        0,
        f"{v1}: stamped 0002 (detected: v1 cb1159c's schema without its live tables)\n",
        "",
    )
    assert run(capsys, "db", "upgrade")[:2] == (0, f"{v1}: upgraded 0002 -> 0009\n")
    assert run(capsys, "db", "check")[0] == 0


def test_db_stamp_takes_a_revision_or_detect(tmp_path, clean_env, capsys):
    configure(clean_env, tmp_path, create_v1_database(tmp_path / "v1.db"))
    for argv in (["db", "stamp"], ["db", "stamp", "0001", "--detect"]):
        with pytest.raises(SystemExit) as exit_:
            cli.main(argv)
        assert exit_.value.code == 2
        assert "revision" in capsys.readouterr().err


def test_db_stamp_refuses_a_database_with_another_schema(tmp_path, clean_env, capsys):
    legacy = tmp_path / "legacy.db"
    with sqlite3.connect(legacy) as connection:
        connection.execute("CREATE TABLE peakindex (peakindex_id INTEGER PRIMARY KEY)")
    connection.close()
    configure(clean_env, tmp_path, legacy)

    status, _out, err = run(capsys, "db", "stamp", "0001")

    assert status == 1
    assert "does not have the schema of revision 0001, so it was not stamped" in err
    assert "missing table metadata" in err and "unexpected table peakindex" in err
    assert migrations.current_revision(DatabaseSettings(path=legacy)) is None


def test_db_upgrade_refuses_a_revision_below_the_database(tmp_path, clean_env, capsys):
    database = tmp_path / "dev.db"
    configure(clean_env, tmp_path, database)
    assert run(capsys, "db", "upgrade")[0] == 0
    digest = file_digest(database)

    for revision in ("0003", "base"):
        status, out, err = run(capsys, "db", "upgrade", revision)
        assert (status, out) == (1, "")
        assert err == (
            f"laueportal db upgrade: database {database} is at revision 0009, past the requested {revision}: "
            "upgrade does not go down, and downgrades are not available from the command line\n"
        )
    assert file_digest(database) == digest
    assert run(capsys, "db", "current")[1] == "0009 (head)\n"


def test_db_commands_refuse_a_database_migrated_with_the_early_numbering(tmp_path, clean_env, capsys):
    """A dev database migrated with the early numbering of the revisions: stamped 0004 (then the head), and without the
    remote-execution columns. upgrade refuses and changes nothing, check fails and current says what to do: rebuild
    it."""
    path = database_with_early_numbering(tmp_path, "0004", "0005", name="dev.db").path
    configure(clean_env, tmp_path, path)
    digest = file_digest(path)
    rebuild = "rebuild it with `laueportal seed demo --force` (a demo database)"
    problem = (
        f"database {path} is at revision 0004, but its job table lacks backend, submission_id, external_id, "
        "external_state, external_url, the columns of revision 0002: it was migrated with the early numbering of the "
        "revisions, before their 0002-0004 moved to 0003-0005 (0002 is now v1 3ad1557's remote-execution "
        f"columns). Its revision does not name its schema, so it cannot be migrated: {rebuild}"
    )

    assert run(capsys, "db", "upgrade") == (1, "", f"laueportal db upgrade: {problem} (nothing was changed)\n")
    status, _out, err = run(capsys, "db", "check")
    assert (status, err) == (1, f"laueportal db check: {problem}\n")
    assert run(capsys, "db", "current") == (
        0,
        f"0004 (the early numbering of the revisions: job lacks revision 0002's columns; {rebuild})\n",
        "",
    )

    assert file_digest(path) == digest
    assert migrations.current_revision(DatabaseSettings(path=path)) == "0004"


def _rollback_journal_databases(tmp_path: Path) -> dict[str, Path]:
    head = database_at(tmp_path, "head", name="head.db", journal_mode="delete").path
    legacy = tmp_path / "legacy.db"
    connection = sqlite3.connect(legacy)
    connection.execute("CREATE TABLE peakindex (peakindex_id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    return {
        "head": head,
        "v1": create_v1_database(tmp_path / "v1.db"),
        "v1-3ad1557": create_v1_database(tmp_path / "v1-3ad1557.db", "3ad1557"),
        "legacy": legacy,
        "early": database_with_early_numbering(tmp_path, "0004", "0005", journal_mode="delete").path,
    }


@pytest.mark.parametrize(
    ("database", "argv", "status"),
    [
        ("head", ["db", "current"], 0),
        ("head", ["db", "check"], 0),
        ("head", ["db", "upgrade", "0003"], 1),  # refused: below the database's revision
        ("head", ["db", "stamp", "0001"], 1),  # refused: not 0001's schema
        ("head", ["db", "stamp", "--detect"], 1),  # refused: already under Alembic
        ("v1", ["db", "current"], 0),
        ("v1", ["db", "check"], 1),
        ("v1", ["db", "upgrade"], 1),  # refused: tables but no revision
        ("v1", ["db", "stamp", "0002"], 1),  # refused: v1 dde1bde's schema is 0001's
        ("v1-3ad1557", ["db", "stamp", "0001"], 1),  # refused: v1 3ad1557's schema is 0002's
        ("legacy", ["db", "stamp", "0001"], 1),  # refused: not v1's schema
        ("legacy", ["db", "stamp", "--detect"], 1),  # refused: neither v1 schema
        ("early", ["db", "current"], 0),
        ("early", ["db", "check"], 1),
        ("early", ["db", "upgrade"], 1),  # refused: migrated with the early numbering
    ],
)
def test_reading_and_refusing_leave_a_rollback_journal_database_as_it_was(
    tmp_path, clean_env, capsys, database, argv, status
):
    """Production's database is in rollback-journal mode (on NFS, where WAL is unsafe). Commands that only
    read or that refuse must not write it, not even the journal mode the settings ask for (WAL here)."""
    path = _rollback_journal_databases(tmp_path)[database]
    configure(clean_env, tmp_path, path)  # journal_mode = "wal"
    assert journal_header(path) == (1, 1)
    digest = file_digest(path)

    assert run(capsys, *argv)[0] == status

    assert journal_header(path) == (1, 1)
    assert file_digest(path) == digest
    assert not [companion for companion in tmp_path.iterdir() if companion.name.startswith(f"{path.name}-")]


def test_reading_works_on_a_read_only_file_and_writing_fails_in_one_line(tmp_path, clean_env, capsys):
    path = database_at(tmp_path, "head", name="read-only.db", journal_mode="delete").path
    path.chmod(0o444)
    configure(clean_env, tmp_path, path)

    assert run(capsys, "db", "current") == (0, "0009 (head)\n", "")
    status, out, err = run(capsys, "db", "check")
    assert (status, out.splitlines()[-1], err) == (0, f"{path}: OK", "")
    for argv in (["db", "stamp", "0009"], ["db", "upgrade"]):
        status, out, err = run(capsys, *argv)
        assert (status, out) == (1, "")
        assert err == f"laueportal {argv[0]} {argv[1]}: database {path}: attempt to write a readonly database\n"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A stand-in workspace whose ``db/`` holds read-only originals, temporary names a test's settings protect
    (``database.protected``, ``ORIGINALS``): two databases with their files and a folder, ``migrated.db``'s
    ``.migration`` record."""
    database_directory = tmp_path / "ws" / "db"
    record = database_directory / "migrated.db.migration"
    (record / "jobs" / "17").mkdir(parents=True)
    files = [database_directory / f"{name}{suffix}" for name in ORIGINALS[:2] for suffix in cli.PROTECTED_SUFFIXES]
    files += [record / "report.json", record / "jobs" / "17" / "request.json"]
    for path in files:
        path.write_text(f"stand-in for {path.name}")
        path.chmod(0o444)
    return tmp_path / "ws"


ORIGINALS = ("snapshot.db", "migrated.db", "migrated.db.migration")
"""The stand-in workspace's protected originals, in its ``db/``."""


def protected(workspace: Path) -> list[Path]:
    return [workspace / "db" / name for name in ORIGINALS]


def _originals(workspace: Path) -> dict[Path, tuple[str, int]]:
    """Every file under the stand-in ``db/``: its digest and mode."""
    return {
        path: (file_digest(path), path.stat().st_mode)
        for path in sorted((workspace / "db").rglob("*"))
        if path.is_file()
    }


def test_make_db_upgrade_and_db_check(tmp_path):
    """The Makefile wrappers (they source scripts/env.sh, which keeps a LAUEPORTAL_CONFIG already set)."""
    database = tmp_path / "make.db"
    config = tmp_path / "make.toml"
    config.write_text(f'[database]\npath = "{database}"\n[files]\nruns_root = "{tmp_path}/runs"\n')
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("LAUEPORTAL_") and key not in {"MAKEFLAGS", "MAKELEVEL", "MFLAGS"}
    }
    env["LAUEPORTAL_CONFIG"] = str(config)

    for target, expected in (
        ("db-check", f"database {database} does not exist"),
        ("db-upgrade", f"{database}: upgraded (empty) -> 0009"),
        ("db-check", f"{database}: OK"),
    ):
        result = subprocess.run(
            ["make", "-C", str(REPO_ROOT), "--no-print-directory", target],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert expected in result.stdout + result.stderr, (target, result.stdout, result.stderr)
        assert (result.returncode == 0) == ("OK" in expected or "upgraded" in expected), target


def test_seed_demo_never_writes_a_protected_original(tmp_path, clean_env, capsys, workspace, monkeypatch):
    """``seed demo --db`` refuses a protected original (``database.protected``) before it builds anything (the demo
    builder is never called), whatever path names it (relative, symlinked, a symlinked directory, a hard link, a
    companion, anything under a protected folder) and with or without ``--force``; and an existing file that is not
    writable. By default nothing is protected."""
    from laueportal.seed import build

    configure(clean_env, tmp_path, tmp_path / "dev.db", protected(workspace))
    db = workspace / "db"
    snapshot, migrated, record = protected(workspace)
    links = tmp_path / "links"
    links.mkdir()
    (links / "symlink.db").symlink_to(snapshot)
    (links / "db").symlink_to(db, target_is_directory=True)
    os.link(snapshot, links / "hardlink.db")
    os.link(record / "jobs" / "17" / "request.json", links / "hardlink-record.db")
    (links / "companion.db-shm").symlink_to(Path(f"{migrated}-shm"))
    read_only = tmp_path / "read-only.db"
    read_only.write_text("keep me")
    read_only.chmod(0o444)
    monkeypatch.chdir(tmp_path)

    def build_demo(*_args, **_kwargs):
        raise AssertionError("seed demo built a refused destination")

    monkeypatch.setattr(build, "build_demo", build_demo)
    before = _originals(workspace)
    destinations = [
        snapshot,
        migrated,
        Path(f"{migrated}-wal"),
        Path(f"{migrated}-shm"),
        Path(f"{snapshot}.sha256"),
        Path("ws/db/../db/./migrated.db"),  # relative
        links / "symlink.db",
        links / "db" / snapshot.name,  # through a symlinked directory
        links / "hardlink.db",
        links / "hardlink-record.db",
        record / "new.db",  # anything under .migration/
        record / "jobs" / "17" / "request.json",
        links / "companion.db",  # its -shm is the original's
    ]
    for destination in destinations:
        for force in ([], ["--force"]):
            status, out, err = run(capsys, "seed", "demo", "--db", str(destination), *force)
            assert (status, out) == (1, ""), (destination, force)
            assert err.startswith("laueportal seed demo: refusing "), (destination, err)
            assert "protected original" in err, (destination, err)
    for force in ([], ["--force"]):
        assert run(capsys, "seed", "demo", "--db", str(read_only), *force) == (
            1,
            "",
            f"laueportal seed demo: refusing {read_only}: {read_only} exists and is not writable; it is not replaced\n",
        )

    assert _originals(workspace) == before
    assert not (record / "new.db").exists()
    assert read_only.read_text() == "keep me"
    assert cli._refuse_destination(db / "dev.db") is None  # a database beside the originals is not refused
    get_settings.cache_clear()
    configure(clean_env, tmp_path, tmp_path / "dev.db")  # the default: nothing is protected
    assert cli._refuse_destination(record / "new.db") is None
    assert "not writable" in str(cli._refuse_destination(snapshot))  # only read-only: not replaced


# ---------------------------------------------------------------------------
# The journal-mode guard, db check, db stamp and db backup

UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def insert(path: Path, table: str, **values: object) -> None:
    """One row, written as stored (no pragma: foreign keys are not enforced)."""
    connection = sqlite3.connect(path)
    with connection:
        connection.execute(
            f'INSERT INTO "{table}" ({", ".join(values)}) VALUES ({", ".join("?" * len(values))})',
            list(values.values()),
        )
    connection.close()


def insert_run(path: Path, job_id: int, *, ago: timedelta, **values: object) -> None:
    """A ``job`` row whose times are ``ago`` before now, in UTC's storage format (a Finished run by default)."""
    when = (utcnow() - ago).strftime(UTC_FORMAT)
    insert(
        path,
        "job",
        **{
            "job_id": job_id,
            "computer_name": "host",
            "status": 2,
            "priority": 0,
            "phase": "finished",
            "submit_time": when,
            "updated_at": when,
            **values,
        },
    )


def test_the_api_the_worker_and_the_db_commands_refuse_wal_on_a_network_file_system(
    tmp_path, clean_env, capsys, monkeypatch, restore_logging
):
    """WAL only on a local disk: they exit 2 (a configuration error) before they open the database, which is left as it
    was, the read-only commands too."""
    from laueportal.app import create_app
    from laueportal.jobs.worker import run_worker

    root = tmp_path.resolve()
    database = database_at(root, "head", name="laue.db", journal_mode="delete").path
    configure(clean_env, root, database)  # journal_mode = "wal"
    monkeypatch.setattr(db, "MOUNTINFO", mount_table(tmp_path, f"{root} nfs4 /export"))
    monkeypatch.setattr("uvicorn.run", lambda *_args, **_kwargs: pytest.fail("serve started"))
    digest = file_digest(database)
    refused = f"laueportal: database.path {database} is on a network file system (nfs4, mounted at {root}), where"

    for argv in (
        ["serve"],
        ["worker", "--burst"],
        ["jobs", "reconcile", "--once"],
        ["db", "upgrade"],
        ["db", "stamp", "0006"],
        ["db", "current"],
        ["db", "backup", str(root / "copy.db")],
    ):
        status, out, err = run(capsys, *argv)
        assert (status, out) == (2, ""), argv
        assert err.startswith(refused) and 'or set journal_mode = "delete" in [database]' in err, (argv, err)
    for start in (create_app, run_worker):
        with pytest.raises(db.UnsafeJournalMode):
            start(get_settings())

    assert file_digest(database) == digest and journal_header(database) == (1, 1)
    assert not (root / "copy.db").exists()


def test_db_check_reports_the_counts_of_a_healthy_database(tmp_path, clean_env, capsys, monkeypatch):
    root = tmp_path.resolve()
    database = database_at(root, "head", name="healthy.db").path
    insert_run(database, 1, ago=timedelta(days=3), start_time="2026-10-01T08:00:00.000000Z", finish_time=None)
    insert_run(database, 2, ago=timedelta(seconds=5), status=0, phase="created")  # being created: not stuck
    configure(clean_env, root, database)
    monkeypatch.setattr(db, "MOUNTINFO", mount_table(tmp_path))

    assert run(capsys, "db", "check") == (
        0,
        "journal: wal on ext4 (local, mounted at /)\n"
        "revision: 0009 (head)\n"
        "quick_check: ok\n"
        "foreign_key_check: 0 violations\n"
        "timestamps: 5 values, 0 not UTC\n"
        "rows: catalog 0, conversion 0, idempotency_key 0, indexing_run 0, job 2, laue_matching_indexing_parameters 0, lauego_indexing_parameters 0, "
        "metadata 0, reconstruction_run 0, scan 0, wire_reconstruction_parameters 0, worker 0\n"
        "runs stuck in created or published: 0\n"
        f"{database}: OK\n",
        "",
    )


def _damaged_page(database: Path) -> None:
    for job_id in range(1, 201):
        insert_run(database, job_id, ago=timedelta(days=1), messages="x" * 200)
    connection = sqlite3.connect(database)
    root_page = connection.execute("SELECT rootpage FROM sqlite_master WHERE name = 'job'").fetchone()[0]
    page_size = connection.execute("PRAGMA page_size").fetchone()[0]
    connection.close()
    with database.open("r+b") as stream:
        stream.seek((root_page - 1) * page_size)
        stream.write(bytes([0x0D, 0, 0, 0, 0x99, 0, 0]))  # a leaf page header with a cell count it does not have


@pytest.mark.parametrize(
    ("damage", "problem"),
    [
        (_damaged_page, "quick_check failed: "),
        (
            lambda path: insert(
                path, "catalog", catalog_id=1, scan_number=404, filefolder="/x", filename_prefix="p", aperture="none"
            ),
            "foreign key check failed (1 rows: catalog row 1 -> metadata)",
        ),
        (
            lambda path: insert_run(path, 1, ago=timedelta(days=1), submit_time="2026-01-23 11:32:08.491324"),
            "timestamps not in UTC's storage format (2026-10-15T12:34:56.000000Z): job.submit_time 1; the models "
            "cannot read them",
        ),
        (
            lambda path: insert_run(path, 7, ago=timedelta(hours=2), status=0, phase="published"),
            "1 runs stuck in created or published for more than 600 s (job 7): the worker's reconciler fails them",
        ),
        ("network", "is on a network file system (nfs4, mounted at {root}), where SQLite's WAL journal is unsafe"),
    ],
    ids=["quick_check", "foreign_keys", "naive_timestamps", "stuck_run", "wal_on_nfs"],
)
def test_db_check_fails_on_each_problem(
    tmp_path, clean_env, capsys, monkeypatch, damage: Callable[[Path], None] | str, problem: str
):
    """A scratch database with one problem each: db check reports it on stderr, exits 1 and still reports what it
    measured (the guard's problem too: db check checks it rather than refusing to run)."""
    root = tmp_path.resolve()
    database = database_at(root, "head", name="scratch.db", journal_mode="delete").path
    configure(clean_env, root, database)
    if damage == "network":
        monkeypatch.setattr(db, "MOUNTINFO", mount_table(tmp_path, f"{root} nfs4 /export"))
    else:
        assert callable(damage)
        damage(database)
    digest = file_digest(database)

    status, out, err = run(capsys, "db", "check")

    assert status == 1
    assert problem.format(root=root) in err and err.startswith("laueportal db check: ")
    assert out.startswith("journal: wal on ") and "quick_check: " in out and not out.endswith(": OK\n")
    assert file_digest(database) == digest


def test_db_stamp_refuses_0005_or_later_while_timestamps_are_naive(tmp_path, clean_env, capsys):
    """Stamped 0005 or later, v1's naive local times would fail on read. Nothing is recorded; the upgrade from the
    revision the values are at converts them."""
    database = database_at(tmp_path, "0004", name="v1-times.db").path
    insert(
        database, "job", job_id=1, computer_name="host", status=2, priority=0, submit_time="2026-01-23 11:32:08.491324"
    )
    configure(clean_env, tmp_path, database)

    status, out, err = run(capsys, "db", "stamp", "0005")

    assert (status, out) == (1, "")
    assert err.startswith(
        f"laueportal db stamp: database {database} holds timestamps that are not UTC (job.submit_time 1), so it was "
        "not stamped 0005: "
    )
    assert migrations.current_revision(DatabaseSettings(path=database)) == "0004"
    assert run(capsys, "db", "upgrade")[:2] == (0, f"{database}: upgraded 0004 -> 0009\n")
    assert run(capsys, "db", "check")[0] == 0


def test_db_backup_copies_a_database_in_use_consistently(tmp_path, clean_env, capsys):
    """SQLite's online backup API while a writer commits: the writer goes on committing while the command runs, and
    the copy is one committed state (each transaction writes a pair of rows, never split), passes quick_check, is one
    rollback-journal file and has the database's permission bits (not the umask's)."""
    database, copy = tmp_path / "live.db", tmp_path / "backups" / "live-copy.db"
    copy.parent.mkdir()
    configure(clean_env, tmp_path, database)
    setup = sqlite3.connect(database)
    setup.execute("PRAGMA journal_mode=WAL")
    setup.execute("CREATE TABLE pair (id INTEGER PRIMARY KEY, half INTEGER, padding BLOB)")
    setup.executemany("INSERT INTO pair (half, padding) VALUES (?, randomblob(4096))", [(0,), (1,)] * 1000)
    setup.commit()
    setup.close()
    database.chmod(0o600)
    stop, commits = threading.Event(), [0]

    def write() -> None:
        connection = sqlite3.connect(database, timeout=30, isolation_level=None)
        while not stop.is_set():
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO pair (half, padding) VALUES (0, randomblob(4096))")
            connection.execute("INSERT INTO pair (half, padding) VALUES (1, randomblob(4096))")
            connection.execute("COMMIT")
            commits[0] += 1
        connection.close()

    def wait_for_commits(count: int) -> None:
        deadline = time.monotonic() + 30
        while commits[0] < count and time.monotonic() < deadline:
            time.sleep(0.001)
        assert commits[0] >= count

    writer = threading.Thread(target=write)
    writer.start()
    try:
        wait_for_commits(5)
        before = commits[0]
        status, out, err = run(capsys, "db", "backup", str(copy))
        during = commits[0] - before
        wait_for_commits(commits[0] + 5)
    finally:
        stop.set()
        writer.join()

    assert (status, err) == (0, "")
    assert out.startswith(f"{copy}: backed up {database} (") and out.endswith("; quick_check ok)\n")
    connection = sqlite3.connect(f"{copy.as_uri()}?mode=ro", uri=True)
    halves = connection.execute("SELECT count(*) FILTER (WHERE half = 0), count(*) FILTER (WHERE half = 1) FROM pair")
    zeros, ones = halves.fetchone()
    assert zeros == ones >= 1005
    assert connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]
    connection.close()
    assert journal_header(copy) == (1, 1)
    assert during >= 1  # in WAL mode the backup's read transaction never held the writer
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    assert sorted(path.name for path in copy.parent.iterdir()) == ["live-copy.db"]


def test_db_backup_never_writes_the_live_database_or_a_protected_original(tmp_path, clean_env, capsys, workspace):
    """Refused before anything is written: a protected original, the configured database or the source (``--db``),
    one of their files, a hard link to one, a folder, and an existing file without ``--force``."""
    database = database_at(tmp_path, "head", name="live.db").path
    source = database_at(tmp_path, "head", name="other.db").path
    configure(clean_env, tmp_path, database, protected(workspace))
    os.link(database, tmp_path / "hardlink.db")
    existing = tmp_path / "yesterday.db"
    existing.write_text("kept")
    original = workspace / "db" / "migrated.db"
    before = _originals(workspace)
    digests = {path: file_digest(path) for path in (database, source)}

    for argv, reason in (
        ([str(original)], "is a protected original"),
        ([f"{original}-wal"], "is a protected original"),
        ([f"{original}-journal"], "is a protected original"),
        ([str(database)], f"is the database {database}"),
        ([f"{database}-wal"], f"is the database {database}"),
        ([f"{database}-journal"], f"is the database {database}"),
        ([str(tmp_path / "hardlink.db")], f"is the database {database}"),
        ([str(database), "--db", str(source), "--force"], f"is the database {database}"),
        ([str(source), "--db", str(source), "--force"], f"is the database {source}"),
        ([str(tmp_path)], "it is a folder"),
        ([str(existing)], f"{existing} exists; pass --force to replace it"),
    ):
        status, out, err = run(capsys, "db", "backup", *argv)
        assert (status, out) == (1, ""), argv
        assert err.startswith("laueportal db backup: refusing ") and reason in err, (argv, err)

    assert _originals(workspace) == before
    assert {path: file_digest(path) for path in (database, source)} == digests
    assert existing.read_text() == "kept"
    assert run(capsys, "db", "backup", str(existing), "--force")[0] == 0
    assert migrations.check(DatabaseSettings(path=existing)) == []


@pytest.mark.parametrize("failure", ["damaged", "read-only folder"])
def test_a_failed_db_backup_leaves_nothing_and_names_the_side_that_failed(tmp_path, clean_env, capsys, failure):
    """A copy that fails quick_check (a damaged page is copied as it is) or cannot be written: exit 1, one line naming
    the copy, and neither the destination nor a partial copy left."""
    database = database_at(tmp_path, "head", name="live.db").path
    configure(clean_env, tmp_path, database)
    backups = tmp_path / "backups"
    backups.mkdir()
    dest = backups / "copy.db"
    if failure == "damaged":
        _damaged_page(database)
        expected = f"laueportal db backup: the copy of {database} failed quick_check, so {dest} was not written: "
    else:
        backups.chmod(0o555)
        expected = f"laueportal db backup: the copy {dest}: unable to open database file"
    try:
        status, out, err = run(capsys, "db", "backup", str(dest))
    finally:
        backups.chmod(0o755)

    assert (status, out) == (1, "")
    assert err.startswith(expected) and err.count("\n") == 1, err
    assert list(backups.iterdir()) == []
