"""Run details for the job page (``jobs.summary``): typed raw values, never display rows."""

import json
import os

import pytest
from sqlalchemy.orm import Session

from laueportal import models
from laueportal.jobs import summary
from laueportal.jobs.rundir import RUN_SUMMARY_FILENAME, FailureRecord
from laueportal.jobs.state import JobStatus
from tests.settings_support import make_database


@pytest.fixture
def engine(tmp_path):
    _, engine = make_database(tmp_path)
    yield engine
    engine.dispose()


def _job(session, job_id, **values):
    job = models.Job(job_id=job_id, computer_name="host", status=JobStatus.FINISHED, priority=0, **values)
    session.add(job)
    session.flush()
    return job


def _write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


REQUEST = {
    "request": {
        "geometry_file": "/data/geo.xml",
        "threshold": None,
        "smooth": False,
        "filename_prefixes": ["frame_%d.h5"],
        "notes": "",
        "submitted_at": "2026-10-15T12:00:00",
        "computer_name": "host",
        "priority": 0,
    },
    "output": {"directory": "/runs/index_1", "xml": None},
    "manifest": {"path": "inputs.jsonl", "n_inputs": 3, "sha256": "abc"},
}
RUN_SUMMARY = {
    "kind": "lauego_indexing",
    "display_id": "Indexing I1",
    "provenance": {"lauelab_version": "0.9", "threads": 2},
    "policy": {"workers": 1, "max_in_flight": None},
    "host": "worker-1",
    "started_at": "2026-10-15T12:00:01",
    "finished_at": "2026-10-15T12:00:09",
    "counters": {"n_indexed": 3},
    "artifacts": {"results": "/runs/index_1/output.h5"},
    "log": [f"line {index}" for index in range(60)],
}


def test_request_and_execution_keep_raw_values(engine, tmp_path):
    run_dir = tmp_path / "runs" / "index_1"
    _write(run_dir / "request.json", REQUEST)
    _write(run_dir / RUN_SUMMARY_FILENAME, RUN_SUMMARY)
    with Session(engine) as session:
        _job(session, 1, run_directory=str(run_dir), request_path=str(run_dir / "request.json"), n_inputs=3)
        details = summary.load_run_details(session, 1)

    assert details is not None
    assert (details.kind, details.display_id, details.problems) == ("lauego_indexing", "Indexing I1", ())
    assert details.artifacts == {"results": "/runs/index_1/output.h5"}
    assert (details.log_tail[0], len(details.log_tail), details.log_truncated) == ("line 10", 50, 10)

    request = summary.submitted_request(details)
    assert request == summary.SubmittedRequest(
        # None, False, "" and lists stay as they are: no "—", "yes"/"no" or joined strings.
        parameters={
            "geometry_file": "/data/geo.xml",
            "threshold": None,
            "smooth": False,
            "filename_prefixes": ["frame_%d.h5"],
            "notes": "",
        },
        output={"directory": "/runs/index_1", "xml": None},
        manifest=summary.ManifestInfo(path="inputs.jsonl", n_inputs=3, sha256="abc"),
    )
    assert list(request.parameters) == ["geometry_file", "threshold", "smooth", "filename_prefixes", "notes"]

    assert summary.execution_info(details) == summary.ExecutionInfo(
        provenance={"lauelab_version": "0.9", "threads": 2},
        policy={"workers": 1, "max_in_flight": None},
        host="worker-1",
        started_at="2026-10-15T12:00:01",
        finished_at="2026-10-15T12:00:09",
        n_indexed=3,
    )
    assert summary.history_info(details) is None


def test_historical_run_reports_its_migration_export(engine, tmp_path):
    export = tmp_path / "export" / "job_7"
    _write(
        export / summary.HISTORY_FILENAME,
        {
            "original_status_name": "Finished",
            "subjob_time_range": ["2026-01-02T03:04:05", None],
            "message_classes": {f"message {index}": index for index in range(12)} | {"tie": 11},
        },
    )
    (export / "failures.jsonl").write_text("")
    with Session(engine) as session:
        _job(session, 7, failure_report_path=str(export / "failures.jsonl"))
        details = summary.load_run_details(session, 7)

    assert details is not None and details.is_historical
    assert summary.submitted_request(details) is None
    assert summary.execution_info(details) is None
    history = summary.history_info(details)
    assert history is not None
    assert (history.original_status_name, history.first_input_at, history.last_input_at) == (
        "Finished",
        "2026-01-02T03:04:05",
        None,
    )
    assert len(history.message_classes) == summary.MAX_MESSAGE_CLASSES
    assert [(item.message, item.count) for item in history.message_classes[:3]] == [
        ("message 11", 11),
        ("tie", 11),  # equal counts: by message
        ("message 10", 10),
    ]


