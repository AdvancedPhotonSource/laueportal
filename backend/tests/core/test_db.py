from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from laueportal.core import db
from tests.settings_support import create_database, make_settings, mount_table


def pragma(engine: Engine, name: str) -> object:
    with engine.connect() as connection:
        return connection.exec_driver_sql(f"PRAGMA {name}").scalar_one()


@pytest.mark.parametrize("journal_mode", ["wal", "delete"])
def test_every_connection_gets_the_pragmas(tmp_path: Path, journal_mode: str) -> None:
    engine = db.make_engine(make_settings(tmp_path, database={"journal_mode": journal_mode}))
    try:
        assert pragma(engine, "journal_mode") == journal_mode
        assert pragma(engine, "foreign_keys") == 1
        assert pragma(engine, "busy_timeout") == 30000
    finally:
        engine.dispose()


def test_make_engine_does_not_touch_the_file(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    db.make_engine(settings).dispose()
    assert not settings.database.path.exists()


def test_by_default_the_first_connection_creates_the_database(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)  # db upgrade and the seed rely on this (the worker refuses a missing database)
    engine = db.make_engine(settings)
    with engine.connect() as connection:
        connection.exec_driver_sql("CREATE TABLE item (id INTEGER PRIMARY KEY)")
        connection.commit()
    engine.dispose()
    assert settings.database.path.is_file()


@pytest.mark.parametrize("journal_mode", ["wal", "delete"])
def test_an_engine_without_create_never_creates_the_database(tmp_path: Path, journal_mode: str) -> None:
    folder = tmp_path / "a dir#with?odd characters"  # the file: URI escapes them
    folder.mkdir()
    settings = make_settings(tmp_path, database={"path": folder / "portal.db", "journal_mode": journal_mode})
    path = settings.database.path
    engine = db.make_engine(settings, create=False)
    try:
        with pytest.raises(db.DatabaseMissing) as missing:
            engine.connect()
        assert (missing.value.status, missing.value.path) == (503, path)
        assert not path.exists()

        create_database(settings)
        assert pragma(engine, "journal_mode") == journal_mode  # an existing file opens read-write
        with Session(engine) as session, session.begin():
            session.execute(text("INSERT INTO probe (id) VALUES (1)"))
        with Session(engine) as session:
            assert session.execute(text("SELECT count(*) FROM probe")).scalar_one() == 1

        engine.dispose()
        for leftover in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            leftover.unlink(missing_ok=True)
        with pytest.raises(db.DatabaseMissing):
            engine.connect()
        assert not path.exists()
    finally:
        engine.dispose()


def test_foreign_keys_are_enforced(tmp_path: Path) -> None:
    engine = db.make_engine(make_settings(tmp_path))
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id))"
        )
    with pytest.raises(Exception, match="FOREIGN KEY constraint failed"), engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO child (parent_id) VALUES (42)")
    engine.dispose()


def test_session_scope_commits_or_rolls_back(tmp_path: Path) -> None:
    engine = db.make_engine(make_settings(tmp_path))
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE item (name TEXT)")

    with db.session_scope(engine) as session:
        session.execute(text("INSERT INTO item VALUES ('kept')"))
    with pytest.raises(RuntimeError), db.session_scope(db.make_sessionmaker(engine)) as session:
        session.execute(text("INSERT INTO item VALUES ('dropped')"))
        raise RuntimeError("boom")

    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT name FROM item").scalars().all() == ["kept"]
    engine.dispose()


def test_get_session_commits_after_the_endpoint_and_rolls_back_on_error(tmp_path: Path) -> None:
    engine = db.make_engine(make_settings(tmp_path))
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE item (name TEXT)")
    sessions = db.make_sessionmaker(engine)

    ok = db.get_session(sessions)  # FastAPI drives the generator like this
    next(ok).execute(text("INSERT INTO item VALUES ('kept')"))
    with pytest.raises(StopIteration):
        next(ok)

    failing = db.get_session(sessions)
    next(failing).execute(text("INSERT INTO item VALUES ('dropped')"))
    with pytest.raises(ValueError):
        failing.throw(ValueError("endpoint failed"))

    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT name FROM item").scalars().all() == ["kept"]
    engine.dispose()


