"""Tests for run input manifests, frozen requests, failure reports, and publication."""

import hashlib
import io
import json
import os
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import BaseModel

from laueportal.files.inputs import ResolvedInput
from laueportal.indexing.lauego.params import check_output_xml_name
from laueportal.jobs import rundir as publication
from laueportal.jobs.rundir import (
    CATEGORY_CANCELLED,
    CATEGORY_ERROR,
    CATEGORY_INPUT,
    CATEGORY_MEMORY,
    CATEGORY_NUMERICAL,
    FAILURE_REPORT_FILENAME,
    MANIFEST_FILENAME,
    REQUEST_FORMAT,
    RESERVED_RUN_FILENAMES,
    FailureRecord,
    FailureReportWriter,
    ManifestEntry,
    ManifestError,
    build_manifest_entries,
    classify_error,
    count_failure_records,
    manifest_digest,
    read_failure_records,
    read_failure_report,
    read_manifest,
    read_request,
    request_document,
    verify_manifest,
    write_manifest,
    write_request,
)
from laueportal.remote.cleanup import CLEANUP_JOB_RECORD


def _inputs(*names, template_index=0):
    return tuple(ResolvedInput(f"/data/{name}", template_index, (index,)) for index, name in enumerate(names, start=1))


def test_entries_use_file_stems_in_run_order_with_selection_identity():
    inputs = (
        ResolvedInput("/data/scan_7_0.h5", 0, (7, 0)),
        ResolvedInput("/data/scan_7_1.h5", 0, (7, 1)),
        ResolvedInput("/data/other_3.h5", 1, (3,)),
        ResolvedInput("/data/fixed.h5", 2, ()),
    )

    entries = build_manifest_entries(inputs, depths={1: -49.0})

    assert [(e.index, e.input_id, e.template_index, e.scan_point, e.depth_point, e.depth) for e in entries] == [
        (0, "scan_7_0", 0, 7, 0, None),
        (1, "scan_7_1", 0, 7, 1, -49.0),
        (2, "other_3", 1, 3, None, None),
        (3, "fixed", 2, None, None, None),
    ]
    assert entries[0].source == "/data/scan_7_0.h5"


def test_shared_stems_fall_back_to_full_filenames_for_the_whole_run():
    entries = build_manifest_entries(_inputs("a.h5", "a.tif", "b.h5"))
    assert [entry.input_id for entry in entries] == ["a.h5", "a.tif", "b.h5"]


def test_duplicate_filenames_and_empty_runs_are_rejected():
    with pytest.raises(ManifestError, match="not unique"):
        build_manifest_entries((ResolvedInput("/x/a.h5", 0, (1,)), ResolvedInput("/y/a.h5", 0, (2,))))
    with pytest.raises(ManifestError, match="at least one input"):
        build_manifest_entries(())


def test_manifest_roundtrip_digest_and_verification(tmp_path):
    path = tmp_path / MANIFEST_FILENAME
    entries = build_manifest_entries(_inputs("f_1.h5", "f_2.h5", "f_3.h5"))

    summary = write_manifest(path, entries)

    assert summary.path == os.fspath(path)
    assert summary.n_inputs == 3
    assert summary.sha256 == manifest_digest(path)
    assert list(read_manifest(path)) == list(entries)
    assert sorted(os.listdir(tmp_path)) == [MANIFEST_FILENAME]
    verify_manifest(path, expected_digest=summary.sha256, expected_count=3)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[1]) == {
        "index": 1,
        "input_id": "f_2",
        "source": "/data/f_2.h5",
        "template_index": 0,
        "scan_point": 2,
        "depth_point": None,
        "depth": None,
    }

    with pytest.raises(ManifestError, match="digest"):
        verify_manifest(path, expected_digest="0" * 64, expected_count=3)
    with pytest.raises(ManifestError, match="holds 3 inputs"):
        verify_manifest(path, expected_digest=summary.sha256, expected_count=4)
    with pytest.raises(ManifestError, match="missing"):
        verify_manifest(tmp_path / "absent.jsonl", expected_digest=summary.sha256, expected_count=3)


