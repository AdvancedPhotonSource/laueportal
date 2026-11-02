"""A live run's arrivals (``jobs.arrivals``): admission (a regular file in the run's folder matching its template; the
same file again a no-op, a changed one refused and logged), the ledger's lines, its lock (two writers racing; a torn
last line; NFS's rule for flock), ``laueportal live notify``, and a follower's points (``admit_point``: the point file
inside the followed run, its line's point keys)."""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import IO, Any

import pytest
from sqlalchemy.orm import Session

from laueportal import cli
from laueportal.core.settings import get_settings
from laueportal.jobs import arrival_index, arrivals, controls
from laueportal.jobs.arrival_index import index_path
from laueportal.jobs.arrivals import MAX_POINT_DEPTHS, ArrivalRefused, ArrivalWriter, notify
from laueportal.jobs.dispatch import claim_run
from laueportal.jobs.lifecycle import STOP_REASON_CANCELLED
from laueportal.jobs.live import LiveFollows, read_live_document
from laueportal.jobs.models import Job
from laueportal.jobs.rundir import CompletedPoint
from tests.jobs.live_support import LiveRun, create_live_follower, create_live_wire_run, live_hooks
from tests.run_support import enqueue_with, load_job, publish_wire_run
from tests.settings_support import fresh_interpreter_env, make_database
from tests.worker_support import write_config

LINE_KEYS = ["n", "path", "size", "mtime_ns", "inode", "device", "source", "admitted_at", "scan_point"]


@pytest.fixture
def engine(tmp_path):
    _, engine = make_database(tmp_path)
    yield engine
    engine.dispose()


def _ledger(run: LiveRun) -> bytes:
    return (run.run_directory / "arrivals.jsonl").read_bytes()


def _lines(run: LiveRun) -> list[dict]:
    return [json.loads(line) for line in _ledger(run).decode().splitlines()]


def _writer(engine, run: LiveRun) -> ArrivalWriter:
    return ArrivalWriter(engine, run.job_id, run.run_directory, read_live_document(run.run_directory))


def test_a_file_of_the_run_is_admitted_as_the_ledger_s_next_whole_line(engine, tmp_path):
    run = create_live_wire_run(engine, tmp_path)
    seven = run.file(7, b"stack")
    first = _writer(engine, run).admit(seven, source="notify")
    through_a_link = tmp_path / "linked-folder"
    through_a_link.symlink_to(run.folder)
    second = _writer(engine, run).admit(through_a_link / run.file(3).name, source="catalog", size=0)

    assert (first.admitted, second.admitted) == (True, True)
    lines = _ledger(run).split(b"\n")
    assert lines[-1] == b"" and len(lines) == 3  # each line whole, compact JSON
    one, two = _lines(run)
    assert list(one) == LINE_KEYS and b" " not in lines[0]
    status = os.stat(seven)
    assert one == {
        "n": 0,
        "path": os.fspath(seven),
        "size": 5,
        "mtime_ns": status.st_mtime_ns,
        "inode": status.st_ino,
        "device": status.st_dev,
        "source": "notify",
        "admitted_at": one["admitted_at"],
        "scan_point": 7,
    }
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", one["admitted_at"])
    assert (two["n"], two["path"], two["source"], two["scan_point"]) == (
        1,
        os.fspath(run.folder / "live_3.h5"),
        "catalog",
        3,
    )


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("relative", "is not an absolute path"),
        ("elsewhere", "is not directly in the run's folder"),
        ("below", "is not directly in the run's folder"),
        ("other-name", "does not match the run's file name live_%d.h5"),
        ("missing", "does not exist"),
        ("folder", "is not a regular file"),
        ("link", "is not a regular file"),
        ("catalog-size", "has 5 bytes; the catalog says 9"),
    ],
)
def test_admission_refuses_what_is_not_one_of_the_run_s_files(engine, tmp_path, case, reason):
    run = create_live_wire_run(engine, tmp_path)
    size = None
    if case == "relative":
        path = Path("live_1.h5")
    elif case == "elsewhere":
        (tmp_path / "elsewhere").mkdir()
        path = tmp_path / "elsewhere" / "live_1.h5"
        path.write_bytes(b"")
    elif case == "below":
        (run.folder / "sub").mkdir()
        path = run.folder / "sub" / "live_1.h5"
        path.write_bytes(b"")
    elif case == "other-name":
        path = run.folder / "dark_1.h5"
        path.write_bytes(b"")
    elif case == "missing":
        path = run.folder / "live_1.h5"
    elif case == "folder":
        path = run.folder / "live_1.h5"
        path.mkdir()
    elif case == "link":
        path = run.folder / "live_1.h5"
        path.symlink_to(run.file(2))
    else:
        path, size = run.file(1, b"12345"), 9
    with pytest.raises(ArrivalRefused, match=re.escape(reason)):
        _writer(engine, run).admit(path, source="catalog" if size else "notify", size=size)
    assert _ledger(run) == b""