def test_missing_and_malformed_documents(engine, tmp_path):
    run_dir = tmp_path / "runs" / "rec_2"
    run_dir.mkdir(parents=True)
    (run_dir / RUN_SUMMARY_FILENAME).write_text("[1, 2]")
    with Session(engine) as session:
        _job(session, 2, run_directory=str(run_dir), request_path=str(run_dir / "missing-request.json"))
        details = summary.load_run_details(session, 2)
        assert summary.load_run_details(session, 99) is None

    assert details is not None
    assert details.request is None and details.summary is None and not details.is_historical
    assert details.problems == (f"run.json at {run_dir / RUN_SUMMARY_FILENAME} is not a JSON object",)
    assert (summary.submitted_request(details), summary.execution_info(details)) == (None, None)
    assert (details.log_tail, details.log_truncated, details.artifacts) == ([], 0, {})


def test_display_rows_are_gone():
    for name in ("request_rows", "execution_rows", "history_rows", "_format_value"):
        assert not hasattr(summary, name)


SUBMISSION = "6f1c2a4e-3b5d-4e7f-8a9b-0c1d2e3f4a5b"
EAGLE = f"/eagle/workspace/runs/{SUBMISSION}"


def _failed_run(engine, tmp_path, job_id, sources, *, plan=None):
    """A run under an APS root whose ``failures.jsonl`` names ``sources``; ``plan``: its ``bdp.json`` content."""
    aps = tmp_path / "aps"
    run_dir = aps / "LauePortal" / "analysis" / f"recon_{job_id}"
    run_dir.mkdir(parents=True)
    if plan is not None:
        (run_dir / "bdp.json").write_text(plan(run_dir, aps) if callable(plan) else plan, encoding="utf-8")
    report = run_dir / "failures.jsonl"
    report.write_text(
        "".join(
            FailureRecord(index=i, input_id=f"in-{i}", source=s, category="input", message="bad").to_json() + "\n"
            for i, s in enumerate(sources)
        )
    )
    with Session(engine) as session, session.begin():
        _job(session, job_id, run_directory=str(run_dir), failure_report_path=str(report), n_inputs=len(sources))
    with Session(engine) as session:
        details = summary.load_run_details(session, job_id)
    assert details is not None
    return details, run_dir, aps


def _crux_plan(run_dir, aps):
    return json.dumps({"version": 1, "path_map": {str(run_dir): f"{EAGLE}/results", str(aps): f"{EAGLE}/inputs"}})


def test_a_remote_runs_failure_sources_are_mapped_to_the_portals_view(engine, tmp_path):
    """crux: the runner wrote Eagle paths into ``failures.jsonl``; the plan's map gives them back, and a value the
    rule refuses (another host's path, one leaving the APS root) is unavailable (``None``)."""
    sources = [
        f"{EAGLE}/inputs/LauePortal/data/wire_1.h5",
        "/home/user/elsewhere.h5",
        f"{EAGLE}/inputs/../../../../etc/passwd",
        None,
    ]
    details, _run_dir, aps = _failed_run(engine, tmp_path, 3, sources, plan=_crux_plan)
    records, has_more = summary.failure_records(details, offset=0)
    assert not has_more
    assert [record.source for record in records] == [f"{aps}/LauePortal/data/wire_1.h5", None, None, None]
    assert [record.input_id for record in records] == ["in-0", "in-1", "in-2", "in-3"]


def test_a_local_runs_failure_sources_are_kept_as_written(engine, tmp_path):
    sources = ["/data/wire_1.h5", "relative/name.h5"]
    details, _run_dir, _aps = _failed_run(engine, tmp_path, 4, sources)
    assert [record.source for record in summary.failure_records(details, offset=0)[0]] == sources


@pytest.mark.parametrize("plan", ["{not json", json.dumps({"version": 1}), json.dumps({"path_map": {"/a": 1}})])
def test_a_remote_run_with_an_unusable_plan_has_no_failure_source(engine, tmp_path, plan):
    """The rule fails closed: a plan that exists but cannot be used maps nothing."""
    details, _run_dir, _aps = _failed_run(engine, tmp_path, 5, [f"{EAGLE}/inputs/x.h5", "/data/x.h5"], plan=plan)
    assert [record.source for record in summary.failure_records(details, offset=0)[0]] == [None, None]


# ---------------------------------------------------------------------------
# Reading a log from its end


def _tail(tmp_path, content: bytes, limit: int, **options):
    path = tmp_path / "pbs-output.log"
    path.write_bytes(content)
    descriptor = os.open(path, os.O_RDONLY)
    try:
        return summary.tail_lines(descriptor, limit, **options)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize(
    ("content", "limit", "expected"),
    [
        (b"", 5, ([], False)),
        (b"a\nb\nc\n", 2, (["b", "c"], True)),
        (b"a\nb\nc\n", 3, (["a", "b", "c"], False)),
        (b"a\nb\nc", 3, (["a", "b", "c"], False)),  # the last line without its break
        (b"a\nb\nc", 1, (["c"], True)),
        (b"a\r\n\r\nb\r\n", 5, (["a", "", "b"], False)),  # CRLF; an empty line is a line
        (b"\n", 5, ([""], False)),
        (b"ok \xff\xfe\n", 5, (["ok ��"], False)),  # not UTF-8: replaced
    ],
)
def test_tail_lines_returns_the_last_whole_lines(tmp_path, content, limit, expected):
    assert _tail(tmp_path, content, limit) == expected