def test_manifest_never_overwrites_and_leaves_no_partial_on_failure(tmp_path):
    path = tmp_path / MANIFEST_FILENAME
    entries = build_manifest_entries(_inputs("f_1.h5"))
    write_manifest(path, entries)
    original = path.read_bytes()

    with pytest.raises(FileExistsError):
        write_manifest(path, build_manifest_entries(_inputs("g_1.h5")))
    assert path.read_bytes() == original
    assert sorted(os.listdir(tmp_path)) == [MANIFEST_FILENAME]

    bad_order = (ManifestEntry(1, "x", "/data/x.h5", 0, 1, None),)
    with pytest.raises(ManifestError, match="expected 0"):
        write_manifest(tmp_path / "other.jsonl", bad_order)
    assert sorted(os.listdir(tmp_path)) == [MANIFEST_FILENAME]

    with pytest.raises(ManifestError, match="at least one input"):
        write_manifest(tmp_path / "empty.jsonl", ())
    assert sorted(os.listdir(tmp_path)) == [MANIFEST_FILENAME]

    write_manifest(path, build_manifest_entries(_inputs("g_1.h5")), overwrite=True)
    assert [entry.input_id for entry in read_manifest(path)] == ["g_1"]


def test_reading_a_corrupt_manifest_names_the_entry(tmp_path):
    path = tmp_path / MANIFEST_FILENAME
    good = ManifestEntry(0, "a", "/data/a.h5", 0, 1, None).to_json()
    path.write_text(good + "\n" + '{"index": 5, "input_id": "b", "source": "/b", "template_index": 0}\n')
    with pytest.raises(ManifestError, match="entry 1 carries index 5"):
        list(read_manifest(path))
    path.write_text(good + "\nnot json\n")
    with pytest.raises(ManifestError, match="entry 1 is not JSON"):
        list(read_manifest(path))
    path.write_text(good + "\n\n")
    with pytest.raises(ManifestError, match="blank line"):
        list(read_manifest(path))


def test_request_document_carries_format_and_version(tmp_path):
    path = tmp_path / "request.json"
    digest = write_request(path, {"kind": "test", "request": {"when": "now"}})

    document = read_request(path)
    assert document["format"] == REQUEST_FORMAT
    assert document["version"] == 1
    assert document["kind"] == "test"
    assert len(digest) == 64
    with pytest.raises(FileExistsError):
        write_request(path, {"kind": "again"})

    path.write_text(json.dumps({"format": "other"}))
    with pytest.raises(ManifestError, match="is not a"):
        read_request(path)


def test_datetimes_and_non_finite_numbers_are_written_as_isoformat_and_pythons_tokens(tmp_path):
    """A datetime object is its ``isoformat()``, aware or naive as given; NaN and the infinities are Python's tokens;
    what JSON cannot hold at all is refused before anything is written."""

    aware = datetime(2026, 10, 15, 7, 34, 56, 123456, tzinfo=timezone(timedelta(hours=-5)))
    payload = {
        "aware": aware,
        "naive": datetime(2026, 10, 15, 7, 34, 56),
        "values": [float("nan"), float("inf"), -float("inf")],
    }
    expected = (
        '{\n  "aware": "2026-10-15T07:34:56.123456-05:00",\n  "naive": "2026-10-15T07:34:56",\n'
        '  "values": [\n    NaN,\n    Infinity,\n    -Infinity\n  ]\n}\n'
    )
    assert publication.json_text(payload) == expected
    digest = publication.write_json_file(tmp_path / "doc.json", payload)
    assert (tmp_path / "doc.json").read_bytes() == expected.encode("utf-8")
    assert digest == hashlib.sha256(expected.encode("utf-8")).hexdigest()
    record = FailureRecord(0, "f_1", "/data/f_1.h5", CATEGORY_INPUT, "boom", context=payload, recorded_at="t")
    assert record.to_json() == (
        '{"index":0,"input_id":"f_1","source":"/data/f_1.h5","category":"input","message":"boom",'
        '"error_type":null,"context":{"aware":"2026-10-15T07:34:56.123456-05:00","naive":"2026-10-15T07:34:56",'
        '"values":[NaN,Infinity,-Infinity]},"recorded_at":"t"}'
    )

    (tmp_path / "doc.json").unlink()
    for value, error in ((object(), TypeError), ("\ud800", UnicodeEncodeError)):
        with pytest.raises(error):
            publication.write_json_file(tmp_path / "doc.json", {"value": value})
    with pytest.raises(TypeError):
        replace(record, context={"value": object()}).to_json()
    assert os.listdir(tmp_path) == []  # refused before anything was written


def test_request_document_keeps_submitted_at_as_written():
    """submitted_at stays isoformat() text with +00:00."""

    class Request(BaseModel):  # a parameter model: its derived values are properties, not fields
        notes: str
        submitted_at: datetime

        @property
        def derived(self) -> int:
            return 0

    document = request_document(Request(notes="n", submitted_at=datetime(2026, 10, 15, 12, 0, 0, 5, tzinfo=UTC)))

    assert document == {"notes": "n", "submitted_at": "2026-10-15T12:00:00.000005+00:00"}


