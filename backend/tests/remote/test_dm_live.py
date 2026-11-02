"""Live runs' DM operations: the DAQ list, a DAQ's info and the catalog's cycle, each one call of the real DM client
running DM's real API script on a stand-in API (``tests.remote_support.FakeDMService``: every process counted, no DM),
with the ``[live]`` settings' client (``jobs.daq.live_client``)."""

from __future__ import annotations

import subprocess

import pytest

from laueportal.core.settings import LiveSettings
from laueportal.jobs.daq import DaqUnavailable, live_client
from laueportal.remote.dm import (
    CatalogRead,
    CatalogRow,
    DaqNotFound,
    DaqRecord,
    DMClient,
    DMRecordError,
    catalog_query,
    parse_catalog,
    parse_catalog_row,
    parse_daqs,
)
from laueportal.remote.settings import DMSettings
from tests.remote_support import FakeDMService

DAQ = {
    "id": "1a2b",
    "experimentName": "e1",
    "status": "running",
    "dataDirectory": "/detector/e1",
    "storageDirectory": "/storage/e1",
    "destDirectory": "34ide/data",
    "dmPassword": "never shown",
}


@pytest.fixture
def dm(tmp_path, monkeypatch) -> FakeDMService:
    return FakeDMService(tmp_path / "dm", monkeypatch)


@pytest.fixture
def settings(tmp_path) -> LiveSettings:
    return LiveSettings(dm_setup_file=tmp_path / "dm-setup.sh", dm_timeout_seconds=7)


def row(path: str, size: int = 10, at: float = 1000.0, daq: str = "1a2b", experiment: str = "e1") -> dict:
    return {
        "experimentFilePath": path,
        "fileSize": size,
        "fileProcessingTime": at,
        "daqId": daq,
        "experimentName": experiment,
        "storageFilePath": "/storage/e1/" + path,
    }


def test_the_live_client_is_one_process_per_call_with_the_live_settings_and_none_without_a_setup_file(
    dm, settings, tmp_path
):
    dm.daqs = {"1a2b": DAQ}
    live_client(settings).daqs()
    assert len(dm.runs) == 1
    (call,) = dm.runs
    assert call["timeout"] == 7 and call["request"] == {"operation": "daqs", "args": {}}
    assert call["argv"][2].startswith(f"source {tmp_path / 'dm-setup.sh'} >/dev/null; exec python -c ")
    with pytest.raises(DaqUnavailable, match=r"\[live\] dm_setup_file"):
        live_client(LiveSettings())
    assert len(dm.runs) == 1


def test_the_daq_list_is_one_call_and_a_record_that_cannot_be_read_is_reported_alone(dm, settings):
    dm.daqs = {"1a2b": DAQ, "broken": "not an object", "3c4d": {"id": "3c4d"}, "7": {**DAQ, "id": 7}}
    daqs = live_client(settings).daqs()
    assert dm.operations() == ["daqs"]
    assert daqs[0] == DaqRecord("1a2b", "e1", "running", "/detector/e1", "/storage/e1", "34ide/data")
    assert [str(error) for error in daqs[1:3]] == [
        "DM's DAQ record is not an object",
        "DM's DAQ 3c4d has no experimentName",
    ]
    assert isinstance(daqs[1], DMRecordError) and daqs[3] == DaqRecord(  # DM's ID as text
        "7", "e1", "running", "/detector/e1", "/storage/e1", "34ide/data"
    )
    assert "dmPassword" not in repr(daqs)  # only a summary's keys


def test_a_daq_s_info_is_one_call_and_a_daq_dm_does_not_know_is_no_record(dm, settings):
    """DM's ``ObjectNotFound`` is an answer (no such DAQ), not a failed call."""
    dm.daqs = {"1a2b": DAQ}
    client = live_client(settings)
    assert client.daq("1a2b") == DaqRecord("1a2b", "e1", "running", "/detector/e1", "/storage/e1", "34ide/data")
    with pytest.raises(DaqNotFound, match="DM has no DAQ 9z"):
        client.daq("9z")
    assert [run["request"] for run in dm.runs] == [
        {"operation": "daq", "args": {"id": "1a2b"}},
        {"operation": "daq", "args": {"id": "9z"}},
    ]


