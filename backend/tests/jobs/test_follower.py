"""A follower's thread (``jobs.follower``), in this process: its reader of the wire run's ``completed.jsonl`` (whole
lines, a missing file empty), its admissions in publication order, its end once the wire run has ended, its errors and
waits, its thread, and the executor's hooks sealing on its end. The follower in real job processes is
``tests/indexing/test_live_follower.py``'s."""

from __future__ import annotations

import errno
import json
import logging
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from laueportal.core.settings import LiveSettings
from laueportal.jobs import controls, executor, follower
from laueportal.jobs.arrivals import ArrivalWriter
from laueportal.jobs.dispatch import claim_run
from laueportal.jobs.follower import CompletedReader, CompletedUnreadable, Follower, FollowerThread
from laueportal.jobs.live import read_live_document
from laueportal.jobs.queue import RunPolicy
from laueportal.jobs.rundir import CompletedPoint, append_completed
from laueportal.remote.dm import FAILED_WAIT_SECONDS
from tests.jobs.live_support import LiveRun, create_live_follower, create_live_wire_run, live_hooks
from tests.run_support import load_job
from tests.settings_support import make_database
from tests.worker_support import wait_for

INTERVAL = 5.0


@pytest.fixture
def engine(tmp_path):
    _, engine = make_database(tmp_path)
    yield engine
    engine.dispose()


class Pair:
    """A live wire run (Queued) and its follower: the follower's ``Follower`` on a reader of the wire run's
    ``completed.jsonl``, its wire run ending when ``ended`` says (``on_ended`` runs first, each time it is asked)."""

    def __init__(self, engine: Any, tmp_path: Path) -> None:
        self.engine = engine
        self.wire: LiveRun = create_live_wire_run(engine, tmp_path, name="wire")
        self.job_id = create_live_follower(engine, tmp_path, self.wire)
        self.directory = Path(load_job(engine, self.job_id).run_directory)
        self.points = self.wire.run_directory / "reconstruction" / "points"
        self.points.mkdir(parents=True)
        self.ended = False
        self.on_ended: Any = None
        self.reads = 0
        self.reader = CompletedReader(self.wire.run_directory)
        read = self.reader.read

        def counted() -> list[CompletedPoint]:
            self.reads += 1
            return read()

        self.reader.read = counted  # type: ignore[method-assign]
        writer = ArrivalWriter(engine, self.job_id, self.directory, read_live_document(self.directory))
        self.follower = Follower(writer, self.reader, ended=self._ended, interval=INTERVAL, job_id=self.job_id)
        self.completed = 0

    def _ended(self) -> bool:
        if self.on_ended is not None:
            self.on_ended()
        return self.ended

    def point(
        self, name: str, *, n_depths: int = 11, scan_point: int | None = None, file: bool = True
    ) -> CompletedPoint:
        path = self.points / f"{name}.h5"
        if file:
            path.write_bytes(b"a point file")
        number = int(name.rsplit("_", 1)[1]) if scan_point is None else scan_point
        point = CompletedPoint(self.completed, name, number, os.fspath(path), n_depths, "2026-10-26T10:00:00+00:00")
        self.completed += 1
        return point

    def complete(self, *points: CompletedPoint) -> None:
        """The wire run's lines for ``points``, as it writes them after a publication."""
        append_completed(self.wire.run_directory, points)

    def arrivals(self) -> list[dict[str, Any]]:
        path = self.directory / "arrivals.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def pair(engine, tmp_path) -> Pair:
    return Pair(engine, tmp_path)


def _line(values: dict[str, Any]) -> bytes:
    return (json.dumps(values) + "\n").encode()