def test_the_same_file_again_is_a_no_op_and_a_changed_one_is_refused_and_logged(engine, tmp_path, caplog):
    run = create_live_wire_run(engine, tmp_path)
    path = run.file(1)
    admitted = _writer(engine, run).admit(path, source="notify")
    again = _writer(engine, run).admit(path, source="catalog", size=0)  # another writer, another source
    assert (again.admitted, again.arrival) == (False, admitted.arrival)

    assert claim_run(engine, run.job_id)
    controls.cancel_run(run.job_id, engine=engine)  # sealed: a repeat stays a no-op
    assert not _writer(engine, run).admit(path, source="notify").admitted

    path.write_bytes(b"rewritten")
    with caplog.at_level(logging.WARNING, logger="laueportal.jobs.arrivals"):
        with pytest.raises(ArrivalRefused, match="changed since it was admitted as arrival 0"):
            _writer(engine, run).admit(path, source="notify")
    assert f"Job {run.job_id}: {path} changed since it was admitted as arrival 0" in caplog.text
    assert len(_lines(run)) == 1


def test_a_writer_truncates_a_torn_last_line_under_the_lock_before_it_appends(engine, tmp_path, caplog):
    run = create_live_wire_run(engine, tmp_path)
    writer = _writer(engine, run)
    writer.admit(run.file(1), source="notify")
    with (run.run_directory / "arrivals.jsonl").open("ab") as ledger:
        ledger.write(b'{"n":1,"path":"/a/writer/that/died')
    with caplog.at_level(logging.WARNING, logger="laueportal.jobs.arrivals"):
        _writer(engine, run).admit(run.file(2), source="notify")
    assert [(line["n"], line["scan_point"]) for line in _lines(run)] == [(0, 1), (1, 2)]
    assert "truncated a torn last line of arrivals.jsonl (34 bytes after arrival 0)" in caplog.text
    writer.admit(run.file(3), source="notify")  # the first writer reads on from its offset
    assert [line["n"] for line in _lines(run)] == [0, 1, 2]


RACER = """
import json, sys, time
from pathlib import Path
import laueportal.models  # noqa: F401
from laueportal.core.db import make_engine
from laueportal.core.settings import Settings
from laueportal.jobs.arrivals import notify
database, job_id, folder, go, numbers = sys.argv[1], int(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5]
engine = make_engine(Settings(database={"path": database}, files={"runs_root": folder}), create=False)
while not go.exists():
    time.sleep(0.005)
results = []
for number in json.loads(numbers):
    admission = notify(engine, job_id, folder / f"live_{number}.h5")
    results.append([admission.arrival.n, admission.admitted, number])
print(json.dumps(results))
"""


