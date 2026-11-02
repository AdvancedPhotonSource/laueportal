"""The catalog thread (``jobs.catalog``): a live run's arrivals from DM's file catalog, in its job process.

The reader's rules run one cycle at a time with a moving clock (``CatalogReader.step``), DM being the real client and
script on a stand-in API (``FakeDMService``: each process counted, each call's queries and each DM request inside it
read back): the cursor and its overlap, a file seen twice admitted once, the cadence and its back-offs, one call per
cycle reading every page since the cursor, the row cap's next cycle at once, the re-scan after the cursor's query, the
run's template before a row's size and time, a file held back until it has the catalog's size, the files never admitted
and the errors in ``arrival-status.json``, the thread's ends at a closed run and at its run's stop. The thread runs in
``execute_run`` in this process with DM installed for real processes (``FakeDMInstall``: its calls logged by pid and
time): it ends before the seal, killing its call in flight, and makes no call after it; none for a ``notify`` run or a
follower (only while its source is a DAQ); a catalog that keeps failing never fails the run. The last test runs a live
run through a real worker (``slow``).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from laueportal.core.settings import LiveSettings
from laueportal.jobs import catalog, contract, controls, executor
from laueportal.jobs.arrivals import ArrivalsClosed, ArrivalWriter
from laueportal.jobs.catalog import CatalogReader, CatalogThread, start_catalog
from laueportal.jobs.daq import live_client
from laueportal.jobs.dispatch import claim_run
from laueportal.jobs.live import read_live_document
from laueportal.jobs.queue import RunPolicy
from laueportal.jobs.state import JobStatus, RunPhase
from laueportal.remote.dm import CatalogRead, CatalogRow, DMRecordError
from tests.jobs.job_computes import live_compute
from tests.jobs.live_support import LiveRun, create_live_follower, create_live_wire_run
from tests.remote_support import FakeDMInstall, FakeDMService
from tests.run_support import FAST_POLICY, load_job
from tests.settings_support import make_database
from tests.worker_support import Live, alive, start_worker, wait_for

DATA = b"data"
SETTINGS = LiveSettings(
    dm_setup_file=Path("/fake/dm-setup.sh"),
    dm_timeout_seconds=15,
    catalog_seconds=5,
    catalog_idle_seconds=30,
    catalog_rescan_seconds=300,
    catalog_page_size=1000,
    catalog_max_rows=5000,
)


def paged(page: int, cap: int) -> LiveSettings:
    return LiveSettings(**{**SETTINGS.model_dump(), "catalog_page_size": page, "catalog_max_rows": cap})


@pytest.fixture
def engine(tmp_path):
    _, engine = make_database(tmp_path)
    yield engine
    engine.dispose()


@pytest.fixture
def dm(tmp_path, monkeypatch) -> FakeDMService:
    return FakeDMService(tmp_path / "dm", monkeypatch)


@pytest.fixture
def run(engine, tmp_path) -> LiveRun:
    """A live wire run whose source is DAQ ``1a2b`` (experiment ``e1``, storage ``tmp_path``), its folder
    ``live-input``."""
    return create_live_wire_run(engine, tmp_path, source="daq")


def row(number: int, at: float = 1000.0, size: int = len(DATA), *, name: str = "live") -> dict:
    """The catalog's row of the run's file ``number``."""
    return {
        "experimentFilePath": f"live-input/{name}_{number}.h5",
        "fileSize": size,
        "fileProcessingTime": at,
        "daqId": "1a2b",
        "experimentName": "e1",
    }


START = datetime(2026, 10, 26, 10, 0, 0, tzinfo=UTC)
"""The portal's clock at a test's start (``Wall``)."""
START_SECONDS = START.timestamp()


class Wall:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


def reader(engine, run: LiveRun, settings: LiveSettings = SETTINGS, *, client=None, wall=None) -> CatalogReader:
    live = read_live_document(run.run_directory)
    assert live.daq is not None
    return CatalogReader(
        engine,
        run.job_id,
        run.run_directory,
        live,
        live.daq,
        settings,
        client or live_client(settings, own_session=True),
        wall=wall or Wall(),
    )


def queries(dm: FakeDMService) -> list[tuple[tuple[float | None, int], ...]]:
    """Each catalog call's reads: each query's ``fileProcessingTime`` bound (None: a full scan) and ``skip``."""
    return [
        tuple(
            (read["query"].get("fileProcessingTime", {}).get("$gte"), read["skip"])
            for read in call["request"]["args"]["reads"]
        )
        for call in dm.runs
    ]


def ledger(run: LiveRun) -> list[str]:
    lines = (run.run_directory / "arrivals.jsonl").read_text().splitlines()
    return [os.path.basename(json.loads(line)["path"]) for line in lines]


def status(run: LiveRun) -> dict:
    return json.loads((run.run_directory / "arrival-status.json").read_text())


# ---------------------------------------------------------------------------
# The reader's rules, one cycle at a time


def test_the_first_query_is_a_full_scan_then_each_from_the_cursor_less_the_overlap_and_a_file_seen_twice_is_admitted_once(
    engine, run, dm, monkeypatch
):
    admits = []
    admit = ArrivalWriter.admit
    monkeypatch.setattr(
        ArrivalWriter, "admit", lambda writer, path, **kw: admits.append(path) or admit(writer, path, **kw)
    )
    for number in (1, 2, 3):
        run.file(number, DATA)
    dm.files = [row(1, 1000.0), row(2, 1100.0)]
    catalog = reader(engine, run)
    catalog.step(0)
    catalog.step(5)  # both again: the overlap
    dm.files.append(row(3, 1200.0))
    catalog.step(10)
    catalog.step(15)
    assert queries(dm) == [((None, 0),), ((980.0, 0),), ((980.0, 0),), ((1080.0, 0),)]
    assert ledger(run) == ["live_1.h5", "live_2.h5", "live_3.h5"]
    assert [os.path.basename(path) for path in admits] == ["live_1.h5", "live_2.h5", "live_3.h5"]  # known: not again


def test_while_nothing_arrives_the_wait_doubles_from_catalog_seconds_to_catalog_idle_seconds_and_an_arrival_resets_it(
    engine, run, dm
):
    catalog = reader(engine, run)
    now, waits = 0.0, []

    def step() -> None:
        nonlocal now
        wait = catalog.step(now)
        assert wait is not None
        waits.append(wait)
        now += wait

    for _ in range(5):
        step()
    run.file(1, DATA)
    dm.files = [row(1, START_SECONDS + now)]  # catalogued now: after the provisional cursor (DM's clock the portal's)
    for _ in range(3):
        step()
    assert waits == [5, 10, 20, 30, 30, 5, 5, 10]
    assert len(dm.runs) == 8  # one call per cycle


def test_one_dm_call_per_cycle_reads_every_page_since_the_cursor_one_request_at_a_time(engine, run, dm):
    """1,300 rows since the cursor (about one cycle's at 10 frames per second with the overlap) are one subprocess, two
    requests of at most ``catalog_page_size`` (1,000), sequential."""
    run.file(1, DATA)
    run.file(2, DATA)
    dm.files = [row(number, 1000.0, name="other") for number in range(1298)] + [row(1, 1001.0), row(2, 1002.0)]
    catalog = reader(engine, run)
    assert catalog.step(0) == 5  # an arrival
    assert len(dm.runs) == 1
    requests = dm.requests()
    assert [(request["skip"], request["limit"]) for request in requests] == [(0, 1000), (1000, 1000)]
    assert len({request["pid"] for request in requests}) == 1
    assert ledger(run) == ["live_1.h5", "live_2.h5"]
    assert status(run)["rows_seen"] == 1300 and status(run)["error"] is None  # the other files: no error


def test_each_cycle_s_call_reads_within_half_of_dm_timeout_seconds(engine, run, dm):
    """The call's ``seconds`` is half of ``live.dm_timeout_seconds`` (the call's own timeout, 15 s here); the other half
    is the margin for what the DM script cannot time (``DEADLINE_SHARE``)."""
    reader(engine, run).step(0)
    assert [(call["request"]["args"]["seconds"], call["timeout"]) for call in dm.runs] == [(7.5, 15)]


def test_a_query_the_row_cap_stops_goes_on_at_once_and_the_cursor_moves_only_at_its_end(engine, run, dm):
    """A call reads at most ``catalog_max_rows``; the query keeps its place (``skip``, the same cursor) and the next
    cycle comes at once, still one call at a time."""
    for number in range(1, 9):
        run.file(number, DATA)
    dm.files = [row(number, 999.0 + number) for number in (1, 2, 3)]
    catalog = reader(engine, run, paged(2, 3))
    assert [catalog.step(0), catalog.step(0)] == [0, 5]  # 3 rows, the cap: more; then the query's end (empty)
    dm.files += [row(number, 999.0 + number) for number in range(4, 9)]
    assert [catalog.step(5) for _ in range(3)] == [0, 0, 5]
    assert catalog.step(10) == 0  # the overlap holds the 8 rows: 3 a call again
    assert queries(dm) == [
        ((None, 0),),
        ((None, 3),),
        ((882.0, 0),),  # the cursor: the newest seen (1002) less 120 s
        ((882.0, 3),),
        ((882.0, 6),),
        ((887.0, 0),),  # moved at the query's end (1007)
    ]
    assert [(request["skip"], request["limit"]) for request in dm.requests()][:4] == [(0, 2), (2, 1), (3, 2), (0, 2)]
    assert ledger(run) == [f"live_{number}.h5" for number in range(1, 9)]


def test_a_full_scan_comes_again_every_catalog_rescan_seconds_and_finds_a_row_catalogued_late(engine, run, dm):
    for number in (1, 2, 3):
        run.file(number, DATA)
    dm.files = [row(1, 1000.0)]
    catalog = reader(engine, run)
    catalog.step(0)
    dm.files.append(row(2, 2000.0))
    catalog.step(5)
    dm.files.append(row(3, 500.0))  # catalogued late, with an old processing time: outside the cursor's overlap
    catalog.step(10)
    catalog.step(299)
    assert ledger(run) == ["live_1.h5", "live_2.h5"]
    catalog.step(300)  # 300 s after the first full scan's start: after the cursor's query, in the same call
    assert queries(dm) == [((None, 0),), ((880.0, 0),), ((1880.0, 0),), ((1880.0, 0),), ((1880.0, 0), (None, 0))]
    assert ledger(run) == ["live_1.h5", "live_2.h5", "live_3.h5"]


def test_a_full_re_scan_of_a_large_folder_never_holds_back_a_new_row_of_the_cursor_s_query(engine, run, dm):
    """The re-scan of a folder of N rows takes about N / ``catalog_max_rows`` calls, back to back; each reads the
    cursor's query first, so a new row is admitted at the first."""
    for number in (1, 2, 3):
        run.file(number, DATA)
    old = [row(number, 100.0, name="old") for number in range(20)]
    dm.files = [*old, row(1, 1000.0)]
    catalog = reader(engine, run, paged(2, 6))
    waits = [catalog.step(0) for _ in range(4)]
    assert waits == [0, 0, 0, 5] and ledger(run) == ["live_1.h5"]  # the first scan: 21 rows, 6 a call
    dm.files.append(row(2, 1300.0))
    assert catalog.step(300) == 0  # the re-scan: more to read
    assert ledger(run) == ["live_1.h5", "live_2.h5"]  # the new row at once
    dm.files.append(row(3, 1310.0))
    assert catalog.step(300) == 0
    assert ledger(run) == ["live_1.h5", "live_2.h5", "live_3.h5"]
    assert queries(dm)[4:6] == [((880.0, 0), (None, 0)), ((1180.0, 0), (None, 4))]
    while catalog.step(300) == 0:
        pass
    assert len(dm.runs) == 10 and queries(dm)[-1] == ((1190.0, 0), (None, 20))  # 23 rows: 4 a call, then the end


