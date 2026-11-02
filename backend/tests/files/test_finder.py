"""The create forms' file helpers on a samples root: ``POST /api/v1/files/patterns`` (Find Matching Files) and
``POST /api/v1/files/indices`` (Find Indices; the wire form's file indices).

The helpers read names only. The test's samples root (``samples``) has ``measured-indexing`` with
``Twin2_wire_<point>_<depth>.h5`` for points 2 and 745 and depths 0, 30, 60 and 90 (and a geometry, a crystal and a
folder), and ``synthetic-reconstruction/raw`` with one file without a number.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from laueportal.app import create_app
from laueportal.files import finder
from tests.api_support import PORTAL_HEADERS
from tests.settings_support import make_settings

MEASURED = "measured-indexing"
RAW = "synthetic-reconstruction/raw"
"""Folders of the samples root (``samples``)."""


@pytest.fixture
def samples(tmp_path: Path) -> Path:
    """The samples root: empty files with the names of a depth-resolved indexing sample and of a raw wire scan."""
    root = tmp_path / "samples"
    names = [f"{MEASURED}/Twin2_wire_{point}_{depth}.h5" for point in (2, 745) for depth in (0, 30, 60, 90)]
    names += [f"{MEASURED}/geometry.xml", f"{MEASURED}/Ni.xml", f"{MEASURED}/provenance/manifest.json"]
    for name in [*names, f"{RAW}/synthetic_wire_scan.h5"]:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(b"")
    return root


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    """A folder of the test's own, inside the roots too."""
    (folder := tmp_path / "scratch").mkdir()
    return folder


@pytest.fixture
def client(tmp_path: Path, samples: Path, scratch: Path) -> Iterator[TestClient]:
    settings = make_settings(tmp_path, files={"roots": {"samples": samples, "scratch": scratch}})
    with TestClient(create_app(settings), headers=PORTAL_HEADERS) as client:
        yield client


def post(client: TestClient, helper: str, **body: Any) -> Any:
    response = client.post(f"/api/v1/files/{helper}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    ("folder", "program", "expected"),
    [
        (MEASURED, "lauego", [("Twin2_wire_%d_%d.h5", "2,745", "0-90:30")]),
        (
            MEASURED,
            "wire",
            [
                ("Twin2_wire_*_%d.h5", "0-90:30", None),
                ("Twin2_wire_2_%d.h5", "0-90:30", None),
                ("Twin2_wire_745_%d.h5", "0-90:30", None),
            ],
        ),
        (RAW, "wire", [("synthetic_wire_scan.h5", None, None)]),
    ],
    ids=["lauego", "wire", "no-number"],
)
def test_patterns_suggest_the_folders_templates_with_their_index_ranges(client, samples, folder, program, expected):
    """The last two numbers (LaueGo) or the last one (wire) made ``%d``, similar templates merged with ``*`` (first),
    each labelled with its ranges and giving them for the form's index fields."""
    found = post(client, "patterns", folder=os.fspath(samples / folder), program=program)
    assert [(item["pattern"], item["scan_points"], item["depth_range"]) for item in found["patterns"]] == expected
    assert found["truncated"] is False
    if program == "lauego":
        assert found["patterns"][0]["label"] == "Twin2_wire_%d_%d.h5 (scanPoints: 2,745, depths: 0-90:30)"


@pytest.mark.parametrize(
    ("folder", "program", "pattern", "expected"),
    [
        (MEASURED, "lauego", "Twin2_wire_%d_%d.h5", ("2,745", "0-90:30", 8)),
        (MEASURED, "lauego", "*_745_%d.h5", ("0-90:30", None, 4)),
        (MEASURED, "lauego", "Twin2_wire_2_0.h5", (None, None, 1)),
        (MEASURED, "lauego", "other_%d.h5", (None, None, 0)),
        (MEASURED, "lauego", "\ue000_%d.h5", (None, None, 0)),  # no character stands in for a %d: no 500
        (MEASURED, "wire", None, ("0-90:30", None, 8)),
        (MEASURED, "wire", "ignored_%d.h5", ("0-90:30", None, 8)),
        (RAW, "wire", None, (None, None, 0)),
    ],
)
def test_indices_are_those_of_the_files_the_template_matches_and_for_wire_of_every_file(
    client, samples, folder, program, pattern, expected
):
    """LaueGo: the files the template matches, as a run's inputs are matched (its first ``%d`` the point, its second
    the depth). Wire: every HDF file's last number, whatever the template."""
    found = post(client, "indices", folder=os.fspath(samples / folder), program=program, pattern=pattern)
    assert (found["scan_points"], found["depth_range"], found["files"], found["truncated"]) == (*expected, False)


@pytest.mark.parametrize(
    ("helper", "body", "status"),
    [
        ("patterns", {"folder": "/", "program": "wire"}, 400),
        ("indices", {"folder": "{samples}/measured-indexing/../../..", "program": "wire"}, 400),
        ("patterns", {"folder": "samples/measured-indexing", "program": "wire"}, 400),
        ("patterns", {"folder": "{samples}/missing", "program": "wire"}, 404),
        ("indices", {"folder": "{samples}/measured-indexing/Ni.xml", "program": "wire"}, 422),
        ("indices", {"folder": "{samples}/measured-indexing", "program": "lauego"}, 422),
        ("indices", {"folder": "{samples}/measured-indexing", "program": "lauego", "pattern": "  "}, 422),
        ("indices", {"folder": "{samples}/measured-indexing", "program": "lauego", "pattern": "x_%d_%d_%d.h5"}, 422),
        ("patterns", {"folder": "{samples}/measured-indexing", "program": "other"}, 422),
    ],
)
def test_a_helper_reads_only_a_folder_inside_the_roots(client, samples, helper, body, status):
    """Roots-checked like the browser (outside, ``..`` out of them, relative: 400; missing: 404; a file: 422); a
    LaueGo request needs a template, with at most two ``%d`` as a run's (each more makes the matcher's work grow
    exponentially)."""
    body = {**body, "folder": body["folder"].replace("{samples}", os.fspath(samples))}
    response = client.post(f"/api/v1/files/{helper}", json=body)
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"


def test_a_helper_reads_a_bounded_number_of_files(client, samples, monkeypatch):
    """At most ``MAX_FILES`` HDF files are read (the first the file system gives), and the answer says so."""
    monkeypatch.setattr(finder, "MAX_FILES", 3)
    patterns = post(client, "patterns", folder=os.fspath(samples / MEASURED), program="lauego")
    indices = post(client, "indices", folder=os.fspath(samples / MEASURED), program="wire")
    assert patterns["truncated"] is True and indices["truncated"] is True
    assert indices["files"] == 3


def test_a_name_that_is_not_utf8_is_left_out(client, scratch):
    """JSON cannot send such a name: the helpers skip it, as if it were not there."""
    for name in (b"scan_1.h5", b"scan_2.h5", b"caf\xe9_3.h5"):  # the last in Latin-1
        open(os.path.join(os.fsencode(scratch), name), "w").close()
    patterns = post(client, "patterns", folder=os.fspath(scratch), program="wire")
    indices = post(client, "indices", folder=os.fspath(scratch), program="wire")
    assert [(item["pattern"], item["scan_points"]) for item in patterns["patterns"]] == [("scan_%d.h5", "1,2")]
    assert (indices["scan_points"], indices["files"]) == ("1,2", 2)
