"""R3 of the demo seed (``seed.demo.register_multipoint_reconstruction``): a finished three-point wire scan made from
the synthetic point, catalogued by lauelab's writer, as the wire viewer serves it."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from laueportal.app import create_app
from laueportal.seed import demo
from tests.recon_support import synthetic_point
from tests.settings_support import make_database


def test_the_multipoint_reconstruction_has_three_points_each_brightest_at_its_own_depth(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    settings, engine = make_database(tmp_path, files={"roots": {"runs": runs}})
    layout = demo.DemoLayout(data=demo.default_data_directory(), inputs=tmp_path / "inputs", runs_root=runs)
    try:
        reconstruction_id = demo.register_multipoint_reconstruction(engine, layout, synthetic_point())
        with TestClient(create_app(settings)) as client:
            run = f"/api/v1/reconstruction-runs/{reconstruction_id}"
            detail = client.get(run).json()
            artifact = client.get(f"{run}/artifact").json()
            points = client.get(f"{run}/points").json()["rows"]
            brightest = [client.get(f"{run}/points/{p['point_id']}/trace").json()["brightest_index"] for p in points]
    finally:
        engine.dispose()

    assert (detail["job_id"], detail["notes"], detail["parameters"]["scan_points"]) == (1, demo.MULTIPOINT_NOTES, "1-3")
    assert detail["parameters"]["filename_prefixes"] == ["synthetic_wire_scan_%d.h5"]
    assert (artifact["state"], artifact["catalog_status"], artifact["points_complete"], artifact["points_total"]) == (
        "ready", "finished", 3, 3,
    )  # fmt: skip
    assert [(p["point_id"], p["status"], p["sample_x_um"]) for p in points] == [
        ("synthetic_wire_scan_1", "complete", 0.0),
        ("synthetic_wire_scan_2", "complete", 5.0),
        ("synthetic_wire_scan_3", "complete", 10.0),
    ]
    assert brightest == [25 + shift for shift in demo.MULTIPOINT_SHIFTS]
    assert os.listdir(runs) == [f"rec_{reconstruction_id}"]  # the staged points are gone