def test_a_quiet_folder_of_30_000_other_files_is_read_whole_once_per_re_scan_and_else_from_a_provisional_cursor(
    engine, run, dm
):
    """While the run has no row, a full scan read to its end sets a provisional cursor, the earlier of the portal's
    clock at the scan's start and the newest time it showed plus the overlap (here the other files' newest plus 120 s:
    an older acquisition, 10 files a second, that ended an hour ago). The idle cycles query from it less the overlap
    (one request, the newest of those files), and the folder is read whole again only every ``catalog_rescan_seconds``,
    after the cursor's query."""
    newest = START_SECONDS - 3600.0
    dm.files = [row(number, newest - (29_999 - number) / 10, name="other") for number in range(30_000)]
    catalog = reader(engine, run)
    assert [catalog.step(0) for _ in range(7)] == [0, 0, 0, 0, 0, 0, 5]  # 5,000 rows a call, then the scan's end
    assert [catalog.step(now) for now in (5, 15, 35, 299)] == [10, 20, 30, 30]  # idle
    catalog.step(300)
    calls = queries(dm)
    assert calls[:7] == [((None, skip),) for skip in range(0, 35_000, 5_000)]
    assert calls[7:] == [((newest, 0),)] * 4 + [((newest, 0), (None, 0))]  # then the re-scan
    pids: dict[int, list[tuple[int, int]]] = {}
    for request in dm.requests():
        pids.setdefault(request["pid"], []).append((request["skip"], request["limit"]))
    assert list(pids.values())[7:11] == [[(0, 1000)]] * 4  # 1 row each
    assert (status(run)["rows_seen"], status(run)["error"]) == (30_000, None)


