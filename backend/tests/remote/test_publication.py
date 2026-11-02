"""The portal's publication of a returned run, on cases of ``remote_support.CASES``: the portal's runner runs the case
and its output is returned into the run's inbox; that APS tree is published by ``remote.poller.publish_returned``. What
a publication writes, and that a republication (a restarted poller) changes nothing, is pinned by
``tests/remote/test_remote_goldens.py``; here: every invalid return is refused and leaves the files as they were, and a
publication interrupted between a link and its unlink is finished.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from laueportal.jobs.rundir import write_json_file
from laueportal.remote.poller import publish_returned
from tests.remote_support import (
    CASES_BY_NAME,
    return_results,
    run_v2_case,
    tree_state,
)

pytestmark = pytest.mark.slow


class Returned:
    """A case's returned run (the portal's runner, then the return into the inbox) and a copy of its APS tree, so that
    each publication starts from the same bytes."""

    def __init__(self, name: str, tmp_path: Path):
        self.case = CASES_BY_NAME[name]
        self.plan, self.layout, reports, _ = run_v2_case(self.case, tmp_path / "case")
        assert reports[-1]["exit"] in (0, 1), reports[-1]["stderr"][-3000:]
        return_results(self.plan)
        self.run = Path(self.plan["run_directory"])
        self.incoming = Path(self.plan.get("incoming_directory", self.plan["output_directory"]))
        self.aps = self.layout.aps
        self.saved = tmp_path / "returned-aps"
        shutil.copytree(self.aps, self.saved, symlinks=True)

    def restore(self, change: Callable[[Returned], None] | None = None) -> None:
        """Back to the returned tree, then ``change`` it."""
        shutil.rmtree(self.aps)
        shutil.copytree(self.saved, self.aps, symlinks=True)
        if change is not None:
            change(self)

    def publish(self) -> dict[str, Any]:
        try:
            return {"summary": json.loads(json.dumps(publish_returned(self.plan)))}
        except Exception as error:
            return {"error": type(error).__name__, "message": str(error)}


def _summary(returned: Returned) -> Path:
    return returned.incoming / "run.json"


def _edit_summary(change: Callable[[dict[str, Any]], None]) -> Callable[[Returned], None]:
    def edit(returned: Returned) -> None:
        document = json.loads(_summary(returned).read_text(encoding="utf-8"))
        change(document)
        _summary(returned).write_text(json.dumps(document), encoding="utf-8")

    return edit


def _another_submission(document):
    document["submission_id"] = "00000000-0000-4000-8000-000000000000"


def _counters_that_do_not_sum(document):
    document["counters"]["n_failed"] += 1


def _an_artifact_outside_the_output(document):
    document["artifacts"]["results"] = "/etc/hostname"


def _pbs_exit_status(returned):
    (returned.incoming / "pbs.json").write_text(json.dumps({"job_id": "1.pbs", "status": "F", "exit_status": 271}))


def _a_returned_symlink(returned):
    (returned.incoming / "extra.log").symlink_to(returned.run / "inputs.jsonl")


def _an_existing_output_in_the_run(returned):
    (returned.run / "output.xml").write_text("<not this run's/>", encoding="utf-8")


def _another_publication_in_the_run(returned):
    (returned.run / ".bdp-publication.json").write_text(
        json.dumps({"submission_id": "00000000-0000-4000-8000-000000000000", "files": []}), encoding="utf-8"
    )


def _another_summary_in_the_run(returned):
    (returned.run / "run.json").write_text(json.dumps({"submission_id": "another"}), encoding="utf-8")


def _restarted(returned, first=()):
    """This publication's marker, as a poller that stopped before moving anything left it: the inbox's entries by name
    (``first``: names it lists before them)."""
    names = sorted(p.name for p in returned.incoming.iterdir() if p.name not in {"run.json", ".runner.lock", *first})
    write_json_file(
        returned.run / ".bdp-publication.json",
        {"submission_id": returned.plan["submission_id"], "files": [*first, *names]},
    )


def _a_symlink_whose_destination_exists_after_a_restart(returned):
    (returned.incoming / "extra.log").symlink_to(returned.run / "inputs.jsonl")
    (returned.run / "extra.log").write_text("the run's own file", encoding="utf-8")
    _restarted(returned, first=("extra.log",))


def _a_different_output_xml_after_a_restart(returned):
    _restarted(returned)
    (returned.run / "output.xml").write_text("<not this publication's/>", encoding="utf-8")


def _a_different_results_file_after_a_restart(returned):
    _restarted(returned)
    (returned.run / "output.h5").write_bytes(b"not this publication's results file")


INVALID_RETURNS = {
    "another submission": _edit_summary(_another_submission),
    "counters that do not sum": _edit_summary(_counters_that_do_not_sum),
    "an artifact outside the output": _edit_summary(_an_artifact_outside_the_output),
    "a nonzero PBS exit status": _pbs_exit_status,
    "a returned symlink": _a_returned_symlink,
    "an existing output in the run": _an_existing_output_in_the_run,
    "another publication in the run": _another_publication_in_the_run,
    "another summary in the run": _another_summary_in_the_run,
    # After a restart (this publication's marker is there), checked while the entries are moved:
    "a returned symlink whose destination exists": _a_symlink_whose_destination_exists_after_a_restart,
    "a different Output XML at its destination": _a_different_output_xml_after_a_restart,
    "a different results file at its destination": _a_different_results_file_after_a_restart,
}


@pytest.fixture(scope="module")
def returned_indexing(tmp_path_factory):
    return Returned("aps-lauego", tmp_path_factory.mktemp("returned-indexing"))


LEFT_BEHIND: dict[str, Callable[[Returned], tuple[set[Path], set[Path]]]] = {
    # Refused while the entries are moved (by name): this publication's marker is written first; an entry moved
    # before the one refused stays moved (a restarted publication finishes it, below).
    "a returned symlink": lambda r: ({r.run / ".bdp-publication.json"}, set()),
    "a different Output XML at its destination": lambda r: ({r.run / "output.h5"}, {r.incoming / "output.h5"}),
}
"""The files a refused return leaves added and removed (none for the others)."""


@pytest.mark.parametrize("change", list(INVALID_RETURNS))
def test_an_invalid_return_is_refused_and_leaves_the_files_as_they_were(change, returned_indexing):
    returned = returned_indexing
    returned.restore(INVALID_RETURNS[change])
    before = tree_state(returned.aps)
    result = returned.publish()
    assert "error" in result, result
    after = tree_state(returned.aps)
    added, removed = LEFT_BEHIND.get(change, lambda _r: (set(), set()))(returned)
    relative = (
        {path.relative_to(returned.aps).as_posix() for path in added},
        {path.relative_to(returned.aps).as_posix() for path in removed},
    )
    assert (set(after) - set(before), set(before) - set(after)) == relative
    assert {name for name in set(after) & set(before) if after[name] != before[name]} == set()


def _garbage_results(returned):
    (returned.incoming / "output.h5").write_bytes(b"not an HDF5 file")


def test_an_artifact_hdf5_cannot_read_is_an_invalid_return(returned_indexing):
    """h5py's ``OSError`` is an invalid return (``ValueError``: the job is finalized Failed, not retried on every
    pass). Nothing is published."""
    returned_indexing.restore(_garbage_results)
    before = tree_state(returned_indexing.aps)
    result = returned_indexing.publish()
    assert result["error"] == "ValueError", result
    assert result["message"].startswith("Returned results artifact is not readable HDF5: ")
    assert tree_state(returned_indexing.aps) == before


def test_a_publication_interrupted_between_a_link_and_its_unlink_is_finished(returned_indexing):
    """A poller that stopped after linking a returned file into the run directory and before removing the inbox
    copy: the next publication sees the same file, removes the copy and finishes, with what an uninterrupted
    publication leaves."""
    returned = returned_indexing
    returned.restore()
    summary = returned.publish()["summary"]
    published = tree_state(returned.aps)
    os.link(returned.run / "progress.json", returned.incoming / "progress.json")

    assert returned.publish() == {"summary": summary}
    assert tree_state(returned.aps) == published


@pytest.fixture(scope="module")
def returned_crux_indexing(tmp_path_factory):
    return Returned("crux-lauego", tmp_path_factory.mktemp("returned-crux-indexing"))


def test_a_transformed_file_left_with_its_inbox_copy_is_recognized_and_finished(returned_crux_indexing):
    """The same interruption for the files publication transforms (``output.h5`` and the Output XML, whose Eagle
    input paths it rewrites): a destination is recognized only by the bytes the transform makes of the inbox copy;
    the copy is removed and the publication finishes. A different file there is refused
    (``test_an_invalid_return_is_refused_and_leaves_the_files_as_they_were``)."""
    returned = returned_crux_indexing
    returned.restore()
    summary = returned.publish()["summary"]
    published = tree_state(returned.aps)
    saved_incoming = returned.saved / returned.incoming.relative_to(returned.aps)
    for name in ("output.h5", "output.xml"):
        shutil.copy2(saved_incoming / name, returned.incoming / name)
        assert (returned.incoming / name).read_bytes() != (returned.run / name).read_bytes()  # transformed

    assert returned.publish() == {"summary": summary}
    assert tree_state(returned.aps) == published
