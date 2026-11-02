"""Locating a reconstruction's output: the scan catalog of new runs, the per-depth files of migrated runs."""

from laueportal.reconstruction.views import locate_artifact
from laueportal.science.recon import KIND_PER_DEPTH, KIND_SCAN, Artifact


def test_locate_artifact_prefers_the_scan_catalog_then_per_depth_files(tmp_path):
    assert locate_artifact(None) is None
    assert locate_artifact(str(tmp_path / "missing")) is None
    assert locate_artifact(str(tmp_path)) is None

    (tmp_path / "point_1_summary.txt").write_text("")
    assert locate_artifact(str(tmp_path)) == Artifact(KIND_PER_DEPTH, str(tmp_path))

    catalog = tmp_path / "reconstruction" / "scan.h5"
    catalog.parent.mkdir()
    catalog.write_bytes(b"")
    assert locate_artifact(str(tmp_path)) == Artifact(KIND_SCAN, str(catalog))