@pytest.mark.slow
def test_two_writers_racing_never_duplicate_or_lose_a_line(tmp_path):
    """Two processes (the catalog thread and ``notify``, say) admit the same 30 files in opposite orders at once:
    every file is one line, ``n`` runs 0-29, and each line was admitted by exactly one of them."""
    settings, engine = make_database(tmp_path)
    try:
        run = create_live_wire_run(engine, tmp_path)
        numbers = list(range(1, 31))
        for number in numbers:
            run.file(number, str(number).encode())
        go = tmp_path / "go"
        racers = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    RACER,
                    os.fspath(settings.database.path),
                    str(run.job_id),
                    os.fspath(run.folder),
                    os.fspath(go),
                    json.dumps(order),
                ],
                cwd=tmp_path,
                env=fresh_interpreter_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for order in (numbers, numbers[::-1])
        ]
        go.touch()
        outputs = [racer.communicate(timeout=120) for racer in racers]
        assert [racer.returncode for racer in racers] == [0, 0], outputs
        results = [json.loads(out) for out, _ in outputs]
    finally:
        engine.dispose()
    lines = _lines(run)
    assert [line["n"] for line in lines] == list(range(30))
    assert sorted(line["scan_point"] for line in lines) == numbers
    admitted = [number for result in results for _, new, number in result if new]
    assert sorted(admitted) == numbers  # each file admitted once, by one of them; the other's was a no-op
    by_number = {line["scan_point"]: line["n"] for line in lines}
    assert all(by_number[number] == n for result in results for n, _, number in result)


# ---------------------------------------------------------------------------
# laueportal live notify


@pytest.fixture
def configured(tmp_path, monkeypatch, engine):
    config = write_config(tmp_path / "cli.toml", database=tmp_path / "laueportal.db", runs_root=tmp_path / "runs")
    for name in [name for name in os.environ if name.upper().startswith("LAUEPORTAL_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("LAUEPORTAL_CONFIG", str(config))
    get_settings.cache_clear()
    yield engine
    get_settings.cache_clear()


def test_notify_admits_a_file_with_one_json_line_and_a_repeat_is_a_no_op(configured, tmp_path, capsys, monkeypatch):
    run = create_live_wire_run(configured, tmp_path)
    path = run.file(4)
    monkeypatch.chdir(run.folder)  # a relative path is the working directory's (by hand)
    assert cli.main(["live", "notify", str(run.job_id), "live_4.h5", "--complete"]) == 0
    assert cli.main(["live", "notify", str(run.job_id), os.fspath(path), "--complete"]) == 0
    out, err = capsys.readouterr()
    expected = {"job_id": run.job_id, "n": 0, "path": os.fspath(path)}
    assert [json.loads(line) for line in out.splitlines()] == [
        {**expected, "admitted": True},
        {**expected, "admitted": False},
    ]
    assert err == "" and len(_lines(run)) == 1


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("incomplete", "--complete is required"),
        ("unknown", "job 999 does not exist"),
        ("batch", "is not a live run"),
        ("daq", "takes its files from 'daq', not from notify"),
        ("elsewhere", "is not directly in the run's folder"),
        ("stopped", "was stopped: it admits no more files"),
        ("ended", "has ended (Cancelled)"),
        ("changed", "changed since it was admitted as arrival 0"),
        ("unreadable", "line 1 of arrivals.jsonl is not an arrival"),
    ],
)
def test_notify_refuses_with_a_one_line_reason_and_status_1(configured, tmp_path, capsys, case, reason):
    engine = configured
    run = create_live_wire_run(engine, tmp_path, source="daq" if case == "daq" else "notify")
    job_id, path, options = run.job_id, run.file(1), ["--complete"]
    if case == "incomplete":
        options = []
    elif case == "unknown":
        job_id = 999
    elif case == "batch":
        batch = publish_wire_run(engine, tmp_path, name="batch")
        enqueue_with(engine, batch.job_id)
        job_id = batch.job_id
    elif case == "elsewhere":
        path = tmp_path / "live_1.h5"
        path.write_bytes(b"")
    elif case == "stopped":
        assert claim_run(engine, run.job_id)
        controls.cancel_run(run.job_id, engine=engine)
    elif case == "ended":
        controls.cancel_run(run.job_id, engine=engine)
    elif case == "changed":
        notify(engine, run.job_id, path)
        path.write_bytes(b"rewritten")
    elif case == "unreadable":  # the ledger cannot be trusted: nothing is appended to it
        (run.run_directory / "arrivals.jsonl").write_bytes(b"{not json\n")

    assert cli.main(["live", "notify", str(job_id), os.fspath(path), *options]) == 1
    out, err = capsys.readouterr()
    assert out == "" and err.count("\n") == 1 and err.startswith("laueportal live notify: refused: "), err
    assert reason in err
    with Session(engine) as session:
        assert session.get(Job, run.job_id).n_inputs == 0
    if case == "unreadable":
        assert _ledger(run) == b"{not json\n"
    else:
        assert len(_lines(run)) == (1 if case == "changed" else 0)


