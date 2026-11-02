"""Native lauelab smoke test (run by `make setup`): index one synthetic Ni frame.

Usage: python scripts/lauelab_smoke.py <fixtures-dir>   (backend/tests/fixtures/lauelab)
"""

import sys
import tempfile
from pathlib import Path

from lauelab.indexing import Indexer, IndexParams, PeakParams

fixtures = Path(sys.argv[1])
indexer = Indexer(
    str(fixtures / "geoN_2022-03-29_14-15-05.xml"),
    str(fixtures / "Ni.xml"),
    peak_params=PeakParams(
        boxsize=18, max_rfactor=0.5, min_size=3, min_separation=20, threshold=None, threshold_ratio=4.0, max_peaks=200
    ),
    index_params=IndexParams(
        kev_max_calc=17.2, kev_max_test=35.0, angle_tolerance_deg=0.1, cone_deg=72.0, hkl_prefer=(0, 0, 1), max_data=300
    ),
)
with tempfile.TemporaryDirectory() as tmp:
    frame = Path(tmp) / "frame.h5"
    frame.write_bytes((fixtures / "synthetic_ni_grain_a.h5").read_bytes())
    result = indexer.index(str(frame), keep_image=False)
n = len(getattr(result, "patterns", []) or [])
if n < 1:
    sys.exit(f"lauelab native smoke failed: no pattern indexed ({result!r})")
print(f"    lauelab native smoke OK: {n} pattern(s) indexed")
