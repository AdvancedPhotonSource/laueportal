"""A place to validate new runs in: one allowed root holding a folder of frame files, the geometry and crystal files and
the runs root; a database with scan 1 (its catalog folder is that folder), scan 2 (no catalog) and reconstruction R1 of
scan 1."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from laueportal.core.settings import Settings
from laueportal.jobs import programs
from laueportal.jobs.validation import Validated
from tests.conftest import create_test_catalog, create_test_job, create_test_metadata, create_test_reconstruction_run
from tests.remote_support import Layout, bdp_settings
from tests.settings_support import make_database

FRAMES = ("img_1.tif", "img_2.tif")


@dataclass
class Place:
    settings: Settings
    engine: Engine
    root: Path
    """The one allowed root."""
    data: Path
    """A folder of frame files (``FRAMES``): scan 1's catalog folder."""
    geometry: Path
    crystal: Path
    reconstruction: Path
    """R1's run directory (not made)."""

    def form(self, kind: programs.ProgramKind, **values: Any) -> dict[str, Any]:
        """A create form that passes every check but the unlinked warning (the configured defaults, the paths
        here, an author), then ``values``."""
        paths: dict[str, Any] = {
            "scan_number": None,
            "input_path": os.fspath(self.data),
            "filename_prefixes": ["img_%d.tif"],
            "scan_points": "1-2",
            "geometry_file": os.fspath(self.geometry),
            "author": "tester",
        }
        if kind in ("lauego", "laue_matching"):
            paths |= {"reconstruction_id": None, "crystal_file": os.fspath(self.crystal)}
        if kind == "laue_matching":
            paths |= {"forward_cache": "cache", "create_forward_cache": False}
        return {**programs.form_defaults(kind, self.settings), **paths, **values}


def make_place(tmp_path: Path, **files: Any) -> Place:
    root = tmp_path / "root"
    data = root / "data"
    data.mkdir(parents=True)
    for name in FRAMES:
        (data / name).write_text("frame")
    geometry, crystal = root / "geometry.xml", root / "crystal.xml"
    geometry.write_text("geometry")
    crystal.write_text("crystal")
    runs = root / "runs"
    (runs / "analysis").mkdir(parents=True)
    settings, engine = make_database(tmp_path, files={"roots": {"data": root}, "runs_root": runs, **files})
    reconstruction = runs / "analysis" / "rec_1"
    with Session(engine) as session, session.begin():
        catalog = create_test_catalog(1)
        catalog.filefolder = os.fspath(data)
        session.add_all([create_test_metadata(1), create_test_metadata(2), catalog, create_test_job(1)])
        session.flush()
        run = create_test_reconstruction_run(scan_number=1, job_id=1)
        run.output_path = os.fspath(reconstruction)
        session.add(run)
    return Place(settings, engine, root, data, geometry, crystal, reconstruction)


def offering(place: Place, *targets: str) -> Settings:
    """``place.settings`` with remote execution on, offering ``targets`` (``aps``, ``crux``); the experiment folder
    (``bdp.yaml``'s, which must be ``files.runs_root``) is the place's root, so a remote run's folder is under
    ``<root>/analysis/`` and the place's files are inside it."""
    document = bdp_settings(Layout(place.root.parent / "remote"))
    document["experiment_directory"] = os.fspath(place.root)
    for name, target in document["targets"].items():
        target["enabled"] = name in targets
    path = place.root.parent / "bdp.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return place.settings.model_copy(
        update={
            "files": place.settings.files.model_copy(update={"runs_root": place.root}),
            "remote": place.settings.remote.model_copy(update={"enabled": True, "settings_file": path}),
        }
    )


def found(validated: Validated) -> set[tuple[tuple[str | int, ...], str, str]]:
    """Each issue as ``(loc, severity, code)``."""
    return {(tuple(issue.loc), issue.severity, issue.code) for issue in validated.issues}