def test_a_burst_of_other_files_just_before_the_scan_is_read_again_only_until_the_next_re_scan(engine, run, dm):
    """Other files catalogued in one burst just before the first scan (1,500 in 60 s, the newest 1 s before it) are
    newer than the portal's clock less the overlap, so the provisional cursor is the clock and the idle cycles read
    the burst again (from the clock less 120 s). The re-scan, 300 s later, starts past the burst's newest plus the
    overlap, so the cursor becomes that time: the idle cycles then read the newest row only."""
    wall = Wall()
    newest = START_SECONDS - 1.0
    dm.files = [row(number, newest - (1_499 - number) * 0.04, name="other") for number in range(1_500)]
    catalog = reader(engine, run, wall=wall)

    def step(now: float) -> float | None:
        wall.now = START + timedelta(seconds=now)
        return catalog.step(now)

    assert [step(now) for now in (0, 5, 15, 35, 299, 300, 330)] == [5, 10, 20, 30, 30, 30, 30]
    burst = ((START_SECONDS - 120, 0),)
    assert queries(dm) == [((None, 0),), *[burst] * 4, (*burst, (None, 0)), ((newest, 0),)]
    pids: dict[int, list[tuple[int, int]]] = {}
    for request in dm.requests():
        pids.setdefault(request["pid"], []).append((request["skip"], request["limit"]))
    whole = [(0, 1000), (1000, 1000)]
    assert list(pids.values()) == [whole] * 5 + [whole * 2, [(0, 1000)]]  # the burst until the re-scan, then 1 row
    assert (status(run)["rows_seen"], status(run)["error"]) == (1_500, None)


def test_a_run_s_first_frame_after_the_provisional_cursor_is_admitted_at_the_next_cycle_and_its_time_replaces_it(
    engine, run, dm
):
    """The portal's clock bounds the provisional cursor (another file's far-future time never moves it past); the run's
    first frame, catalogued after the scan, is admitted at the next cycle, and its time replaces the provisional cursor
    (no max with it) though earlier: DM's clock is 30 s behind the portal's here."""
    wall = Wall()
    notes = {**row(0, 9.0e9), "experimentFilePath": "live-input/notes.txt"}
    dm.files = [notes, row(1, START_SECONDS - 7200.0, name="other")]
    catalog = reader(engine, run, wall=wall)
    assert catalog.step(0) == 5
    run.file(1, DATA)
    dm.files.append(row(1, START_SECONDS - 25.0))  # catalogued at the portal's 10:00:05
    wall.now += timedelta(seconds=5)
    assert catalog.step(5) == 5 and ledger(run) == ["live_1.h5"]
    catalog.step(10)
    assert queries(dm) == [((None, 0),), ((START_SECONDS - 120, 0),), ((START_SECONDS - 145, 0),)]


