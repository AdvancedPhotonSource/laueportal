"""The read-time path rule (``remote.paths.portal_path``, ``run_path_map``).

Paths a remote run wrote that publication leaves remote are mapped when read, through the ``path_map`` of the run's own
plan, ``bdp.json`` (written by the portal at submission), never through the returned ``run.json``'s, and a mapped path
must stay inside the portal directory its prefix maps to.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from laueportal.jobs.rundir import write_json_file
from laueportal.remote.paths import MAX_PLAN_BYTES, PLAN_NAME, portal_path, run_path_map

SUBMISSION = "6f1c2a4e-3b5d-4e7f-8a9b-0c1d2e3f4a5b"
EAGLE = f"/eagle/workspace/runs/{SUBMISSION}"


@pytest.fixture
def layout(tmp_path):
    """An APS root with the experiment's run directory, and the plans' maps for ``aps`` and ``crux``."""
    aps = tmp_path / "aps"
    run = aps / "LauePortal" / "analysis" / "recon_1"
    run.mkdir(parents=True)
    return {
        "aps_root": aps,
        "run": run,
        "aps": {str(run): str(run / "incoming" / SUBMISSION), str(aps): str(aps)},
        "crux": {str(run): f"{EAGLE}/results", str(aps): f"{EAGLE}/inputs"},
    }


def _plan(run: Path, path_map, **extra) -> None:
    write_json_file(run / PLAN_NAME, {"version": 1, "submission_id": SUBMISSION, "path_map": path_map, **extra})


# --- portal_path ---------------------------------------------------------------------------------------------


def test_aps_maps_the_runs_inbox_back_to_the_run_and_keeps_aps_paths(layout):
    run, aps = layout["run"], layout["aps_root"]
    mappings = layout["aps"]
    assert portal_path(f"{run}/incoming/{SUBMISSION}/reconstruction/points/p.h5", mappings) == (
        f"{run}/reconstruction/points/p.h5"
    )
    assert portal_path(f"{aps}/LauePortal/data/wire_1.h5", mappings) == f"{aps}/LauePortal/data/wire_1.h5"
    assert portal_path(f"{run}/output.h5", mappings) == f"{run}/output.h5"  # a portal path maps to itself


def test_crux_maps_results_to_the_run_and_inputs_to_the_aps_root(layout):
    run, aps = layout["run"], layout["aps_root"]
    mappings = layout["crux"]
    assert portal_path(f"{EAGLE}/results/reconstruction/scan.h5", mappings) == f"{run}/reconstruction/scan.h5"
    assert portal_path(f"{EAGLE}/inputs/LauePortal/data/config/geo.xml", mappings) == (
        f"{aps}/LauePortal/data/config/geo.xml"
    )


@pytest.mark.parametrize("value", ["/data/wire_1.h5", "relative/name.h5", "name.h5", ""])
def test_without_a_map_a_value_is_returned_as_written(value):
    """A run without a plan (a local run): its paths are portal paths already (readers still confine them to the
    file roots)."""
    assert portal_path(value, None) == value


@pytest.mark.parametrize("value", ["/data/wire_1.h5", f"{EAGLE}/inputs/x.h5", "relative/name.h5", ""])
def test_the_empty_map_makes_every_value_unavailable(value):
    """The map of a plan that exists but cannot be used: the rule fails closed."""
    assert portal_path(value, {}) is None


@pytest.mark.parametrize(
    "value",
    [
        "/home/user/.ssh/id_ed25519",  # another path, no prefix covers it
        "/eagle/workspace/runs/another/inputs/x.h5",  # another run's workspace
        f"{EAGLE}/inputs-lookalike/x.h5",  # a prefix lookalike: prefixes match whole components
        f"{EAGLE}/resultsx/scan.h5",
        "reconstruction/scan.h5",  # a relative name
        "",
    ],
)
def test_a_remote_value_no_prefix_covers_is_unavailable(layout, value):
    assert portal_path(value, layout["crux"]) is None


@pytest.mark.parametrize(
    "value",
    [
        f"{EAGLE}/inputs/../../../../../../etc/passwd",
        f"{EAGLE}/results/../../../../../../etc/passwd",
        f"{EAGLE}/results/..",  # the run's parent
        f"{EAGLE}/inputs",  # the APS root itself is not a file inside it
    ],
)
def test_a_mapped_path_that_leaves_its_portal_directory_is_unavailable(layout, value):
    assert portal_path(value, layout["crux"]) is None


def test_a_parent_segment_that_stays_inside_maps(layout):
    assert portal_path(f"{EAGLE}/results/sub/../output.h5", layout["crux"]) == f"{layout['run']}/sub/../output.h5"


def test_a_symlink_leaving_the_portal_directory_is_unavailable(layout, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("not a run's file")
    (layout["run"] / "escape").symlink_to(outside)
    assert portal_path(f"{EAGLE}/results/escape/secret", layout["crux"]) is None
    (layout["run"] / "inside").symlink_to(layout["run"])
    assert portal_path(f"{EAGLE}/results/inside/output.h5", layout["crux"]) == f"{layout['run']}/inside/output.h5"


# --- run_path_map ---------------------------------------------------------------------------------------------


def test_the_map_is_the_plans(layout):
    _plan(layout["run"], layout["crux"])
    assert run_path_map(layout["run"]) == layout["crux"]


def test_a_tampered_returned_summary_cannot_redirect_readers(layout):
    """The returned ``run.json`` comes from the remote side: its ``path_map`` is never used."""
    run = layout["run"]
    _plan(run, layout["crux"])
    tampered = {"/home/user/.ssh": f"{EAGLE}/inputs", str(run): f"{EAGLE}/results"}
    write_json_file(run / "run.json", {"submission_id": SUBMISSION, "provenance": {"path_map": tampered}})
    mapped = portal_path(f"{EAGLE}/inputs/id_ed25519", run_path_map(run))
    assert mapped == f"{layout['aps_root']}/id_ed25519"


def test_a_summary_without_provenance_keeps_the_mapping(layout):
    """PBS ending without the runner's summary (walltime): the crux task's fallback ``run.json`` has no
    ``provenance``, and ``failures.jsonl``'s sources still map."""
    run = layout["run"]
    _plan(run, layout["crux"])
    write_json_file(run / "run.json", {"submission_id": SUBMISSION, "status": "Failed", "counters": {}})
    source = f"{EAGLE}/inputs/LauePortal/data/wire/wire_1.h5"
    assert portal_path(source, run_path_map(run)) == f"{layout['aps_root']}/LauePortal/data/wire/wire_1.h5"


