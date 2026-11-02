"""A live run's ``live.json`` (``jobs.live``)."""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from pydantic import ValidationError

from laueportal.jobs.live import (
    LIVE_FILENAME,
    LiveDocument,
    read_live_document,
    write_live_document,
)

DAQ = {"id": "1a2b", "experiment": "e1", "storage": "/data/e1", "destination": "/data/e1/raw"}

DOCUMENTS: dict[str, dict[str, Any]] = {
    "daq": {"source": "daq", "daq": DAQ, "folder": "/data/e1/raw/scan_5", "filename_template": "Si_%d.h5"},
    "notify": {"source": "notify", "folder": "/data/e1/raw/scan_5/", "filename_template": "Si_*%d.h5"},
    "run": {
        "source": "run",
        "folder": "/data/e1/raw/scan_5",
        "filename_template": "Si_%d.h5",
        "follows": {"job_id": 12, "directory": "/data/e1/analysis/live/rec_12"},
    },
}
"""One document per source: a DAQ's catalog, notifications, and a follower of a live wire run."""


@pytest.mark.parametrize("source", list(DOCUMENTS))
def test_live_json_is_written_once_with_every_key_and_read_back(tmp_path, source):
    document = LiveDocument.model_validate(DOCUMENTS[source])
    write_live_document(tmp_path, document)

    text = (tmp_path / LIVE_FILENAME).read_text(encoding="utf-8")
    assert list(json.loads(text)) == ["daq", "filename_template", "folder", "follows", "format", "source", "version"]
    assert json.loads(text)["format"] == "laue-portal-live"
    assert json.loads(text)["version"] == 1
    assert read_live_document(tmp_path) == document
    with pytest.raises(FileExistsError):
        write_live_document(tmp_path, document)
    assert os.listdir(tmp_path) == [LIVE_FILENAME]  # no partial file left behind
    assert (tmp_path / LIVE_FILENAME).read_text(encoding="utf-8") == text


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"source": "notify"}, "a DAQ is named exactly when the source is a DAQ"),
        ({"daq": None}, "a DAQ is named exactly when the source is a DAQ"),
        ({"follows": {"job_id": 12, "directory": "/runs/rec_12"}}, "a followed run is named exactly"),
        ({"folder": "/data/e1/other/scan_5"}, "the folder must lie inside the DAQ's destination"),
        ({"daq": {**DAQ, "destination": "/data/e2/raw"}}, "destination must lie inside"),
        ({"folder": "raw/scan_5"}, "must be an absolute path"),
        ({"filename_template": "Si.h5"}, "exactly one %d"),
        ({"filename_template": "Si_%d_%d.h5"}, "exactly one %d"),
        ({"filename_template": "raw/Si_%d.h5"}, "exactly one %d"),
        ({"filename_template": " Si_%d.h5"}, "exactly one %d"),
        ({"filename_template": "S" * 253 + "_%d"}, "at most 255 characters"),
        ({"format": "laue-portal-run-request"}, "format"),
        ({"version": 2}, "version"),
        ({"cursor": 0}, "Extra inputs are not permitted"),
    ],
)
def test_live_json_refuses_what_a_run_cannot_follow(change, message):
    with pytest.raises(ValidationError, match=message):
        LiveDocument.model_validate({**DOCUMENTS["daq"], **change})


@pytest.mark.parametrize("key", ["format", "version", "daq", "follows"])
def test_live_json_is_read_only_with_every_key(tmp_path, key):
    stored = LiveDocument.model_validate(DOCUMENTS["notify"]).model_dump(mode="json")
    del stored[key]
    (tmp_path / LIVE_FILENAME).write_text(json.dumps(stored), encoding="utf-8")
    with pytest.raises(ValidationError, match=f"{key}\n  Field required"):
        read_live_document(tmp_path)


def test_a_followers_run_is_named_by_its_job_and_directory():
    with pytest.raises(ValidationError, match="follows"):
        LiveDocument.model_validate({**DOCUMENTS["run"], "follows": None})
    with pytest.raises(ValidationError, match="job_id"):
        LiveDocument.model_validate({**DOCUMENTS["run"], "follows": {"job_id": 0, "directory": "/runs/rec_12"}})
    assert LiveDocument.model_validate(DOCUMENTS["notify"]).folder == "/data/e1/raw/scan_5"
