"""The Calibration page's API on the synthetic Si frame (``science.synthetic.write_calibration_frame``): its setup, the
frame, peak search, a clicked peak, indexing, the detector's fit from a perturbed starting geometry, and the saved
geometry file."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from lauelab.indexing import load_geometry

from laueportal.app import create_app
from laueportal.calibration import service
from laueportal.calibration.schemas import CalibrationSaveRequest
from laueportal.science import synthetic
from tests.api_support import PORTAL_HEADERS
from tests.probe_support import gc_disabled, hang, install_probe  # noqa: F401  (gc_disabled is a fixture)
from tests.settings_support import make_settings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "lauelab"
TRUE_GEOMETRY = FIXTURES / "geoN_2022-03-29_14-15-05.xml"
API = "/api/v1/calibration"


@dataclass(frozen=True)
class Inputs:
    root: Path
    frame: Path
    geometry: Path
    crystal: Path
    folder: Path
    truth: synthetic.CalibrationTruth


@pytest.fixture(scope="module")
def made(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, synthetic.CalibrationTruth]:
    folder = tmp_path_factory.mktemp("calibration-inputs")
    truth = synthetic.write_calibration_frame(
        FIXTURES / "synthetic_ni_grain_a.h5", folder / "si.h5", TRUE_GEOMETRY, FIXTURES / "Si.xml"
    )
    synthetic.write_starting_geometry(TRUE_GEOMETRY, folder / "geoN_2025-01-01_00-00-00.xml")
    return folder, truth


@pytest.fixture
def inputs(tmp_path: Path, made: tuple[Path, synthetic.CalibrationTruth]) -> Inputs:
    source, truth = made
    root = tmp_path / "data"
    folder = root / "geometries"
    folder.mkdir(parents=True)
    for name in ("si.h5",):
        (root / name).write_bytes((source / name).read_bytes())
    (folder / "geoN_2025-01-01_00-00-00.xml").write_bytes((source / "geoN_2025-01-01_00-00-00.xml").read_bytes())
    (root / "Si.xml").write_bytes((FIXTURES / "Si.xml").read_bytes())
    return Inputs(root, root / "si.h5", folder / "geoN_2025-01-01_00-00-00.xml", root / "Si.xml", folder, truth)


def client_for(tmp_path: Path, inputs: Inputs, **calibration: Any) -> TestClient:
    settings = make_settings(
        tmp_path,
        files={"roots": {"data": inputs.root}},
        calibration={"geometry_dir": inputs.folder, **calibration},
    )
    return TestClient(create_app(settings), headers=PORTAL_HEADERS)


@pytest.fixture
def client(tmp_path: Path, inputs: Inputs) -> Iterator[TestClient]:
    with client_for(tmp_path, inputs) as client:
        yield client


def _files(inputs: Inputs) -> dict[str, str]:
    return {"frame": str(inputs.frame), "geometry": str(inputs.geometry), "crystal": str(inputs.crystal)}


def _setup(client: TestClient) -> dict[str, Any]:
    response = client.get(f"{API}/setup")
    assert response.status_code == 200, response.text
    return response.json()


def _peaks(client: TestClient, inputs: Inputs) -> list[dict[str, float]]:
    settings = _setup(client)["peak_settings"]
    response = client.post(f"{API}/peaks", json={"frame": str(inputs.frame), "settings": settings})
    assert response.status_code == 200, response.text
    return [{"x": p["x"], "y": p["y"]} for p in response.json()["peaks"]]


def _index_body(client: TestClient, inputs: Inputs, peaks: list[dict[str, float]]) -> dict[str, Any]:
    return {**_files(inputs), "peaks": peaks, "settings": _setup(client)["index_settings"]}


def _measurements(inputs: Inputs, peaks: list[dict[str, float]], count: int = 2) -> list[dict[str, Any]]:
    """The true energies of the ``count`` brightest reflections, on their peaks."""
    xy = np.array([[p["x"], p["y"]] for p in peaks])
    measured = []
    for k in range(count):
        peak = int(np.argmin(np.linalg.norm(xy - inputs.truth.xy[k], axis=1)))
        order = int(np.gcd.reduce(np.abs(inputs.truth.hkl[k])))
        measured.append(
            {"peak": peak, "energy_kev": float(inputs.truth.energy_kev[k]), "source": "scan 7", "order": order}
        )
    return measured


def _save_body(client: TestClient, inputs: Inputs, note: str) -> dict[str, Any]:
    """A Save request: the refine request of the frame with two energies, and its fit's ``inputs_version``."""
    peaks = _peaks(client, inputs)
    body = {**_index_body(client, inputs, peaks), "measurements": _measurements(inputs, peaks)}
    refined = client.post(f"{API}/refine", json=body)
    assert refined.status_code == 200, refined.text
    return {**body, "note": note, "inputs_version": refined.json()["inputs_version"]}