@pytest.mark.parametrize(
    ("skew", "other", "bound"),
    [
        (200.0, False, 110.0),  # until a frame's time passes the cursor less the overlap; then all of them
        (1000.0, False, 330.0),  # the next re-scan finds them: catalog_rescan_seconds and a cycle
        (-200.0, False, 30.0),  # the portal behind: no cost
        (200.0, True, 30.0),  # another file's time plus the overlap bounds the cursor: no cost
        (1000.0, True, 30.0),
        (-200.0, True, 30.0),
    ],
)
def test_with_the_portal_s_clock_ahead_of_dm_s_or_behind_no_frame_is_lost_and_the_wait_is_bounded(
    engine, run, dm, skew, other, bound
):
    """The provisional cursor's cost is the two clocks. DM's clock is ``START_SECONDS`` plus the test's ``now``,
    the portal's ``skew`` ahead; the folder is empty, or holds another file catalogued 5 s before the first scan; the
    run's frames come every 30 s from 30 s to 360 s. Every frame is admitted, none twice, and the longest wait from a
    frame's cataloguing to its admission is within ``bound``."""
    wall = Wall()
    dm.files = [row(0, START_SECONDS - 5.0, name="other")] if other else []
    catalog = reader(engine, run, wall=wall)
    frames = {f"live_{number}.h5": 30.0 * number for number in range(1, 13)}
    admitted: dict[str, float] = {}
    now = 0.0
    while now < 400:
        for name, at in frames.items():
            if at <= now and not (run.folder / name).exists():
                (run.folder / name).write_bytes(DATA)
                dm.files.append({**row(0, START_SECONDS + at), "experimentFilePath": f"live-input/{name}"})
        wall.now = START + timedelta(seconds=now + skew)
        wait = catalog.step(now)
        assert wait is not None
        for name in ledger(run) if catalog.admitted else []:
            admitted.setdefault(name, now)
        now += wait
    clock = START_SECONDS + skew
    cursor = min(clock, START_SECONDS - 5.0 + 120) if other else clock  # the provisional cursor
    assert queries(dm)[1] == ((cursor - 120, 0),)
    assert admitted.keys() == frames.keys()  # none lost
    assert sorted(ledger(run)) == sorted(frames)  # none twice
    assert max(admitted[name] - at for name, at in frames.items()) <= bound


def test_a_file_without_the_catalog_s_size_is_held_back_and_admitted_once_it_matches_by_its_own_stat(engine, run, dm):
    run.file(1, DATA[:2])  # still being written
    dm.files = [row(1, 1000.0), row(9, 1000.0)]  # file 9 is not there yet
    catalog = reader(engine, run)
    catalog.step(0)
    for now, number, at in ((5, 2, 1500.0), (10, 3, 5000.0)):  # the cursor moves far past the held rows
        run.file(number, DATA)
        dm.files.append(row(number, at))
        catalog.step(now)
    assert ledger(run) == ["live_2.h5", "live_3.h5"] and queries(dm)[-1] == ((1380.0, 0),)
    run.file(1, DATA)
    run.file(9, DATA)
    calls = len(dm.runs)
    assert catalog.step(15) == 5  # an arrival
    assert ledger(run) == ["live_2.h5", "live_3.h5", "live_1.h5", "live_9.h5"]
    assert len(dm.runs) == calls + 1 and queries(dm)[-1] == ((4880.0, 0),)  # the held files by their stat, no call
    assert status(run)["error"] is None  # admitted: nothing left to report


def test_a_catalogued_file_never_admitted_shows_in_arrival_status_json_with_the_counts_and_the_latest_reason(
    engine, run, dm
):
    """A file held back (its size never reached) or refused for good by ``admit`` shows in ``arrival-status.json``'s
    error, not only in the job log: one refused at once, one held once it has been held across a full re-scan, one that
    started after it was first held."""
    run.file(1, DATA[:2])  # never reaches the catalog's size
    dm.files = [row(1, 1000.0)]
    catalog = reader(engine, run)
    catalog.step(0)  # the first full scan holds it
    assert status(run)["error"] is None
    (run.folder / "live_2.h5").symlink_to(run.folder / "live_1.h5")  # admit refuses a link for good
    dm.files.append(row(2, 1001.0))
    catalog.step(5)
    refused = f"{run.folder / 'live_2.h5'} is not a regular file"
    assert status(run)["error"] == f"0 catalogued file(s) not admitted yet, 1 refused: {refused}"
    catalog.step(299)
    assert status(run)["error"] == f"0 catalogued file(s) not admitted yet, 1 refused: {refused}"
    catalog.step(300)  # the re-scan, read to its end with file 1 still held
    assert queries(dm)[-1] == ((881.0, 0), (None, 0))
    held = f"{run.folder / 'live_1.h5'} has 2 bytes; the catalog says 4"
    assert status(run)["error"] == f"1 catalogued file(s) not admitted yet, 1 refused: {held}"
    run.file(1, DATA)
    catalog.step(305)
    assert ledger(run) == ["live_1.h5"]
    assert status(run)["error"] == f"0 catalogued file(s) not admitted yet, 1 refused: {refused}"


