"""The create forms' prefill and clone: ``GET /api/v1/programs/{kind}/prefill``.

The place (``tests.validation_support``) has scan 1 (its catalog folder is the place's data folder), scan 2 (no catalog)
and R1 of scan 1; here R1 gets wire parameters and a catalog (``scan.h5``), R2 (scan 1) has neither file nor parameters,
and I1 indexes R1 with a migrated run's saved values the models refuse.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from laueportal.app import create_app
from laueportal.core.settings import Settings
from laueportal.jobs import prefill as jobs_prefill
from laueportal.jobs import programs
from laueportal.jobs.rundir import reconstruction_catalog_path
from laueportal.reconstruction.models import ReconstructionRun
from tests.conftest import (
    create_test_indexing_run,
    create_test_job,
    create_test_lauego_parameters,
    create_test_wire_reconstruction_parameters,
)
from tests.validation_support import Place, make_place, offering

WIRE_SAVED = {
    "filename_prefixes": ["wire_%d.h5"],
    "geometry_file": "/saved/wire-geometry.xml",
    "percent_brightest": 42.0,
    "wire_edges": "trailing",
    "depth_start": -20.0,
    "depth_end": 30.0,
    "depth_resolution": 2.0,
    "frame_skip": None,  # migrated wire runs saved them empty (8 of 8)
    "wire_skip": None,
    "num_threads": 7,  # config values: never copied
    "memory_limit_mb": 999,
    "verbose": 3,
    "scan_points": "1-5",
    "scan_points_len": 5,
}
LAUEGO_SAVED = {
    "filename_prefixes": ["img_%d.tif"],
    "threshold_ratio": -1,  # a migrated run's "automatic"
    "depth": "nan",  # a migrated run's (49 of 75 LaueGo runs)
    "index_kev_max_calc": 21.5,
    "box_size": 9,
    "energy_unit": "eV",  # a config value: never copied
    "beamline": "1-BM",
}


def saved(row: Any, values: dict[str, Any]) -> Any:
    for name, value in values.items():
        setattr(row, name, value)
    return row


@pytest.fixture
def place(tmp_path) -> Iterator[Place]:
    place = make_place(tmp_path)
    catalog = reconstruction_catalog_path(place.reconstruction)
    os.makedirs(os.path.dirname(catalog))
    open(catalog, "w").close()
    with Session(place.engine) as session, session.begin():
        r1 = session.get_one(ReconstructionRun, 1)
        r1.job.backend = "aps"
        session.add(saved(create_test_wire_reconstruction_parameters(1), WIRE_SAVED))
        session.add_all([saved(create_test_job(2), {"backend": "crux"}), create_test_job(3)])
        session.flush()
        indexing = saved(
            create_test_indexing_run(scan_number=1, job_id=2, reconstruction_id=1),
            {"input_path": os.fspath(place.data)},
        )
        session.add(indexing)
        session.flush()
        session.add(saved(create_test_lauego_parameters(indexing.id), LAUEGO_SAVED))
        session.add(
            ReconstructionRun(
                scan_number=1,
                job_id=3,
                method="wire",
                input_path="/in",
                output_path=os.fspath(place.root / "rec_2"),
                created_at=r1.created_at,
            )
        )
    yield place
    place.engine.dispose()


def prefill(settings: Settings, kind: str, **query: str) -> Any:
    with TestClient(create_app(settings)) as client:
        return client.get(f"/api/v1/programs/{kind}/prefill", params=query)


def values(settings: Settings, kind: str, **query: str) -> dict[str, Any]:
    response = prefill(settings, kind, **query)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body["values"]) == set(programs.PROGRAMS[kind].fields_from("form"))  # form fields only
    return body["values"]


def template(place: Place, kind: programs.ProgramKind, scan: int | None = None, rec: int | None = None) -> str:
    return programs.output_path_template(kind, place.settings, scan_number=scan, reconstruction_id=rec)


SOURCES = ("scan_number", "reconstruction_id", "input_path", "filename_prefixes", "scan_points", "geometry_file")


@pytest.mark.parametrize(
    ("kind", "query", "expected"),
    [
        ("lauego", {}, lambda p: {"scan_number": None, "input_path": None, "filename_prefixes": []}),
        (
            "lauego",
            {"scan": "1"},
            lambda p: {"scan_number": 1, "input_path": os.fspath(p.data), "filename_prefixes": ["test_prefix"]},
        ),
        ("lauego", {"scan": "2"}, lambda p: {"scan_number": 2, "input_path": None, "filename_prefixes": []}),
        (
            "lauego",
            {"reconstruction": "1"},
            lambda p: {
                "scan_number": 1,
                "reconstruction_id": 1,
                "input_path": reconstruction_catalog_path(p.reconstruction),
                "filename_prefixes": ["wire_%d.h5"],
                "scan_points": "1-5",
                "geometry_file": "/saved/wire-geometry.xml",
            },
        ),
        (
            "lauego",
            {"scan": "1", "reconstruction": "2"},
            lambda p: {"scan_number": 1, "reconstruction_id": 2, "input_path": os.fspath(p.root / "rec_2")},
        ),
        ("wire", {}, lambda p: {"scan_number": None, "input_path": None, "filename_prefixes": []}),
        (
            "wire",
            {"scan": "1"},
            lambda p: {"scan_number": 1, "input_path": os.fspath(p.data), "filename_prefixes": ["test_prefix"]},
        ),
    ],
    ids=["lauego-none", "lauego-scan", "lauego-scan-no-catalog", "lauego-R", "lauego-scan-R-no-scan.h5", "wire-none",
         "wire-scan"],
)  # fmt: skip
def test_a_source_fills_its_paths_and_the_rest_are_the_configured_defaults(place, kind, query, expected):
    """A scan gives its catalog's folder and templates; a reconstruction on the LaueGo form its ``scan.h5`` (its run
    directory without one), templates, scan points, geometry and scan; the energy cutoffs and everything else stay
    the configured defaults; the output folder is the template for the source's scan and reconstruction."""
    found = values(place.settings, kind, **query)
    wanted = expected(place)
    scan, rec = wanted.get("scan_number"), wanted.get("reconstruction_id")
    defaults = programs.form_defaults(kind, place.settings) | {"backend": "local"}
    assert {name: found[name] for name in wanted} == wanted
    assert found["output_path_template"] == template(place, kind, scan, rec if kind == "lauego" else None)
    unchanged = set(found) - set(wanted) - set(SOURCES) - {"output_path_template"}
    assert {name: found[name] for name in unchanged} == {name: defaults[name] for name in unchanged}
    assert prefill(place.settings, kind, **query).json()["clone_of"] is None