def test_the_reader_takes_whole_lines_from_its_offset_and_a_missing_file_is_empty(tmp_path):
    reader = CompletedReader(tmp_path)
    assert (reader.changed(), reader.read()) == (False, [])  # no line written yet
    first = CompletedPoint(0, "wire_3", 3, "/rec/points/wire_3.h5", 11, "2026-10-26T10:00:00+00:00")
    second = CompletedPoint(1, "wire_1", 1, "/rec/points/wire_1.h5", 11, "2026-10-26T10:00:05+00:00")
    (tmp_path / "completed.jsonl").write_bytes(first.line() + second.line()[:20])  # the second still being written
    assert reader.changed()
    assert (reader.read(), reader.tail) == ([first], second.line()[:20])
    assert not reader.changed()  # nothing more since that read
    with open(tmp_path / "completed.jsonl", "ab") as handle:
        handle.write(second.line()[20:])
    assert reader.changed() and reader.read() == [second] and reader.tail == b""


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        (b"not a point\n", "is not a point (JSONDecodeError"),
        (
            _line({"n": 1, "point_id": "wire_1", "scan_point": 1, "path": "/p.h5", "completed_at": "t"}),
            "(no 'n_depths')",
        ),
        (
            _line({"n": 2, "point_id": "w", "scan_point": 1, "path": "/p", "n_depths": 3, "completed_at": "t"}),
            "is point 2, not 1",
        ),
        (
            _line({"n": 1, "point_id": "w", "scan_point": "1", "path": "/p", "n_depths": 3, "completed_at": "t"}),
            "scan_point",
        ),
    ],
    ids=["not-json", "a-key-missing", "n-out-of-order", "a-scan-point-of-text"],
)
def test_a_whole_line_that_is_not_the_next_point_is_unreadable_and_nothing_moves(tmp_path, line, reason):
    first = CompletedPoint(0, "wire_3", 3, "/rec/points/wire_3.h5", 11, "2026-10-26T10:00:00+00:00")
    (tmp_path / "completed.jsonl").write_bytes(first.line() + line)
    reader = CompletedReader(tmp_path)
    with pytest.raises(CompletedUnreadable, match="^line 2 of the followed run's completed.jsonl ") as caught:
        reader.read()
    assert reason in str(caught.value)
    assert (reader.offset, reader.count) == (0, 0)


def test_each_completed_point_is_admitted_in_publication_order_once_and_the_follower_is_done_once_the_wire_run_ended(
    pair,
):
    """The wire run's lines in publication order (3 and 1 in one publication, then 2), neither scan nor any other order:
    each admitted as the follower's next arrival: the point file, its scan point, ``point_id`` and ``n_depths``, source
    ``run``. A look whose stat shows no change reads nothing; once the wire run has ended the file is read to its end
    and the follower is done."""
    assert pair.follower.step() == INTERVAL and pair.reads == 0  # missing: nothing to read
    pair.complete(pair.point("wire_3"), pair.point("wire_1", n_depths=7))
    assert pair.follower.step() == INTERVAL and pair.reads == 1
    assert pair.follower.step() == INTERVAL and pair.reads == 1  # unchanged: no read
    pair.complete(pair.point("wire_2"))
    pair.ended = True
    assert pair.follower.step() is None and pair.follower.done.is_set()
    lines = pair.arrivals()
    assert [(line["n"], line["point_id"], line["scan_point"], line["n_depths"], line["source"]) for line in lines] == [
        (0, "wire_3", 3, 11, "run"),
        (1, "wire_1", 1, 7, "run"),
        (2, "wire_2", 2, 11, "run"),
    ]
    assert [line["path"] for line in lines] == [os.fspath(pair.points / f"wire_{n}.h5") for n in (3, 1, 2)]
    assert pair.follower.admitted == 3


def test_the_wire_runs_end_is_asked_before_its_file_is_read_so_a_line_written_just_before_it_ended_is_admitted(pair):
    """The wire run writes its last lines before its row turns terminal: the follower asks for the row first, then
    reads, so the last point is admitted before the follower is done."""
    pair.complete(pair.point("wire_1"))
    assert pair.follower.step() == INTERVAL

    def last_line_then_the_end() -> None:
        pair.complete(pair.point("wire_2"))
        pair.ended, pair.on_ended = True, None

    pair.on_ended = last_line_then_the_end
    assert pair.follower.step() is None and pair.follower.done.is_set()
    assert [line["point_id"] for line in pair.arrivals()] == ["wire_1", "wire_2"]