def test_a_file_this_host_sees_complete_a_cycle_after_the_catalog_is_no_error_and_logs_no_warning(
    engine, run, dm, caplog
):
    """In six cycles, each catalogues a file this host does not have at its size yet (a copy still landing; complete at
    the next cycle): the error stays null, nothing is logged as a warning, and every file is admitted."""
    caplog.set_level(logging.INFO, logger="laueportal.jobs.catalog")
    catalog = reader(engine, run)
    now = 0.0
    for number in range(1, 7):
        if number > 1:
            run.file(number - 1, DATA)  # the last cycle's file, complete now
        run.file(number, DATA[:2])
        dm.files.append(row(number, 1000.0 + number))
        wait = catalog.step(now)
        assert wait is not None and status(run)["error"] is None, number
        now += wait
    run.file(6, DATA)
    catalog.step(now)
    assert ledger(run) == [f"live_{number}.h5" for number in range(1, 7)]
    assert status(run)["error"] is None
    assert [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING] == []


def test_a_failed_call_backs_off_shows_in_arrival_status_json_and_its_page_is_read_again_never_failing_the_run(
    engine, run, dm, caplog
):
    caplog.set_level(logging.INFO, logger="laueportal.jobs.catalog")
    wall = Wall()
    run.file(1, DATA)
    dm.files = [row(1)]
    dm.failing = [
        subprocess.CalledProcessError(1, ["dm"], "", "Traceback\nDmException: down\n"),
        subprocess.TimeoutExpired(["dm"], 15),
        subprocess.TimeoutExpired(["dm"], 15),
    ]
    catalog = reader(engine, run, wall=wall)
    seen = []
    for at in (0, 5, 15, 35):
        seen.append((catalog.step(at), status(run)))
        wall.now += timedelta(seconds=10)
    first, second, third, recovered = seen
    assert first == (
        5,
        {
            "checked_at": "2026-10-26T10:00:00+00:00",
            "rows_seen": 0,
            "error": "DM call failed: exit status 1: DmException: down",
            "error_at": "2026-10-26T10:00:00+00:00",
        },
    )
    assert second == (
        10,
        {
            "checked_at": "2026-10-26T10:00:10+00:00",
            "rows_seen": 0,
            "error": "DM call failed: no answer within 15 s",
            "error_at": "2026-10-26T10:00:10+00:00",
        },
    )
    assert third[0] == 20 and third[1]["error_at"] == "2026-10-26T10:00:10+00:00"  # the same error: since its first
    assert recovered == (
        5,
        {"checked_at": "2026-10-26T10:00:30+00:00", "rows_seen": 1, "error": None, "error_at": None},
    )
    assert queries(dm) == [((None, 0),)] * 4 and ledger(run) == ["live_1.h5"]  # the same page, read again
    job = load_job(engine, run.job_id)
    assert (job.status, job.phase, job.n_failed) == (JobStatus.QUEUED, RunPhase.QUEUED, 0)
    messages = [record.getMessage() for record in caplog.records]
    assert len([text for text in messages if "the DM catalog:" in text]) == 2  # once per change
    assert len([text for text in messages if "the DM catalog answers again" in text]) == 1


def test_failed_calls_double_the_wait_up_to_60_s_while_an_idle_wait_stops_at_catalog_idle_seconds(engine, run, dm):
    """Each failed call doubles the wait, up to 60 s."""
    dm.failing = [subprocess.TimeoutExpired(["dm"], 15)] * 6
    catalog = reader(engine, run)
    now, waits = 0.0, []
    for _ in range(8):
        wait = catalog.step(now)
        assert wait is not None
        waits.append(wait)
        now += wait
    assert waits == [5, 10, 20, 40, 60, 60, 30, 30]  # then answers with nothing new: catalog_idle_seconds


def test_a_cycle_whose_held_files_cannot_be_tried_reports_it_with_the_last_query_s_checked_at(
    engine, run, dm, monkeypatch
):
    """``checked_at`` is the last catalog query's, written only when one ran."""
    wall = Wall()
    dm.files = [row(1)]  # file 1 is not there: held
    catalog = reader(engine, run, wall=wall)
    catalog.step(0)
    wall.now += timedelta(seconds=10)

    def broken(writer, path, **kwargs):
        raise OSError("the ledger cannot be read")

    monkeypatch.setattr(ArrivalWriter, "admit", broken)
    catalog.step(5)
    assert len(dm.runs) == 1
    assert status(run) == {
        "checked_at": "2026-10-26T10:00:00+00:00",
        "rows_seen": 1,
        "error": "admission failed: OSError: the ledger cannot be read",
        "error_at": "2026-10-26T10:00:10+00:00",
    }


def test_catalog_rows_the_run_cannot_use_are_reported_and_the_others_admitted(engine, run):
    class Reads:
        def catalog(self, experiment, reads, *, limit, cap, seconds):
            def found(path, daq="1a2b", experiment="e1", size: int | None = 4):
                return CatalogRow(path, size, 1000.0, daq, experiment)

            rows = (
                found("live-input/live_1.h5"),
                found("live-input/notes.txt"),  # the folder's, not the run's template: no error
                found("live-input/live_2.h5", daq="other"),
                found("live-input/live_3.h5", experiment="e2"),
                found("live-input/deeper/live_4.h5"),
                found("live-input/../live_5.h5"),
                found("/abs/live-input/live_6.h5"),
                found("live-input/live_7.h5", size=None),
            )
            return (CatalogRead(rows, (DMRecordError("a catalog row is not an object"),), len(rows) + 1, False),)

        def abort(self):
            pass

    run.file(1, DATA)
    run.file(7, DATA)
    catalog = reader(engine, run, client=Reads())
    assert catalog.step(0) == 5
    assert ledger(run) == ["live_1.h5"]
    assert status(run)["rows_seen"] == 3
    assert status(run)["error"] == "7 catalog row(s) not used: a catalog row is not an object"