def test_reserved_names_cover_every_support_file():
    assert RESERVED_RUN_FILENAMES >= {"inputs.jsonl", "request.json", "failures.jsonl", "run.json", "output.h5"}
    # The portal's own control file in a remote run's directory (the clean-up of its leftovers).
    assert CLEANUP_JOB_RECORD in RESERVED_RUN_FILENAMES
    with pytest.raises(ValueError, match="reserved"):
        check_output_xml_name(CLEANUP_JOB_RECORD)


class _InputError(ValueError):
    pass


class _NumericalIndexingError(RuntimeError):
    pass


InputError = type("InputError", (_InputError,), {})
NumericalIndexingError = type("NumericalIndexingError", (_NumericalIndexingError,), {})


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (InputError("bad frame"), CATEGORY_INPUT),
        (NumericalIndexingError("pixel-to-q failed"), CATEGORY_NUMERICAL),
        (MemoryError(), CATEGORY_MEMORY),
        (FileNotFoundError("gone"), CATEGORY_INPUT),
        (KeyError("entry1/data"), CATEGORY_INPUT),
        (RuntimeError("worker died"), CATEGORY_ERROR),
    ],
)
def test_error_classification_uses_class_names(error, category):
    assert classify_error(error) == category


def test_failure_report_is_created_only_when_something_is_recorded(tmp_path):
    path = tmp_path / FAILURE_REPORT_FILENAME
    with FailureReportWriter(path) as writer:
        pass
    assert writer.close() is None
    assert os.listdir(tmp_path) == []

    entry = ManifestEntry(3, "f_4", "/data/f_4.h5", 0, 4, None)
    writer = FailureReportWriter(path)
    writer.append(FailureRecord.from_error(entry, FileNotFoundError("no such file"), context={"attempt": 1}))
    writer.append(FailureRecord.not_run(entry, category=CATEGORY_CANCELLED, message="cancelled by user"))
    assert writer.count == 2
    assert path.exists() is False  # published on close only
    assert writer.close() == os.fspath(path)
    assert sorted(os.listdir(tmp_path)) == [FAILURE_REPORT_FILENAME]

    records, has_more = read_failure_report(path)
    assert has_more is False
    assert [(r.index, r.input_id, r.category, r.error_type, r.message) for r in records] == [
        (3, "f_4", CATEGORY_INPUT, "FileNotFoundError", "no such file"),
        (3, "f_4", CATEGORY_CANCELLED, None, "cancelled by user"),
    ]
    assert records[0].context == {"attempt": 1}
    assert records[0].recorded_at is not None
    assert count_failure_records(path) == 2

    with pytest.raises(FileExistsError):
        FailureReportWriter(path)
    with pytest.raises(RuntimeError, match="closed"):
        writer.append(records[0])


def test_failure_report_pages_are_bounded(tmp_path):
    path = tmp_path / FAILURE_REPORT_FILENAME
    with FailureReportWriter(path) as writer:
        for index in range(250):
            entry = ManifestEntry(index, f"f_{index}", f"/data/f_{index}.h5", 0, index, None)
            writer.append(FailureRecord.not_run(entry))

    first, more = read_failure_report(path, offset=0, limit=100)
    last, no_more = read_failure_report(path, offset=200, limit=100)
    assert (len(first), more) == (100, True)
    assert [record.index for record in first[:3]] == [0, 1, 2]
    assert (len(last), no_more) == (50, False)
    assert last[-1].index == 249
    with pytest.raises(ValueError):
        read_failure_report(path, offset=-1)


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("[1, 2]", "not a JSON object (list)"),
        ('"text"', "not a JSON object (str)"),
        ('{"index": 1e400, "category": "input", "message": "x"}', "its index is not a whole number"),
        ('{"index": NaN, "category": "input", "message": "x"}', "its index is not a whole number"),
        ('{"index": "SECRET-7", "category": "input", "message": "x"}', "its index is not a whole number"),
        ('{"index": [1], "category": "input", "message": "x"}', "its index is not a whole number"),
        ('{"index": 1, "message": "x"}', "'category'"),
        ('{"index": 1, "category": "input", "message": "x", "context": "text"}', "dictionary update sequence"),
    ],
)
def test_a_malformed_failure_record_is_a_manifest_error(tmp_path, line, message):
    """Whatever JSON a line holds, a record that is not one is ``ManifestError`` (the job page reports it), never
    another exception."""
    path = tmp_path / FAILURE_REPORT_FILENAME
    path.write_text(line + "\n")
    with pytest.raises(ManifestError, match="malformed failure record") as raised:
        read_failure_report(path)
    assert message in str(raised.value)
    assert "SECRET" not in str(raised.value)  # the message names what is wrong, never the record's values


