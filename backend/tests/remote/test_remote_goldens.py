"""Byte compatibility with the remote goldens.

``tests/fixtures/remote_goldens/`` (its ``README.md``) holds the files that run creation, the plan, the runner and the
publication must write for each case of ``tests.remote_support.CASES``, normalized. Here the portal writes them:

- fast: run creation and the plan write the goldens' ``request.json``, ``inputs.jsonl``, ``bdp.json`` and the DM inbox
  copy, also through ``jobs.queue.enqueue_run`` → ``remote.submission.submit_run``;
- slow: the runner and the publication (``remote.poller.publish_returned``) publish a returned run directory as the
  goldens hold it (the same moved files, rewritten input paths, remote originals, marker and ``run.json``), and
  publishing it again changes nothing.

``tests/jobs/test_runner.py`` and ``test_mpi_runner.py`` compare the runners' own files with the same goldens.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from laueportal.jobs.queue import enqueue_run
from laueportal.jobs.state import RunPhase
from laueportal.remote import submission
from laueportal.remote.poller import publish_returned
from tests.remote_support import (
    CASES,
    CASES_BY_NAME,
    PUBLISHED,
    SUBMISSION_ID,
    Layout,
    case_settings,
    committed_goldens,
    create_run,
    golden_documents,
    output_listing,
    prepare,
    publication_documents,
    remote_settings,
    return_results,
    run_v2_case,
    text_differences,
    tree_state,
)
from tests.run_support import dispatch_queue, load_job, make_engine

PORTAL_FILES = ("request.json", "inputs.jsonl", "bdp.json", "submission.json")
"""What the portal writes before anything runs: the run's files, the plan and its DM inbox copy."""


def _portal_differences(case, plan, layout) -> dict[str, str]:
    expected, actual = committed_goldens(case), golden_documents(plan, layout)
    return text_differences(
        {name: expected[name] for name in PORTAL_FILES}, {name: actual[name] for name in PORTAL_FILES}
    )


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_the_portal_writes_the_goldens_request_manifest_plan_and_inbox_copy(case, tmp_path):
    layout = Layout(tmp_path)
    engine = make_engine(tmp_path / "portal")
    try:
        plan = prepare(case, layout, engine)
    finally:
        engine.dispose()
    differences = _portal_differences(case, plan, layout)
    assert not differences, "\n".join(f"{name}:\n{diff}" for name, diff in differences.items())


class FakeDM:
    def __init__(self):
        self.submissions = []

    def submit(self, plan):
        self.submissions.append(plan)
        return {"id": "dm-001", "status": "pending"}


@pytest.mark.parametrize("name", ["aps-lauego", "crux-wire"])
def test_submission_through_the_queue_writes_the_goldens_plan_and_inbox_copy(name, tmp_path, monkeypatch):
    """A remote job leaves through ``enqueue_run`` (no queued row for the worker) and DM gets the goldens' plan."""
    case = CASES_BY_NAME[name]
    layout = Layout(tmp_path)
    engine = make_engine(tmp_path / "portal")
    monkeypatch.setattr(submission, "uuid4", lambda: UUID(SUBMISSION_ID))
    client = FakeDM()
    try:
        job = create_run(case, layout, engine)
        settings = remote_settings(case_settings(case, layout))
        external_id = enqueue_run(job.job_id, engine=engine, remote=settings, dm_client=client)
        row = load_job(engine, job.job_id)
        local = dispatch_queue(engine)
    finally:
        engine.dispose()
    assert external_id == "dm-001" and local == []  # never the worker's
    [plan] = client.submissions
    assert plan == json.loads((Path(plan["run_directory"]) / "bdp.json").read_text(encoding="utf-8"))
    assert (row.backend, row.submission_id, row.queue_job_id) == (case.backend, SUBMISSION_ID, f"dm:{SUBMISSION_ID}")
    assert (row.phase, row.external_id, row.external_state) == (RunPhase.QUEUED, "dm-001", "pending")
    differences = _portal_differences(case, plan, layout)
    assert not differences, "\n".join(f"{name}:\n{diff}" for name, diff in differences.items())


@pytest.mark.slow
@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_the_portal_publishes_a_returned_run_as_the_goldens_hold_it(case, tmp_path):
    plan, layout, reports, _published = run_v2_case(case, tmp_path)
    assert reports[-1]["exit"] in (0, 1), reports[-1]["stderr"][-3000:]
    return_results(plan)
    summary = publish_returned(plan)
    assert summary["submission_id"] == SUBMISSION_ID
    expected, actual = committed_goldens(case, PUBLISHED), publication_documents(plan, layout)
    differences = text_differences(expected, actual)
    assert not differences, "the publication differs from the goldens:\n" + "\n".join(
        f"{name}:\n{diff}" for name, diff in differences.items()
    )

    run = Path(plan["run_directory"])
    incoming = Path(plan.get("incoming_directory", plan["output_directory"]))
    assert sorted(path.name for path in incoming.iterdir()) == [".runner.lock", "run.json"]  # not moved
    snapshot = {path: path.read_bytes() for path in run.rglob("*") if path.is_file()}
    listing, tree = output_listing(run), tree_state(layout.aps)
    assert publish_returned(plan) == summary  # republication (a restarted poller) is idempotent
    assert output_listing(run) == listing
    assert {path: path.read_bytes() for path in run.rglob("*") if path.is_file()} == snapshot
    assert tree_state(layout.aps) == tree  # the whole APS tree: names, kinds, modes, links, bytes
