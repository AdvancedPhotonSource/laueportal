"""The DAQ side of a live run (``jobs.daq``): the Live page's DAQ list, one answer shared by every caller, at most one
DM call per ``live.daq_list_seconds`` and none unless asked; and the bind check at a live run's creation. DM is the real
client and script on a stand-in API (``FakeDMService``: each process counted); a stand-in client holds a call in flight
where a test needs one."""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import anyio
import pytest

from laueportal.core.settings import LiveSettings
from laueportal.jobs.daq import DaqList, DaqRefused, DaqUnavailable, bind_daq
from laueportal.jobs.live import LiveDaq
from laueportal.remote.dm import DaqRecord
from tests.remote_support import FakeDMService
from tests.worker_support import wait_for

DAQ = {
    "id": "1a2b",
    "experimentName": "e1",
    "status": "Running",
    "dataDirectory": "/detector/e1",
    "storageDirectory": "/storage/e1",
    "destDirectory": "34ide/data",
}
RECORD = DaqRecord("1a2b", "e1", "Running", "/detector/e1", "/storage/e1", "34ide/data")


@pytest.fixture
def dm(tmp_path, monkeypatch) -> FakeDMService:
    fake = FakeDMService(tmp_path / "dm", monkeypatch)
    fake.daqs = {"1a2b": DAQ}
    return fake


@pytest.fixture
def settings(tmp_path) -> LiveSettings:
    return LiveSettings(dm_setup_file=tmp_path / "dm-setup.sh", daq_list_seconds=30)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# The DAQ list


def test_the_daq_list_is_one_answer_shared_by_every_caller_with_at_most_one_call_per_daq_list_seconds(dm, settings):
    clock = Clock()
    daqs = DaqList(settings, clock=clock)
    first = daqs.get()
    clock.now += 29.9
    again = daqs.get()
    clock.now += 0.1
    later = daqs.get()
    assert dm.operations() == ["daqs", "daqs"]
    assert again is first and later is not first
    assert (first.daqs, first.failed, first.error) == ((RECORD,), (), None)


def test_no_daq_list_call_is_made_unless_one_is_asked_for(dm, settings):
    """Only while a Live page form is open (it asks): nothing refreshes the list in the background."""
    clock = Clock()
    daqs = DaqList(settings, clock=clock)
    clock.now += 600
    assert dm.runs == []
    daqs.get()
    clock.now += 600  # long past daq_list_seconds, nobody asking
    assert dm.operations() == ["daqs"]


def test_callers_that_come_while_the_call_runs_wait_for_it_and_share_its_answer(settings):
    class Held:
        calls = 0
        release = threading.Event()
        started = threading.Event()

        def daqs(self):
            Held.calls += 1
            Held.started.set()
            assert Held.release.wait(10)
            return [RECORD]

    daqs = DaqList(settings, client=Held())  # type: ignore[arg-type]
    answers = []
    first = threading.Thread(target=lambda: answers.append(daqs.get()))
    first.start()
    assert Held.started.wait(10)
    waiting = [threading.Thread(target=lambda: answers.append(daqs.get())) for _ in range(3)]
    for thread in waiting:
        thread.start()

    async def asynchronous():
        answers.append(await daqs.get_async())

    def in_a_loop():
        anyio.run(asynchronous)

    looping = threading.Thread(target=in_a_loop)
    looping.start()
    call = daqs._calls["daqs"]  # the call in flight: the coroutine waits on it in its loop, holding no thread
    assert wait_for(lambda: len(call._wakers) == 1)
    Held.release.set()
    for thread in (first, *waiting, looping):
        thread.join(10)
    assert Held.calls == 1 and len(answers) == 5 and all(answer is answers[0] for answer in answers)


def test_a_failed_daq_list_call_is_the_shared_answer_and_dm_is_not_asked_again_before_daq_list_seconds(dm, settings):
    clock = Clock()
    daqs = DaqList(settings, clock=clock)
    dm.failing = [subprocess.CalledProcessError(1, ["dm"], "", "Traceback\nDmException: DM is down\n")]
    failed = daqs.get()
    clock.now += 10
    assert daqs.get() is failed
    clock.now += 20
    recovered = daqs.get()
    assert dm.operations() == ["daqs", "daqs"]
    assert (failed.daqs, failed.error) == ((), "DM call failed: exit status 1: DmException: DM is down")
    assert (recovered.daqs, recovered.error) == ((RECORD,), None)