def test_a_run_without_a_plan_has_no_map(layout):
    """A local run: its ``run.json`` (whatever it records) gives no map, and its paths are used as written."""
    run = layout["run"]
    write_json_file(run / "run.json", {"provenance": {"path_map": layout["crux"]}})
    assert run_path_map(run) is None
    assert run_path_map(layout["aps_root"] / "missing") is None
    assert portal_path("/data/wire_1.h5", run_path_map(run)) == "/data/wire_1.h5"


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[1, 2]",
        json.dumps({"version": 1}),
        json.dumps({"path_map": ["a", "b"]}),
        json.dumps({"path_map": {"/a": 1}}),
        json.dumps({"path_map": None}),
    ],
)
def test_an_unusable_plan_gives_the_empty_map(layout, content):
    """The plan exists, so the run is remote: nothing it wrote is trusted as a portal path."""
    (layout["run"] / PLAN_NAME).write_text(content, encoding="utf-8")
    assert run_path_map(layout["run"]) == {}
    assert portal_path(f"{layout['aps_root']}/LauePortal/data/wire_1.h5", run_path_map(layout["run"])) is None


def test_an_oversized_or_unreadable_plan_gives_the_empty_map(layout):
    run = layout["run"]
    with open(run / PLAN_NAME, "wb") as handle:
        handle.truncate(MAX_PLAN_BYTES + 1)
    assert run_path_map(run) == {}
    (run / PLAN_NAME).unlink()
    (run / PLAN_NAME).mkdir()  # a directory: reading it fails
    assert run_path_map(run) == {}


def _map_within(run: Path, seconds: float = 10.0) -> dict[str, str] | None:
    """``run_path_map(run)``, failing the test (instead of hanging it) if the read blocks."""
    result: list[dict[str, str] | None] = []
    reader = threading.Thread(target=lambda: result.append(run_path_map(run)), daemon=True)
    reader.start()
    reader.join(seconds)
    if reader.is_alive():
        os.close(os.open(run / PLAN_NAME, os.O_WRONLY | os.O_NONBLOCK))  # let the blocked reader end
        pytest.fail("reading the plan blocked")
    return result[0]


@pytest.mark.parametrize("kind", ["a symbolic link", "a FIFO", "nested too deeply to parse", "not UTF-8"])
def test_a_plan_that_is_no_regular_json_file_gives_the_empty_map(layout, tmp_path, kind):
    """The plan is opened without following a link and without blocking: a link (even to a usable plan) and a FIFO
    are plans that cannot be used, and so is one the parser cannot take."""
    run = layout["run"]
    plan = run / PLAN_NAME
    if kind == "a symbolic link":
        elsewhere = tmp_path / "elsewhere.json"
        elsewhere.write_text(json.dumps({"version": 1, "path_map": layout["crux"]}))
        plan.symlink_to(elsewhere)
    elif kind == "a FIFO":
        os.mkfifo(plan)
    elif kind == "nested too deeply to parse":
        plan.write_text('{"path_map": ' + "[" * 100_000)
    else:
        plan.write_bytes(b'{"path_map": {"/a\xff": "/b"}}')
    assert _map_within(run) == {}