def _fill_ledger(run: LiveRun, lines: int, *, real: int) -> None:
    """``lines`` arrivals written into the run's ledger directly, as a long notify session leaves it: their files need
    not exist (only their lines are read), but for number ``real``'s, written with its identity."""
    folder = os.fspath(run.folder)
    status = os.lstat(run.file(real))
    with (run.run_directory / "arrivals.jsonl").open("wb") as ledger:
        for n in range(lines):
            line = {
                "n": n,
                "path": f"{folder}/live_{n}.h5",
                "size": status.st_size if n == real else 1,
                "mtime_ns": status.st_mtime_ns if n == real else 1_761_500_000_000_000_000 + n,
                "inode": status.st_ino if n == real else 1_000_000 + n,
                "device": status.st_dev if n == real else 64768,
                "source": "notify",
                "admitted_at": "2026-10-27T10:00:00+00:00",
                "scan_point": n,
            }
            ledger.write((json.dumps(line, separators=(",", ":")) + "\n").encode())


def _counted_lines(monkeypatch) -> list[int]:
    """The ledger lines parsed from now on (``Arrival.from_line``'s calls, each line's ``n``)."""
    parsed: list[int] = []
    from_line = arrivals.Arrival.from_line

    def counted(line: bytes, n: int) -> arrivals.Arrival:
        parsed.append(n)
        return from_line(line, n)

    monkeypatch.setattr(arrivals.Arrival, "from_line", staticmethod(counted))
    return parsed


def test_notify_s_admission_reads_a_bounded_number_of_ledger_lines_at_100_000_lines(engine, tmp_path, monkeypatch):
    """``notify`` (a process per file, no memory of the ledger) looks its path up in the run's admission index under the
    lock (``jobs.arrival_index``), so a new file and a repeat each read a line or two of a 100,000-line ledger; the
    index is built from the ledger once."""
    run = create_live_wire_run(engine, tmp_path)
    _fill_ledger(run, 100_000, real=123)
    parsed = _counted_lines(monkeypatch)
    first = notify(engine, run.job_id, run.file(100_000))  # no index yet: built from the ledger, once
    assert (first.arrival.n, first.admitted, len(parsed)) == (100_000, True, 100_000)
    index = Path(index_path(engine.url.database, run.job_id))
    assert index.is_file() and index.parent.name == "laueportal.db.notify-index"
    parsed.clear()
    second = notify(engine, run.job_id, run.file(100_001))
    assert (second.arrival.n, second.admitted) == (100_001, True)
    assert len(parsed) <= 2, parsed  # the index's last line checked; nothing else read
    parsed.clear()
    repeat = notify(engine, run.job_id, run.folder / "live_123.h5")  # admitted long ago: a no-op, by its one line
    assert (repeat.arrival.n, repeat.admitted, len(parsed)) == (123, False, 2)
    assert [line["n"] for line in _lines(run)[-3:]] == [99_999, 100_000, 100_001]