def _report(lines: list[bytes]) -> io.BytesIO:
    return io.BytesIO(b"".join(line + b"\n" for line in lines))


def _record(index: int, message: str = "bad") -> bytes:
    return json.dumps({"index": index, "category": "input", "message": message}).encode()


class _CountingReport(io.BytesIO):
    """A report that remembers the longest piece any read returned."""

    longest = 0

    def readline(self, size: int | None = -1, /) -> bytes:
        piece = super().readline(size)
        self.longest = max(self.longest, len(piece))
        return piece


def test_a_failure_report_page_holds_no_record_longer_than_the_bound():
    """A record longer than ``max_record_bytes`` on the page is ``ManifestError``; one before the page is skipped
    piece by piece (never held whole), and one after it only means that more follow."""
    bound = 1024
    long_record = _record(1, "x" * (64 * bound))
    lines = [_record(0), long_record, _record(2), b"", _record(4)]

    with pytest.raises(ManifestError, match=f"report: record 1 is longer than {bound} bytes"):
        read_failure_records(_report(lines), offset=0, limit=10, max_record_bytes=bound, name="report")

    handle = _CountingReport(b"".join(line + b"\n" for line in lines))
    records, has_more = read_failure_records(handle, offset=2, limit=10, max_record_bytes=bound)
    assert ([record.index for record in records], has_more) == ([2, 4], False)  # the blank line 3 holds no record
    assert handle.longest <= 64 * 1024  # the long record was skipped in pieces

    records, has_more = read_failure_records(_report(lines), offset=0, limit=1, max_record_bytes=bound)
    assert ([record.index for record in records], has_more) == ([0], True)

    filler = "y" * (bound - len(_record(0, "")))
    exact = _record(0, filler)  # exactly the bound
    assert len(exact) == bound
    assert read_failure_records(_report([exact]), max_record_bytes=bound)[0][0].message == filler
    unterminated = io.BytesIO(exact)  # the last line without its break
    assert len(read_failure_records(unterminated, max_record_bytes=bound)[0]) == 1
    with pytest.raises(ManifestError, match="longer than"):
        read_failure_records(io.BytesIO(exact + b" "), max_record_bytes=bound)


def test_failure_records_are_parsed_only_on_the_page():
    """Lines before the page are counted, not parsed: a malformed one there does not spoil a later page."""
    lines = [b"{not json", b'{"index": 1, "category": "input", "message": "\xff"}', _record(2)]
    assert [record.index for record in read_failure_records(_report(lines), offset=2)[0]] == [2]
    with pytest.raises(ManifestError, match="record 1 is not UTF-8"):
        read_failure_records(_report(lines), offset=1)
    with pytest.raises(ManifestError, match="record 0 is not JSON"):
        read_failure_records(_report(lines))
    with pytest.raises(ManifestError, match="record 0 is nested too deeply"):
        read_failure_records(_report([b"[" * 100_000]))


def test_failure_report_keeps_its_partial_when_the_run_raises(tmp_path):
    path = tmp_path / FAILURE_REPORT_FILENAME
    entry = ManifestEntry(0, "f_0", "/data/f_0.h5", 0, 0, None)
    with pytest.raises(RuntimeError, match="boom"):
        with FailureReportWriter(path) as writer:
            writer.append(FailureRecord.not_run(entry))
            raise RuntimeError("boom")
    assert sorted(os.listdir(tmp_path)) == [FAILURE_REPORT_FILENAME + publication.PARTIAL_SUFFIX]
    assert not path.exists()


def test_publish_file_links_atomically_and_refuses_to_clobber(tmp_path):
    final = tmp_path / "file.txt"
    partial = publication.partial_path(final)
    assert partial.name == "file.txt.partial"
    partial.write_text("one")

    assert publication.publish_file(partial, final) == final
    assert final.read_text() == "one"
    assert not partial.exists()

    partial.write_text("two")
    with pytest.raises(FileExistsError):
        publication.publish_file(partial, final)
    assert final.read_text() == "one"
    assert partial.read_text() == "two"

    publication.publish_file(partial, final, overwrite=True)
    assert final.read_text() == "two"

    with pytest.raises(FileNotFoundError):
        publication.publish_file(partial, final)
    with pytest.raises(ValueError, match="same directory"):
        publication.publish_file(tmp_path / "a" / "x.partial", tmp_path / "x")
    publication.discard_partial(final)  # absent partial is fine