def test_the_folder_s_other_files_never_move_the_run_s_cursor_or_show_in_its_error_however_their_rows_read(
    engine, run, dm
):
    """The template comes before the size and the time. A zero-byte file of the folder that is not the run's, with no
    usable time, is no error and does not move the cursor (``rows_seen`` counts it); a run's file of size zero is not
    admitted and is reported, once on a re-scan cycle that shows it in both of its reads."""
    run.file(1, DATA)
    run.file(2, DATA)
    lock = {**row(0), "experimentFilePath": "live-input/.writing.lock", "fileSize": 0, "fileProcessingTime": "x"}
    late = {**row(0), "experimentFilePath": "live-input/notes.txt", "fileProcessingTime": 9.0e9}
    dm.files = [row(1, 1000.0), lock, late]
    catalog = reader(engine, run)
    catalog.step(0)
    catalog.step(5)
    assert ledger(run) == ["live_1.h5"]
    assert status(run)["error"] is None and status(run)["rows_seen"] == 3
    assert queries(dm)[-1] == ((880.0, 0),)  # the run's rows' cursor, not the other file's time
    dm.files.append(row(2, 1001.0, size=0))
    catalog.step(10)
    assert ledger(run) == ["live_1.h5"]
    unusable = "1 catalog row(s) not used: catalog row live-input/live_2.h5: fileSize is not a positive whole number"
    assert status(run)["error"] == unusable
    catalog.step(300)  # a re-scan cycle: the cursor's query and the full scan both show it
    assert queries(dm)[-1] == ((881.0, 0), (None, 0)) and status(run)["error"] == unusable


def test_an_admission_that_finds_the_run_closed_ends_the_thread_with_no_call_after_it(engine, run, dm, monkeypatch):
    """``ArrivalsClosed`` ends the cycle (None) and the thread, with no further call."""
    run.file(1, DATA)
    dm.files = [row(1)]

    def closed(writer, path, **kwargs):
        raise ArrivalsClosed("the run was stopped")

    monkeypatch.setattr(ArrivalWriter, "admit", closed)
    settings = LiveSettings(**{**SETTINGS.model_dump(), "catalog_seconds": 0.01, "catalog_idle_seconds": 0.02})
    client = live_client(settings, own_session=True)
    thread = CatalogThread(reader(engine, run, settings, client=client), client, stopping=lambda: False).start()
    assert wait_for(lambda: not thread.alive, timeout=10)
    time.sleep(0.2)
    assert len(dm.runs) == 1


def test_the_thread_ends_at_its_run_s_stop_with_no_call_after_it_and_no_close(engine, run, dm):
    """The thread's own exit when ``stopping()`` turns true (the run's monitor read the stop flag or the service stop),
    before any further call; nothing closed it."""
    settings = LiveSettings(**{**SETTINGS.model_dump(), "catalog_seconds": 0.01, "catalog_idle_seconds": 0.02})
    client = live_client(settings, own_session=True)
    aborts: list[bool] = []
    client.abort = lambda: aborts.append(True)  # type: ignore[method-assign]
    thread = CatalogThread(
        reader(engine, run, settings, client=client), client, stopping=lambda: len(dm.runs) >= 2
    ).start()
    assert wait_for(lambda: not thread.alive, timeout=10)
    time.sleep(0.2)
    assert len(dm.runs) == 2 and aborts == []


def test_a_defect_in_a_cycle_is_waited_out_its_traceback_logged_once_per_change_of_its_reason_and_reported(caplog):
    """As in the follower's thread, an error that escapes a cycle waits ``idle_seconds``, never the run's, its traceback
    logged once while it repeats, again once another or a good cycle comes between; each such cycle's reason goes to
    ``arrival-status.json`` (``report_defect``)."""
    caplog.set_level(logging.ERROR, logger="laueportal.jobs.catalog")
    defect, other = RuntimeError("a defect"), ValueError("another")
    results: list[float | Exception | None] = [defect, defect, defect, other, other, 0.01, other, None]
    cycles: list[float] = []

    def step(now: float) -> float | None:
        cycles.append(now)
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    reported: list[str] = []
    reader = SimpleNamespace(step=step, idle_seconds=0.01, report_defect=reported.append)
    client = SimpleNamespace(abort=lambda: None)
    thread = CatalogThread(reader, client, stopping=lambda: False).start()  # type: ignore[arg-type]
    assert wait_for(lambda: not thread.alive, timeout=5) and len(cycles) == 8
    logged = [record.exc_info[1] for record in caplog.records if record.exc_info is not None]
    assert logged == [defect, other, other]  # the last after a good look: again
    reasons = ["RuntimeError: a defect"] * 3 + ["ValueError: another"] * 3
    assert reported == [f"the catalog's cycle failed: {reason}" for reason in reasons]


def test_arrival_status_json_is_replaced_when_its_rows_or_error_change_and_for_its_last_check_alone_once_a_minute(
    engine, run, dm
):
    """An idle catalog does not rewrite its status after every query."""
    wall = Wall()
    catalog_reader = reader(engine, run, wall=wall)
    path = run.run_directory / "arrival-status.json"
    catalog_reader.step(0)
    first, written = status(run), path.stat().st_mtime_ns
    for seconds in (30, 59):  # nothing new, less than a minute later: not rewritten
        wall.now = START + timedelta(seconds=seconds)
        catalog_reader.step(seconds)
        assert status(run) == first and path.stat().st_mtime_ns == written
    wall.now = START + timedelta(seconds=61)
    catalog_reader.step(61)
    assert status(run)["checked_at"] == "2026-10-26T10:01:01+00:00"
    run.file(1, DATA)
    dm.files = [row(1, START_SECONDS + 65)]
    wall.now = START + timedelta(seconds=66)
    catalog_reader.step(66)  # a row seen: at once
    assert (status(run)["checked_at"], status(run)["rows_seen"]) == ("2026-10-26T10:01:06+00:00", 1)