@pytest.mark.parametrize("case", ["lost", "corrupt", "behind", "torn", "replaced", "wrong"])
def test_an_admission_index_that_disagrees_with_the_ledger_is_rebuilt_or_caught_up_from_it(
    engine, tmp_path, caplog, case
):
    """The ledger is the source of truth (``jobs.arrival_index``): an index lost or unreadable, a ledger rewritten
    under another inode, or an entry that is not its arrival's line is rebuilt from the ledger's first line; lines
    another writer appended (or one that died before its index's commit) are caught up, a torn last line truncated.
    Every case: a repeat is a no-op, the next file the next number, nothing duplicated."""
    run = create_live_wire_run(engine, tmp_path)
    for number in (1, 2):
        notify(engine, run.job_id, run.file(number))
    index = Path(index_path(engine.url.database, run.job_id))
    ledger = run.run_directory / "arrivals.jsonl"
    if case == "lost":
        index.unlink()
    elif case == "corrupt":
        index.write_bytes(b"not a database" * 100)
    elif case == "behind":  # a writer without the index (or one that died before its index's commit)
        _writer(engine, run).admit(run.file(3), source="notify")
    elif case == "torn":
        with ledger.open("ab") as handle:
            handle.write(b'{"n":2,"path":"/a/writer/that/died')
    elif case == "replaced":  # the same lines in a new file: another inode
        copy = ledger.with_suffix(".new")
        copy.write_bytes(ledger.read_bytes())
        copy.replace(ledger)
    elif case == "wrong":  # an entry pointing at another arrival's line
        with sqlite3.connect(index) as connection:
            connection.execute("UPDATE arrival SET line_offset = 0, n = 0 WHERE path LIKE '%live_2.h5'")
    with caplog.at_level(logging.INFO, logger="laueportal.jobs"):
        assert not notify(engine, run.job_id, run.folder / "live_2.h5").admitted
    assert ("rebuilt" in caplog.text) == (case in ("corrupt", "replaced", "wrong")), caplog.text
    assert index.is_file()
    numbers = [1, 2, 3] if case == "behind" else [1, 2]
    added = notify(engine, run.job_id, run.file(9))
    assert (added.arrival.n, added.admitted) == (len(numbers), True)
    assert [(line["n"], line["scan_point"]) for line in _lines(run)] == [*enumerate([*numbers, 9])]


def test_an_index_unchanged_for_a_week_is_removed_when_another_run_s_is_made(engine, tmp_path):
    first = create_live_wire_run(engine, tmp_path, name="first")
    notify(engine, first.job_id, first.file(1))
    old = Path(index_path(engine.url.database, first.job_id))
    week = time.time() - arrival_index.INDEX_KEEP_SECONDS - 60
    os.utime(old, (week, week))
    second = create_live_wire_run(engine, tmp_path, name="second")
    notify(engine, second.job_id, second.file(1))
    assert not old.exists() and Path(index_path(engine.url.database, second.job_id)).is_file()
    assert not notify(engine, first.job_id, first.folder / "first_1.h5").admitted  # rebuilt: never wrong


_flock = fcntl.flock