NOT_ON_THE_ROW = {
    "scan_number",
    "reconstruction_id",
    "input_path",
    "output_path_template",
    "author",
    "notes",
    "backend",
}
"""The form fields a clone takes from the run (or the job, or the configuration), not its parameter row: every
other form field is a column of it, copied (a renamed column fails this test)."""


@pytest.mark.parametrize("kind", ["lauego", "wire"])
def test_a_clone_copies_the_runs_form_values_mapped_to_the_form_and_resets_author_and_notes(place, kind):
    """A clone (I1 on the LaueGo form, R1 on the wire form): the saved form values, input, scan and reconstruction; the
    author and notes are the configured defaults; no config value is copied (they are not form fields); an empty frame
    or wire skip becomes 1, a threshold ratio of 0 or less null, and a ``nan`` depth null; the target is the run's when
    still offered (I1's ``crux``), else the default (R1's ``aps``: APS is ``local`` here)."""
    settings = offering(place, "crux")
    query = {"indexing": "1"} if kind == "lauego" else {"reconstruction": "1"}
    response = prefill(settings, kind, **query)
    assert response.status_code == 200, response.text
    body = response.json()
    found, defaults = body["values"], programs.form_defaults(kind, settings)

    assert body["clone_of"] == ({"kind": "indexing", "id": 1, "label": "I1"} if kind == "lauego" else
                                {"kind": "reconstruction", "id": 1, "label": "R1"})  # fmt: skip
    assert found["output_path_template"] == programs.output_path_template(
        kind, settings, scan_number=1, reconstruction_id=1 if kind == "lauego" else None
    )
    row = (
        saved(create_test_lauego_parameters(), LAUEGO_SAVED)
        if kind == "lauego"
        else saved(create_test_wire_reconstruction_parameters(), WIRE_SAVED)
    )
    copied = {name: getattr(row, name) for name in set(programs.PROGRAMS[kind].fields_from("form")) - NOT_ON_THE_ROW}
    mapped = (
        {"threshold_ratio": None, "depth": None, "reconstruction_id": 1, "input_path": os.fspath(place.data)}
        if kind == "lauego"
        else {"frame_skip": 1, "wire_skip": 1, "input_path": "/test/input"}
    )
    assert found == copied | mapped | {
        "scan_number": 1,
        "author": defaults["author"],
        "notes": defaults["notes"],
        "output_path_template": found["output_path_template"],
        "backend": "crux" if kind == "lauego" else "local",
    }
    assert (found.get("index_kev_max_calc"), found.get("box_size")) == ((21.5, 9) if kind == "lauego" else (None, None))
    assert not {"num_threads", "memory_limit_mb", "verbose", "energy_unit", "beamline"} & set(found)