def test_a_point_file_not_there_yet_holds_the_points_after_it_and_is_tried_again_after_a_doubling_wait(pair, caplog):
    """A file that cannot be read now (a back-off, up to 60 s, logged once per change): nothing after it is admitted
    (publication order), and the follower is not done while it waits, even once the wire run has ended."""
    pair.complete(pair.point("wire_2", file=False), pair.point("wire_4"))
    pair.ended = True
    waits = [pair.follower.step() for _ in range(5)]
    assert waits == [2 * INTERVAL, 4 * INTERVAL, 8 * INTERVAL, FAILED_WAIT_SECONDS, FAILED_WAIT_SECONDS]
    assert not pair.follower.done.is_set() and not (pair.directory / "arrivals.jsonl").read_bytes()
    assert [record.getMessage() for record in caplog.records if "look failed" in record.getMessage()] == [
        f"Job {pair.job_id}: the follower's look failed, tried again: ArrivalNotReady: "
        f"{pair.points / 'wire_2.h5'} does not exist"
    ]
    (pair.points / "wire_2.h5").write_bytes(b"a point file")
    assert pair.follower.step() is None and pair.follower.done.is_set()
    assert [line["point_id"] for line in pair.arrivals()] == ["wire_2", "wire_4"]


def test_a_held_point_shows_in_the_followers_arrival_status_json_from_its_first_look_until_it_is_admitted(
    pair, monkeypatch
):
    """A completed point whose file cannot be read holds the follower, even once the wire run has ended, so its own
    ``arrival-status.json`` says which point, why and since when (the job page's arrival status). Written when the
    reason changes, not at each look, again at the next look when that write failed, and cleared at the look that admits
    the point; none while nothing waits."""
    status_file = pair.directory / "arrival-status.json"
    writes: list[dict[str, Any]] = []
    write_json_file = follower.write_json_file

    def counted(path: Any, payload: dict[str, Any], **kwargs: Any) -> str:
        writes.append(dict(payload))
        if len(writes) == 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        return write_json_file(path, payload, **kwargs)

    monkeypatch.setattr(follower, "write_json_file", counted)
    pair.complete(pair.point("wire_1"))
    assert pair.follower.step() == INTERVAL and writes == []  # nothing waits: no status
    pair.complete(pair.point("wire_2", file=False), pair.point("wire_4"))
    pair.ended = True
    first = datetime.now(UTC).replace(microsecond=0)
    for _ in range(4):
        pair.follower.step()
    status = json.loads(status_file.read_text())
    assert status["error"] == (
        "point 'wire_2' (line 2 of the followed run's completed.jsonl) not admitted yet: "
        f"{pair.points / 'wire_2.h5'} does not exist"
    )
    assert first <= datetime.fromisoformat(status["error_at"]) <= datetime.now(UTC)
    assert (status["checked_at"], status["rows_seen"]) == (status["error_at"], 3)
    assert len(writes) == 2 and writes[1] == status  # the reason once, again after its failed write: not every look
    assert not pair.follower.done.is_set()
    (pair.points / "wire_2.h5").write_bytes(b"a point file")
    assert pair.follower.step() is None and pair.follower.done.is_set()
    status = json.loads(status_file.read_text())
    assert (status["error"], status["error_at"], status["rows_seen"]) == (None, None, 3) and len(writes) == 3


def _status(pair: Pair) -> dict[str, Any] | None:
    path = pair.directory / "arrival-status.json"
    return json.loads(path.read_text()) if path.exists() else None


