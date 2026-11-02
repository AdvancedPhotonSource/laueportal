"""lauelab's validation of finished outputs: an indexing results file and a reconstruction scan catalog.

The publication of a remote run's returned outputs (``remote.poller.publish_returned``) checks
them with these before anything is published. Only ``science`` imports lauelab (import-linter rule 2).
"""

from __future__ import annotations

from lauelab.indexing import validate_results_file
from lauelab.reconstruct import validate_scan_file

__all__ = ["validate_results_file", "validate_scan_file"]