def test_a_defect_s_reason_is_written_to_arrival_status_json_and_cleared_by_the_next_good_cycle(engine, run, dm):
    """The Live page's arrival status says a cycle failed, never stays stale silently."""
    read = reader(engine, run, wall=Wall())
    read.report_defect("the catalog's cycle failed: TypeError: a defect")
    assert status(run)["error"] == "the catalog's cycle failed: TypeError: a defect" and status(run)["error_at"]
    read.step(0.0)
    assert status(run)["error"] is None


# ---------------------------------------------------------------------------
# The thread in the live run's job process (here: execute_run in this process, DM's calls real processes)


def _settings(fake: FakeDMInstall, **values) -> LiveSettings:
    return LiveSettings(
        **{
            "dm_setup_file": fake.setup_file,
            "dm_timeout_seconds": 30,
            "catalog_seconds": 0.05,
            "catalog_idle_seconds": 0.1,
            "arrivals_seconds": 0.02,
            **values,
        }
    )


def _execute(engine, job_id: int, settings: LiveSettings, monkeypatch) -> tuple[threading.Thread, dict]:
    """Claim the live run and run it with the stand-in live compute in a thread of this process; the result's
    ``summary`` (or ``error``) once it ends."""
    monkeypatch.setitem(contract._LIVE_REGISTRY, "wire_reconstruction", live_compute)
    assert claim_run(engine, job_id)
    result: dict = {}

    def target() -> None:
        try:
            result["summary"] = executor.execute_run(
                job_id, engine=engine, policy=FAST_POLICY, shutdown=threading.Event(), live_settings=settings
            )
        except BaseException as error:
            result["error"] = error

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result


def _catalog_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "live-catalog"]


def test_at_a_stop_the_call_in_flight_is_killed_one_last_read_admits_what_was_catalogued_and_no_call_follows_the_seal(
    engine, run, tmp_path, monkeypatch
):
    """A Stop takes one last catalog read before the seal; the call in flight is ended at once, the last read's files
    are admitted though the stop flag is set, and no DM call follows the seal."""
    fake = FakeDMInstall(tmp_path / "dm")
    run.file(1, DATA)
    fake.set_state(files=[row(1)])
    sealed: dict = {}
    real_seal = executor.seal

    def recording_seal(directory) -> None:
        sealed["at"] = time.time()
        sealed["calls running"] = [pid for pid, _ in fake.calls() if alive(pid)]
        real_seal(directory)

    monkeypatch.setattr(executor, "seal", recording_seal)
    thread, result = _execute(engine, run.job_id, _settings(fake), monkeypatch)
    try:
        assert wait_for(lambda: len(run.processed()) == 1)
        fake.set_state(files=[row(1)], delay=60)  # the next page holds its call in flight
        started = len(fake.calls())
        assert wait_for(lambda: len(fake.calls()) > started)
        in_flight = fake.calls()[-1][0]
        run.file(2, DATA)  # catalogued while the call in flight waits: only a read after the Stop finds it
        stopped_at, stopped_wall = time.monotonic(), time.time()
        controls.cancel_run(run.job_id, engine=engine)
        fake.set_state(files=[row(1), row(2, 1001.0)])  # DM answers the last read at once
        thread.join(30)
    finally:
        fake.set_state(files=[row(1)])
    assert not thread.is_alive() and "error" not in result, result
    assert time.monotonic() - stopped_at < 20  # the call in flight was killed, not waited for
    assert sealed["calls running"] == [] and not alive(in_flight)
    assert all(at < sealed["at"] for _, at in fake.calls())
    assert [at for _, at in fake.calls() if at > stopped_wall] != []  # the last read, after the Stop
    assert _catalog_threads() == []
    job = load_job(engine, run.job_id)
    assert (job.status, job.n_inputs, job.n_succeeded) == (JobStatus.FINISHED, 2, 2), job.messages
    assert ledger(run) == ["live_1.h5", "live_2.h5"]


def test_a_last_read_that_dm_fails_is_in_arrival_status_json_and_the_seal_proceeds(engine, run, tmp_path, monkeypatch):
    """DM failing at the last read: the run is sealed with what it had, the failure in arrival-status.json."""
    fake = FakeDMInstall(tmp_path / "dm")
    run.file(1, DATA)
    fake.set_state(files=[row(1)])
    thread, result = _execute(engine, run.job_id, _settings(fake), monkeypatch)
    try:
        assert wait_for(lambda: len(run.processed()) == 1)
        fake.state.write_text("not JSON")  # every call fails in DM's script from now on
        calls = len(fake.calls())
        controls.cancel_run(run.job_id, engine=engine)
        thread.join(30)
    finally:
        fake.set_state()
    assert not thread.is_alive() and "error" not in result, result
    assert len(fake.calls()) > calls  # the last read was made
    assert status(run)["error"].startswith("DM call failed: ")
    job = load_job(engine, run.job_id)
    assert (job.status, job.n_inputs, job.n_succeeded) == (JobStatus.FINISHED, 1, 1), job.messages