def test_a_catalog_call_reads_every_page_of_its_queries_in_one_process_one_request_at_a_time(dm, settings):
    """One subprocess per cycle; inside it, each query's pages in order, from its skip, each request at most ``limit``
    rows, until a shorter page ends the query."""
    dm.files = [
        row("scan+1/a_1.h5", at=900.0),
        row("scan+1/a_2.h5", at=1000.0),
        row("scan+1/nested/a_3.h5", at=1100.0),  # not directly in the folder
        row("scann1/a_4.h5", at=1100.0),  # the folder's name read as a pattern would match it
        row("scan+1/a_5.h5", at=1100.0, daq="other"),
        row("scan+1/a_6.h5", at=1100.0, experiment="e2"),
        row("scan+1/a_7.h5", size=0, at=1200.0),  # read; its size is the catalog thread's to check (a run's file)
        row("scan+1/a_8.h5", at=1300.0),
    ]
    since = catalog_query("1a2b", "scan+1", 1000.0)
    everything = catalog_query("1a2b", "scan+1", None)
    recent, full = live_client(settings).catalog("e1", [(since, 0), (everything, 1)], limit=2, cap=10, seconds=7)

    assert dm.operations() == ["catalog"]
    assert dm.runs[0]["request"]["args"] == {
        "experiment": "e1",
        "reads": [
            {
                "query": {
                    "daqId": "1a2b",
                    "experimentFilePath": {"$regex": r"^scan\+1/[^/]+$"},
                    "fileProcessingTime": {"$gte": 1000.0},
                },
                "skip": 0,
            },
            {"query": everything, "skip": 1},
        ],
        "limit": 2,
        "cap": 10,
        "seconds": 7,
    }
    requests = dm.requests()
    assert [(request["query"] == since, request["skip"], request["limit"]) for request in requests] == [
        (True, 0, 2),
        (True, 2, 2),  # a shorter page: the query is read
        (False, 1, 2),
        (False, 3, 2),  # the same
    ]
    assert len({request["pid"] for request in requests}) == 1  # one process
    assert recent == CatalogRead(
        (
            CatalogRow("scan+1/a_2.h5", 10, 1000.0, "1a2b", "e1"),
            CatalogRow("scan+1/a_7.h5", None, 1200.0, "1a2b", "e1"),
            CatalogRow("scan+1/a_8.h5", 10, 1300.0, "1a2b", "e1"),
        ),
        (),
        3,
        False,
    )
    assert ([found.path for found in full.rows], full.count, full.more) == (
        ["scan+1/a_2.h5", "scan+1/a_7.h5", "scan+1/a_8.h5"],
        3,
        False,
    )
    assert catalog_query("1a2b", ".", None)["experimentFilePath"] == {"$regex": "^[^/]+$"}  # the storage itself


@pytest.mark.parametrize(
    ("cap", "seconds", "expected"),
    [
        (3, 7, [(0, 2), (2, 1)]),  # the cap: 3 rows in all, the last request asks for what is left
        (10, 0, [(0, 2)]),  # the time: no request after the first
    ],
)
def test_a_catalog_call_stops_at_its_row_cap_or_its_time_and_says_more_may_follow(dm, settings, cap, seconds, expected):
    dm.files = [row(f"scan/a_{number}.h5") for number in range(5)]
    query = catalog_query("1a2b", "scan", None)
    first, second = live_client(settings).catalog("e1", [(query, 0), (query, 0)], limit=2, cap=cap, seconds=seconds)
    assert [(request["skip"], request["limit"]) for request in dm.requests()] == expected
    assert (first.count, first.more) == (sum(limit for _, limit in expected), True)
    assert (second.count, second.more) == (0, True)  # never begun


