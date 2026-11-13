"""Side detectors keep detector 0's calibrated sample rotation: synthetic Si frames of one sample position on all three
detectors of the demo geometry, detector 0 calibrated first, then each side detector with its orientation, whatever
symmetry-equivalent setting the side frame's own indexing names the reflections in."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from lauelab.indexing import load_geometry
from scipy.spatial.transform import Rotation

from laueportal.core.settings import IndexingDefaults
from laueportal.science import detector_calibration as dc
from laueportal.science import synthetic

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "lauelab"
GEOMETRY = FIXTURES / "geoN_2022-03-29_14-15-05.xml"
CRYSTAL = FIXTURES / "Si.xml"
DEFAULTS = IndexingDefaults()
PEAKS = dc.PeakSettings(
    DEFAULTS.box_size,
    DEFAULTS.max_rfactor,
    DEFAULTS.min_size,
    DEFAULTS.min_separation,
    DEFAULTS.threshold,
    DEFAULTS.threshold_ratio,
    DEFAULTS.peak_shape,
    DEFAULTS.max_number,
    DEFAULTS.smooth,
)
INDEXING = dc.IndexSettings(
    DEFAULTS.index_kev_max_calc,
    DEFAULTS.index_kev_max_test,
    DEFAULTS.index_angle_tolerance,
    DEFAULTS.index_cone,
    tuple(DEFAULTS.index_hkl),  # pyright: ignore[reportArgumentType]
    DEFAULTS.max_peaks,
)


def _frame(slot: int) -> tuple[dc.CalibrationFrame, synthetic.CalibrationTruth]:
    image, truth = synthetic.calibration_frame(GEOMETRY, CRYSTAL, slot=slot)
    return dc.CalibrationFrame(image, load_geometry(GEOMETRY).detector(slot).detector_id, (0, 0), (1, 1)), truth


def _measured(peaks: np.ndarray, truth: synthetic.CalibrationTruth, count: int = 2) -> dict[int, dc.Measurement]:
    measured = {}
    for k in range(count):
        peak = int(np.argmin(np.linalg.norm(peaks - truth.xy[k], axis=1)))
        order = int(np.gcd.reduce(np.abs(truth.hkl[k])))
        measured[peak] = dc.Measurement(float(truth.energy_kev[k]), "test", order)
    return measured


@pytest.fixture(scope="module")
def detector0_reciprocal() -> np.ndarray:
    frame, truth = _frame(0)
    peaks = dc.search_peaks(frame, PEAKS).xy
    detector = load_geometry(GEOMETRY).detector(0)
    start = (detector.rotation_vector + [0.0008, -0.0006, 0.0005], detector.translation + [150.0, -120.0, 250.0])
    with dc.calibration_files(GEOMETRY, CRYSTAL) as files:
        refined = dc.refine_detector(
            files, frame, peaks, INDEXING, start, _measured(peaks, truth), sample_reciprocal=None, azimuth_deg=90.0
        )
    assert refined.calibration is not None and refined.calibration.success
    return np.asarray(refined.calibration.refined_reciprocal)


@pytest.mark.parametrize("slot", [1, 2])
def test_a_side_detector_keeps_detector0s_orientation(slot: int, detector0_reciprocal: np.ndarray) -> None:
    frame, _ = _frame(slot)
    peaks = dc.search_peaks(frame, PEAKS).xy
    truth = load_geometry(GEOMETRY).detector(slot)
    start = (truth.rotation_vector + [0.0005, 0.0004, -0.0006], truth.translation + [200.0, 150.0, -200.0])
    with dc.calibration_files(GEOMETRY, CRYSTAL) as files:
        refined = dc.refine_detector(
            files, frame, peaks, INDEXING, start, {}, sample_reciprocal=detector0_reciprocal, azimuth_deg=90.0
        )
    calibration = refined.calibration
    assert calibration is not None and calibration.success, calibration and calibration.message
    assert refined.sample_rotation_from == "detector0"
    np.testing.assert_allclose(calibration.refined_reciprocal, detector0_reciprocal, atol=1e-12)
    # The side frames have 10 to 14 noisy peaks, so their fits are noise-limited: the claim is that keeping detector
    # 0's orientation gives the fit that the sample's true orientation gives, within the fit's own standard errors,
    # not a fixed gain over the start.
    with dc.calibration_files(GEOMETRY, CRYSTAL) as files:
        ideal = dc.refine_detector(
            files, frame, peaks, INDEXING, start, {}, sample_reciprocal=_true_reciprocal(), azimuth_deg=90.0
        ).calibration
    assert ideal is not None and ideal.success
    sigma = calibration.translation_sigma_um
    np.testing.assert_array_less(np.abs(calibration.refined.translation - ideal.refined.translation), sigma)
    np.testing.assert_array_less(np.abs(calibration.refined.translation - truth.translation), 4 * sigma + 1.0)
    assert calibration.rms_pixel_error < 0.2
    # Every reflection is predicted on the detector, where it was found.
    assert np.isfinite(calibration.predicted_pixels).all()


def _true_reciprocal() -> np.ndarray:
    from lauelab.analysis import lattice_params_to_reciprocal
    from lauelab.calibration import rotation_matrix

    reference = lattice_params_to_reciprocal(*[0.5431020511] * 3, 90, 90, 90, space_group=227)
    return reference @ rotation_matrix(np.asarray(synthetic.CALIBRATION_SAMPLE_AXIS)).T


def test_equivalent_settings_are_carried_and_other_orientations_refused(detector0_reciprocal: np.ndarray) -> None:
    swap = np.array([[0, 1, 0], [0, 0, 1], [1, 0, 0]])  # a cubic symmetry: another setting of the same lattice
    setting = dc.equivalent_setting(swap @ detector0_reciprocal, detector0_reciprocal)
    assert setting is not None
    np.testing.assert_array_equal(setting, swap)
    turned = detector0_reciprocal @ Rotation.from_rotvec([0.0, 0.0, np.radians(10)]).as_matrix().T
    assert dc.equivalent_setting(turned, detector0_reciprocal) is None


def test_a_different_orientation_is_refused(detector0_reciprocal: np.ndarray) -> None:
    frame, _ = _frame(1)
    peaks = dc.search_peaks(frame, PEAKS).xy
    turned = detector0_reciprocal @ Rotation.from_rotvec([0.0, 0.0, np.radians(10)]).as_matrix().T
    with dc.calibration_files(GEOMETRY, CRYSTAL) as files, pytest.raises(dc.CalibrationInputError) as refused:
        dc.refine_detector(files, frame, peaks, INDEXING, None, {}, sample_reciprocal=turned, azimuth_deg=90.0)
    assert refused.value.field == "sample_reciprocal"