def test_tail_lines_reads_across_blocks(tmp_path):
    lines = [f"{index:09d}" for index in range(30_000)]  # 300 kB: five blocks
    content = ("\n".join(lines) + "\n").encode()
    assert _tail(tmp_path, content, 20_000) == (lines[-20_000:], True)
    assert _tail(tmp_path, content, 30_000) == (lines, False)
    assert _tail(tmp_path, content, 1000) == (lines[-1000:], True)


def test_tail_lines_reads_at_most_max_bytes(tmp_path):
    """A line cut where the read stops is left out, unless it is the only one: then it is cut at its start."""
    block = summary.TAIL_BLOCK_BYTES
    long_then_short = b"x" * (3 * block) + b"\nlast\n"
    assert _tail(tmp_path, long_then_short, 10, max_bytes=2 * block) == (["last"], True)
    huge = b"y" * (3 * block) + b"\n"
    assert _tail(tmp_path, huge, 10, max_bytes=block) == (["y" * (block - 1)], True)
    assert _tail(tmp_path, b"z" * (3 * block), 10, max_bytes=block) == (["z" * block], True)
    many = b"".join(b"%06d\n" % index for index in range(100_000))  # 700 kB, limit 50,000 lines: 350 kB needed
    lines, truncated = _tail(tmp_path, many, 50_000, max_bytes=block)
    whole = block // 7 if block % 7 else block // 7 - 1  # the read's first line, cut (or maybe cut), is left out
    assert truncated and lines == [f"{index:06d}" for index in range(100_000 - whole, 100_000)]


def _arrival(n: int) -> bytes:
    """A line of ``arrivals.jsonl`` as ``jobs.arrivals`` writes it (about 250 bytes)."""
    path = f"/data/live/frame_{n + 1}.h5"
    line = {"n": n, "path": path, "size": 8_400_000, "mtime_ns": 1_761_559_200_000_000_000 + n, "inode": 4_000 + n}
    line |= {"device": 2049, "source": "notify", "admitted_at": "2026-10-27T10:00:00+00:00", "scan_point": n + 1}
    return (json.dumps(line, separators=(",", ":")) + "\n").encode()


def test_the_arrivals_are_the_last_whole_lines_n_plus_one_from_a_bounded_tail(tmp_path, monkeypatch):
    """``count_arrivals`` reads a bounded tail, never the ledger (the job page asks every 2 s), so a long one (here
    300,000 arrivals in a sparse file of more than 64 MiB) is counted at once; a last line still being written is not
    counted. A ledger whose last whole line has no arrival number (a malformed ledger) is counted line by line only up
    to ``MAX_COUNTED_BYTES``."""
    read: list[int] = []
    pread = os.pread

    def counted(descriptor: int, length: int, offset: int) -> bytes:
        read.append(length)
        return pread(descriptor, length, offset)

    monkeypatch.setattr(os, "pread", counted)

    def count(content: bytes, *, hole: int = 0) -> tuple[int | None, str, str | None]:
        path = tmp_path / "arrivals.jsonl"
        with path.open("wb") as ledger:
            ledger.truncate(hole)  # a sparse start: no byte of it is read
            ledger.seek(hole)
            ledger.write(content)
        number, state = summary.count_arrivals(os.fspath(path), "arrivals.jsonl")
        return number, state.state, state.detail

    long = 70 * 1024 * 1024
    tail = b"".join(_arrival(n) for n in range(299_990, 300_000)) + _arrival(300_000)[:40]
    assert count(tail, hole=long) == (300_000, "available", None)
    assert sum(read) <= summary.ARRIVAL_TAIL_BYTES
    assert count(b"") == (0, "available", None)
    assert count(_arrival(0)[:40]) == (0, "available", None)  # its only line still being written
    assert count(_arrival(0) + _arrival(1)) == (2, "available", None)
    unnumbered = b'{"path":"/data/live/frame_1.h5"}\n' * 3
    assert count(unnumbered) == (3, "available", None)
    assert count(_arrival(41)) == (1, "available", None)  # a number its size cannot hold: counted
    read.clear()
    assert count(unnumbered, hole=long) == (None, "unreadable", "its last line holds no arrival number")
    assert sum(read) <= summary.ARRIVAL_TAIL_BYTES


def test_run_log_is_run_jsons_log(engine, tmp_path):
    read = summary.FileRead("run.json", "/runs/run.json", summary.FileState.AVAILABLE)
    log = summary.run_log({"log": [f"line {index}" for index in range(5)]}, read, limit=2)
    assert log == summary.LogTail(["line 3", "line 4"], True, 5, read)
    assert summary.run_log({}, read, limit=2) == summary.LogTail([], False, 0, read)  # the PBS task's fallback
    assert summary.run_log({"log": "one"}, read, limit=2) == summary.LogTail([], False, None, read)  # not a list
    assert summary.run_log(None, read, limit=2) == summary.LogTail([], False, None, read)
    assert summary.run_log({"log": [1, None]}, read, limit=5).lines == ["1", "None"]