def test_setup_lists_the_folder_and_the_form_defaults(client: TestClient, inputs: Inputs) -> None:
    (inputs.folder / "geoN_2026-11-13_10-00-00.xml").write_text("<geoN/>")
    (inputs.folder / "other.xml").write_text("<geoN/>")
    setup = _setup(client)
    assert setup["geometry_folder"]["state"] == "ok"
    names = [item["name"] for item in setup["geometry_folder"]["files"]]
    assert names == ["geoN_2026-11-13_10-00-00.xml", "geoN_2025-01-01_00-00-00.xml"]
    assert setup["default_geometry"] == str(inputs.folder / "geoN_2026-11-13_10-00-00.xml")
    assert setup["peak_settings"]["box_size"] == 18
    assert setup["index_settings"]["index_hkl"] == [0, 0, 1]
    assert setup["azimuth_deg"] == 90.0


@pytest.mark.parametrize(
    ("folder", "state"),
    [(None, "not_set"), ("missing", "not_found"), ("outside", "outside_roots")],
)
def test_setup_reports_an_unusable_folder(tmp_path: Path, inputs: Inputs, folder: str | None, state: str) -> None:
    configured: Path | str | None = None
    if folder == "missing":
        configured = inputs.root / "missing"
    elif folder == "outside":
        configured = tmp_path / "outside"
        configured.mkdir()
    with client_for(tmp_path, inputs, geometry_dir=configured or "") as client:
        setup = _setup(client)
    assert setup["geometry_folder"]["state"] == state
    assert setup["default_geometry"] is None


def test_frame_is_sent_as_a_tagged_uint16_image(client: TestClient, inputs: Inputs) -> None:
    response = client.get(f"{API}/frame", params={"path": str(inputs.frame)})
    assert response.status_code == 200, response.text
    assert response.headers["x-frame-shape"] == "2048,2048"
    assert response.headers["x-frame-dtype"] == "uint16"
    assert len(response.content) == 2048 * 2048 * 2
    again = client.get(
        f"{API}/frame", params={"path": str(inputs.frame)}, headers={"If-None-Match": response.headers["etag"]}
    )
    assert again.status_code == 304


def test_paths_are_confined_to_the_roots(client: TestClient, inputs: Inputs, tmp_path: Path) -> None:
    outside = tmp_path / "outside.h5"
    outside.write_bytes(inputs.frame.read_bytes())
    assert client.get(f"{API}/frame", params={"path": str(outside)}).status_code == 400
    missing = client.post(f"{API}/inputs", json={**_files(inputs), "crystal": str(inputs.root / "none.xml")})
    assert missing.status_code == 422
    assert missing.json()["errors"][0]["loc"] == ["crystal"]


def test_inputs_name_the_detector_and_the_crystal(client: TestClient, inputs: Inputs) -> None:
    response = client.post(f"{API}/inputs", json=_files(inputs))
    assert response.status_code == 200, response.text
    info = response.json()
    assert info["detector_id"] == "PE1621 723-3335"
    assert info["detector"]["slot"] == 0
    assert info["fit_sample_rotation"] is True
    assert (info["crystal_name"], info["space_group"]) == ("Si", 227)
    assert (info["width"], info["height"]) == (2048, 2048)


def test_a_crystal_lauelab_refuses_is_a_422_on_its_field(client: TestClient, inputs: Inputs) -> None:
    bad = inputs.root / "bad.xml"
    bad.write_text("<cif><cell></cell></cif>")
    response = client.post(f"{API}/inputs", json={**_files(inputs), "crystal": str(bad)})
    assert response.status_code == 422
    assert response.json()["errors"][0]["loc"] == ["crystal"]


def test_peak_search_and_a_clicked_peak(client: TestClient, inputs: Inputs) -> None:
    peaks = _peaks(client, inputs)
    assert len(peaks) == len(inputs.truth.hkl)
    x, y = inputs.truth.xy[3]
    fitted = client.post(f"{API}/peak", json={"frame": str(inputs.frame), "x": x + 3, "y": y - 2})
    assert fitted.status_code == 200, fitted.text
    peak = fitted.json()["peak"]
    assert abs(peak["x"] - x) < 0.3 and abs(peak["y"] - y) < 0.3
    empty = client.post(f"{API}/peak", json={"frame": str(inputs.frame), "x": 30, "y": 30})
    assert empty.json()["peak"] is None