@pytest.mark.parametrize(
    ("kind", "agreeing", "alone"),
    [
        ("lauego", {"indexing": "1", "scan": "1", "reconstruction": "1"}, {"indexing": "1"}),
        ("lauego", {"reconstruction": "1", "scan": "1"}, {"reconstruction": "1"}),
        ("wire", {"reconstruction": "1", "scan": "1"}, {"reconstruction": "1"}),
    ],
    ids=["lauego-I-scan-R", "lauego-R-scan", "wire-R-scan"],
)
def test_sources_that_agree_give_what_the_most_specific_gives_alone(place, kind, agreeing, alone):
    """A link may name the source's scan and reconstruction too; when they are the source's own (I1 indexes R1 of scan
    1), the values are the source's."""
    together, single = prefill(place.settings, kind, **agreeing), prefill(place.settings, kind, **alone)
    assert (together.status_code, single.status_code) == (200, 200), together.text
    assert together.json() == single.json()


@pytest.mark.parametrize(
    ("kind", "query", "status", "code"),
    [
        ("lauego", {"scan": "1,2"}, 422, "int_parsing"),  # one ID per parameter
        ("lauego", {"scan": "x"}, 422, "int_parsing"),
        ("lauego", {"scan": "9223372036854775808"}, 422, "int_parsing"),  # SQLite's largest INTEGER + 1
        ("lauego", {"indexing": "9" * 5000}, 422, "int_parsing"),  # more digits than Python converts
        ("lauego", {"scan": "9"}, 404, None),
        ("lauego", {"reconstruction": "9"}, 404, None),
        ("lauego", {"indexing": "9"}, 404, None),
        ("wire", {"reconstruction": "2"}, 404, None),  # R2 has no wire parameters to clone
        ("lauego", {"scan": "2", "reconstruction": "1"}, 422, "source_mismatch"),
        ("lauego", {"scan": "2", "indexing": "1"}, 422, "source_mismatch"),
        ("lauego", {"reconstruction": "2", "indexing": "1"}, 422, "source_mismatch"),
        ("wire", {"scan": "2", "reconstruction": "1"}, 422, "source_mismatch"),
        ("wire", {"indexing": "1"}, 422, "no_source"),
    ],
)
def test_one_source_per_form_and_an_unknown_source_is_a_404(place, kind, query, status, code):
    """A parameter that is not one ID is a 422; an unknown source is a 404; sources that disagree, or an indexing source
    on the wire form, a 422."""
    response = prefill(place.settings, kind, **query)
    assert response.status_code == status, response.text
    problem = response.json()
    if code is not None:
        assert [error["code"] for error in problem["errors"]] == [code]
        assert problem["errors"][0]["loc"][0] == "query"


def test_a_blank_parameter_names_no_source(place):
    assert values(place.settings, "lauego", scan=" ", reconstruction="")["scan_number"] is None


def test_a_reconstructions_catalog_is_looked_for_with_no_connection_held(place, monkeypatch):
    """Prefill reads its rows, closes its session, then stats the file system, so a hanging ``/net`` path holds no
    pooled connection (docs/contributing/backend.md, Long work)."""
    held: list[int] = []
    looked_at = jobs_prefill.is_file_here

    def stat(path: Any) -> bool:
        held.append(place.engine.pool.checkedout())
        return looked_at(path)

    monkeypatch.setattr(jobs_prefill, "is_file_here", stat)
    found = jobs_prefill.prefill(
        "lauego", scan=None, reconstruction="1", indexing=None, settings=place.settings, engine=place.engine
    )

    assert (found.values["input_path"], held) == (reconstruction_catalog_path(place.reconstruction), [0])