@pytest.mark.parametrize(
    ("start_delay", "delay"),
    [
        (0.0, 1.0),  # a slow first request: another as slow would end past the call's 1.5 s
        (1.0, 0.3),  # a slow start (DM's imports, its login): the time counts from the script's start
    ],
)
def test_a_catalog_call_starts_no_request_its_longest_so_far_says_would_end_past_its_seconds_from_the_script_s_start(
    dm, settings, start_delay, delay
):
    """After its first request, the call starts none that the longest request so far says would end past ``seconds``
    from the DM script's own start; the read stopped says ``more``."""
    dm.files = [row(f"scan/a_{number}.h5") for number in range(5)]
    dm.start_delay, dm.delay = start_delay, delay
    query = catalog_query("1a2b", "scan", None)
    (read,) = live_client(settings).catalog("e1", [(query, 0)], limit=2, cap=10, seconds=1.5)
    assert [(request["skip"], request["limit"]) for request in dm.requests()] == [(0, 2)]
    assert (read.count, read.more) == (2, True)


def test_a_catalog_row_s_size_and_time_are_read_as_none_when_they_cannot_be_used_and_one_without_a_path_fails():
    """Only the path is required here; the catalog thread checks a run's file's size and time after its template."""
    assert parse_catalog_row({"experimentFilePath": "s/a.h5", "fileSize": 0, "fileProcessingTime": -1}) == CatalogRow(
        "s/a.h5", None, None, None, None
    )
    assert parse_catalog_row({"experimentFilePath": "s/a.h5", "fileSize": True, "fileProcessingTime": "1"}).size is None
    with pytest.raises(DMRecordError, match="a catalog row has no experimentFilePath"):
        parse_catalog_row({"fileSize": 1})
    (read,) = parse_catalog({"reads": [{"files": [{"experimentFilePath": "s/a.h5"}, "no row"], "more": False}]}, 1, 2)
    assert (read.count, [str(error) for error in read.failed]) == (2, ["a catalog row is not an object"])


@pytest.mark.parametrize(
    ("parse", "answer", "reason"),
    [
        (lambda answer: parse_catalog(answer, 1, 2), {"files": []}, "catalog call is malformed: reads"),
        (lambda answer: parse_catalog(answer, 2, 2), {"reads": [{"files": [], "more": False}]}, "malformed: reads"),
        (lambda answer: parse_catalog(answer, 1, 2), {"reads": [{"rows": [], "more": False}]}, "malformed: files"),
        (lambda answer: parse_catalog(answer, 1, 2), {"reads": [{"files": []}]}, "malformed: more"),
        (lambda answer: parse_catalog(answer, 1, 2), {"reads": [{"files": [{}, {}, {}], "more": True}]}, "3 rows"),
        (parse_daqs, {"daqs": []}, "DAQ list is malformed: not a list"),
    ],
)
def test_an_answer_without_the_operation_s_shape_fails_the_call(parse, answer, reason):
    with pytest.raises(ValueError, match=reason):
        parse(answer)


def test_a_failed_catalog_call_raises_as_every_dm_call_does(dm, settings):
    dm.failing = [subprocess.TimeoutExpired(["dm"], 7)]
    with pytest.raises(subprocess.TimeoutExpired):
        live_client(settings).catalog("e1", [(catalog_query("1a2b", "scan", None), 0)], limit=2, cap=4, seconds=7)
    assert dm.operations() == ["catalog"]


def test_the_live_operations_build_only_their_own_dm_api_and_need_no_workflow_service(tmp_path, monkeypatch, settings):
    """A DM setup for the DAQ and catalog services alone serves the live runs; only the remote runs' operations build
    the workflow API."""
    dm = FakeDMService(tmp_path / "live-only", monkeypatch, workflow=False)
    dm.daqs = {"1a2b": DAQ}
    dm.files = [row("scan/a_1.h5")]
    client = live_client(settings)
    assert [record.id for record in client.daqs() if isinstance(record, DaqRecord)] == ["1a2b"]
    assert client.daq("1a2b").experiment == "e1"
    (read,) = client.catalog("e1", [(catalog_query("1a2b", "scan", None), 0)], limit=2, cap=4, seconds=7)
    assert [found.path for found in read.rows] == ["scan/a_1.h5"]
    remote = DMClient(DMSettings(setup_file=str(settings.dm_setup_file), owner="laue"))
    with pytest.raises(subprocess.CalledProcessError) as failed:
        remote.get("dm-001")  # the workflow API: not installed here
    assert "No module named 'dm.proc_web_service'" in failed.value.stderr