@pytest.mark.parametrize("overwrite", [False, True])
def test_publish_file_makes_the_file_and_its_name_durable(tmp_path, monkeypatch, overwrite):
    """The data are fsynced before the name appears, the directory after it."""

    final = tmp_path / "output.h5"
    partial = publication.partial_path(final)
    partial.write_bytes(b"results")  # written and closed by its writer (lauelab), never fsynced there
    events: list[str] = []
    real_fsync, real_link, real_replace = os.fsync, os.link, os.replace

    def fsync(descriptor: int) -> None:
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        events.append(f"fsync {os.path.basename(target) or target}")
        real_fsync(descriptor)

    def link(source, target):
        events.append("publish")
        real_link(source, target)

    def replace(source, target):
        events.append("publish")
        real_replace(source, target)

    monkeypatch.setattr(publication.os, "fsync", fsync)
    monkeypatch.setattr(publication.os, "link", link)
    monkeypatch.setattr(publication.os, "replace", replace)
    publication.publish_file(partial, final, overwrite=overwrite)
    monkeypatch.undo()

    assert events == ["fsync output.h5.partial", "publish", f"fsync {tmp_path.name}"]
    assert final.read_bytes() == b"results" and not partial.exists()


def test_publish_file_falls_back_to_rename_when_links_are_unsupported(tmp_path, monkeypatch):
    import errno

    final = tmp_path / "file.txt"
    partial = publication.partial_path(final)
    partial.write_text("payload")

    def unsupported_link(source, target):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(publication.os, "link", unsupported_link)
    publication.publish_file(partial, final)
    assert final.read_text() == "payload"
    assert not partial.exists()

    partial.write_text("again")
    with pytest.raises(FileExistsError):
        publication.publish_file(partial, final)
    assert final.read_text() == "payload"


def test_json_documents_go_through_a_partial_only_their_writer_uses(tmp_path):
    final = tmp_path / "doc.json"
    shared = publication.partial_path(final)
    shared.write_text("another writer's partial\n")  # the shared partial name: write_json_file never touches it

    publication.write_json_file(final, {"n": 1})
    with pytest.raises(FileExistsError):
        publication.write_json_file(final, {"n": 2})
    assert json.loads(final.read_text()) == {"n": 1}
    publication.write_json_file(final, {"n": 3}, overwrite=True)

    assert json.loads(final.read_text()) == {"n": 3}
    assert shared.read_text() == "another writer's partial\n"
    assert sorted(os.listdir(tmp_path)) == ["doc.json", "doc.json.partial"]  # no writer's partial left


def test_unique_partials_are_new_files_named_after_the_document(tmp_path):
    names = set()
    for _ in range(3):
        partial, descriptor = publication.create_unique_partial(tmp_path / "progress.json")
        os.close(descriptor)
        assert partial.parent == tmp_path and partial.stat().st_size == 0
        assert partial.name.startswith("progress.json.") and partial.name.endswith(publication.PARTIAL_SUFFIX)
        names.add(partial.name)
    assert len(names) == 3


def test_published_files_are_regular_files(tmp_path):
    path = tmp_path / MANIFEST_FILENAME
    write_manifest(path, build_manifest_entries(_inputs("f_1.h5")))
    assert stat.S_ISREG(path.stat().st_mode)
    assert path.stat().st_nlink == 1


def test_a_short_write_to_completed_jsonl_leaves_only_whole_lines_and_a_retry_appends_them_whole(tmp_path, monkeypatch):
    def point(n: int) -> publication.CompletedPoint:
        return publication.CompletedPoint(
            n=n,
            point_id=f"live_{n}",
            scan_point=n,
            path=f"/runs/R1/reconstruction/points/live_{n}.h5",
            n_depths=11,
            completed_at="2026-10-26T12:00:00+00:00",
        )

    completed = tmp_path / publication.COMPLETED_FILENAME
    publication.append_completed(tmp_path, [point(0)])
    real_write = os.write
    monkeypatch.setattr(
        publication.os, "write", lambda descriptor, data: real_write(descriptor, data[: len(data) // 2])
    )
    with pytest.raises(OSError, match="wrote"):
        publication.append_completed(tmp_path, [point(1), point(2)])
    monkeypatch.undo()
    assert completed.read_bytes() == point(0).line()  # the part written is gone

    publication.append_completed(tmp_path, [point(1), point(2)])
    assert [json.loads(line) for line in completed.read_text().splitlines()] == [
        json.loads(point(n).line()) for n in range(3)
    ]