def test_the_status_says_the_last_check_and_the_points_read_at_a_bounded_rate_while_it_has_something_to_say(
    pair, monkeypatch
):
    """While the follower follows, its ``arrival-status.json`` keeps its last check and its points read current (the job
    page's Last Check and Points Read, the Live page's "Checked"), written at most every ``status_seconds`` (not at each
    look) but at once when its waiting reason changes; a refresh while it waits keeps the wait's start. With nothing new
    (no wait, no point read), nothing is written (the job's heartbeat says it is alive)."""
    now = [0.0]
    writes: list[dict[str, Any]] = []
    write_json_file = follower.write_json_file

    def counted(path: Any, payload: dict[str, Any], **kwargs: Any) -> str:
        writes.append(dict(payload))
        return write_json_file(path, payload, **kwargs)

    monkeypatch.setattr(follower, "write_json_file", counted)
    looks = Follower(
        pair.follower.writer, pair.reader, ended=pair._ended, interval=INTERVAL, job_id=pair.job_id,
        status_seconds=10.0, clock=lambda: now[0],
    )  # fmt: skip

    def look(at: float) -> None:
        now[0] = at
        looks.step()

    pair.complete(pair.point("wire_1"))
    look(0.0)
    pair.complete(pair.point("wire_2"))
    look(5.0)
    assert writes == [] and _status(pair) is None  # none due yet
    look(10.0)
    assert [(write["rows_seen"], write["error"], write["error_at"]) for write in writes] == [(2, None, None)]
    pair.complete(pair.point("wire_3", file=False))
    look(15.0)  # a wait starts: at once
    look(17.0)
    look(25.0)  # due again: refreshed, the wait's start kept
    waiting = [(write["rows_seen"], write["error"] is not None, write["error_at"]) for write in writes[1:]]
    assert waiting == [(3, True, writes[1]["checked_at"])] * 2 and writes[1]["error_at"] == writes[1]["checked_at"]
    (pair.points / "wire_3.h5").write_bytes(b"a point file")
    look(26.0)  # the wait clears: at once
    assert len(writes) == 4 and _status(pair) == {**writes[3], "error": None, "error_at": None}
    assert datetime.fromisoformat(writes[3]["checked_at"]) >= datetime.fromisoformat(writes[0]["checked_at"])
    look(40.0)
    look(60.0)  # nothing new: nothing to say
    assert len(writes) == 4
    pair.complete(pair.point("wire_4"))
    look(65.0)  # a point read: due, written
    assert len(writes) == 5 and writes[4]["rows_seen"] == 4


@pytest.mark.parametrize("case", ["refused-for-good", "clearing-write-failed", "stopped"])
def test_nothing_stale_stays_in_the_status_once_the_follower_has_ended(pair, tmp_path, monkeypatch, case):
    """The thread's end writes its last look's status with no waiting reason, so an ended follower's
    ``arrival-status.json`` never says it waits: not once the held point is refused for good (the run's error), nor when
    the clearing write failed at its last look (written again at its close), nor after a Stop."""
    failing = threading.Event()
    write_json_file = follower.write_json_file

    def flaky(path: Any, payload: dict[str, Any], **kwargs: Any) -> str:
        if failing.is_set():
            raise OSError(errno.EIO, "Input/output error")
        return write_json_file(path, payload, **kwargs)

    monkeypatch.setattr(follower, "write_json_file", flaky)
    looks = Follower(pair.follower.writer, pair.reader, ended=pair._ended, interval=0.01, job_id=pair.job_id)
    stopped = threading.Event()
    pair.complete(pair.point("wire_1", file=False))
    pair.ended = case != "stopped"
    thread = FollowerThread(looks, stopping=stopped.is_set).start()
    try:
        assert wait_for(lambda: (_status(pair) or {}).get("error") is not None, 5)
        point_file = pair.points / "wire_1.h5"
        if case == "refused-for-good":
            elsewhere = tmp_path / "elsewhere.h5"
            elsewhere.write_bytes(b"a point file")
            point_file.symlink_to(elsewhere)  # its real path outside the wire run's directory
        elif case == "clearing-write-failed":
            failing.set()
            point_file.write_bytes(b"a point file")  # admitted at the last look, whose write fails
        else:
            stopped.set()
        assert wait_for(lambda: not thread.alive, 5)
        if case == "clearing-write-failed":
            assert looks.done.is_set() and _status(pair)["error"] is not None  # type: ignore[index]
            failing.clear()
    finally:
        thread.close()
    status = _status(pair)
    assert status is not None and (status["error"], status["error_at"], status["rows_seen"]) == (None, None, 1)
    assert (looks.error is not None) == (case == "refused-for-good")