def test_ping_never_creates_the_database(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    engine = db.make_engine(settings)
    with pytest.raises(FileNotFoundError):
        db.ping(engine)
    assert not settings.database.path.exists()
    create_database(settings)
    db.ping(engine)
    assert pragma(engine, "busy_timeout") == 30000  # the probe's short timeout is not left behind
    engine.dispose()


@pytest.fixture
def write_locked(tmp_path: Path):
    """A ``journal_mode=delete`` database plus a function that takes its write lock (released at teardown)."""
    settings = make_settings(tmp_path, database={"journal_mode": "delete"})
    create_database(settings)
    holders: list[sqlite3.Connection] = []

    def lock() -> None:
        holder = sqlite3.connect(settings.database.path, isolation_level=None)
        holder.execute("BEGIN EXCLUSIVE")  # readers wait for an exclusive lock in rollback-journal mode
        holder.execute("INSERT INTO probe DEFAULT VALUES")
        holders.append(holder)

    yield settings, lock
    for holder in holders:
        holder.execute("ROLLBACK")
        holder.close()


def test_ping_is_bounded_while_a_writer_holds_the_lock(write_locked) -> None:
    """A fresh pooled connection would run the journal_mode pragma under busy_timeout=30000 and wait 30 s."""
    settings, lock = write_locked
    engine = db.make_engine(settings)  # a new engine: no pooled connection yet
    lock()

    started = time.perf_counter()
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        db.ping(engine, timeout_ms=200)
    elapsed = time.perf_counter() - started

    assert elapsed < 2, f"the probe waited {elapsed:.1f}s"
    assert engine.pool.checkedin() == 0  # the probe never opened a pooled connection
    engine.dispose()


def test_ping_succeeds_beside_a_writer_in_wal_mode(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, database={"journal_mode": "wal"})
    engine = db.make_engine(settings)
    with engine.begin() as connection:  # creates the file in WAL mode
        connection.exec_driver_sql("CREATE TABLE item (name TEXT)")
    holder = sqlite3.connect(settings.database.path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO item VALUES ('pending')")
    try:
        db.ping(engine, timeout_ms=200)  # WAL readers do not wait for the writer
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        engine.dispose()


@pytest.mark.parametrize("journal_mode", ["wal", "delete"])
def test_begin_write_makes_a_check_and_its_write_one_step(tmp_path: Path, journal_mode: str) -> None:
    """Two transactions each read a counter and write it back incremented. With ``begin_write`` the second waits
    for the first to commit (``busy_timeout``) and reads its value, so no increment is lost (``jobs.controls.cancel_run``
    and ``remote.submission.submit_run`` rely on it)."""
    engine = db.make_engine(make_settings(tmp_path, database={"journal_mode": journal_mode}))
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE counter (value INTEGER)")
            connection.exec_driver_sql("INSERT INTO counter VALUES (0)")
        holding, seen = threading.Event(), []

        def increment(hold: bool) -> None:
            with Session(engine) as session, session.begin():
                db.begin_write(session)
                value = session.execute(text("SELECT value FROM counter")).scalar_one()
                seen.append(value)
                if hold:
                    holding.set()
                    time.sleep(0.3)
                session.execute(text("UPDATE counter SET value = :value"), {"value": value + 1})

        first = threading.Thread(target=increment, args=(True,))
        first.start()
        try:
            assert holding.wait(10)
            increment(False)
        finally:
            first.join(10)
        assert seen == [0, 1]
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT value FROM counter").scalar_one() == 2
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("place", "journal_mode", "refused"),
    [
        ("nfs/laue.db", "wal", "nfs4 nfs"),  # a network file system with WAL
        ("nfs/laue.db", "delete", None),  # the rollback journal is safe there
        ("local/laue.db", "wal", None),  # a local disk with WAL
        ("local/bound/laue.db", "wal", "nfs4 local/bound"),  # an NFS folder bind-mounted into the local tree
        ("nfs/scratch/laue.db", "wal", None),  # a local folder bind-mounted inside the NFS mount
        ("local/link/laue.db", "wal", "nfs4 nfs"),  # a link to a folder on NFS: its target's file system
        ("ceph/laue.db", "wal", "fuse.ceph-fuse ceph"),  # a FUSE client of a cluster file system
    ],
)
def test_the_guard_refuses_wal_on_a_network_file_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, place: str, journal_mode: str, refused: str | None
) -> None:
    """The file system is the one holding the database's real location (links resolved, the longest mount point of the
    mount table), and making an engine refuses WAL there (the API, the worker and the commands all make one)."""
    root = tmp_path.resolve()
    for folder in ("nfs/scratch", "local/bound", "ceph"):
        (root / folder).mkdir(parents=True)
    (root / "local" / "link").symlink_to(root / "nfs", target_is_directory=True)
    monkeypatch.setattr(
        db,
        "MOUNTINFO",
        mount_table(
            tmp_path,
            f"{root / 'nfs'} nfs4 /export",
            f"{root / 'local' / 'bound'} nfs4 /export/db",
            f"{root / 'nfs' / 'scratch'} ext4 /local/scratch",
            f"{root / 'ceph'} fuse.ceph-fuse /",
        ),
    )
    settings = make_settings(tmp_path, database={"path": root / place, "journal_mode": journal_mode})

    if refused is None:
        db.make_engine(settings).dispose()
        assert db.journal_mode_problem(settings.database) is None
    else:
        with pytest.raises(db.UnsafeJournalMode) as refusal:
            db.make_engine(settings)
        message = str(refusal.value)
        fstype, folder = refused.split()
        assert f"is on a network file system ({fstype}, mounted at {root / folder})" in message
        assert os.path.realpath(root / place) in message
        assert "/local/" in message and 'journal_mode = "delete"' in message