def _nfs_flock(descriptor: int | IO[Any], operation: int) -> None:
    """flock(2) on an NFS client, which emulates it as a whole-file lock on the server: an exclusive lock on a
    descriptor not open for writing is EBADF."""
    number = descriptor if isinstance(descriptor, int) else descriptor.fileno()
    if operation & fcntl.LOCK_EX and fcntl.fcntl(number, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY:
        raise OSError(errno.EBADF, os.strerror(errno.EBADF))
    _flock(descriptor, operation)


def test_the_arrivals_lock_holds_on_nfs_where_an_exclusive_flock_needs_a_descriptor_open_for_writing(
    configured, tmp_path, capsys, monkeypatch
):
    """``runs_root`` may be on NFS (the module's docstring, "The lock"): with the NFS client's rule, a writer admits,
    ``notify`` admits, and the executor's seal seals."""
    engine = configured
    run = create_live_wire_run(engine, tmp_path)
    monkeypatch.setattr(fcntl, "flock", _nfs_flock)
    assert _writer(engine, run).admit(run.file(1), source="notify").admitted
    assert cli.main(["live", "notify", str(run.job_id), os.fspath(run.file(2)), "--complete"]) == 0
    assert json.loads(capsys.readouterr().out)["admitted"]
    hooks, monitor = live_hooks(engine, run.job_id)
    controls.cancel_run(run.job_id, engine=engine)
    monitor.stop_requested, monitor.stop_reason = True, STOP_REASON_CANCELLED
    assert [entry.input_id for entry in hooks.arrivals()] == ["live_1", "live_2"]
    assert hooks.sealed and hooks.should_stop()


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("ledger", "PermissionError: [Errno 13] Permission denied"),
        ("database", "OperationalError: unable to open database file"),
    ],
)
def test_notify_answers_an_unexpected_error_with_one_line_and_status_1(configured, tmp_path, capsys, case, reason):
    """One line for an acquisition hook, for a file system or database failure too: no traceback."""
    engine = configured
    run = create_live_wire_run(engine, tmp_path)
    path = run.file(1)
    unusable = run.run_directory / "arrivals.jsonl" if case == "ledger" else tmp_path / "laueportal.db"
    mode = unusable.stat().st_mode
    unusable.chmod(0o444 if case == "ledger" else 0)  # the hook's user may not write the ledger, or open the database
    try:
        assert cli.main(["live", "notify", str(run.job_id), os.fspath(path), "--complete"]) == 1
    finally:
        unusable.chmod(mode)
    out, err = capsys.readouterr()
    assert out == "" and err.count("\n") == 1 and err.startswith("laueportal live notify: failed: "), err
    assert reason in err
    assert _lines(run) == []