@pytest.mark.parametrize("case", ["database", "file"])
def test_a_failed_look_shows_its_type_in_arrival_status_json_and_a_database_errors_text_only_in_the_log(pair, case):
    """The status holds no secret: a database error (or a defect) shows its type only, a file's error its text (a path
    and the system's reason)."""

    def failing() -> bool:
        if case == "database":
            raise OperationalError("SELECT job.status FROM job", {}, Exception("the database's own words"))
        raise OSError(errno.EIO, "Input/output error", os.fspath(pair.wire.run_directory / "completed.jsonl"))

    pair.on_ended = failing
    assert pair.follower.step() == 2 * INTERVAL
    expected = {
        "database": "the follower's look failed: OperationalError",
        "file": "the follower's look failed: OSError: [Errno 5] Input/output error: "
        f"'{pair.wire.run_directory / 'completed.jsonl'}'",
    }
    assert json.loads((pair.directory / "arrival-status.json").read_text())["error"] == expected[case]


@pytest.mark.parametrize("case", ["outside-the-wire-run", "unreadable-line"])
def test_a_point_refused_for_good_or_an_unreadable_line_is_the_followers_run_level_error(pair, tmp_path, case):
    if case == "outside-the-wire-run":
        elsewhere = tmp_path / "elsewhere.h5"
        elsewhere.write_bytes(b"a point file")
        pair.complete(CompletedPoint(0, "wire_1", 1, os.fspath(elsewhere), 11, "2026-10-26T10:00:00+00:00"))
        expected = "point 'wire_1''s file is not in the followed run's directory"
    else:
        (pair.wire.run_directory / "completed.jsonl").write_bytes(b"[]\n")
        expected = "line 1 of the followed run's completed.jsonl is not a point (TypeError: not an object)"
    assert pair.follower.step() is None
    assert str(pair.follower.error) == expected and not pair.follower.done.is_set()
    assert not (pair.directory / "arrivals.jsonl").read_bytes()


def test_a_follower_whose_run_was_stopped_admits_nothing_more_and_ends(pair):
    assert claim_run(pair.engine, pair.job_id)
    controls.cancel_run(pair.job_id, engine=pair.engine)  # the follower's own Stop
    pair.complete(pair.point("wire_1"))
    assert pair.follower.step() is None
    assert pair.follower.error is None and not pair.follower.done.is_set()
    assert not (pair.directory / "arrivals.jsonl").read_bytes()


def test_wire_run_ended_reads_the_wire_runs_row(pair):
    assert not follower.wire_run_ended(pair.engine, pair.wire.job_id)  # Queued
    controls.cancel_run(pair.wire.job_id, engine=pair.engine)  # a Queued run: Cancelled at once
    assert follower.wire_run_ended(pair.engine, pair.wire.job_id)
    assert follower.wire_run_ended(pair.engine, 9999)  # a row that is gone


class _Steps:
    """A stand-in ``Follower`` whose looks are counted, each followed by ``waits``' next wait (an error: raised)."""

    def __init__(self, *waits: float | Exception | None) -> None:
        self.waits = list(waits)
        self.looks: list[float] = []
        self.done = threading.Event()
        self.error = None
        self.finished = 0

    def finish(self) -> None:
        self.finished += 1

    def step(self) -> float | None:
        self.looks.append(time.monotonic())
        wait = self.waits.pop(0) if self.waits else 0.05
        if isinstance(wait, Exception):
            raise wait
        return wait

    def failed(self, error: Exception) -> float:
        return 0.05


