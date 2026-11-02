"""Shared by the API tests: what the portal's own pages send with every request."""

from __future__ import annotations

from laueportal.core.api import WRITE_HEADER, WRITE_HEADER_VALUE

PORTAL_HEADERS = {WRITE_HEADER: WRITE_HEADER_VALUE}
"""The header ``src/api/fetcher.ts`` sends with every request; a request that changes data needs it
(``core.api.refuse_cross_site_writes``). Give it to a ``TestClient`` that posts: ``TestClient(app, headers=...)``."""