@pytest.mark.parametrize("case", ["config file", "database folder"])
def test_notify_answers_a_config_or_a_database_folder_it_may_not_read_with_one_line_and_status_1(
    engine, tmp_path, capsys, monkeypatch, case
):
    """An acquisition hook run as another user meets these first. A config file it may not read (``get_settings``) and a
    database folder it may not enter (``Path.is_file`` raises EACCES in Python 3.12) are one line and status 1, not a
    traceback."""
    run = create_live_wire_run(engine, tmp_path)
    folder = tmp_path / "closed"
    folder.mkdir()
    database = folder / "laueportal.db" if case == "database folder" else tmp_path / "laueportal.db"
    config = write_config(tmp_path / "hook.toml", database=database, runs_root=tmp_path / "runs")
    for name in [name for name in os.environ if name.upper().startswith("LAUEPORTAL_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("LAUEPORTAL_CONFIG", str(config))
    get_settings.cache_clear()
    closed = config if case == "config file" else folder
    mode = closed.stat().st_mode
    closed.chmod(0)
    try:
        status = cli.main(["live", "notify", str(run.job_id), os.fspath(run.file(1)), "--complete"])
    finally:
        closed.chmod(mode)
        get_settings.cache_clear()
    out, err = capsys.readouterr()
    assert status == 1
    assert out == "" and err.count("\n") == 1, err
    assert err.startswith("laueportal live notify: failed: PermissionError: [Errno 13] Permission denied"), err
    assert _lines(run) == []


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_the_arrivals_lock_is_let_go_while_a_child_forked_without_exec_still_holds_its_descriptor(engine, tmp_path):
    """A child forked without exec while the lock was held shares its open file; the lock is unlocked explicitly before
    the descriptor is closed, so the next writer takes it while the child lives. (The child only reads a pipe and exits,
    so the fork in a process with threads cannot deadlock it.)"""
    from laueportal.jobs import arrivals

    run = create_live_wire_run(engine, tmp_path)
    directory = os.fspath(run.run_directory)
    read, write = os.pipe()
    with arrivals._locked(directory):
        child = os.fork()
        if child == 0:  # the child: it keeps its copy of the descriptor until the parent writes
            os.close(write)
            os.read(read, 1)
            os._exit(0)
    os.close(read)
    try:
        descriptor = os.open(arrivals.lock_path(directory), os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)  # BlockingIOError while the child's copy holds it
        finally:
            os.close(descriptor)
    finally:
        os.write(write, b"x")
        os.close(write)
        os.waitpid(child, 0)


def _follower(engine, tmp_path) -> tuple[LiveRun, ArrivalWriter, Path, Path]:
    """A live wire run, its follower's writer, the follower's run directory and the wire run's points folder."""
    wire = create_live_wire_run(engine, tmp_path, name="wire")
    job_id = create_live_follower(engine, tmp_path, wire)
    with Session(engine) as session:
        job = session.get(Job, job_id)
        assert job is not None and job.run_directory is not None
        directory = Path(job.run_directory)
    points = wire.run_directory / "reconstruction" / "points"
    points.mkdir(parents=True)
    return wire, ArrivalWriter(engine, job_id, directory, read_live_document(directory)), directory, points


def _completed(path: Path, point_id: str = "wire_3", scan_point: int | None = 3) -> CompletedPoint:
    return CompletedPoint(0, point_id, scan_point, os.fspath(path), 11, "2026-10-26T10:00:00+00:00")


def test_a_followers_point_is_admitted_as_its_point_file_with_its_scan_point_point_id_and_depths(engine, tmp_path):
    """A follower's line: its point file's identity, source ``run``, the point's scan point, then ``point_id`` and
    ``n_depths``; the same point again is a no-op."""
    _, writer, directory, points = _follower(engine, tmp_path)
    point_file = points / "wire_3.h5"
    point_file.write_bytes(b"a point file")
    admission = writer.admit_point(_completed(point_file))
    assert admission.admitted and not writer.admit_point(_completed(point_file)).admitted
    (line,) = [json.loads(text) for text in (directory / "arrivals.jsonl").read_text().splitlines()]
    status = os.stat(point_file)
    assert line == {
        "n": 0,
        "path": os.fspath(point_file),
        "size": 12,
        "mtime_ns": status.st_mtime_ns,
        "inode": status.st_ino,
        "device": status.st_dev,
        "source": "run",
        "admitted_at": line["admitted_at"],
        "scan_point": 3,
        "point_id": "wire_3",
        "n_depths": 11,
    }
    assert list(line) == [*LINE_KEYS, "point_id", "n_depths"]


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("outside", "point 'wire_3''s file is not in the followed run's directory"),
        ("not-a-stem", "'../wire_3' is not a point ID (a file's stem)"),
        ("no-scan-point", "point 'wire_3' has no scan point"),
        ("not-a-file", "is not a regular file"),
    ],
)
def test_a_point_outside_the_followed_run_or_not_a_points_file_is_refused(engine, tmp_path, case, reason):
    _, writer, directory, points = _follower(engine, tmp_path)
    point_file = points / "wire_3.h5"
    point_file.write_bytes(b"a point file")
    if case == "outside":
        elsewhere = tmp_path / "wire_3.h5"
        elsewhere.write_bytes(b"a point file")
        point = _completed(elsewhere)
    elif case == "not-a-stem":
        point = _completed(point_file, point_id="../wire_3")
    elif case == "no-scan-point":
        point = _completed(point_file, scan_point=None)
    else:
        (points / "wire_3").mkdir()  # named after its point, so only the file's kind is wrong
        point = _completed(points / "wire_3")
    with pytest.raises(ArrivalRefused) as refused:
        writer.admit_point(point)
    assert reason in str(refused.value)
    assert (directory / "arrivals.jsonl").read_bytes() == b""