def test_the_thread_looks_at_once_then_after_each_wait_and_ends_at_its_stop_its_close_or_its_last_look():
    stopped = threading.Event()
    steps = _Steps(0.2)
    thread = FollowerThread(steps, stopping=stopped.is_set).start()  # type: ignore[arg-type]
    try:
        assert wait_for(lambda: len(steps.looks) == 2, 5)
        assert steps.looks[1] - steps.looks[0] >= 0.2  # the wait the look returned
        stopped.set()
        assert wait_for(lambda: not thread.alive, 5)
        looked = len(steps.looks)
        time.sleep(0.2)
        assert len(steps.looks) == looked  # no look once stopped
    finally:
        thread.close()
    closing = FollowerThread(_Steps(), stopping=lambda: False).start()  # type: ignore[arg-type]
    closing.close()
    assert not closing.alive
    ending = _Steps(None)
    last = FollowerThread(ending, stopping=lambda: False).start()  # type: ignore[arg-type]
    assert wait_for(lambda: not last.alive, 5) and len(ending.looks) == 1
    assert (steps.finished, closing.follower.finished, ending.finished) == (2, 2, 1)  # type: ignore[attr-defined]


def test_a_defect_in_a_look_is_waited_out_and_its_traceback_logged_once_per_change_of_its_reason(caplog):
    """An error that escapes a look is waited out (``failed``), its traceback logged once while it repeats, again once
    another or a good look comes between."""
    caplog.set_level(logging.ERROR, logger="laueportal.jobs.follower")
    defect, other = RuntimeError("a defect"), ValueError("another")
    steps = _Steps(defect, defect, defect, other, other, 0.01, other, None)
    thread = FollowerThread(steps, stopping=lambda: False).start()  # type: ignore[arg-type]
    assert wait_for(lambda: not thread.alive, 5) and len(steps.looks) == 8
    logged = [record.exc_info[1] for record in caplog.records if record.exc_info is not None]
    assert logged == [defect, other, other]  # the last after a good look: again


def test_a_follower_thread_starts_only_for_a_run_that_follows_a_wire_run(pair):
    for job_id in (pair.wire.job_id, pair.job_id):
        assert claim_run(pair.engine, job_id)
    started = []
    for job_id in (pair.wire.job_id, pair.job_id):
        request, _ = executor._start(pair.engine, job_id, RunPolicy())
        assert request is not None
        started.append(follower.start_follower(pair.engine, request, LiveSettings(), stopping=lambda: True))
    assert started[0] is None  # a notify run
    assert started[1] is not None
    started[1].close()
    assert not started[1].alive


def test_the_hooks_seal_a_follower_once_its_thread_is_done_and_raise_its_run_level_error(pair):
    """The follower's end (its wire run ended, every completed point admitted) seals the run as a Stop would (the lock
    taken once, the ledger read to its end); its thread's error is the run's."""
    hooks, _ = live_hooks(pair.engine, pair.job_id, interval=3600.0)  # its own rate never reads the ledger here
    thread = SimpleNamespace(done=False, error=None, close=lambda: closed.append(True))
    closed: list[bool] = []
    hooks.follower = thread  # type: ignore[assignment]
    hooks.before_seal = thread.close
    assert hooks.arrivals() == [] and not hooks.should_stop()  # its first look reads nothing (the rate)
    pair.complete(pair.point("wire_1", n_depths=2))
    pair.follower.step()  # its line admitted, not read yet: the rate
    assert hooks.arrivals() == [] and not hooks.sealed
    thread.done = True
    entries = hooks.arrivals()
    # read to its end at the seal: each depth of the follower's depth_range (0-2), the one the point lacks too
    assert [entry.input_id for entry in entries] == ["wire_1_0", "wire_1_1", "wire_1_2"]
    assert hooks.sealed and hooks.should_stop() and closed == [True]
    assert "its wire run has ended and every point it completed is admitted" in hooks.messages
    failing, _ = live_hooks(pair.engine, pair.wire.job_id)
    failing.follower = SimpleNamespace(done=False, error=CompletedUnreadable("line 1 ..."))  # type: ignore[assignment]
    with pytest.raises(CompletedUnreadable, match="line 1"):
        failing.arrivals()