def test_while_the_daq_list_s_calls_fail_each_failure_doubles_how_long_its_answer_stands_up_to_60_s(dm, settings):
    """Each failed call doubles the wait, up to 60 s: 30, 60, 60 s; an answer resets it to ``daq_list_seconds``."""
    clock = Clock()
    daqs = DaqList(settings, clock=clock)
    down = subprocess.CalledProcessError(1, ["dm"], "", "DmException: DM is down\n")
    dm.failing = [down, down, down]
    stood = []
    for _ in range(4):
        answer = daqs.get()
        asked = clock.now
        while daqs.get() is answer:
            clock.now += 1
        stood.append(clock.now - asked)
    assert stood == [30, 60, 60, 30]
    assert dm.operations() == ["daqs"] * 5


def test_the_daq_list_without_dm_settings_makes_no_call_and_says_why(dm):
    answer = DaqList(LiveSettings()).get()
    assert (answer.daqs, answer.error, dm.runs) == ((), "DM is not configured for live runs ([live] dm_setup_file)", [])


# ---------------------------------------------------------------------------
# The bind check


def _bind(dm, settings, folder: Path, daq: dict | None = None, daq_id: str = "1a2b") -> LiveDaq:
    """The bind check with DM's storage directory at ``folder``'s ``storage`` ancestor on this host."""
    if daq is not None:
        dm.daqs = {daq_id: daq}
    return bind_daq(settings, daq_id, os.fspath(folder))


@pytest.fixture
def storage(tmp_path) -> Path:
    root = tmp_path / "gdata" / "e1"
    (root / "34ide" / "data" / "scan1").mkdir(parents=True)
    return root


def test_the_bind_check_is_one_call_and_gives_live_json_s_daq_for_a_running_daq_and_a_folder_in_its_destination(
    dm, settings, storage
):
    daq = {**DAQ, "storageDirectory": os.fspath(storage)}
    bound = _bind(dm, settings, storage / "34ide" / "data" / "scan1", daq)
    assert bound == LiveDaq(
        id="1a2b", experiment="e1", storage=os.fspath(storage), destination=os.fspath(storage / "34ide" / "data")
    )
    assert dm.operations() == ["daq"]


@pytest.mark.parametrize(
    ("change", "folder", "reason"),
    [
        ({"status": "Stopped"}, "34ide/data/scan1", "DAQ 1a2b is not running (Stopped): select a running DAQ"),
        ({"status": None}, "34ide/data/scan1", "DAQ 1a2b is not running (no status)"),
        ({}, "34ide/other", "The folder must lie inside DAQ 1a2b's destination "),
        ({}, "34ide", "The folder must lie inside DAQ 1a2b's destination "),
        ({"destDirectory": "../elsewhere"}, "34ide/data/scan1", "DAQ 1a2b has an invalid destination directory"),
        ({"destDirectory": "/abs"}, "34ide/data/scan1", "DAQ 1a2b has an invalid destination directory"),
        ({"storageDirectory": "relative/e1"}, "34ide/data/scan1", "DAQ 1a2b has no absolute storage directory"),
        ({"destDirectory": "outside"}, "34ide/data/scan1", "DAQ 1a2b's destination "),  # a link out of the storage
    ],
)
def test_the_bind_check_refuses_a_daq_not_running_or_a_folder_outside_its_destination(
    dm, settings, storage, change, folder, reason
):
    (storage / "outside").symlink_to(storage.parent)
    daq = {**DAQ, "storageDirectory": os.fspath(storage), **change}
    with pytest.raises(DaqRefused) as refused:
        _bind(dm, settings, storage / folder, daq)
    assert str(refused.value).startswith(reason)
    assert dm.operations() == ["daq"]


def test_the_bind_check_says_dm_is_unavailable_when_it_cannot_ask_or_dm_does_not_answer(dm, settings, storage):
    folder = os.fspath(storage / "34ide" / "data" / "scan1")
    dm.failing = [subprocess.TimeoutExpired(["dm"], 15)]
    with pytest.raises(DaqUnavailable, match=r"^DM call failed: no answer within 15 s$"):
        bind_daq(settings, "1a2b", folder)
    with pytest.raises(DaqUnavailable, match=r"\[live\] dm_setup_file"):
        bind_daq(LiveSettings(), "1a2b", folder)
    dm.daqs = {"1a2b": "not an object"}
    with pytest.raises(DaqRefused, match="DM has no readable record of DAQ '1a2b'"):
        bind_daq(settings, "1a2b", folder)
    assert dm.operations() == ["daq", "daq"]


def test_the_bind_check_refuses_a_daq_dm_does_not_know(dm, settings, storage):
    """A stale DAQ ID is a refusal, not "DM unavailable"."""
    with pytest.raises(DaqRefused, match=r"^DM has no DAQ '9z': select a running DAQ$"):
        bind_daq(settings, "9z", os.fspath(storage / "34ide" / "data" / "scan1"))
    assert dm.operations() == ["daq"]
