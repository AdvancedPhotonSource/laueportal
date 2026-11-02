"""``science.synthetic``'s wire scan: lauelab's synthetic recipe exactly, and the same scan at a finer binning. Its
reconstruction is the wire viewer's tests' (``tests/recon_support.py``) and the map the seed's
(``tests/indexing/test_viewer_demo.py``, ``tests/seed/test_seed_demo.py``)."""

from __future__ import annotations

import hashlib

import h5py
import numpy as np

from laueportal.science import synthetic

RECIPE_IMAGES_SHA256 = "580eb1f6c55e94196273345d9bef289906361e6d5b7089800870a601763c70fd"
RECIPE_WIRE_SHA256 = "c8f53becf3bd1acbf3c7161f1fa6d972432685ca7a8c5599bbe4a8d0fa84ae73"
"""The images and raw wire positions of lauelab's ``tests/data/reconstruction/generate_reference.py``
(``synthetic_images``), compared array for array when this port was made."""


def test_the_wire_scan_is_lauelabs_synthetic_recipe(tmp_path) -> None:
    path = synthetic.write_wire_scan(tmp_path / "scan.h5")
    with h5py.File(path, "r") as file:
        images = file["entry1/data/data"][()]
        wire = np.stack([file[f"entry1/wire/wire{axis}"][()] for axis in "XYZ"], axis=-1)
        binning = (file["entry1/detector/binx"][0], file["entry1/detector/biny"][0])
    assert (images.shape, images.dtype, binning) == ((82, 128, 128), np.uint16, (16, 16))
    assert hashlib.sha256(images.tobytes()).hexdigest() == RECIPE_IMAGES_SHA256
    assert hashlib.sha256(wire.tobytes()).hexdigest() == RECIPE_WIRE_SHA256


def test_a_finer_binning_is_the_same_scan_at_a_higher_resolution() -> None:
    coarse, coarse_wire = synthetic.wire_scan_images()
    fine, fine_wire = synthetic.wire_scan_images(8)
    assert fine.shape == (82, 256, 256)
    np.testing.assert_array_equal(fine_wire, coarse_wire)
    brightest = np.array(np.unravel_index(coarse[0].argmax(), coarse[0].shape))
    assert np.abs(np.array(np.unravel_index(fine[0].argmax(), fine[0].shape)) - 2 * brightest).max() <= 1