def test_indexing_labels_the_reflections_and_checks_the_surface(client: TestClient, inputs: Inputs) -> None:
    peaks = _peaks(client, inputs)
    response = client.post(f"{API}/index", json=_index_body(client, inputs, peaks))
    assert response.status_code == 200, response.text
    indexing = response.json()
    assert indexing["found"] is True
    assert indexing["n_indexed"] == len(peaks)
    brightest = next(row for row in indexing["rows"] if row["peak"] == 0)
    assert (brightest["h"], brightest["k"], brightest["l"]) == (0, 0, 4)
    assert brightest["calculated_energy_kev"] == pytest.approx(inputs.truth.energy_kev[0], rel=2e-3)
    assert [h["order"] for h in brightest["harmonics"]] == list(range(1, 9))
    surface = indexing["surface"]
    assert surface["between"] is True
    assert surface["angle_to_expected_deg"] < 3


def test_refine_recovers_the_detector_from_the_perturbed_start(client: TestClient, inputs: Inputs) -> None:
    peaks = _peaks(client, inputs)
    body = {**_index_body(client, inputs, peaks), "measurements": _measurements(inputs, peaks)}
    response = client.post(f"{API}/refine", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "ok", result["message"]
    truth = load_geometry(TRUE_GEOMETRY).detector(0)
    start_error = np.linalg.norm(np.array(result["initial"]["translation_um"]) - truth.translation)
    end_error = np.linalg.norm(np.array(result["refined"]["translation_um"]) - truth.translation)
    assert start_error > 300 and end_error < 100
    assert result["rms_pixel_error"] < 0.2
    assert result["n_energies"] == 2
    assert result["sample_rotation_from"] == "fitted"
    assert len(result["note"]) < 100 and result["note"].isascii()


def test_match_again_indexes_with_the_refined_detector(client: TestClient, inputs: Inputs) -> None:
    peaks = _peaks(client, inputs)
    body = {**_index_body(client, inputs, peaks), "measurements": _measurements(inputs, peaks)}
    refined = client.post(f"{API}/refine", json=body).json()["refined"]
    again = client.post(f"{API}/index", json={**_index_body(client, inputs, peaks), "start": refined}).json()
    assert again["detector"]["translation_um"] == pytest.approx(refined["translation_um"])
    assert again["n_indexed"] == len(peaks)


def test_refine_reports_why_it_cannot_fit(client: TestClient, inputs: Inputs) -> None:
    peaks = _peaks(client, inputs)
    body = _index_body(client, inputs, peaks)
    assert client.post(f"{API}/refine", json=body).json()["status"] == "insufficient_data"
    doubled = _measurements(inputs, peaks, 1)
    doubled[0] = {**doubled[0], "energy_kev": 2 * doubled[0]["energy_kev"]}
    mismatch = client.post(f"{API}/refine", json={**body, "measurements": doubled}).json()
    assert mismatch["status"] == "energy_mismatch"
    harmonic = [{**doubled[0], "order": 2 * doubled[0]["order"]}]
    assert client.post(f"{API}/refine", json={**body, "measurements": harmonic}).json()["status"] == "ok"
    none = client.post(f"{API}/refine", json={**body, "peaks": peaks[:1]}).json()
    assert none["status"] == "not_indexed"


def test_save_writes_the_refined_detector_into_the_folder(client: TestClient, inputs: Inputs) -> None:
    body = _save_body(client, inputs, "test save")
    response = client.post(f"{API}/geometry-files", json=body)
    assert response.status_code == 200, response.text
    saved = response.json()
    path = Path(saved["path"])
    assert path.parent == inputs.folder.resolve() and saved["name"].startswith("geoN_")
    detector = load_geometry(path).detector(0)
    np.testing.assert_allclose(detector.translation, saved["result"]["refined"]["translation_um"], atol=1e-6)
    text = path.read_text()
    assert "<note>test save</note>" in text and "<fileNote>Calibrated from si.h5</fileNote>" in text
    assert _setup(client)["default_geometry"] == str(path)


def test_save_refuses_an_existing_name_and_a_bad_note(tmp_path: Path, inputs: Inputs) -> None:
    settings = make_settings(
        tmp_path, files={"roots": {"data": inputs.root}}, calibration={"geometry_dir": inputs.folder}
    )
    when = datetime(2026, 11, 13, 9, 30, 0)
    (inputs.folder / when.strftime(service.GEOMETRY_NAME)).write_text("kept")
    with client_for(tmp_path, inputs) as client:
        body = _save_body(client, inputs, "n")
    request = CalibrationSaveRequest.model_validate(body)
    from laueportal.core.problems import ProblemError

    with pytest.raises(ProblemError) as conflict:
        service.save(settings, request, now=when)
    assert conflict.value.status == 409
    assert (inputs.folder / when.strftime(service.GEOMETRY_NAME)).read_text() == "kept"
    with pytest.raises(ProblemError) as bad:
        service.save(settings, request.model_copy(update={"note": "a<b"}), now=when)
    assert bad.value.status == 422


def test_save_writes_to_a_chosen_folder_and_name(client: TestClient, inputs: Inputs) -> None:
    other = inputs.root / "elsewhere"
    other.mkdir()
    body = {**_save_body(client, inputs, "n"), "folder": str(other), "name": "geoN_test.xml"}
    response = client.post(f"{API}/geometry-files", json=body)
    assert response.status_code == 200, response.text
    assert Path(response.json()["path"]) == other.resolve() / "geoN_test.xml"
    again = client.post(f"{API}/geometry-files", json=body)
    assert again.status_code == 409


@pytest.mark.parametrize(
    ("change", "status", "field"),
    [
        ({"folder": "missing"}, 422, "folder"),
        ({"name": "../geoN.xml"}, 422, "name"),
        ({"name": "geoN.txt"}, 422, "name"),
    ],
)
def test_save_refuses_a_bad_folder_or_name(
    client: TestClient, inputs: Inputs, change: dict[str, str], status: int, field: str
) -> None:
    if "folder" in change:
        change = {"folder": str(inputs.root / change["folder"])}
    body = {**_save_body(client, inputs, "n"), **change}
    response = client.post(f"{API}/geometry-files", json=body)
    assert response.status_code == status, response.text
    assert field in str(response.json()["errors"][0]["loc"])


def test_a_folder_outside_the_roots_is_refused(client: TestClient, inputs: Inputs, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    response = client.post(f"{API}/geometry-files", json={**_save_body(client, inputs, "n"), "folder": str(outside)})
    assert response.status_code == 400
    assert not any(outside.iterdir())


def test_save_needs_the_folder(tmp_path: Path, inputs: Inputs) -> None:
    with client_for(tmp_path, inputs, geometry_dir="") as client:
        body = _save_body(client, inputs, "n")
        response = client.post(f"{API}/geometry-files", json=body)
    assert response.status_code == 422
    assert "not set" in response.json()["detail"]


def test_save_refuses_inputs_changed_since_the_fit(client: TestClient, inputs: Inputs) -> None:
    """The fit shown is the one saved: a crystal file changed after it (another lattice) is a 409, nothing written."""
    body = _save_body(client, inputs, "n")
    text = inputs.crystal.read_text()
    inputs.crystal.write_text(text.replace("0.5431020511", "0.5441020511"))
    response = client.post(f"{API}/geometry-files", json=body)
    assert response.status_code == 409, response.text
    assert response.json()["title"] == "Inputs changed"
    assert [p.name for p in inputs.folder.iterdir()] == ["geoN_2025-01-01_00-00-00.xml"]


def test_the_geometry_file_is_written_in_the_probe(
    tmp_path: Path, inputs: Inputs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The folder is shared (often on /net): the write runs on a probe thread, not the request's."""
    import threading

    from laueportal.science import detector_calibration

    written_on: list[threading.Thread] = []
    write = detector_calibration.save_geometry

    def recorded(*args: Any, **kwargs: Any) -> Path:
        written_on.append(threading.current_thread())
        return write(*args, **kwargs)

    monkeypatch.setattr(detector_calibration, "save_geometry", recorded)
    settings = make_settings(
        tmp_path, files={"roots": {"data": inputs.root}}, calibration={"geometry_dir": inputs.folder}
    )
    with client_for(tmp_path, inputs) as client:
        body = _save_body(client, inputs, "n")
    saved = service.save(settings, CalibrationSaveRequest.model_validate(body), now=datetime(2026, 11, 13, 9, 31, 0))
    assert Path(saved.path).is_file()
    assert written_on and written_on[0] is not threading.current_thread()


@pytest.mark.usefixtures("gc_disabled")
def test_a_hung_frame_is_not_responding(tmp_path: Path, inputs: Inputs, monkeypatch: pytest.MonkeyPatch) -> None:
    install_probe(monkeypatch, mounts=(inputs.root,))
    hung = hang(monkeypatch, inputs.root)
    try:
        with client_for(tmp_path, inputs) as client:
            response = client.get(f"{API}/frame", params={"path": str(inputs.frame)})
        assert response.status_code == 503
        assert response.json()["type"].endswith("not-responding")
    finally:
        hung.released.set()