@pytest.mark.parametrize("source", ["notify", "run"])
def test_no_catalog_thread_or_call_for_a_notify_run_or_a_follower(engine, tmp_path, source, caplog):
    """The live arrival thread runs only while its source is a DAQ (nothing for a follower). ``start_catalog``'s source
    check is the one check: without it the reader is built for a run without a DAQ and fails, which ``start_catalog``
    logs as an error; so the test asks that nothing at all was logged, and does not rest on ``LiveDocument``'s validator
    (which ties the DAQ to the source)."""
    caplog.set_level(logging.DEBUG, logger="laueportal.jobs.catalog")
    fake = FakeDMInstall(tmp_path / "dm")
    wire = create_live_wire_run(engine, tmp_path, source="notify")
    job_id = wire.job_id if source == "notify" else create_live_follower(engine, tmp_path, wire)
    assert claim_run(engine, job_id)
    request, _ = executor._start(engine, job_id, RunPolicy())
    assert request is not None and request.live is not None and request.live["source"] == source
    assert start_catalog(engine, request, _settings(fake), stopping=lambda: False) is None
    time.sleep(0.3)
    assert fake.calls() == [] and _catalog_threads() == []
    assert not os.path.exists(catalog.status_path(request.run_directory))
    assert [record.getMessage() for record in caplog.records if record.name == "laueportal.jobs.catalog"] == []


def test_without_dm_settings_a_daq_run_fails_at_its_setup_and_arrival_status_json_says_why(engine, run, monkeypatch):
    """A DAQ run whose catalog cannot start would have no source: it fails at once, never left Running; so does one
    whose start fails otherwise (logged)."""
    assert claim_run(engine, run.job_id)
    request, _ = executor._start(engine, run.job_id, RunPolicy())
    assert request is not None
    with pytest.raises(catalog.CatalogUnavailable, match=r"no DM catalog: DM is not configured"):
        start_catalog(engine, request, LiveSettings(), stopping=lambda: False)
    monkeypatch.setattr(catalog, "CatalogReader", None)  # a defect at the start
    with pytest.raises(catalog.CatalogUnavailable, match="the DM catalog could not start: TypeError: "):
        start_catalog(
            engine, request, _settings(FakeDMInstall(run.run_directory.parent / "dm")), stopping=lambda: False
        )
    found = status(run)
    assert (found["checked_at"], found["rows_seen"], found["error"]) == (
        None,
        0,
        "DM is not configured for live runs ([live] dm_setup_file)",
    )


def test_a_catalog_that_keeps_failing_shows_its_error_and_never_fails_the_run(engine, run, tmp_path, monkeypatch):
    fake = FakeDMInstall(tmp_path / "dm")
    fake.state.write_text("not JSON")  # every call fails in DM's script
    thread, result = _execute(engine, run.job_id, _settings(fake), monkeypatch)
    status_file = run.run_directory / "arrival-status.json"
    try:
        assert wait_for(lambda: status_file.exists() and len(fake.calls()) >= 3)
        controls.cancel_run(run.job_id, engine=engine)
        thread.join(30)
    finally:
        fake.set_state()
    assert not thread.is_alive() and "error" not in result, result
    assert status(run)["error"].startswith("DM call failed: exit status 1: json.decoder.JSONDecodeError: ")
    job = load_job(engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.CANCELLED, RunPhase.CANCELLED)
    assert job.messages.endswith("Stopped before any input arrived")


# ---------------------------------------------------------------------------
# Through a real worker


@pytest.mark.slow
def test_a_live_run_whose_source_is_a_daq_admits_the_catalog_s_files_through_a_real_worker_and_ends_its_calls(
    tmp_path, live: Live
):
    fake = FakeDMInstall(tmp_path / "dm")
    run = create_live_wire_run(live.engine, tmp_path, source="daq")
    for number in (1, 2):
        run.file(number, DATA)
    fake.set_state(files=[row(1, 1000.0), row(2, 1001.0)])
    config = live.write_config(
        tmp_path / "catalog.toml",
        live={
            "dm_setup_file": str(fake.setup_file),
            "catalog_seconds": 0.2,
            "catalog_idle_seconds": 0.5,
            "arrivals_seconds": 0.1,
        },
    )
    worker = start_worker(config, log=tmp_path / "worker.log", burst=True, test_computes=True)
    try:
        assert wait_for(lambda: len(run.processed()) == 2, timeout=60), worker.log_text()
        run.file(3, b"late")
        fake.set_state(files=[row(1, 1000.0), row(2, 1001.0), row(3, 1002.0, size=4)])
        assert wait_for(lambda: len(run.processed()) == 3, timeout=60), worker.log_text()
        controls.cancel_run(run.job_id, engine=live.engine)
        assert worker.wait(60) == 0, worker.log_text()
    finally:
        worker.cleanup()
    calls = fake.calls()
    time.sleep(1.0)
    assert fake.calls() == calls and not any(alive(pid) for pid, _ in calls)  # none after the run, none left
    job = load_job(live.engine, run.job_id)
    assert (job.status, job.n_inputs, job.n_succeeded) == (JobStatus.FINISHED, 3, 3), job.messages
    assert ledger(run) == ["live_1.h5", "live_2.h5", "live_3.h5"]
    assert [json.loads(line)["source"] for line in (run.run_directory / "arrivals.jsonl").read_text().splitlines()] == [
        "catalog"
    ] * 3
    found = status(run)
    assert (found["rows_seen"], found["error"], found["error_at"]) == (3, None, None)