@pytest.mark.parametrize("case", ["not-its-files-stem", "another-files-id", "too-many-depths", "through-a-link"])
def test_a_points_id_must_be_its_files_alone_its_depths_bounded_and_its_real_path_inside_the_followed_run(
    engine, tmp_path, case
):
    """A line's meaning, refused for good (never ``ArrivalNotReady``: the follower's run-level error) with nothing
    appended. lauelab names a point file after its point, so its ``point_id`` is the file's stem, and one file's alone
    (it names the point's shard folder and frame IDs); ``n_depths`` at most ``MAX_POINT_DEPTHS`` (the executor builds an
    input per depth); the file inside the followed run's directory by its real path too (a link inside it that leads
    elsewhere), as the run's folder is checked for a file."""
    _, writer, directory, points = _follower(engine, tmp_path)
    point_file = points / "wire_3.h5"
    point_file.write_bytes(b"a point file")
    if case == "not-its-files-stem":
        point = _completed(point_file, point_id="wire_9")
        reason = "point 'wire_9' is not its file's stem 'wire_3'"
    elif case == "another-files-id":
        assert writer.admit_point(_completed(point_file)).admitted
        (points / "again").mkdir()
        other = points / "again" / "wire_3.h5"
        other.write_bytes(b"another point file")
        point = CompletedPoint(1, "wire_3", 4, os.fspath(other), 11, "2026-10-26T10:00:05+00:00")
        reason = f"point 'wire_3' was admitted as {point_file}; {other} is another file"
    elif case == "too-many-depths":
        point = CompletedPoint(0, "wire_3", 3, os.fspath(point_file), MAX_POINT_DEPTHS + 1, "2026-10-26T10:00:00+00:00")
        reason = f"point 'wire_3' has {MAX_POINT_DEPTHS + 1:,} depths; at most {MAX_POINT_DEPTHS:,}"
    else:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "wire_3.h5").write_bytes(b"a file outside the wire run")
        (points / "linked").symlink_to(elsewhere)
        point = _completed(points / "linked" / "wire_3.h5")
        reason = "point 'wire_3''s file is not in the followed run's directory"
    ledger = (directory / "arrivals.jsonl").read_bytes()
    with pytest.raises(ArrivalRefused) as refused:
        writer.admit_point(point)
    assert type(refused.value) is ArrivalRefused and str(refused.value) == reason
    assert (directory / "arrivals.jsonl").read_bytes() == ledger


def test_a_followed_run_whose_directory_is_named_through_a_link_admits_its_points(engine, tmp_path):
    """The real-path check compares real paths on both sides: a followed directory named through a link (a runs root
    that is a link) still holds its points."""
    wire = create_live_wire_run(engine, tmp_path, name="wire")
    job_id = create_live_follower(engine, tmp_path, wire)
    directory = Path(load_job(engine, job_id).run_directory)
    (tmp_path / "runs-link").symlink_to(wire.run_directory.parent)
    linked = tmp_path / "runs-link" / wire.run_directory.name
    live = read_live_document(directory)
    assert live.follows is not None
    follows = LiveFollows(job_id=live.follows.job_id, directory=os.fspath(linked))
    writer = ArrivalWriter(engine, job_id, directory, live.model_copy(update={"follows": follows}))
    points = wire.run_directory / "reconstruction" / "points"
    points.mkdir(parents=True)
    (points / "wire_3.h5").write_bytes(b"a point file")
    point = _completed(linked / "reconstruction" / "points" / "wire_3.h5")
    assert writer.admit_point(point).admitted


def test_a_run_that_follows_no_run_admits_no_point(engine, tmp_path):
    run = create_live_wire_run(engine, tmp_path)
    with pytest.raises(ValueError, match="follows no run"):
        _writer(engine, run).admit_point(_completed(run.file(3)))
